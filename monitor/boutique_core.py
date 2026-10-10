"""Shared monitoring logic for the Online Boutique dashboard and the headless monitor.

Two backends are supported for workload health and the "restart frontend" test:
  * docker      - Online Boutique running as Docker / Docker Compose containers (default)
  * kubernetes  - Online Boutique running in a cluster (original behaviour, uses kubectl)

HTTP probing, telemetry.json and SLA_BREACH.flag are identical for both.
Only fixed, explicitly built command lists are executed - never a shell string.
"""
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------- Configuration ----------------
DEFAULT_BASE_URL = os.getenv("BOUTIQUE_URL", "http://localhost:8080")
DEFAULT_BACKEND = os.getenv("BOUTIQUE_BACKEND", "docker").strip().lower()
DEFAULT_NAMESPACE = os.getenv("BOUTIQUE_NAMESPACE", "default")
DEFAULT_COMPOSE_PROJECT = os.getenv("BOUTIQUE_COMPOSE_PROJECT", "")
LATENCY_WARN_MS = 500
LATENCY_SLA_MS = 1000
PROBE_TIMEOUT_S = 3
HISTORY_SIZE = 60

# Default output: <repo>/results when this file lives in <repo>/monitor, else the current directory.
_REPO_RESULTS = Path(__file__).resolve().parent.parent / "results"
OUT_DIR = Path(os.getenv("BOUTIQUE_TELEMETRY_DIR") or (_REPO_RESULTS if _REPO_RESULTS.is_dir() else "."))
TELEMETRY_PATH = OUT_DIR / "telemetry.json"
FLAG_FILE = OUT_DIR / "SLA_BREACH.flag"
# Append-only history (one compact JSON sample per line) for baselines and experiment analysis.
LOG_PATH = OUT_DIR / "telemetry_log.jsonl"
LOG_MAX_BYTES = int(os.getenv("BOUTIQUE_LOG_MAX_MB", "60")) * 1024 * 1024  # rotates once to .jsonl.1
JOURNEY_EVERY = int(os.getenv("BOUTIQUE_JOURNEY_EVERY", "5"))  # checkout probe every N samples; 0 = off
BURST_MAX_REQUESTS = int(os.getenv("BOUTIQUE_BURST_MAX_REQUESTS", "10000"))
BURST_MAX_WORKERS = int(os.getenv("BOUTIQUE_BURST_MAX_WORKERS", "20"))

ENDPOINT_PATHS = {
    "Home": "/",
    "Cart": "/cart",
    "Product": "/product/0PUK6V6EV0",
}

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"


def _run(cmd, timeout=8):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
        encoding="utf-8", errors="replace",
    )


def _empty_snapshot(backend):
    return {
        "backend": backend,
        "available": False,
        "ready_pods": None,
        "total_pods": None,
        "restarts": None,
        "cpu_memory": "Metrics unavailable",
        "pods": [],
        "events": [],
        "error": None,
    }


# ---------------- HTTP probing ----------------
def classify_error(status, exc=None):
    """timeout | connection | request_error | http_5xx | http_4xx | none"""
    if exc is not None:
        if isinstance(exc, requests.Timeout):
            return "timeout"
        if isinstance(exc, requests.ConnectionError):
            return "connection"
        return "request_error"
    if status >= 500:
        return "http_5xx"
    if status >= 400:
        return "http_4xx"
    return "none"


def probe_endpoints(base, simulate_failure=False):
    rows = []
    for name, path in ENDPOINT_PATHS.items():
        url = base + path
        started = time.perf_counter()
        status = 0
        error = ""
        exc = None
        try:
            status = int(requests.get(url, timeout=PROBE_TIMEOUT_S).status_code)
        except requests.RequestException as e:
            exc, error = e, str(e)[:160]
        latency_ms = (time.perf_counter() - started) * 1000
        success = 200 <= status < 400
        error_type = classify_error(status, exc)
        if simulate_failure:
            # Tests the monitor's alerting path only; the application is untouched.
            status, success, error, error_type = 599, False, "SIMULATED PROBE FAILURE (application not modified)", "simulated"
        rows.append({
            "endpoint": name, "url": url, "status": status,
            "latency_ms": round(latency_ms, 2), "success": success, "error": error,
            "error_type": error_type,
        })
    return rows


class CheckoutJourney:
    """Synthetic purchase (add to cart -> place order) through the frontend.

    It exercises cart, checkout, payment, shipping, email, currency and product catalog, which the
    plain page probes never touch. It runs every JOURNEY_EVERY samples and its last result is carried
    into the samples in between (with age_s) so a failure isn't hidden by the next quiet sample.
    If it has never succeeded (e.g. form fields differ on this app version) it switches itself off
    after a few tries, so it can never cause a permanent false alarm."""
    MAX_FAILS_BEFORE_FIRST_SUCCESS = 3

    def __init__(self, base, every=JOURNEY_EVERY):
        self.base, self.every = base, every
        self.enabled = every > 0
        self.disabled_reason = None if self.enabled else "disabled by configuration"
        self.ever_ok = False
        self._fails = 0
        self._ticks = 0
        self._last = None
        self._last_at = 0.0

    def info(self):
        return {"enabled": self.enabled, "verified": self.ever_ok, "disabled_reason": self.disabled_reason}

    def _run_once(self):
        started = time.perf_counter()
        status, error, exc = 0, "", None
        try:
            sess = requests.Session()
            sess.get(self.base + "/", timeout=PROBE_TIMEOUT_S)
            add = sess.post(self.base + "/cart", data={"product_id": "OLJCESPC7Z", "quantity": "1"},
                            timeout=5, allow_redirects=False)
            if add.status_code >= 400:
                status, error = add.status_code, "add-to-cart failed"
            else:
                year = datetime.now().year + 2
                order = sess.post(self.base + "/cart/checkout", timeout=10, data={
                    "email": "monitor@example.com", "street_address": "1600 Amphitheatre Parkway",
                    "zip_code": "94043", "city": "Mountain View", "state": "CA", "country": "United States",
                    "credit_card_number": "4432-8015-6152-0454",  # the demo app's built-in mock card
                    "credit_card_expiration_month": "1", "credit_card_expiration_year": str(year),
                    "credit_card_cvv": "672",
                })
                status = order.status_code
                if status >= 400:
                    error = "place-order failed"
        except requests.RequestException as e:
            exc, error = e, str(e)[:160]
        return {
            "endpoint": "Checkout", "url": self.base + "/cart/checkout", "status": status,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "success": 200 <= status < 400, "error": error, "error_type": classify_error(status, exc),
        }

    def tick(self):
        """Call once per sample. Returns the row to include (fresh or carried over) or None."""
        if not self.enabled:
            return None
        self._ticks += 1
        if self._last is None or not self.ever_ok or self._ticks % self.every == 0:
            row = self._run_once()
            self._last, self._last_at = row, time.time()
            if row["success"]:
                self.ever_ok, self._fails = True, 0
            elif not self.ever_ok:
                self._fails += 1
                if self._fails >= self.MAX_FAILS_BEFORE_FIRST_SUCCESS:
                    self.enabled = False
                    self.disabled_reason = (f"never succeeded in {self._fails} tries (last: HTTP {row['status']}, "
                                            f"{row['error_type']}); excluded from error rate")
                return None  # not trusted until it has worked once
        if not self.ever_ok:
            return None
        row = dict(self._last)
        row["age_s"] = round(time.time() - self._last_at, 1)
        return row


def _p95(values):
    values = sorted(values)
    return values[max(0, int(0.95 * len(values) + 0.999) - 1)] if values else 0


def bounded_traffic_burst(base, total_requests=10000, workers=5):
    """GET burst across the monitored pages. Capped by BOUTIQUE_BURST_MAX_REQUESTS / _MAX_WORKERS."""
    urls = [base + p for p in ENDPOINT_PATHS.values()]

    def hit(i):
        started = time.perf_counter()
        try:
            r = requests.get(urls[i % len(urls)], timeout=5)
            ok = 200 <= r.status_code < 400
        except requests.RequestException:
            ok = False
        return {"latency_ms": round((time.perf_counter() - started) * 1000, 2), "success": ok}

    total_requests = max(1, min(int(total_requests), BURST_MAX_REQUESTS))
    workers = max(1, min(int(workers), BURST_MAX_WORKERS))
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(hit, i) for i in range(total_requests)]):
            results.append(future.result())
    errors = sum(not x["success"] for x in results)
    lats = [x["latency_ms"] for x in results]
    return {
        "total": len(results),
        "errors": errors,
        "error_rate_pct": round(100 * errors / max(1, len(results)), 2),
        "mean_latency_ms": round(sum(lats) / max(1, len(lats)), 2),
        "p95_latency_ms": _p95(lats),
    }


# ---------------- Docker backend ----------------
def _parse_docker_time(value):
    """'2026-10-10T06:49:01.123456789Z' -> epoch seconds, or None for unset/zero dates."""
    if not value or value.startswith("0001"):
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def get_docker_snapshot(project=""):
    """Read-only container health. If `project` is blank, the Compose project that owns
    the 'frontend' service is auto-detected (when there is exactly one)."""
    snap = _empty_snapshot("docker")
    try:
        ps = _run(["docker", "ps", "-a", "-q"])
        if ps.returncode != 0:
            snap["error"] = (ps.stderr or ps.stdout).strip()[:300]
            return snap
        ids = ps.stdout.split()
        containers = []
        if ids:
            insp = _run(["docker", "inspect"] + ids, timeout=10)
            if insp.returncode != 0:
                snap["error"] = (insp.stderr or insp.stdout).strip()[:300]
                return snap
            containers = json.loads(insp.stdout)

        def labels(c):
            return (c.get("Config") or {}).get("Labels") or {}

        wanted = (project or "").strip()
        if not wanted:
            owners = {labels(c).get(COMPOSE_PROJECT) for c in containers
                      if labels(c).get(COMPOSE_SERVICE) == "frontend"}
            owners.discard(None)
            if len(owners) == 1:
                wanted = owners.pop()
        snap["project"] = wanted or None
        if wanted:
            containers = [c for c in containers if labels(c).get(COMPOSE_PROJECT) == wanted]

        now = time.time()
        for c in containers:
            state = c.get("State") or {}
            name = (c.get("Name") or "").lstrip("/")
            status = state.get("Status", "unknown")
            running = bool(state.get("Running")) and not state.get("Restarting")
            health = (state.get("Health") or {}).get("Status")
            ready = running and health in (None, "healthy")
            started = _parse_docker_time(state.get("StartedAt", ""))
            phase = status + (f" ({state.get('ExitCode')})" if status == "exited" else "")
            snap["pods"].append({
                "name": name,
                "app": labels(c).get(COMPOSE_SERVICE) or name,
                "phase": phase,
                "ready": "Ready" if ready else "Not ready",
                "restarts": int(c.get("RestartCount", 0)),
                "uptime_s": round(now - started) if (running and started) else None,
                "oom_killed": bool(state.get("OOMKilled")),
                "health": health,
                "exit_code": None if running else state.get("ExitCode"),
                "started_at": state.get("StartedAt") if started else None,
            })
        snap["available"] = True
        snap["total_pods"] = len(snap["pods"])
        snap["ready_pods"] = sum(p["ready"] == "Ready" for p in snap["pods"])
        snap["restarts"] = sum(p["restarts"] for p in snap["pods"])
        return snap
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        snap["error"] = str(exc)[:300]
        return snap


_UNIT_MB = {"b": 1 / 1048576, "kb": 1 / 1000, "kib": 1 / 1024, "mb": 1, "mib": 1, "gb": 1000, "gib": 1024, "tb": 1e6, "tib": 1048576}


def _to_mb(text):
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]*)", text or "")
    return round(float(m.group(1)) * _UNIT_MB.get(m.group(2).lower(), 1), 2) if m else None


def _to_pct(text):
    m = re.match(r"\s*([\d.]+)\s*%", text or "")
    return float(m.group(1)) if m else None


def get_docker_resources(names=None):
    """{container: {cpu_pct, mem_mb, mem_limit_mb, mem_pct}} from one `docker stats --no-stream`
    (takes ~1-3 s, so call it from the background sampler). Returns None if docker stats failed."""
    try:
        r = _run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = {}
    for line in r.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = row.get("Name", "")
        if names and name not in names:
            continue
        used, _, limit = (row.get("MemUsage") or "").partition("/")
        out[name] = {"cpu_pct": _to_pct(row.get("CPUPerc")), "mem_mb": _to_mb(used),
                     "mem_limit_mb": _to_mb(limit), "mem_pct": _to_pct(row.get("MemPerc"))}
    return out


def format_resources(resources):
    """Plain-text CPU / memory table for the dashboard."""
    if not resources:
        return "No resource data yet (docker stats unavailable or still warming up)."
    lines = [f"{'CONTAINER':<42} {'CPU %':>7} {'MEM MB':>9} {'MEM %':>7}"]
    for name, r in sorted(resources.items()):
        lines.append(f"{name:<42} {r['cpu_pct'] if r['cpu_pct'] is not None else '-':>7} "
                     f"{r['mem_mb'] if r['mem_mb'] is not None else '-':>9} {r['mem_pct'] if r['mem_pct'] is not None else '-':>7}")
    return "\n".join(lines)


_EVENT_ACTIONS = ("start", "die", "kill", "stop", "restart", "pause", "unpause", "oom", "destroy", "health_status")


def get_docker_events_struct(names=None, since_s=120):
    """Container lifecycle events as dicts (start/die/kill/stop/restart/pause/oom/health_status)."""
    until = int(time.time())
    try:
        r = _run(["docker", "events", "--since", str(until - since_s), "--until", str(until),
                  "--filter", "type=container", "--format", "{{json .}}"], timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = []
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = e.get("Action") or e.get("status") or ""
        attrs = (e.get("Actor") or {}).get("Attributes") or {}
        name = attrs.get("name", "")
        if not action.startswith(_EVENT_ACTIONS) or (names and name not in names):
            continue
        exit_code = attrs.get("exitCode")
        out.append({
            "time": datetime.fromtimestamp(e.get("time", 0)).astimezone().isoformat(),
            "action": action, "container": name,
            "exit_code": int(exit_code) if str(exit_code).lstrip("-").isdigit() else None,
            "signal": attrs.get("signal"),
        })
    return out


def get_docker_events(names=None, since="10m", limit=8):
    """Recent events as display lines (kept for the dashboard)."""
    since_s = 600 if since == "10m" else int(since)
    events = get_docker_events_struct(names, since_s) or []
    lines = [f"{e['time'][11:19]}  {e['action']:<22} {e['container']}"
             + (f" exitCode={e['exit_code']}" if e["exit_code"] not in (None, 0) else "") for e in events]
    return lines[-limit:]


class DockerExtrasSampler:
    """Collects slow Docker data (docker stats, docker events) on a background thread so the
    2-second probe loop is never delayed. Read the newest values with .latest()."""

    def __init__(self, interval=10.0, event_window_s=120):
        self.interval, self.event_window_s = interval, event_window_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._data = {"resources": {}, "events": [], "updated": None}
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="docker-extras", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                res = get_docker_resources()
                ev = get_docker_events_struct(since_s=self.event_window_s)
                with self._lock:
                    if res is not None:
                        self._data["resources"] = res
                    if ev is not None:
                        self._data["events"] = ev
                    self._data["updated"] = time.time()
            except Exception:  # never let monitoring-extras kill the sampler
                pass
            self._stop.wait(self.interval)

    def latest(self):
        with self._lock:
            d = dict(self._data)
        d["age_s"] = round(time.time() - d["updated"], 1) if d["updated"] else None
        return d


def restart_frontend_container(project=""):
    """Restart exactly one running 'frontend' container (SIGKILL + start, ~1-3 s outage)."""
    snap = get_docker_snapshot(project)
    if not snap["available"]:
        return False, f"Docker unavailable: {snap['error']}"
    cands = [p for p in snap["pods"] if p["app"] == "frontend" and p["phase"] == "running"]
    if not cands:
        return False, "No running container for the 'frontend' service was found. Check the Compose project."
    if len(cands) > 1:
        return False, "More than one running 'frontend' container matched; set the Compose project explicitly."
    name = cands[0]["name"]
    r = _run(["docker", "restart", "--time", "0", name], timeout=30)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:400]
    return True, f"Restarted frontend container {name} (SIGKILL then start). Watch probes and uptime recover."


# ---------------- Kubernetes backend (original behaviour) ----------------
def run_kubectl(args, timeout=10):
    return _run(["kubectl"] + args, timeout=timeout)


def get_k8s_snapshot(ns):
    """Read-only Kubernetes checks. Returns None values if kubectl is unavailable."""
    result = _empty_snapshot("kubernetes")
    try:
        proc = run_kubectl(["-n", ns, "get", "pods", "-o", "json"], timeout=5)
        if proc.returncode != 0:
            result["error"] = (proc.stderr or proc.stdout).strip()[:300]
            return result
        pods = json.loads(proc.stdout).get("items", [])
        result.update(available=True, total_pods=len(pods), ready_pods=0, restarts=0)
        for pod in pods:
            labels = pod.get("metadata", {}).get("labels", {})
            statuses = pod.get("status", {}).get("containerStatuses", []) or []
            ready = bool(statuses) and all(c.get("ready", False) for c in statuses)
            restarts = sum(int(c.get("restartCount", 0)) for c in statuses)
            result["ready_pods"] += int(ready)
            result["restarts"] += restarts
            result["pods"].append({
                "name": pod.get("metadata", {}).get("name", ""),
                "app": labels.get("app", labels.get("app.kubernetes.io/name", "—")),
                "phase": pod.get("status", {}).get("phase", "Unknown"),
                "ready": "Ready" if ready else "Not ready",
                "restarts": restarts,
            })
        top = run_kubectl(["-n", ns, "top", "pods"], timeout=5)
        result["cpu_memory"] = (top.stdout.strip() if top.returncode == 0 and top.stdout.strip()
                                else "Metrics API unavailable (kubectl top pods not working)")
        events = run_kubectl(["-n", ns, "get", "events", "--sort-by=.lastTimestamp"], timeout=5)
        if events.returncode == 0:
            lines = events.stdout.strip().splitlines()
            result["events"] = lines[-8:] if len(lines) > 1 else lines
        return result
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        result["error"] = str(exc)[:300]
        return result


def restart_one_frontend_pod(ns):
    listing = run_kubectl(["-n", ns, "get", "pods", "-l", "app=frontend", "-o", "json"], timeout=8)
    if listing.returncode != 0:
        return False, (listing.stderr or listing.stdout).strip()[:400]
    try:
        items = json.loads(listing.stdout).get("items", [])
    except json.JSONDecodeError:
        return False, "Could not parse Kubernetes pod list."
    candidates = [
        p for p in items
        if p.get("metadata", {}).get("name")
        and p.get("metadata", {}).get("ownerReferences")
        and p.get("status", {}).get("phase") == "Running"
    ]
    if not candidates:
        return False, "No running, controller-managed pod with label app=frontend was found. Check namespace/labels."
    candidates.sort(key=lambda p: not all(c.get("ready", False) for c in p.get("status", {}).get("containerStatuses", [])))
    pod_name = candidates[0]["metadata"]["name"]
    deleted = run_kubectl(["-n", ns, "delete", "pod", pod_name, "--wait=false"], timeout=8)
    if deleted.returncode != 0:
        return False, (deleted.stderr or deleted.stdout).strip()[:400]
    return True, f"Requested deletion of frontend pod {pod_name}. Kubernetes should recreate it if managed by a healthy controller."


# ---------------- Backend dispatch ----------------
def get_snapshot(backend, namespace="default", project=""):
    return get_docker_snapshot(project) if backend == "docker" else get_k8s_snapshot(namespace)


def restart_frontend(backend, namespace="default", project=""):
    return restart_frontend_container(project) if backend == "docker" else restart_one_frontend_pod(namespace)


# ---------------- Telemetry ----------------
def build_sample(probe_rows, snap, simulated=False, extras=None, journey_info=None):
    """One telemetry record. `extras` is DockerExtrasSampler.latest(); `journey_info` is CheckoutJourney.info()."""
    n = len(probe_rows)
    successful = sum(r["success"] for r in probe_rows)
    lats = [r["latency_ms"] for r in probe_rows]
    extras = extras or {}
    resources = extras.get("resources") or {}
    names = {p["name"] for p in snap["pods"]}

    metrics = {}
    for r in probe_rows:
        m = {"status": r["status"], "latency": r["latency_ms"], "success": r["success"],
             "error": r["error"], "error_type": r.get("error_type", "none")}
        if "age_s" in r:
            m["age_s"] = r["age_s"]
        metrics[r["endpoint"]] = m

    workloads = [{**p, **resources.get(p["name"], {})} for p in snap["pods"]]
    summary = {
        "request_count": n,
        "error_rate_pct": round(100 * (n - successful) / max(1, n), 2),
        "mean_latency_ms": round(sum(lats) / max(1, len(lats)), 2),
        "p95_latency_ms": round(_p95(lats), 2),
        "slowest_endpoint": max(probe_rows, key=lambda r: r["latency_ms"])["endpoint"] if probe_rows else None,
        "error_types": {t: sum(1 for r in probe_rows if r.get("error_type") == t)
                        for t in sorted({r.get("error_type") for r in probe_rows} - {"none", None})},
        "ready_pods": snap["ready_pods"],
        "total_pods": snap["total_pods"],
        "restart_count": snap["restarts"],
        "not_ready": [p["name"] for p in snap["pods"] if p["ready"] != "Ready"],
    }
    with_cpu = [w for w in workloads if w.get("cpu_pct") is not None]
    if with_cpu:
        top_cpu = max(with_cpu, key=lambda w: w["cpu_pct"])
        top_mem = max(with_cpu, key=lambda w: w.get("mem_mb") or 0)
        summary.update(max_cpu_pct=top_cpu["cpu_pct"], max_cpu_container=top_cpu["name"],
                       max_mem_mb=top_mem.get("mem_mb"), max_mem_container=top_mem["name"])

    sample = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "simulated": simulated,
        "backend": snap["backend"],
        "metrics": metrics,
        "summary": summary,
        "workloads": workloads,
        "recent_events": [e for e in (extras.get("events") or []) if not names or e["container"] in names],
        "resources_age_s": extras.get("age_s"),
    }
    if journey_info is not None:
        sample["journey"] = journey_info
    return sample


def is_breach(sample, sla_ms=LATENCY_SLA_MS):
    s = sample["summary"]
    return s["error_rate_pct"] > 0 or s["p95_latency_ms"] > sla_ms


def _atomic_write(path, text):
    """Write via temp file + replace so a reader never sees half a JSON document."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: target momentarily held open by a reader
            time.sleep(0.05 * (attempt + 1))
    path.write_text(text, encoding="utf-8")
    tmp.unlink(missing_ok=True)


def _append_log(sample):
    """Append one compact line to telemetry_log.jsonl, rotating once at LOG_MAX_BYTES."""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH.with_name(LOG_PATH.name + ".1"))
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sample, separators=(",", ":")) + "\n")
    except OSError:
        pass


def write_telemetry(sample, sla_ms=LATENCY_SLA_MS):
    """Append the sample (keeping the latest HISTORY_SIZE) and set/clear SLA_BREACH.flag.
    Returns None on success or an error string."""
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        history = []
        if TELEMETRY_PATH.exists():
            try:
                loaded = json.loads(TELEMETRY_PATH.read_text(encoding="utf-8"))
                history = loaded if isinstance(loaded, list) else []
            except json.JSONDecodeError:
                history = []  # corrupt file: start fresh instead of failing forever
        history.append(sample)
        _atomic_write(TELEMETRY_PATH, json.dumps(history[-HISTORY_SIZE:], indent=2))
        _append_log(sample)
        if is_breach(sample, sla_ms):
            FLAG_FILE.write_text("BREACH", encoding="utf-8")
        else:
            FLAG_FILE.unlink(missing_ok=True)
        return None
    except OSError as exc:
        return str(exc)[:200]

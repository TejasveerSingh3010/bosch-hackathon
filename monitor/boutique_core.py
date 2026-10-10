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
import socket
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
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
# While the checkout probe has never worked, it is retried this often (in samples) instead of
# being switched off for good, so a fixed app or form is picked up without restarting the monitor.
JOURNEY_RETRY_EVERY = int(os.getenv("BOUTIQUE_JOURNEY_RETRY_EVERY", "30"))
BURST_MAX_REQUESTS = int(os.getenv("BOUTIQUE_BURST_MAX_REQUESTS", "20000"))
BURST_MAX_WORKERS = int(os.getenv("BOUTIQUE_BURST_MAX_WORKERS", "20"))
BURST_MAX_SECONDS = int(os.getenv("BOUTIQUE_BURST_MAX_SECONDS", "600"))

ENDPOINT_PATHS = {
    "Home": "/",
    "Cart": "/cart",
    "Product": "/product/0PUK6V6EV0",
}

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"

# ---------------- Per-service coverage ----------------
# Not part of the application under test: shown, but never counted as an outage.
INFRA_SERVICES = {"loadgenerator"}

# What a user would notice when each service is down, taken from the v0.10.7 frontend and checkout
# source. `pages` lists the probes that fail; an empty list means the failure is invisible to every
# HTTP probe and is only caught by the container state and the direct health check below.
#   - ads and recommendations are optional on every page: the frontend logs and carries on.
#   - checkoutservice only logs a failed confirmation email, so an order still succeeds.
#   - /cart always asks shippingservice for a quote (even for an empty cart) and fails without it.
SERVICE_IMPACT = {
    "frontend": ["Home", "Product", "Cart", "Checkout"],
    "productcatalogservice": ["Home", "Product", "Checkout"],
    "currencyservice": ["Home", "Product", "Cart", "Checkout"],
    "cartservice": ["Home", "Product", "Cart", "Checkout"],
    "redis-cart": ["Home", "Product", "Cart", "Checkout"],
    "shippingservice": ["Cart", "Checkout"],
    "checkoutservice": ["Checkout"],
    "paymentservice": ["Checkout"],
    "emailservice": [],
    "adservice": [],
    "recommendationservice": [],
    "loadgenerator": [],
}
SERVICE_IMPACT_NOTE = {
    "emailservice": "checkout logs the failure and still succeeds",
    "adservice": "frontend hides the ad",
    "recommendationservice": "frontend hides recommendations",
    "loadgenerator": "traffic generator, not part of the app",
}

# Direct health checks against the host ports published in app/docker-compose.yml.
# "grpc" = the standard grpc.health.v1 Check every Online Boutique backend implements;
# "redis" = PING. A paused container accepts the TCP connection but never answers, so the
# call times out and the service is reported down.
SERVICE_PROBE_HOST = os.getenv("BOUTIQUE_SERVICE_HOST", "127.0.0.1")
SERVICE_PROBE_TIMEOUT_S = float(os.getenv("BOUTIQUE_SERVICE_PROBE_TIMEOUT", "1.0"))
SERVICE_PROBES_ON = os.getenv("BOUTIQUE_SERVICE_PROBES", "1") != "0"
SERVICE_PROBES = {
    "cartservice": ("grpc", 7070),
    "productcatalogservice": ("grpc", 3550),
    "currencyservice": ("grpc", 7000),
    "paymentservice": ("grpc", 50051),
    "shippingservice": ("grpc", 50052),
    "emailservice": ("grpc", 8081),
    "checkoutservice": ("grpc", 5050),
    "recommendationservice": ("grpc", 8082),
    "adservice": ("grpc", 9555),
    "redis-cart": ("redis", 6379),
}


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


def _probe_one(name, url, simulate_failure):
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
    return {
        "endpoint": name, "url": url, "status": status,
        "latency_ms": round(latency_ms, 2), "success": success, "error": error,
        "error_type": error_type,
    }


def probe_endpoints(base, simulate_failure=False):
    """GET every monitored page at once (not one after another), so one hung page costs at most
    PROBE_TIMEOUT_S per sample instead of that times the number of pages."""
    with ThreadPoolExecutor(max_workers=len(ENDPOINT_PATHS)) as pool:
        futures = [pool.submit(_probe_one, name, base + path, simulate_failure)
                   for name, path in ENDPOINT_PATHS.items()]
        return [f.result() for f in futures]


class CheckoutJourney:
    """Synthetic purchase (add to cart -> place order) through the frontend.

    It exercises cart, checkout, payment, shipping, currency and product catalog, which the plain
    page probes only partly touch. It runs every JOURNEY_EVERY samples and its last result is
    carried into the samples in between (with age_s) so a failure isn't hidden by the next quiet
    sample.

    Until it has succeeded once, its result is kept out of the error rate (so a form mismatch can
    never raise a false alarm), but it is NOT switched off: after a few failed tries it keeps
    retrying every JOURNEY_RETRY_EVERY samples and reports why it fails in `info()`, so the
    dashboard can say which services are unmonitored and why."""
    MAX_FAILS_BEFORE_BACKOFF = 3
    # Digits only: the frontend validates this with go-playground's `credit_card` rule. This is
    # the demo app's own mock Visa (it passes the Luhn check).
    CARD_NUMBER = "4432801561520454"

    def __init__(self, base, every=JOURNEY_EVERY, retry_every=JOURNEY_RETRY_EVERY):
        self.base, self.every = base, every
        self.retry_every = max(1, retry_every)
        self.enabled = every > 0
        self.disabled_reason = None if self.enabled else "disabled by configuration"
        self.ever_ok = False
        self.last_error = None
        self._fails = 0
        self._ticks = 0
        self._last = None
        self._last_at = 0.0

    @property
    def status(self):
        if not self.enabled:
            return "off"
        return "verified" if self.ever_ok else "unverified"

    def info(self):
        return {"enabled": self.enabled, "verified": self.ever_ok, "status": self.status,
                "failed_tries": self._fails, "last_error": self.last_error,
                "disabled_reason": self.disabled_reason}

    @staticmethod
    def _excerpt(response):
        """The frontend's error page puts the reason in a <pre> block; fall back to the page text."""
        text = response.text or ""
        m = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S | re.I)
        text = m.group(1) if m else re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:200]

    def _run_once(self):
        started = time.perf_counter()
        status, error, exc = 0, "", None
        try:
            sess = requests.Session()
            sess.get(self.base + "/", timeout=PROBE_TIMEOUT_S)
            add = sess.post(self.base + "/cart", data={"product_id": "OLJCESPC7Z", "quantity": "1"},
                            timeout=5, allow_redirects=False)
            if add.status_code >= 400:
                status = add.status_code
                error = f"add-to-cart failed: {self._excerpt(add)}"
            else:
                year = datetime.now().year + 2
                order = sess.post(self.base + "/cart/checkout", timeout=10, data={
                    "email": "monitor@example.com", "street_address": "1600 Amphitheatre Parkway",
                    "zip_code": "94043", "city": "Mountain View", "state": "CA", "country": "United States",
                    "credit_card_number": self.CARD_NUMBER,
                    "credit_card_expiration_month": "1", "credit_card_expiration_year": str(year),
                    "credit_card_cvv": "672",
                })
                status = order.status_code
                if status >= 400:
                    error = f"place-order failed: {self._excerpt(order)}"
        except requests.RequestException as e:
            exc, error = e, str(e)[:160]
        return {
            "endpoint": "Checkout", "url": self.base + "/cart/checkout", "status": status,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "success": 200 <= status < 400, "error": error, "error_type": classify_error(status, exc),
        }

    def _due(self):
        if self._last is None:
            return True
        if self.ever_ok:
            return self._ticks % self.every == 0
        if self._fails < self.MAX_FAILS_BEFORE_BACKOFF:
            return True
        return self._ticks % self.retry_every == 0

    def tick(self):
        """Call once per sample. Returns the row to include (fresh or carried over) or None."""
        if not self.enabled:
            return None
        self._ticks += 1
        if self._due():
            row = self._run_once()
            self._last, self._last_at = row, time.time()
            if row["success"]:
                self.ever_ok, self._fails, self.last_error = True, 0, None
            else:
                self.last_error = f"HTTP {row['status']} ({row['error_type']}): {row['error']}"[:300]
                if not self.ever_ok:
                    self._fails += 1
        if not self.ever_ok:
            return None  # not trusted until it has worked once
        row = dict(self._last)
        row["age_s"] = round(time.time() - self._last_at, 1)
        return row


def _p95(values):
    values = sorted(values)
    return values[max(0, int(0.95 * len(values) + 0.999) - 1)] if values else 0


def bounded_traffic_burst(base, total_requests=None, workers=5, duration_s=None):
    """GET burst across the monitored pages.

    With duration_s, the workers keep requesting until that many seconds have passed.
    Requests already in flight are allowed to finish; no new ones start after the deadline.
    total_requests (default BOUTIQUE_BURST_MAX_REQUESTS) is a hard stop either way.
    Workers are capped by BOUTIQUE_BURST_MAX_WORKERS and duration by BOUTIQUE_BURST_MAX_SECONDS.
    """
    urls = [base + p for p in ENDPOINT_PATHS.values()]
    workers = max(1, min(int(workers), BURST_MAX_WORKERS))
    cap = BURST_MAX_REQUESTS if total_requests is None else int(total_requests)
    cap = max(1, min(cap, BURST_MAX_REQUESTS))
    if duration_s is not None:
        duration_s = max(1, min(int(duration_s), BURST_MAX_SECONDS))
        deadline = time.perf_counter() + duration_s
    else:
        deadline = None

    def hit(i):
        started = time.perf_counter()
        try:
            r = requests.get(urls[i % len(urls)], timeout=5)
            ok = 200 <= r.status_code < 400
        except requests.RequestException:
            ok = False
        return {"latency_ms": round((time.perf_counter() - started) * 1000, 2), "success": ok}

    results = []
    started_at = time.perf_counter()
    sent = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        if deadline is None:
            for future in as_completed([pool.submit(hit, i) for i in range(cap)]):
                results.append(future.result())
        else:
            pending = set()
            while sent < cap and len(pending) < workers and time.perf_counter() < deadline:
                pending.add(pool.submit(hit, sent))
                sent += 1
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    results.append(future.result())
                while sent < cap and len(pending) < workers and time.perf_counter() < deadline:
                    pending.add(pool.submit(hit, sent))
                    sent += 1
    errors = sum(not x["success"] for x in results)
    lats = [x["latency_ms"] for x in results]
    return {
        "total": len(results),
        "errors": errors,
        "error_rate_pct": round(100 * errors / max(1, len(results)), 2),
        "mean_latency_ms": round(sum(lats) / max(1, len(lats)), 2) if lats else 0,
        "p95_latency_ms": _p95(lats),
        "elapsed_s": round(time.perf_counter() - started_at, 2),
        "duration_s": duration_s,
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
            # A paused container still reports Running=True, but it answers nothing.
            running = bool(state.get("Running")) and not state.get("Restarting") and not state.get("Paused")
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
            "paused": bool(state.get("Paused")),
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


# ---------------- Direct service health checks ----------------
_GRPC_HEALTH = {0: "unknown", 1: "serving", 2: "not serving", 3: "service unknown"}


def _grpc_health(host, port, timeout):
    """grpc.health.v1.Health/Check with an empty request, decoded by hand so no generated stubs
    are needed. Returns (ok, detail)."""
    import grpc  # optional dependency; the caller falls back to a TCP check without it

    with grpc.insecure_channel(f"{host}:{port}") as channel:
        check = channel.unary_unary("/grpc.health.v1.Health/Check",
                                    request_serializer=lambda _: b"",
                                    response_deserializer=lambda raw: raw)
        try:
            raw = check(None, timeout=timeout)
        except grpc.RpcError as exc:
            code = exc.code()
            if code == grpc.StatusCode.UNIMPLEMENTED:
                return True, "responding (no health API)"
            return False, f"gRPC {code.name.lower().replace('_', ' ')}"
    # HealthCheckResponse is a single varint field 1 (tag 0x08); empty means UNKNOWN (0).
    status = raw[1] if len(raw) >= 2 and raw[0] == 0x08 else 0
    return status == 1, _GRPC_HEALTH.get(status, f"status {status}")


def _redis_ping(host, port, timeout):
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"PING\r\n")
        reply = sock.recv(64)
    if reply.startswith(b"+PONG"):
        return True, "PONG"
    return False, f"unexpected reply {reply[:20]!r}"


def _tcp_open(host, port, timeout):
    with socket.create_connection((host, port), timeout=timeout):
        return True, "port open (install grpcio for a real health check)"


def check_service(name, host=None, timeout=None):
    """One direct health check. Returns {ok, detail, latency_ms, kind, port}."""
    kind, port = SERVICE_PROBES[name]
    host = host or SERVICE_PROBE_HOST
    timeout = SERVICE_PROBE_TIMEOUT_S if timeout is None else timeout
    started = time.perf_counter()
    try:
        if kind == "redis":
            ok, detail = _redis_ping(host, port, timeout)
        else:
            try:
                ok, detail = _grpc_health(host, port, timeout)
            except ImportError:
                kind = "tcp"
                ok, detail = _tcp_open(host, port, timeout)
    except TimeoutError:  # socket.timeout is TimeoutError on Python 3.10+
        ok, detail = False, "timed out (hung or paused)"
    except ConnectionRefusedError:
        ok, detail = False, "connection refused (process down)"
    except OSError as exc:
        ok, detail = False, f"unreachable: {exc}"[:120]
    return {"ok": ok, "detail": detail, "kind": kind, "port": port,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


def check_all_services(host=None, timeout=None, names=None):
    names = list(names or SERVICE_PROBES)
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        futures = {name: pool.submit(check_service, name, host, timeout) for name in names}
        return {name: f.result() for name, f in futures.items()}


class ServiceProbeSampler:
    """Runs the direct health checks on a background thread (a hung service takes up to the
    timeout to answer), so the 2-second probe loop never waits on them."""

    def __init__(self, interval=2.0, host=None, timeout=None):
        self.interval, self.host, self.timeout = interval, host, timeout
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._data, self._updated = {}, None
        self._thread = None

    def start(self):
        if self._thread is None and SERVICE_PROBES_ON:
            self._thread = threading.Thread(target=self._loop, name="service-probes", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                result = check_all_services(self.host, self.timeout)
                with self._lock:
                    self._data, self._updated = result, time.time()
            except Exception:  # never let a probe bug kill the sampler
                pass
            self._stop.wait(self.interval)

    def latest(self):
        """{service: check result}, or {} until the first round has finished. Results older than
        three intervals are dropped, so a stuck sampler cannot report stale health."""
        with self._lock:
            data, updated = dict(self._data), self._updated
        if updated is None or time.time() - updated > max(3 * self.interval, 5):
            return {}
        return data


def service_status(snap, probes=None):
    """One row per service: container state + direct health check + what a user would notice.

    A service is `down` when its container is not ready or its direct check fails. Services in
    INFRA_SERVICES are listed but never counted as down."""
    probes = probes or {}
    rows, seen = [], set()
    for pod in snap.get("pods") or []:
        app = pod.get("app") or pod.get("name")
        seen.add(app)
        rows.append(_service_row(app, pod, probes.get(app)))
    for app, probe in probes.items():  # no container data (e.g. docker unavailable)
        if app not in seen:
            rows.append(_service_row(app, None, probe))
    order = list(SERVICE_IMPACT)

    def rank(row):
        name = row["service"]
        return (not row["down"], order.index(name) if name in order else len(order), name)

    rows.sort(key=rank)
    return rows


def _service_row(app, pod, probe):
    reasons = []
    container_ready = None if pod is None else pod.get("ready") == "Ready"
    if pod is not None and not container_ready:
        reasons.append(f"container {pod.get('phase', 'not ready')}")
    probe_ok = None if probe is None else bool(probe.get("ok"))
    if probe is not None and not probe_ok:
        reasons.append(f"health check: {probe.get('detail')}")
    infra = app in INFRA_SERVICES
    down = bool(reasons) and not infra
    pages = SERVICE_IMPACT.get(app)
    return {
        "service": app,
        "container": None if pod is None else pod.get("name"),
        "state": None if pod is None else pod.get("phase"),
        "container_ready": container_ready,
        "health_check": None if probe is None else probe.get("detail"),
        "health_ok": probe_ok,
        "health_latency_ms": None if probe is None else probe.get("latency_ms"),
        "pages": pages if pages is not None else [],
        "visible_to_page_probes": bool(pages and any(p != "Checkout" for p in pages)),
        "impact_note": SERVICE_IMPACT_NOTE.get(app, ""),
        "infrastructure": infra,
        "down": down,
        "reason": "; ".join(reasons),
    }


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


# ---------------- Live sampling on background threads ----------------
class JourneyRunner:
    """Runs CheckoutJourney.tick() on its own thread. A checkout can take up to ~18 s when a
    dependency hangs; on its own thread that never delays a sample."""

    def __init__(self, journey, interval=2.0):
        self.journey, self.interval = journey, interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._row = None
        self._thread = None

    def start(self):
        if self._thread is None and self.journey.enabled:
            self._thread = threading.Thread(target=self._loop, name="checkout-journey", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                row = self.journey.tick()
                with self._lock:
                    self._row = row
            except Exception:  # never let a probe bug kill the thread
                pass
            self._stop.wait(self.interval)

    def latest_row(self):
        with self._lock:
            row = None if self._row is None else dict(self._row)
        if row is not None and self.journey._last_at:
            row["age_s"] = round(time.time() - self.journey._last_at, 1)
        return row


class LiveSampler:
    """Takes one telemetry sample every `interval` seconds on a background thread.

    The dashboard only reads `latest()` and `history()`, so a page refresh is instant even while a
    stopped service makes the probes slow. (Doing the probes inside each refresh let a slow sample
    overrun the next refresh: Streamlit then abandons the half-drawn page, which shows as the whole
    dashboard greying out and the lower sections never appearing.)"""

    def __init__(self, base_url, backend="docker", namespace="default", project="", interval=2.0,
                 journey_every=JOURNEY_EVERY, write=False, sla_ms=LATENCY_SLA_MS, on_sample=None):
        self.key = (base_url, backend, namespace, project)
        self.base_url, self.backend, self.namespace, self.project = base_url, backend, namespace, project
        self.interval = interval
        self.write, self.sla_ms, self.on_sample = write, sla_ms, on_sample
        self.simulate_until = 0.0
        self.journey = CheckoutJourney(base_url, every=journey_every)
        self.journey_runner = JourneyRunner(self.journey, interval)
        self.extras = DockerExtrasSampler() if backend == "docker" else None
        self.health = ServiceProbeSampler(interval=interval)
        self.last_error = None
        self._history = deque(maxlen=HISTORY_SIZE)
        self._latest = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._first = threading.Event()
        self._thread = None

    def start_helpers(self):
        self.health.start()
        if self.extras:
            self.extras.start()
        return self

    def start(self):
        self.start_helpers()
        self.journey_runner.start()
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="live-sampler", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self.journey_runner.stop()
        self.health.stop()
        if self.extras:
            self.extras.stop()

    @property
    def simulating(self):
        return time.time() < self.simulate_until

    def collect_once(self, sync_journey=False):
        started = time.time()
        simulate = self.simulating
        rows = probe_endpoints(self.base_url, simulate_failure=simulate)
        jrow = self.journey.tick() if sync_journey else self.journey_runner.latest_row()
        if jrow:
            rows.append(jrow)
        snap = get_snapshot(self.backend, self.namespace, self.project)
        extras = None
        if self.extras:
            extras = self.extras.latest()
            names = {p["name"] for p in snap["pods"]}
            resources = {n: r for n, r in extras["resources"].items() if n in names}
            snap["cpu_memory"] = format_resources(resources)
            snap["events"] = [f"{e['time'][11:19]}  {e['action']:<22} {e['container']}"
                              + (f" exitCode={e['exit_code']}" if e["exit_code"] not in (None, 0) else "")
                              for e in extras["events"] if e["container"] in names][-8:]
        sample = build_sample(rows, snap, simulated=simulate, extras=extras,
                              journey_info=self.journey.info(), service_probes=self.health.latest())
        write_error = write_telemetry(sample, self.sla_ms) if self.write else None
        bundle = {"sample": sample, "probe_rows": rows, "snap": snap, "write_error": write_error,
                  "collected_at": time.time(), "cycle_s": round(time.time() - started, 2)}
        with self._lock:
            self._history.append(sample)
            self._latest = bundle
        self._first.set()
        if self.on_sample:
            self.on_sample(bundle)
        return bundle

    def _loop(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                self.collect_once()
                self.last_error = None
            except Exception as exc:  # keep sampling; surface the problem instead
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            self._stop.wait(max(0.2, self.interval - (time.time() - started)))

    def wait_for_first(self, timeout=8.0):
        return self._first.wait(timeout)

    def latest(self):
        """The newest bundle (sample, probe_rows, snap, write_error, collected_at, cycle_s, age_s)."""
        with self._lock:
            bundle = None if self._latest is None else dict(self._latest)
        if bundle is not None:
            bundle["age_s"] = round(time.time() - bundle["collected_at"], 1)
        return bundle

    def history(self):
        with self._lock:
            return list(self._history)


_SHARED_LOCK = threading.Lock()
_SHARED = None


def shared_live_sampler(base_url, backend="docker", namespace="default", project="", interval=2.0):
    """One sampler per process (module globals survive Streamlit reruns). Changing the settings
    stops the old sampler and starts a new one."""
    global _SHARED
    with _SHARED_LOCK:
        key = (base_url, backend, namespace, project)
        if _SHARED is None or _SHARED.key != key or _SHARED.interval != interval:
            if _SHARED is not None:
                _SHARED.stop()
            _SHARED = LiveSampler(base_url, backend, namespace, project, interval=interval).start()
        return _SHARED


# ---------------- Telemetry ----------------
def build_sample(probe_rows, snap, simulated=False, extras=None, journey_info=None, service_probes=None):
    """One telemetry record. `extras` is DockerExtrasSampler.latest(); `journey_info` is
    CheckoutJourney.info(); `service_probes` is ServiceProbeSampler.latest()."""
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
    services = service_status(snap, service_probes)
    summary["services_down"] = [r["service"] for r in services if r["down"]]
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
        "services": services,
        "recent_events": [e for e in (extras.get("events") or []) if not names or e["container"] in names],
        "resources_age_s": extras.get("age_s"),
    }
    if journey_info is not None:
        sample["journey"] = journey_info
    return sample


def is_breach(sample, sla_ms=LATENCY_SLA_MS):
    """Probe errors, a latency SLA breach, or any application service down (by container state
    or direct health check). The last one matters because several services - ads,
    recommendations, email - can fail without a single page probe noticing."""
    s = sample["summary"]
    return s["error_rate_pct"] > 0 or s["p95_latency_ms"] > sla_ms or bool(s.get("services_down"))


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

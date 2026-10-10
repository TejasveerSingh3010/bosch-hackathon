"""Resilience score: one 0-100 number built from what the monitor and the experiments measured.

    R = sum(w_i * s_i) / sum(w_i)    over the factors that have data

Each factor score s_i is 0-100 and comes from a fixed linear rule over files in results/ (the
telemetry log and the experiment results), so every point traces back to a measurement. There is no
universal standard for this number; the weights follow a common split and are easy to change.
A factor with no data yet is left out and its weight is shared by the others (the dashboard says so).

Factors:
  availability      probe success over the telemetry log (simulated samples excluded)
  fault_tolerance   share of page probes still passing while a fault was injected; redundancy (more
                    than one container per service) counts for a fifth
  recovery          MTTR (rollback -> healthy) times the share of experiments that recovered at all
  failure_handling  whether requests stayed fast during a fault (fail fast / time out) or hung until
                    the probe gave up: p95 at the SLA scores 100, at the probe timeout 0
  observability     how fast the monitor saw the fault (injection -> first degraded sample), plus the
                    share of services with a direct health check

Only the newest result per (service, action) counts, so re-running an experiment after a fix replaces
the old evidence instead of being averaged with it. Pure functions; the dashboard does the file I/O
caching and passes the live sample in.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

WEIGHTS = {
    "availability": 0.25,
    "fault_tolerance": 0.25,
    "recovery": 0.20,
    "failure_handling": 0.15,
    "observability": 0.15,
}
LABELS = {
    "availability": "Availability",
    "fault_tolerance": "Fault tolerance",
    "recovery": "Recovery (MTTR)",
    "failure_handling": "Failure handling",
    "observability": "Observability",
}
# (score, label), highest first: the first band the score reaches.
BANDS = [(85, "Resilient"), (70, "Moderate"), (50, "Fragile"), (0, "Brittle")]

AVAILABILITY_FLOOR_PCT = 95.0  # scores 0
AVAILABILITY_TARGET_PCT = 99.9  # scores 100
MTTR_GOOD_S = 10.0
MTTR_BAD_S = 120.0
DETECT_GOOD_S = 5.0  # about two monitor samples
DETECT_BAD_S = 60.0
REDUNDANCY_SHARE = 0.2  # of fault_tolerance
DETECTION_SHARE = 0.6  # of observability; the rest is health-check coverage
COUNTED_VERDICTS = {"completed", "aborted", "stopped"}
NOT_SCORED_APPS = {"loadgenerator"}


def linear(value: float, bad: float, good: float) -> float:
    """0 at `bad`, 100 at `good`, straight line between, clamped. Works in either direction."""
    if good == bad:
        return 100.0 if value == good else 0.0
    return round(max(0.0, min(1.0, (value - bad) / (good - bad))) * 100, 1)


def band(score: float) -> str:
    return next(label for floor, label in BANDS if score >= floor)


# ---------------- loading (called by the dashboard, cached there) ----------------
def load_history(log_path: Path) -> list[dict]:
    """Compact rows from telemetry_log.jsonl (and its rotated .1, oldest first)."""
    rows = []
    for path in (log_path.with_name(log_path.name + ".1"), log_path):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                sample = json.loads(line)
                summary = sample.get("summary") or {}
                rows.append({
                    "ts": _time(sample["timestamp"]),
                    "error_rate_pct": float(summary.get("error_rate_pct") or 0.0),
                    "p95_ms": float(summary.get("p95_latency_ms") or 0.0),
                    "not_ready": bool(summary.get("not_ready")),
                    "simulated": bool(sample.get("simulated")),
                })
            except (ValueError, KeyError, TypeError, AttributeError):
                continue  # a torn last line while the monitor writes
    return rows


def load_experiments(results_dir: Path) -> list[dict]:
    """The newest counted result per (service, action), from results/*.json."""
    newest: dict[tuple, dict] = {}
    for path in results_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("verdict") not in COUNTED_VERDICTS:
            continue
        exp = data.get("experiment") or {}
        if not data.get("injected_at") or not (data.get("during") or {}).get("samples"):
            continue  # nothing was measured while the fault was in place
        try:
            data["_injected"] = _time(data["injected_at"])
        except (TypeError, ValueError):
            continue
        data["_id"] = exp.get("id") or path.stem
        key = (data.get("service") or exp.get("target"), exp.get("action"))
        if key not in newest or data["_injected"] > newest[key]["_injected"]:
            newest[key] = data
    return sorted(newest.values(), key=lambda d: d["_injected"])


def _time(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


# ---------------- factors ----------------
def availability(history: list[dict]) -> dict:
    real = [r for r in history if not r["simulated"]]
    if not real:
        return _none("no telemetry log yet")
    pct = sum(100 - r["error_rate_pct"] for r in real) / len(real)
    hours = (real[-1]["ts"] - real[0]["ts"]) / 3600
    return _factor(
        linear(pct, AVAILABILITY_FLOOR_PCT, AVAILABILITY_TARGET_PCT),
        f"{pct:.2f}% probe success over {len(real)} samples ({hours:.1f} h); "
        f"{AVAILABILITY_TARGET_PCT}% scores 100, {AVAILABILITY_FLOOR_PCT:.0f}% scores 0",
    )


def fault_tolerance(experiments: list[dict], workloads: list[dict]) -> dict:
    if not experiments:
        return _none("no experiments yet")
    kept = [100 - float(e["during"].get("error_rate_pct_mean") or 0.0) for e in experiments]
    measured = sum(kept) / len(kept)
    worst = max(experiments, key=lambda e: e["during"].get("error_rate_pct_mean") or 0.0)
    apps = _container_counts(workloads)
    if apps:
        redundant = sum(1 for n in apps.values() if n > 1)
        redundancy = 100 * redundant / len(apps)
        score = (1 - REDUNDANCY_SHARE) * measured + REDUNDANCY_SHARE * redundancy
        red_txt = f"; {redundant}/{len(apps)} services run more than one container"
    else:
        score, red_txt = measured, ""
    return _factor(
        round(score, 1),
        f"{measured:.0f}% of page probes passed during {len(experiments)} faults "
        f"(worst: {_name(worst)}){red_txt}",
    )


def recovery(experiments: list[dict]) -> dict:
    if not experiments:
        return _none("no experiments yet")
    recovered = [e for e in experiments if e.get("recovered_at")]
    share = len(recovered) / len(experiments)
    times = [float(e["recovery_seconds"]) for e in recovered if e.get("recovery_seconds") is not None]
    if not times:
        return _factor(0.0, f"{len(recovered)}/{len(experiments)} experiments recovered; no recovery time")
    mttr = sum(times) / len(times)
    return _factor(
        round(linear(mttr, MTTR_BAD_S, MTTR_GOOD_S) * share, 1),
        f"MTTR {mttr:.1f} s; {len(recovered)}/{len(experiments)} experiments recovered "
        f"(<={MTTR_GOOD_S:.0f} s scores 100, >={MTTR_BAD_S:.0f} s scores 0)",
    )


def failure_handling(experiments: list[dict], sla_ms: float, timeout_ms: float) -> dict:
    if not experiments:
        return _none("no experiments yet")
    scores = [linear(float(e["during"].get("p95_ms_max") or 0.0), timeout_ms, sla_ms) for e in experiments]
    hung = [e for e in experiments if float(e["during"].get("p95_ms_max") or 0.0) >= 0.95 * timeout_ms]
    text = (f"p95 during a fault stayed under the {sla_ms:.0f} ms SLA in "
            f"{sum(s == 100 for s in scores)}/{len(experiments)} experiments")
    if hung:
        text += f"; requests hung to the {timeout_ms:.0f} ms probe timeout in " + ", ".join(map(_name, hung))
    return _factor(round(sum(scores) / len(scores), 1), text)


def observability(experiments: list[dict], history: list[dict], services: list[dict],
                  monitored: set[str], p95_limit_ms: float) -> dict:
    """`monitored`: services the monitor checks directly (configured, so the share does not drop while
    the first checks are still running)."""
    parts, texts = [], []
    delays = [d for d in (detection_seconds(e, history, p95_limit_ms) for e in experiments) if d is not None]
    if delays:
        mean = sum(delays) / len(delays)
        parts.append((DETECTION_SHARE, linear(mean, DETECT_BAD_S, DETECT_GOOD_S)))
        texts.append(f"faults detected in {mean:.1f} s on average ({len(delays)} measured)")
    checked = [r for r in services if not r.get("infrastructure")]
    if checked:
        covered = sum(1 for r in checked if r["service"] in monitored)
        parts.append((1 - DETECTION_SHARE, 100 * covered / len(checked)))
        texts.append(f"{covered}/{len(checked)} services checked directly (health check or page probe)")
    if not parts:
        return _none("no detection or health-check data yet")
    return _factor(round(sum(w * s for w, s in parts) / sum(w for w, _ in parts), 1), "; ".join(texts))


def detection_seconds(experiment: dict, history: list[dict], p95_limit_ms: float) -> float | None:
    """Injection -> first degraded monitor sample; None if the fault never showed or the log has no data.

    Uses the result's own detection_seconds when the runner set it.
    """
    if experiment.get("detection_seconds") is not None:
        return float(experiment["detection_seconds"])
    limit = float((experiment.get("thresholds_used") or {}).get("p95_ms") or p95_limit_ms)
    try:
        start = _time(experiment["injected_at"])
        end = _time(experiment.get("rolled_back_at") or experiment.get("ended_at")
                    or experiment["injected_at"])
    except (KeyError, TypeError, ValueError):
        return None
    for row in history:
        if row["simulated"] or row["ts"] < start or row["ts"] > end:
            continue
        if row["error_rate_pct"] > 0 or row["p95_ms"] > limit or row["not_ready"]:
            return round(row["ts"] - start, 1)
    return None


# ---------------- total ----------------
def compute(history: list[dict], experiments: list[dict], services: list[dict], workloads: list[dict],
            monitored: set[str], sla_ms: float, timeout_ms: float, p95_limit_ms: float) -> dict:
    """Every factor plus the weighted total (None until at least one factor has data)."""
    factors = {
        "availability": availability(history),
        "fault_tolerance": fault_tolerance(experiments, workloads),
        "recovery": recovery(experiments),
        "failure_handling": failure_handling(experiments, sla_ms, timeout_ms),
        "observability": observability(experiments, history, services, monitored, p95_limit_ms),
    }
    scored = {k: f for k, f in factors.items() if f["score"] is not None}
    total_weight = sum(WEIGHTS[k] for k in scored)
    for key, factor in factors.items():
        factor["weight"] = WEIGHTS[key]
        factor["effective_weight"] = WEIGHTS[key] / total_weight if key in scored else 0.0
    score = round(sum(f["score"] * f["effective_weight"] for f in scored.values()), 1) if scored else None
    return {
        "score": score,
        "band": None if score is None else band(score),
        "factors": factors,
        "missing": [k for k in factors if k not in scored],
        "experiments": [e["_id"] for e in experiments],
    }


def _factor(score: float, evidence: str) -> dict:
    return {"score": score, "evidence": evidence}


def _none(reason: str) -> dict:
    return {"score": None, "evidence": reason}


def _name(experiment: dict) -> str:
    return f"{(experiment.get('experiment') or {}).get('action')} {experiment.get('service')}"


def _container_counts(workloads: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for w in workloads or []:
        app = w.get("app")
        if app and app not in NOT_SCORED_APPS:
            counts[app] = counts.get(app, 0) + 1
    return counts

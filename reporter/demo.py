"""A synthetic failure, so Agent 2's output can be seen without Docker or a running app.

    python -m reporter demo                       # pause productcatalogservice (a hang)
    python -m reporter demo --scenario kill       # kill redis-cart (Docker restarts it)

The scenario writes realistic telemetry (the monitor's real sample format), a finished experiment
record and a few container-log findings into results/demo/, then runs the reporter on them. The
report lands in reports/ exactly where a real one would, and the dashboard shows it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from safety.telemetry import summarise_phase
from schemas import Experiment, ExperimentResult, Phase

from .models import LogFinding
from .service import Reporter

PROJECT = "online-boutique"
PAGES = ("Home", "Cart", "Product", "Checkout")
SERVICES = (
    "adservice",
    "cartservice",
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "frontend",
    "paymentservice",
    "productcatalogservice",
    "recommendationservice",
    "redis-cart",
    "shippingservice",
)

SCENARIOS: dict[str, dict[str, Any]] = {
    "pause": {
        "target": "productcatalogservice",
        "action": "pause",
        "duration_s": 40,
        "reason": "Does the frontend fail fast or hang when the catalog stops answering?",
        "failed_pages": ["Home", "Product", "Checkout"],
        "error_type": "timeout",
        "latency_ms": 5000.0,
        "down_for_s": 40,
        "self_healed": False,
        "docker_events": [(0.4, "pause", None, None), (40.3, "unpause", None, None)],
        "rollback_note": "unpaused online-boutique-productcatalogservice-1",
        "logs": [
            (
                "frontend",
                "deadline_exceeded",
                14,
                4.6,
                "could not retrieve products: rpc error: code = DeadlineExceeded "
                "desc = context deadline exceeded",
            ),
            ("recommendationservice", "timeout", 6, 7.9, "ListProducts request timed out after 3s"),
            (
                "checkoutservice",
                "deadline_exceeded",
                3,
                12.2,
                "failed to get product #OLJCESPC7Z: rpc error: code = DeadlineExceeded",
            ),
        ],
    },
    "kill": {
        "target": "redis-cart",
        "action": "kill",
        "duration_s": 30,
        "reason": "Single point of failure for cart and checkout",
        "failed_pages": ["Home", "Cart", "Product", "Checkout"],
        "error_type": "http_5xx",
        "latency_ms": 90.0,
        "down_for_s": 6,
        "self_healed": True,
        "docker_events": [(0.3, "kill", 137, "9"), (0.5, "die", 137, None), (6.2, "start", None, None)],
        "rollback_note": "already running (Docker's restart policy recovered it)",
        "logs": [
            (
                "cartservice",
                "connection_refused",
                9,
                1.4,
                "Can't access cart storage: unable to connect to redis-cart:6379 (connection refused)",
            ),
            (
                "frontend",
                "error",
                8,
                2.1,
                "could not retrieve cart: rpc error: code = Internal desc = cart failure",
            ),
        ],
    },
}


def _sample(
    at: datetime, spec: dict[str, Any], failing: bool, down: bool, events: list[dict[str, Any]], restarts: int
) -> dict[str, Any]:
    failed = set(spec["failed_pages"]) if failing else set()
    metrics = {
        page: {
            "status": 0
            if page in failed and spec["error_type"] == "timeout"
            else (503 if page in failed else 200),
            "latency": spec["latency_ms"] if page in failed else 38.0,
            "success": page not in failed,
            "error": spec["error_type"] if page in failed else "",
            "error_type": spec["error_type"] if page in failed else "none",
        }
        for page in PAGES
    }
    latencies = [m["latency"] for m in metrics.values()]
    target = spec["target"]
    rows = [
        {
            "service": name,
            "container": f"{PROJECT}-{name}-1",
            "state": "running",
            "container_ready": True,
            "health_ok": not (down and name == target),
            "pages": [],
            "infrastructure": False,
            "down": down and name == target,
            "reason": f"health check: {spec['error_type']}" if down and name == target else "",
        }
        for name in SERVICES
    ]
    return {
        "timestamp": at.isoformat(),
        "simulated": False,
        "backend": "docker",
        "metrics": metrics,
        "summary": {
            "request_count": len(PAGES),
            "error_rate_pct": 100.0 * len(failed) / len(PAGES),
            "mean_latency_ms": round(sum(latencies) / len(latencies), 2),
            "p95_latency_ms": max(latencies),
            "slowest_endpoint": max(metrics, key=lambda p: metrics[p]["latency"]),
            "error_types": {spec["error_type"]: len(failed)} if failed else {},
            "ready_pods": 12,
            "total_pods": 12,
            "restart_count": restarts,
            "not_ready": [],
            "services_down": [target] if down else [],
        },
        "workloads": [
            {"name": f"{PROJECT}-{s}-1", "app": s, "ready": "Ready", "phase": "running"} for s in SERVICES
        ],
        "services": rows,
        "recent_events": [
            e for e in events if 0 <= (at - datetime.fromisoformat(e["time"])).total_seconds() <= 120
        ],
        "resources_age_s": 1.0,
    }


def build_scenario(
    name: str, results_dir: Path, now: datetime | None = None
) -> tuple[ExperimentResult, list[LogFinding]]:
    """Write telemetry + the experiment record into `results_dir`; return the record and log findings."""
    spec = SCENARIOS[name]
    now = now or datetime.now().astimezone()
    t0 = now - timedelta(seconds=150)
    injected = t0 + timedelta(seconds=60)
    rolled_back = injected + timedelta(seconds=spec["duration_s"])
    recovered = rolled_back + timedelta(seconds=4)
    end = recovered + timedelta(seconds=12)
    container = f"{PROJECT}-{spec['target']}-1"

    events = [
        {
            "time": (injected + timedelta(seconds=off)).isoformat(),
            "action": action,
            "container": container,
            "exit_code": code,
            "signal": sig,
        }
        for off, action, code, sig in spec["docker_events"]
    ]
    restart_at = injected + timedelta(seconds=spec["down_for_s"])

    samples, tick = [], t0
    while tick <= end:
        failing = injected < tick <= injected + timedelta(seconds=spec["down_for_s"])
        restarts = 1 if spec["self_healed"] and tick >= restart_at else 0
        samples.append(_sample(tick, spec, failing, failing, events, restarts))
        tick += timedelta(seconds=2)

    first_bad = next((s for s in samples if s["summary"]["error_rate_pct"] > 0), None)
    detected = datetime.fromisoformat(first_bad["timestamp"]) if first_bad else None

    def window(a: datetime, b: datetime) -> Phase:
        return Phase(
            **summarise_phase([s for s in samples if a <= datetime.fromisoformat(s["timestamp"]) <= b])
        )

    experiment = Experiment(
        id=f"demo-{spec['action']}-{spec['target']}",
        target=spec["target"],
        action=spec["action"],
        duration_s=spec["duration_s"],
        reason=spec["reason"],
    )
    result = ExperimentResult(
        experiment=experiment,
        verdict="completed",
        container=container,
        service=spec["target"],
        started_at=injected - timedelta(seconds=3),
        ended_at=end,
        injected_at=injected,
        detected_at=detected,
        rolled_back_at=rolled_back,
        recovered_at=recovered,
        detection_seconds=round((detected - injected).total_seconds(), 1) if detected else None,
        impact_seconds=float(spec["duration_s"]),
        recovery_seconds=4.0,
        self_healed=spec["self_healed"],
        restart_count_delta=1 if spec["self_healed"] else 0,
        before=window(t0, injected),
        during=window(injected, rolled_back),
        after=window(rolled_back, recovered),
        thresholds_used={"error_rate_pct": 20.0, "p95_ms": 2000.0},
        baseline={"overall": {"error_rate_pct": 0.0, "sample_p95_ms": {"mean": 38.0, "p95": 52.0}}},
        observations=[f"rollback: {spec['rollback_note']}"],
    )

    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "telemetry.json").write_text(json.dumps(samples[-60:]), encoding="utf-8")
    (results_dir / "telemetry_log.jsonl").write_text(
        "\n".join(json.dumps(s, separators=(",", ":")) for s in samples) + "\n", encoding="utf-8"
    )
    (results_dir / f"{experiment.id}.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")

    findings = [
        LogFinding(
            container=f"{PROJECT}-{service}-1",
            service=service,
            kind=kind,
            count=count,
            first_at=injected + timedelta(seconds=offset),
            first_line=line,
            last_line=line,
        )
        for service, kind, count, offset, line in spec["logs"]
    ]
    return result, findings


def run_demo(name: str, results_dir: Path, reports_dir: Path | None = None) -> tuple[Any, Path]:
    result, findings = build_scenario(name, results_dir)
    reporter = Reporter(results_dir, reports_dir, project=PROJECT, capture_container_logs=False)
    report = reporter.report_experiment(
        result, log_findings=findings, notes=["demo data: telemetry and logs are synthetic"]
    )
    return report, reporter.reports_dir / f"{report.id}.md"

"""Agent 2: the report names the first failing component, the propagation path and the timings,
and every claim cites an evidence id. Facts and inferences are labelled; it writes only to reports/.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agents.schemas import Report
from reporter.evidence import build_pack, component_degradation
from reporter.report import generate_report
from tests.conftest import make_sample

PROJECT = "online-boutique"


def _sample(at: datetime, *, error_rate=0.0, failed=None, services_down=None) -> dict[str, Any]:
    s = make_sample(at=at, error_rate_pct=error_rate, failed_endpoints=failed or [])
    s["summary"]["services_down"] = services_down or []
    s["recent_events"] = []
    return s


def _write_run(results_dir: Path, *, injected: datetime, degraded_service: str,
               failed_pages: list[str], verdict="aborted") -> str:
    """Write a results/<id>.json plus a telemetry window that degrades, then a captured log dir."""
    recovered = injected + timedelta(seconds=20)
    experiment_id = f"pause-{degraded_service}-1"
    result = {
        "experiment": {"id": experiment_id, "target": degraded_service, "action": "pause",
                       "params": {}, "duration_s": 30},
        "verdict": verdict,
        "container": f"{PROJECT}-{degraded_service}-1",
        "service": degraded_service,
        "injected_at": injected.isoformat(),
        "rolled_back_at": (injected + timedelta(seconds=12)).isoformat(),
        "recovered_at": recovered.isoformat(),
        "ended_at": recovered.isoformat(),
        "detection_seconds": 4.0,
        "recovery_seconds": 6.0,
        "self_healed": False,
        "abort_reason": "error rate 100.0% exceeded the limit 20.0%" if verdict == "aborted" else None,
        "during": {"samples": 3, "seconds": 6.0, "error_rate_pct_mean": 80.0,
                   "error_rate_pct_max": 100.0, "p95_ms_mean": 1500.0, "p95_ms_max": 2200.0,
                   "failed_endpoints": failed_pages, "error_types": {"timeout": 3}},
        "before": {}, "after": {},
        "timeline": [
            {"t": injected.isoformat(), "source": "injector",
             "event": f"injected pause: paused {degraded_service}"},
            {"t": (injected + timedelta(seconds=4)).isoformat(), "source": "watchdog",
             "event": "first degraded sample observed"},
            {"t": (injected + timedelta(seconds=12)).isoformat(), "source": "runner", "event": "rolled back"},
            {"t": recovered.isoformat(), "source": "runner", "event": "application healthy again"},
        ],
    }
    (results_dir / f"{experiment_id}.json").write_text(json.dumps(result), encoding="utf-8")

    # telemetry window: healthy before, degraded during, healthy after
    samples = []
    for i in range(5):  # before injection
        samples.append(_sample(injected - timedelta(seconds=2 * (5 - i))))
    for i in range(4):  # during
        samples.append(_sample(injected + timedelta(seconds=2 + 2 * i),
                               error_rate=100.0, failed=failed_pages, services_down=[degraded_service]))
    for i in range(3):  # after recovery
        samples.append(_sample(recovered + timedelta(seconds=2 * i)))
    (results_dir / "telemetry_log.jsonl").write_text(
        "\n".join(json.dumps(s) for s in samples) + "\n", encoding="utf-8")
    (results_dir / "telemetry.json").write_text(json.dumps(samples[-60:]), encoding="utf-8")

    # captured logs
    log_dir = results_dir / f"{experiment_id}.logs"
    log_dir.mkdir()
    (log_dir / "summary.json").write_text(json.dumps({
        f"{PROJECT}-{degraded_service}-1": {
            "container": f"{PROJECT}-{degraded_service}-1", "phases": ["injection", "rollback"],
            "total_lines": 20,
            "errors": [{"kind": "timeout", "first_line": "rpc error: context deadline exceeded", "count": 7}],
            "tail": ["still waiting", "deadline exceeded"], "note": "",
        }
    }), encoding="utf-8")

    (results_dir / "graph.json").write_text(json.dumps({"analysis": {
        degraded_service: {"score": 70.0, "rationale": "on checkout path",
                           "direct_dependents": ["frontend", "checkoutservice"],
                           "dependents": ["frontend", "checkoutservice"], "on_checkout_path": True},
    }}), encoding="utf-8")
    return experiment_id


def test_component_degradation_orders_by_first_time():
    now = datetime.now().astimezone()
    samples = [
        _sample(now, services_down=["cartservice"]),
        _sample(now + timedelta(seconds=2), services_down=["cartservice"], failed=["Cart"]),
    ]
    deg = component_degradation(samples)
    assert deg[0][1] == "cartservice"  # the service degraded before the page


def test_report_names_origin_propagation_and_timings(results_dir: Path):
    injected = datetime.now().astimezone() - timedelta(seconds=40)
    eid = _write_run(results_dir, injected=injected, degraded_service="cartservice",
                     failed_pages=["Cart", "Home", "Product"])
    report = generate_report(results_dir, eid, reports_dir=results_dir / "reports")

    assert report.failure_origin.component == "cartservice"
    assert report.failure_origin.is_injected_target is True
    assert report.failure_origin.evidence_ids  # cited
    assert any(s.role == "target" and s.component == "cartservice" for s in report.propagation)
    assert report.timings.detection_seconds == 4.0 and report.timings.recovery_seconds == 6.0
    assert report.impact.peak_error_rate_pct == 100.0
    assert report.impact.baseline_p95_ms is None or isinstance(report.impact.baseline_p95_ms, float)


def test_every_claim_cites_evidence(results_dir: Path):
    injected = datetime.now().astimezone() - timedelta(seconds=40)
    eid = _write_run(results_dir, injected=injected, degraded_service="cartservice",
                     failed_pages=["Cart"])
    report = generate_report(results_dir, eid, reports_dir=results_dir / "reports")
    valid_ids = {e.id for e in report.evidence}
    # failure origin, each propagation step, impact and timings reference only real evidence ids
    assert set(report.failure_origin.evidence_ids) <= valid_ids
    for step in report.propagation:
        assert step.evidence_ids and set(step.evidence_ids) <= valid_ids
    assert set(report.impact.evidence_ids) <= valid_ids
    assert set(report.timings.evidence_ids) <= valid_ids


def test_facts_and_inferences_are_labelled(results_dir: Path):
    injected = datetime.now().astimezone() - timedelta(seconds=40)
    eid = _write_run(results_dir, injected=injected, degraded_service="cartservice", failed_pages=["Cart"])
    report = generate_report(results_dir, eid, reports_dir=results_dir / "reports")
    for obs in report.what_worked + report.anomalies:
        assert obs.kind in ("observed", "inferred")


def test_log_error_is_in_the_evidence_pack(results_dir: Path):
    injected = datetime.now().astimezone() - timedelta(seconds=40)
    eid = _write_run(results_dir, injected=injected, degraded_service="cartservice", failed_pages=["Cart"])
    pack = build_pack(results_dir, eid)
    log_items = [i for i in pack.items if i.source == "logs"]
    assert any("timeout" in i.summary for i in log_items)


def test_markdown_and_json_and_campaign_are_written(results_dir: Path):
    injected = datetime.now().astimezone() - timedelta(seconds=40)
    reports = results_dir / "reports"
    eid = _write_run(results_dir, injected=injected, degraded_service="cartservice", failed_pages=["Cart"])
    generate_report(results_dir, eid, reports_dir=reports)
    assert (reports / f"{eid}.md").exists()
    assert (reports / f"{eid}.json").exists()
    campaign = (reports / "campaign.md").read_text(encoding="utf-8")
    assert eid in campaign and "Failure origin" in campaign
    # the markdown names the origin and cites evidence
    md = (reports / f"{eid}.md").read_text(encoding="utf-8")
    assert "cartservice" in md and "[E" in md


def test_a_tolerated_run_reports_no_degradation(results_dir: Path):
    """adservice kill that nothing noticed: origin is 'none', and that is stated, not guessed."""
    injected = datetime.now().astimezone() - timedelta(seconds=40)
    recovered = injected + timedelta(seconds=20)
    eid = "kill-adservice-ok"
    (results_dir / f"{eid}.json").write_text(json.dumps({
        "experiment": {"id": eid, "target": "adservice", "action": "kill", "duration_s": 20},
        "verdict": "completed", "container": f"{PROJECT}-adservice-1", "service": "adservice",
        "injected_at": injected.isoformat(), "rolled_back_at": (injected + timedelta(seconds=15)).isoformat(),
        "recovered_at": recovered.isoformat(), "ended_at": recovered.isoformat(),
        "self_healed": True,
        "during": {"failed_endpoints": [], "error_types": {}, "error_rate_pct_max": 0.0, "p95_ms_max": 40.0},
        "timeline": [],
    }), encoding="utf-8")
    samples = [_sample(injected + timedelta(seconds=2 * i)) for i in range(10)]
    (results_dir / "telemetry_log.jsonl").write_text(
        "\n".join(json.dumps(s) for s in samples) + "\n", encoding="utf-8")
    (results_dir / "telemetry.json").write_text(json.dumps(samples), encoding="utf-8")

    report = generate_report(results_dir, eid, reports_dir=results_dir / "reports")
    assert report.failure_origin.component is None
    assert any("tolerated" in o.statement for o in report.what_worked)
    assert isinstance(report, Report)

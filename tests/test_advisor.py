"""Agent 3: deduplicated, ranked, verifiable fixes - recommendations only, never applied, each
grounded in the reports and the Compose file and citing the report/evidence ids.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from advisor.recommender import build_recommendations, generate_recommendations
from agents.schemas import Recommendations

COMPOSE = """\
name: online-boutique
services:
  cartservice:
    image: cartservice:v0.10.7
    restart: unless-stopped
  redis-cart:
    image: redis:alpine
    restart: unless-stopped
  frontend:
    image: frontend:v0.10.7
    restart: unless-stopped
"""

GRAPH = {"analysis": {
    "cartservice": {"score": 70.0, "on_checkout_path": True, "single_point_of_failure": True,
                    "direct_dependents": ["frontend", "checkoutservice"]},
    "redis-cart": {"score": 85.0, "on_checkout_path": True, "single_point_of_failure": True,
                   "direct_dependents": ["cartservice"]},
    "adservice": {"score": 5.0, "on_checkout_path": False, "direct_dependents": []},
}}


def _report(reports_dir: Path, eid, service, action, *, failed, verdict="aborted",
            what_worked=None, unknowns=None) -> None:
    # Mirror the real reporter: a tolerated run (completed, nothing failed) has no failure origin.
    degraded = bool(failed) or verdict in ("aborted", "stopped")
    origin = {"component": service, "at": "2026-10-10T10:00:00+00:00",
              "is_injected_target": True, "evidence_ids": ["E3"], "note": ""} if degraded else {
              "component": None, "at": None, "is_injected_target": None, "evidence_ids": [], "note": "none"}
    data = {
        "experiment_id": eid, "source": "deterministic", "verdict": verdict,
        "action": action, "service": service, "target": f"online-boutique-{service}-1",
        "summary": f"{action} on {service}",
        "failure_origin": origin,
        "propagation": [], "impact": {"failed_endpoints": failed, "error_types": {"timeout": 3},
                                      "peak_error_rate_pct": 100.0, "peak_p95_ms": 2200.0,
                                      "baseline_p95_ms": 40.0, "evidence_ids": ["E5", "E6"]},
        "timings": {"detection_seconds": 4.0, "recovery_seconds": 6.0, "self_healed": False,
                    "evidence_ids": ["E1"]},
        "what_worked": what_worked or [], "anomalies": [], "unknowns": unknowns or [],
        "evidence": [{"id": f"E{i}", "source": "telemetry", "at": None, "summary": "x"} for i in range(1, 7)],
    }
    (reports_dir / f"{eid}.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def setup(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(COMPOSE, encoding="utf-8")
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(GRAPH), encoding="utf-8")
    return reports, compose, graph


def test_each_failure_gets_a_classified_fix_with_verification(setup):
    reports, compose, graph = setup
    _report(reports, "pause-cartservice-1", "cartservice", "pause", failed=["Cart", "Home"])
    recs = build_recommendations(reports, compose, graph)

    assert len(recs.recommendations) == 1
    rec = recs.recommendations[0]
    assert rec.root_cause in {"no_timeout_or_retry", "single_point_of_failure"}
    assert rec.verification.attack is not None  # a concrete attack to re-run
    assert rec.verification.attack.target == "cartservice"
    assert rec.metric and rec.effort in {"S", "M", "L"}
    assert rec.report_ids == ["pause-cartservice-1"]
    assert rec.evidence_refs  # cites the report + its evidence ids


def test_fixes_are_deduplicated_across_experiments(setup):
    """Two experiments with the same root cause on the same service -> one fix, both reports linked."""
    reports, compose, graph = setup
    _report(reports, "cpu_throttle-cartservice-1", "cartservice", "cpu_throttle", failed=["Cart"])
    _report(reports, "memory_limit-cartservice-1", "cartservice", "memory_limit", failed=["Cart"])
    recs = build_recommendations(reports, compose, graph)

    resource_recs = [r for r in recs.recommendations if r.root_cause == "no_resource_limits"]
    assert len(resource_recs) == 1
    assert set(resource_recs[0].report_ids) == {"cpu_throttle-cartservice-1", "memory_limit-cartservice-1"}


def test_priority_orders_by_impact_over_effort(setup):
    reports, compose, graph = setup
    _report(reports, "stop-cartservice-1", "cartservice", "stop",
            failed=["Home", "Cart", "Product", "Checkout"])
    _report(reports, "cpu_throttle-adservice-1", "adservice", "cpu_throttle", failed=[], verdict="completed")
    recs = build_recommendations(reports, compose, graph)
    assert recs.recommendations[0].priority == 1
    # the high-impact checkout-path failure outranks the low one
    assert recs.recommendations[0].verification.attack.target == "cartservice"


def test_a_tolerated_run_produces_praise_not_a_fix(setup):
    reports, compose, graph = setup
    _report(reports, "kill-adservice-1", "adservice", "kill", failed=[], verdict="completed",
            what_worked=[{"statement": "Home/Product stayed up without ads", "kind": "observed",
                          "evidence_ids": ["E2"]}])
    recs = build_recommendations(reports, compose, graph)
    assert recs.recommendations == []  # nothing to fix
    assert any("stayed up" in p.statement for p in recs.praise)


def test_thin_evidence_asks_for_an_experiment_not_a_guess(setup):
    reports, compose, graph = setup
    # aborted but with no failed endpoints and an unknown -> thin
    _report(reports, "network_delay-redis-cart-1", "redis-cart", "network_delay", failed=[],
            verdict="aborted", unknowns=["no container logs were captured"])
    recs = build_recommendations(reports, compose, graph)
    rec = recs.recommendations[0]
    assert rec.needs_more_evidence is True
    assert "re-run" in rec.fix.lower()
    assert rec.confidence <= 0.4


def test_upstream_only_fix_is_labelled(setup):
    reports, compose, graph = setup
    _report(reports, "network_delay-cartservice-1", "cartservice", "network_delay", failed=["Cart"])
    recs = build_recommendations(reports, compose, graph)
    rec = next(r for r in recs.recommendations if r.root_cause == "no_timeout_or_retry")
    assert rec.scope == "upstream_code"


def test_generate_writes_both_files(setup):
    reports, compose, graph = setup
    _report(reports, "pause-cartservice-1", "cartservice", "pause", failed=["Cart"])
    generate_recommendations(reports, compose, graph)
    assert (reports / "recommendations.json").exists()
    md = (reports / "recommendations.md").read_text(encoding="utf-8")
    assert "Fix recommendations" in md and "nothing here has been applied" in md
    reloaded = Recommendations.model_validate_json(
        (reports / "recommendations.json").read_text(encoding="utf-8"))
    assert reloaded.source == "deterministic"


def test_no_reports_is_handled(setup):
    reports, compose, graph = setup
    recs = build_recommendations(reports, compose, graph)
    assert recs.recommendations == []
    assert any("no reports" in n for n in recs.notes)

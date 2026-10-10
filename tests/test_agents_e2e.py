"""End-to-end, fully offline (no LLM, no Docker): rank -> approve+run -> report -> recommend.

This is the Phase 1 acceptance in one test: with LLM_API_KEY unset, all three agents run end to end
and produce a deterministic ranking, a facts-only report and facts-only recommendations.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from advisor.recommender import generate_recommendations
from agents.llm import get_client
from orchestrator.runner import ExperimentRunner
from planner.ranker import Ranker, write_ranking
from reporter.report import generate_report
from tests.conftest import PROJECT, make_sample

GRAPH = {"analysis": {
    "redis-cart": {"score": 85.0, "rationale": "stateful SPOF on checkout path",
                   "direct_dependents": ["cartservice"], "dependents": ["cartservice", "frontend"],
                   "on_checkout_path": True, "single_point_of_failure": True, "stateful": True},
    "adservice": {"score": 5.0, "rationale": "nothing depends on it", "direct_dependents": [],
                  "dependents": []},
}}


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += max(s, 0.01)


def _fresh_telemetry(results_dir: Path, **kw) -> None:
    now = datetime.now().astimezone()
    samples = [make_sample(at=now - timedelta(seconds=2 * (4 - i)), **kw) for i in range(5)]
    (results_dir / "telemetry.json").write_text(json.dumps(samples), encoding="utf-8")
    (results_dir / "telemetry_log.jsonl").write_text(
        "\n".join(json.dumps(s) for s in samples) + "\n", encoding="utf-8")


def test_full_pipeline_offline(docker, config, results_dir: Path):
    reports_dir = results_dir / "reports"
    (results_dir / "graph.json").write_text(json.dumps(GRAPH), encoding="utf-8")
    _fresh_telemetry(results_dir)

    # With no key, the whole pipeline is deterministic.
    assert get_client(results_dir) is None

    # 1. Agent 1 ranks every attack.
    runner = ExperimentRunner(docker, config, poll_s=2.0, clock=Clock().time, sleep=lambda _s: None)
    ranking = Ranker(runner, config, client=None).rank()
    write_ranking(ranking, results_dir)
    assert ranking.source == "deterministic" and ranking.attacks
    top = ranking.attacks[0]
    assert top.predicted_harm == max(a.predicted_harm for a in ranking.attacks)

    # 2+3. Approve a specific attack and run it with a fake clock (a tolerated adservice kill).
    docker.logs_by_container[f"{PROJECT}-adservice-1"] = "serving\nconnection refused\nrecovered"
    clock = Clock()
    run_dir = ExperimentRunner(docker, config, poll_s=2.0, clock=clock.time, sleep=clock.sleep)
    from schemas import Experiment
    result = run_dir.run(
        Experiment(id="e2e-kill-adservice", target="adservice", action="kill", duration_s=10))
    assert result.verdict in ("completed", "aborted")
    assert result.timeline  # the runner recorded a timeline

    # 4+5. Agent 2 documents it (facts only, offline).
    report = generate_report(results_dir, "e2e-kill-adservice", reports_dir=reports_dir, client=None)
    assert report.source == "deterministic"
    assert (reports_dir / "e2e-kill-adservice.md").exists()
    assert (reports_dir / "campaign.md").exists()

    # 6. Agent 3 recommends (facts only, offline), reading the report + compose + graph.
    from advisor.recommender import DEFAULT_COMPOSE
    recs = generate_recommendations(
        reports_dir, DEFAULT_COMPOSE, results_dir / "graph.json", client=None)
    assert recs.source == "deterministic"
    assert (reports_dir / "recommendations.md").exists()

    # Audit trail: no agent_log.jsonl should have been written, because no LLM call was made.
    assert not (results_dir / "agent_log.jsonl").exists()

"""Agent 1: the ranking is complete, sorted by harm, state-dependent, and nothing runs without a
typed confirmation or past a plan the validator refuses.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from orchestrator.runner import ExperimentRunner
from planner.catalogue import build_catalogue
from planner.ranker import Ranker, write_ranking

GRAPH = {
    "project": "online-boutique",
    "analysis": {
        "redis-cart": {"score": 85.0, "rationale": "3 depend on it; checkout path; holds state",
                       "dependents": ["cartservice", "frontend", "checkoutservice"],
                       "on_checkout_path": True, "single_point_of_failure": True, "stateful": True},
        "checkoutservice": {"score": 60.0, "rationale": "checkout path",
                            "dependents": ["frontend"], "on_checkout_path": True},
        "frontend": {"score": 20.0, "rationale": "user-facing entry point",
                     "dependents": [], "user_facing": True},
        "adservice": {"score": 5.0, "rationale": "no other service depends on it", "dependents": []},
    },
}


@pytest.fixture
def graph_file(results_dir: Path) -> None:
    (results_dir / "graph.json").write_text(json.dumps(GRAPH), encoding="utf-8")


def make_ranker(docker, config) -> Ranker:
    return Ranker(ExperimentRunner(docker, config), config, client=None)


def test_catalogue_covers_every_service_and_fault(config):
    specs = build_catalogue(config)
    services = {s.target for s in specs}
    assert "redis-cart" in services and "adservice" in services
    assert "loadgenerator" not in services  # never allow-listed
    # 8 container faults per service, plus one frontend spike.
    assert sum(1 for s in specs if s.action == "spike") == 1
    assert all(s.action in {
        "kill", "stop", "pause", "network_partition", "network_delay",
        "cpu_throttle", "memory_limit", "flapping", "spike",
    } for s in specs)


def test_ranking_is_complete_and_sorted_by_harm(docker, config, write_telemetry, graph_file):
    write_telemetry(count=5)  # healthy, fresh
    ranking = make_ranker(docker, config).rank()

    assert len(ranking.attacks) == len(build_catalogue(config))
    harms = [a.predicted_harm for a in ranking.attacks]
    assert harms == sorted(harms, reverse=True)  # strictly highest-first
    assert [a.rank for a in ranking.attacks] == list(range(1, len(ranking.attacks) + 1))
    assert ranking.source == "deterministic" and ranking.app_state == "healthy"


def test_critical_stateful_service_outranks_an_invisible_one(docker, config, write_telemetry, graph_file):
    write_telemetry(count=5)
    ranking = make_ranker(docker, config).rank()
    rank_of = {}
    for a in ranking.attacks:
        rank_of.setdefault(a.experiment.target, a.rank)
    assert rank_of["redis-cart"] < rank_of["adservice"]  # redis is far more harmful


def test_healthy_app_allows_preflight(docker, config, write_telemetry, graph_file):
    write_telemetry(count=5)
    ranking = make_ranker(docker, config).rank()
    assert all(a.preflight_verdict == "allow" for a in ranking.attacks)


def test_a_degraded_app_marks_every_attack_refused(docker, config, write_telemetry, graph_file):
    """State-dependent: when the app is already degraded, pre-flight would refuse everything."""
    write_telemetry(count=5, error_rate_pct=50.0, failed_endpoints=["Home", "Cart"])
    ranking = make_ranker(docker, config).rank()
    assert ranking.app_state == "degraded"
    assert all(a.preflight_verdict == "refuse" for a in ranking.attacks)
    assert any("degraded" in n for n in ranking.notes)


def test_a_prior_run_shows_up_in_the_rationale(docker, config, write_telemetry, graph_file, results_dir):
    write_telemetry(count=5)
    (results_dir / "kill-adservice-old.json").write_text(json.dumps({
        "experiment": {"id": "kill-adservice-old", "target": "adservice", "action": "kill"},
        "verdict": "completed",
    }), encoding="utf-8")
    ranking = make_ranker(docker, config).rank()
    kill_ad = next(a for a in ranking.attacks
                   if a.experiment.target == "adservice" and a.experiment.action == "kill")
    assert kill_ad.prior_run_id == "kill-adservice-old"
    assert "already tested" in kill_ad.rationale


def test_every_entry_embeds_a_valid_experiment_with_evidence(docker, config, write_telemetry, graph_file):
    write_telemetry(count=5)
    ranking = make_ranker(docker, config).rank()
    for a in ranking.attacks:
        assert a.experiment.target and a.experiment.action  # a runnable Experiment
        assert a.evidence_refs  # evidence or silence


def test_missing_graph_is_noted_not_fatal(docker, config, write_telemetry):
    write_telemetry(count=5)  # no graph.json written
    ranking = make_ranker(docker, config).rank()
    assert ranking.attacks  # still ranks
    assert any("graph.json" in n for n in ranking.notes)


def test_write_ranking_roundtrips(docker, config, write_telemetry, graph_file, results_dir):
    write_telemetry(count=5)
    ranking = make_ranker(docker, config).rank()
    path = write_ranking(ranking, results_dir)
    assert path.exists()
    from agents.schemas import Ranking
    reloaded = Ranking.model_validate_json(path.read_text(encoding="utf-8"))
    assert len(reloaded.attacks) == len(ranking.attacks)


# -------------------------------------------------- approve: human-in-the-loop gating
def _approve_args(results_dir: Path, rank: int, **kw) -> Namespace:
    base = {"target_file": None, "results_dir": results_dir, "rank": rank,
            "dry_run": False, "confirm": None, "poll": 2.0, "yes": False}
    base.update(kw)
    return Namespace(**base)


@pytest.fixture
def ranked(docker, config, write_telemetry, graph_file, results_dir, monkeypatch):
    """A written ranking.json plus DockerCli patched to the fake, for the approve-path tests."""
    write_telemetry(count=5)
    import planner.__main__ as cli
    monkeypatch.setattr(cli, "DockerCli", lambda *_a, **_k: docker)
    write_ranking(make_ranker(docker, config).rank(), results_dir)
    return cli


def test_approve_dry_run_changes_nothing(ranked, docker, results_dir):
    code = ranked.cmd_approve(_approve_args(results_dir, 1, dry_run=True))
    assert code == 0
    assert docker.calls == []  # dry run touched no container


def test_approve_without_the_typed_confirmation_runs_nothing(ranked, docker, results_dir):
    code = ranked.cmd_approve(_approve_args(results_dir, 1, confirm="not-the-id"))
    assert code == 1
    # no injection happened: no kill/stop/pause/update/network call on any container
    assert not any(m in {"kill", "stop", "pause", "update", "network_disconnect"}
                   for m, _ in docker.calls)


def test_approve_cannot_force_through_a_refused_plan(ranked, docker, config, write_telemetry, results_dir):
    write_telemetry(count=5, error_rate_pct=50.0, failed_endpoints=["Home"])  # now degraded
    # confirm is correct, but the validator must still refuse at approve time
    entry = __import__("agents.schemas", fromlist=["Ranking"]).Ranking.model_validate_json(
        (results_dir / "ranking.json").read_text(encoding="utf-8")).attacks[0]
    code = ranked.cmd_approve(_approve_args(results_dir, 1, confirm=entry.experiment.id))
    assert code == 2  # EXIT_REJECTED
    assert not any(m in {"kill", "stop", "pause"} for m, _ in docker.calls)


def test_approve_yes_runs_the_attack_without_a_prompt(ranked, docker, results_dir, monkeypatch):
    # --yes must auto-confirm and never fall back to input(): make input() fail if it is called.
    def _boom(*_a, **_k):
        raise AssertionError("approve --yes must not prompt")
    monkeypatch.setattr("builtins.input", _boom)
    code = ranked.cmd_approve(_approve_args(results_dir, 1, yes=True))
    assert code in (0, 3)  # a real run: completed, or aborted by the watchdog
    assert docker.calls  # an injection actually happened
    eid = json.loads((results_dir / "ranking.json").read_text(encoding="utf-8"))["attacks"][0]["experiment"]["id"]
    assert (results_dir / f"{eid}.json").exists()



def _menu_args(results_dir: Path):
    return Namespace(target_file=None, results_dir=results_dir, poll=2.0)


def test_menu_runs_the_attack_for_the_typed_serial(ranked, docker, results_dir, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "1")
    code = ranked.cmd_menu(_menu_args(results_dir))
    assert code in (0, 3)  # a real run
    assert docker.calls  # the picked attack was injected
    eid = json.loads((results_dir / "ranking.json").read_text(encoding="utf-8"))["attacks"][0]["experiment"]["id"]
    assert (results_dir / f"{eid}.json").exists()


def test_menu_blank_choice_runs_nothing(ranked, docker, results_dir, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
    code = ranked.cmd_menu(_menu_args(results_dir))
    assert code == 0
    assert not any(m in {"kill", "stop", "pause"} for m, _ in docker.calls)


def test_menu_non_numeric_choice_runs_nothing(ranked, docker, results_dir, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "redis")
    code = ranked.cmd_menu(_menu_args(results_dir))
    assert code == 1
    assert not any(m in {"kill", "stop", "pause"} for m, _ in docker.calls)

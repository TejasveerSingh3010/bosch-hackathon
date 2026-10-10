"""Agent 1, the Planner: the catalogue, the structured plan, the independent policy check, human
approval, the constrained tool interface, the audit trail, and the planner's own selection.

The safety-critical property under test: an autonomous planner cannot reach Docker except through a
plan that passed the policy check and was approved. Every invalid request from Phase 3 - unknown
experiment, out-of-range parameters, an unhealthy cluster, an unapproved/unknown target - is rejected
by independent validation, and nothing is injected without approval.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.runner import ExperimentRunner
from planner.agent import AttackPlanner, blast_radius, rank, select
from planner.approval import AutoApprover, CLIApprover, DenyAll
from planner.audit import Auditor
from planner.catalogue import ATTACK_NAMES, CATALOGUE, get_attack
from planner.interface import ToolInterface
from planner.plan import AttackPlan
from planner.policy import check_plan
from safety.limits import TargetConfig
from tests.conftest import PROJECT
from tests.fake_docker import FakeDocker


# --------------------------------------------------------------------------- helpers
class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


def auditor(results_dir: Path, session: str = "test") -> Auditor:
    return Auditor(results_dir / "plans", session)


def make_tools(config, docker, results_dir, write_telemetry, approver, **kwargs) -> ToolInterface:
    """A tool interface whose runner uses a fake clock and keeps telemetry fresh and healthy."""
    write_telemetry(count=5)  # fresh, healthy pre-flight telemetry
    clock = FakeClock()

    def make_runner(d, c):
        def sleep(seconds: float) -> None:
            clock.now += max(seconds, 0.01)
            write_telemetry(count=5)  # stay fresh and healthy so a tolerated fault completes

        return ExperimentRunner(d, c, poll_s=2.0, clock=clock.time, sleep=sleep)

    return ToolInterface(config, docker, auditor(results_dir), approver=approver,
                         make_runner=make_runner, **kwargs)


def a_plan(action="pause", target="adservice", params=None, duration_s=20) -> AttackPlan:
    return AttackPlan.build(action, target, "online-boutique", params, duration_s)


# --------------------------------------------------------------------------- catalogue
def test_the_catalogue_has_eight_attacks_and_no_spike():
    assert len(CATALOGUE) == 8
    assert set(ATTACK_NAMES) == {"kill", "stop", "pause", "throttle", "memory_limit",
                                 "network_partition", "network_delay", "flapping"}
    assert "spike" not in CATALOGUE


def test_catalogue_param_validation_matches_the_injector():
    assert get_attack("throttle").normalise({"cpus": 0.1}) == {"cpus": 0.1}
    assert get_attack("memory_limit").normalise({"memory_mb": 128}) == {"memory_mb": 128}
    assert get_attack("kill").normalise({}) == {}
    with pytest.raises(ValueError, match="memory_mb"):
        get_attack("memory_limit").normalise({"memory_mb": 1})  # below the floor
    with pytest.raises(ValueError, match="no parameters"):
        get_attack("kill").normalise({"cpus": 0.1})
    with pytest.raises(ValueError, match="non-zero"):
        get_attack("network_delay").normalise({"delay_ms": 0, "loss_pct": 0})


# --------------------------------------------------------------------------- plan
def test_a_plan_carries_every_required_field_and_becomes_an_experiment():
    plan = a_plan("memory_limit", "currencyservice", {"memory_mb": 128})
    assert plan.action == "memory_limit" and plan.target == "currencyservice"
    assert plan.hypothesis and plan.expected_observations and plan.recovery_criteria
    assert plan.max_duration_s == 20
    exp = plan.to_experiment()
    assert exp.action == "memory_limit" and exp.params == {"memory_mb": 128}
    assert exp.target == "currencyservice"


def test_building_a_plan_rejects_unknown_attacks_and_bad_params():
    with pytest.raises(KeyError):
        AttackPlan.build("meltdown", "adservice", "online-boutique")
    with pytest.raises(ValueError, match="cpus"):
        AttackPlan.build("throttle", "adservice", "online-boutique", {"cpus": 99})


# --------------------------------------------------------------------------- policy (independent validation)
def _containers(docker: FakeDocker, config: TargetConfig):
    return docker.list_containers(config.compose_project)


def test_policy_accepts_a_valid_plan(config, docker, telemetry, write_telemetry):
    write_telemetry(count=5)
    decision = check_plan(a_plan(), config, _containers(docker, config), telemetry)
    assert decision.allowed and decision.validated is not None


def test_policy_rejects_an_unapproved_experiment_type(config, docker):
    plan = a_plan().model_copy(update={"action": "spike"})
    decision = check_plan(plan, config, _containers(docker, config), skip_health_check=True)
    assert not decision.allowed and "unapproved experiment type" in decision.summary


def test_policy_rejects_out_of_range_parameters(config, docker):
    # A plan built around the schema guard still cannot smuggle bad params past the policy check.
    plan = a_plan("throttle", "adservice", {"cpus": 0.1}).model_copy(update={"params": {"cpus": 99}})
    decision = check_plan(plan, config, _containers(docker, config), skip_health_check=True)
    assert not decision.allowed and "unsafe parameters" in decision.summary


def test_policy_rejects_an_unknown_target(config, docker):
    decision = check_plan(a_plan(target="nope-service"), config, _containers(docker, config),
                          skip_health_check=True)
    assert not decision.allowed and "not found" in decision.summary.lower()


def test_policy_rejects_an_excessive_duration(config, docker):
    decision = check_plan(a_plan(duration_s=600), config, _containers(docker, config),
                          skip_health_check=True)
    assert not decision.allowed and "exceeds the cap" in decision.summary


def test_policy_rejects_a_protected_target(config, docker):
    decision = check_plan(a_plan(target="loadgenerator"), config, _containers(docker, config),
                          skip_health_check=True)
    assert not decision.allowed
    assert "protected" in decision.summary or "not in target.yaml" in decision.summary


def test_policy_rejects_an_unhealthy_cluster(config, docker, telemetry, write_telemetry):
    write_telemetry(count=5, error_rate_pct=80.0, failed_endpoints=["Home"])
    decision = check_plan(a_plan(), config, _containers(docker, config), telemetry)
    assert not decision.allowed and "degraded" in decision.summary


def test_policy_rejects_stale_telemetry(config, docker, telemetry):
    decision = check_plan(a_plan(), config, _containers(docker, config), telemetry)  # none written
    assert not decision.allowed


# --------------------------------------------------------------------------- approval
def test_approvers():
    plan, decision = a_plan(), check_plan(a_plan(), None, [], skip_health_check=True)  # decision unused
    assert AutoApprover().decide(plan, decision).approved
    assert not DenyAll().decide(plan, decision).approved
    assert CLIApprover(pre_approved={plan.experiment_id}).decide(plan, decision).approved
    # no terminal and not pre-approved -> denied, never blocks
    assert not CLIApprover(assume_no=True).decide(plan, decision).approved


# --------------------------------------------------------------------------- read-only tools
def test_list_and_details_tools(config, docker, results_dir):
    tools = ToolInterface(config, docker, auditor(results_dir), approver=DenyAll())
    listing = tools.list_experiments()
    assert len(listing["attacks"]) == 8
    assert "adservice" in listing["targets"] and "loadgenerator" not in listing["targets"]
    details = tools.get_experiment_details("memory_limit")
    assert details["action"] == "memory_limit" and "memory_mb" in details["params"]
    assert tools.get_experiment_details("meltdown")["error"]


def test_cluster_health_and_recovery_tools(config, docker, results_dir, write_telemetry):
    tools = ToolInterface(config, docker, auditor(results_dir), approver=DenyAll())
    assert tools.get_cluster_health()["available"] is False  # nothing written yet
    write_telemetry(count=5)
    health = tools.get_cluster_health()
    assert health["available"] and health["fresh"] and health["healthy"]
    recovery = tools.check_recovery()
    assert recovery["clean"] and recovery["pending_experiment"] is None


def test_get_experiment_results_reads_the_record(config, docker, results_dir, write_telemetry):
    tools = make_tools(config, docker, results_dir, write_telemetry, AutoApprover())
    assert tools.get_experiment_results("absent")["found"] is False
    tools.inject_pause(target="adservice", duration_s=10)
    summary = tools.get_experiment_results(_only_plan_id(results_dir))
    assert summary["found"] and summary["verdict"] in ("completed", "aborted")


# --------------------------------------------------------------------------- injection tools
def test_denied_injection_never_touches_docker(config, docker, results_dir, write_telemetry):
    write_telemetry(count=5)
    tools = ToolInterface(config, docker, auditor(results_dir), approver=DenyAll())
    out = tools.inject_kill(target="adservice", duration_s=10)
    assert out["status"] == "not_approved"
    assert not docker.calls  # the kill never happened


def test_rejected_injection_is_never_offered_for_approval(config, docker, results_dir):
    seen = []

    class Spy(DenyAll):
        def decide(self, plan, decision):  # noqa: ARG002
            seen.append(plan.experiment_id)
            return super().decide(plan, decision)

    tools = ToolInterface(config, docker, auditor(results_dir), approver=Spy(), skip_health_check=True)
    # Out-of-range params are caught at plan-build time (status "error"), before the policy stage.
    bad_params = tools.inject_cpu_throttle(target="adservice", cpus=99)
    assert bad_params["status"] == "error"
    # A disallowed target builds a plan but the policy check rejects it (status "rejected").
    bad_target = tools.inject_kill(target="loadgenerator")
    assert bad_target["status"] == "rejected"
    # Neither was ever offered for approval, and Docker was never touched.
    assert not seen and not docker.calls


def test_approved_injection_runs_and_rolls_back(config, docker, results_dir, write_telemetry):
    tools = make_tools(config, docker, results_dir, write_telemetry, AutoApprover())
    out = tools.inject_pause(target="adservice", duration_s=10)
    assert out["status"] == "ran" and out["verdict"] in ("completed", "aborted")
    assert not docker.containers[f"{PROJECT}-adservice-1"].paused  # rolled back
    assert ("pause", f"{PROJECT}-adservice-1") in docker.calls


def test_injection_records_a_full_audit_trail(config, docker, results_dir, write_telemetry):
    tools = make_tools(config, docker, results_dir, write_telemetry, AutoApprover())
    tools.inject_pause(target="adservice", duration_s=10)
    events = _audit_events(results_dir)
    assert events.count("tool_request") >= 1
    assert "plan" in events and "policy_decision" in events and "approval" in events
    assert "result" in events


# --------------------------------------------------------------------------- the planner's selection
def test_ranking_prefers_services_with_a_larger_blast_radius():
    blast = {"currencyservice": 4, "cartservice": 4, "adservice": 0}
    picks = rank(["adservice", "currencyservice", "cartservice"], blast, 2)
    assert {c.target for c in picks} == {"currencyservice", "cartservice"}
    assert all(c.action in CATALOGUE for c in picks)


def test_blast_radius_reads_services_yaml():
    blast = blast_radius("docs/services.yaml")
    assert blast.get("currencyservice", 0) >= 1 and blast.get("cartservice", 0) >= 1


def test_the_planner_runs_selected_experiments_and_concludes(config, docker, results_dir, write_telemetry):
    tools = make_tools(config, docker, results_dir, write_telemetry, AutoApprover())
    report = AttackPlanner(tools).run(goal="probe resilience", limit=2)
    assert "experiment(s)" in report["conclusion"]
    assert len(report["outcomes"]) == 2
    assert report["narrator"] == "stub"  # no LLM key in tests
    assert "conclusion" in _audit_events(results_dir)


def test_dry_run_selects_and_checks_but_never_injects(config, docker, results_dir, write_telemetry):
    write_telemetry(count=5)
    tools = ToolInterface(config, docker, auditor(results_dir), approver=DenyAll())
    report = AttackPlanner(tools).run(goal="plan only", limit=3)
    assert all(o["status"] == "not_approved" for o in report["outcomes"])
    assert not docker.calls


def test_select_falls_back_to_the_deterministic_ranker_offline(config, docker, results_dir, write_telemetry):
    write_telemetry(count=5)
    tools = ToolInterface(config, docker, auditor(results_dir), approver=DenyAll())
    selection, narrator = select(tools, "goal", 3, services_file="docs/services.yaml")
    assert narrator == "stub" and 0 < len(selection.selections) <= 3


def test_planner_refuses_when_a_previous_experiment_is_open(config, docker, results_dir, write_telemetry):
    write_telemetry(count=5)
    from safety.journal import Journal, JournalEntry

    Journal(results_dir).write(JournalEntry(
        experiment_id="stuck", action="pause", container=f"{PROJECT}-adservice-1",
        service="adservice", project=PROJECT, params={}, pre_state=None, injected_at=0.0, pid=1,
    ))
    tools = ToolInterface(config, docker, auditor(results_dir), approver=AutoApprover())
    report = AttackPlanner(tools).run(goal="g", limit=2)
    assert "previous experiment is still open" in report["conclusion"]
    assert not docker.calls


# --------------------------------------------------------------------------- audit helpers
def _audit_events(results_dir: Path) -> list[str]:
    lines = (results_dir / "plans" / "test" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["event"] for line in lines]


def _only_plan_id(results_dir: Path) -> str:
    plans = list((results_dir / "plans" / "test").glob("*.plan.json"))
    return plans[0].stem.replace(".plan", "")

"""Remediation: policy checks, review gate, the before/after verdict, and the verifier's control flow."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from remediation.agent import (
    GroqProposer,
    ProposalError,
    build_context,
    experiment_records,
    fault_timeline,
    resource_caps,
    strict_schema,
)
from remediation.compose import config_diff, load_services
from remediation.consistency import CartWrite
from remediation.models import (
    ConfigChange,
    Evidence,
    Proposal,
    ProposalDraft,
    RunRecord,
    SuccessCriterion,
    ValidationExperiment,
    Verification,
)
from remediation.policy import check_env, check_proposal, service_docs
from remediation.store import Store
from remediation.verify import Verifier, decide
from schemas import Experiment, ExperimentResult, Phase

SERVICES = load_services()


def make_result(exp_id: str, err_max: float = 50.0, recovery: float | None = 10.0) -> ExperimentResult:
    now = datetime.now().astimezone()
    return ExperimentResult(
        experiment=Experiment(id=exp_id, target="cartservice", action="kill", duration_s=20),
        verdict="completed",
        container="online-boutique-cartservice-1",
        service="cartservice",
        injected_at=now - timedelta(seconds=40),
        rolled_back_at=now - timedelta(seconds=20),
        recovered_at=now - timedelta(seconds=20 - (recovery or 0)),
        recovery_seconds=recovery,
        during=Phase(
            samples=10,
            error_rate_pct_mean=err_max / 2,
            error_rate_pct_max=err_max,
            p95_ms_max=300.0,
            failed_endpoints=["Cart"] if err_max else [],
        ),
    )


def write_record(results_dir: Path, result: ExperimentResult) -> None:
    (results_dir / f"{result.experiment.id}.json").write_text(result.model_dump_json(), encoding="utf-8")


def make_draft(**overrides) -> ProposalDraft:
    fields = {
        "remediable": True,
        "diagnosed_problem": "cartservice takes 10s to come back after a crash",
        "evidence": [
            Evidence(
                result_id="kill-cart-1",
                field="recovery_seconds",
                observed="10.0",
                interpretation="slow recovery",
            )
        ],
        "changes": [ConfigChange(service="cartservice", setting="mem_limit", value="512m")],
        "expected_benefit": "faster start",
        "success_criteria": [SuccessCriterion(metric="recovery_seconds", min_improvement_pct=30)],
        "side_effects": ["reserves more memory"],
        "rollback_procedure": "remove the override and recreate cartservice",
        "validation_experiment": ValidationExperiment(
            target="cartservice", action="kill", duration_s=20, repeats=3
        ),
    }
    fields.update(overrides)
    return ProposalDraft(**fields)


def make_proposal(draft: ProposalDraft | None = None, pid: str = "rem-test") -> Proposal:
    draft = draft or make_draft()
    return Proposal(**draft.model_dump(), id=pid, author="human", created_at=datetime.now().astimezone())


def run(arm: str, i: int, recovery: float, steady: float = 40.0, lost: float = 0.0) -> RunRecord:
    return RunRecord(
        arm=arm,
        experiment_id=f"x-{arm}-{i}",
        verdict="completed",
        valid=True,
        metrics={
            "recovery_seconds": recovery,
            "writes_lost": lost,
            "during_error_rate_mean": 10.0,
            "during_error_rate_max": 20.0,
            "during_p95_max": 300.0,
            "failed_endpoint_count": 1.0,
        },
        steady_p95_ms=steady,
        steady_error_rate_pct=0.0,
    )


def verdict_for(runs: list[RunRecord], min_pct: float = 30) -> Verification:
    v = Verification(
        proposal_id="p",
        verdict="inconclusive",
        repeats=3,
        started_at=datetime.now().astimezone(),
        experiment=Experiment(id="p-x", target="cartservice", action="kill", duration_s=20),
        runs=runs,
    )
    return decide(v, [SuccessCriterion(metric="recovery_seconds", min_improvement_pct=min_pct)])


# ---------------------------------------------------------------- policy
def test_a_well_formed_proposal_passes_policy(results_dir, config):
    write_record(results_dir, make_result("kill-cart-1"))
    assert check_proposal(make_draft(), SERVICES, config) == []


@pytest.mark.parametrize(
    "change, expected",
    [
        (ConfigChange(service="redis-cart", setting="restart", value="always"), "protected"),
        (ConfigChange(service="loadgenerator", setting="cpus", value="1"), "protected"),
        (ConfigChange(service="cartservice", setting="mem_limit", value="lots"), "not a size"),
        (ConfigChange(service="cartservice", setting="mem_limit", value="16g"), "between 64m and 4g"),
        (ConfigChange(service="cartservice", setting="restart", value="sometimes"), "restart policy"),
        (
            ConfigChange(service="frontend", setting="environment", value="CART_SERVICE_ADDR=evil:1"),
            "not a documented tunable",
        ),
        (ConfigChange(service="nosuch", setting="cpus", value="1"), "no such Compose service"),
    ],
)
def test_policy_rejects_out_of_bounds_changes(results_dir, config, change, expected):
    write_record(results_dir, make_result("kill-cart-1"))
    problems = check_proposal(make_draft(changes=[change]), SERVICES, config)
    assert any(expected in p for p in problems), problems


def test_policy_catches_evidence_that_does_not_match_the_record(results_dir, config):
    write_record(results_dir, make_result("kill-cart-1", recovery=10.0))
    misquoted = make_draft(
        evidence=[
            Evidence(
                result_id="kill-cart-1", field="recovery_seconds", observed="45", interpretation="made up"
            )
        ]
    )
    invented = make_draft(
        evidence=[
            Evidence(
                result_id="kill-ghost", field="verdict", observed="completed", interpretation="no such run"
            )
        ]
    )
    assert any("not '45'" in p for p in check_proposal(misquoted, SERVICES, config))
    assert any("no experiment record" in p for p in check_proposal(invented, SERVICES, config))


def test_a_declined_proposal_is_recorded_but_cannot_be_verified(config):
    problems = check_proposal(
        make_draft(remediable=False, declined_reason="code bug", changes=[]), SERVICES, config
    )
    assert problems == ["agent declined: code bug"]


def test_the_review_diff_shows_the_exact_change():
    diff = config_diff(SERVICES, [ConfigChange(service="cartservice", setting="mem_limit", value="512m")])
    assert "+  mem_limit: 512m" in diff and "original/cartservice" in diff


def env_draft(service: str, value: str) -> ProposalDraft:
    return make_draft(changes=[ConfigChange(service=service, setting="environment", value=value)])


def test_env_changes_must_be_documented_tunables(results_dir, config):
    write_record(results_dir, make_result("kill-cart-1"))
    # Today's real proposal: cartservice does not read DISABLE_PROFILER at all.
    problems = check_proposal(env_draft("cartservice", "DISABLE_PROFILER=1"), SERVICES, config)
    assert any("not tunable on cartservice" in p for p in problems), problems
    assert check_proposal(env_draft("frontend", "ENABLE_PROFILER=0"), SERVICES, config) == []
    problems = check_proposal(env_draft("productcatalogservice", "EXTRA_LATENCY=5s"), SERVICES, config)
    assert any("a fault, never a fix" in p for p in problems)


@pytest.mark.parametrize(
    "value, ok",
    [
        ("redis-cart:6379,asyncTimeout=1000,abortConnect=false", True),
        ("redis-cart:6379,connectTimeout=500", True),
        ("evil:6379,asyncTimeout=1000", False),  # the address itself may not change
        ("redis-cart:6379", False),  # nothing appended: not a change
        ("redis-cart:6379,password=x", False),  # only documented client options
    ],
)
def test_redis_client_options_are_tunable_but_the_address_is_not(value, ok):
    assert (check_env("cartservice", "REDIS_ADDR", value, service_docs()) is None) is ok


def test_every_service_doc_matches_a_compose_service():
    assert set(service_docs()) == set(SERVICES)


# ---------------------------------------------------------------- review gate
def test_editing_the_override_after_approval_voids_it(results_dir):
    store = Store(results_dir)
    store.save_proposal(make_proposal(), SERVICES)
    assert "not reviewed" in store.approval_problem("rem-test")
    store.record_decision("rem-test", "approved", "alice")
    assert store.approval_problem("rem-test") is None
    override = store.dir("rem-test") / "override.yml"
    override.write_text(override.read_text() + "    cpus: '4'\n")
    assert "changed after it was approved" in store.approval_problem("rem-test")


def test_a_rejected_proposal_stays_rejected(results_dir):
    store = Store(results_dir)
    store.save_proposal(make_proposal(), SERVICES)
    store.record_decision("rem-test", "rejected", "bob", "too risky")
    assert "rejected by bob" in store.approval_problem("rem-test")


# ---------------------------------------------------------------- verdict
def test_clean_improvement_on_every_run_is_effective():
    v = verdict_for(
        [run("before", i, r) for i, r in enumerate((10, 12, 11))]
        + [run("after", i, r) for i, r in enumerate((4, 5, 4))]
    )
    assert v.verdict == "effective", v.reasons
    assert v.criteria[0].status == "met"


def test_too_small_an_improvement_is_ineffective():
    v = verdict_for(
        [run("before", i, r) for i, r in enumerate((10, 10, 10))]
        + [run("after", i, r) for i, r in enumerate((9, 9, 9))]
    )
    assert v.verdict == "ineffective"


def test_overlapping_runs_are_inconclusive_even_if_the_median_improves():
    v = verdict_for(
        [run("before", i, r) for i, r in enumerate((10, 4, 12))]
        + [run("after", i, r) for i, r in enumerate((5, 5, 6))]
    )
    assert v.verdict == "inconclusive"
    assert v.criteria[0].status == "not_separated"


def test_a_problem_that_never_reproduced_cannot_be_called_fixed():
    v = verdict_for([run("before", i, 0) for i in range(3)] + [run("after", i, 0) for i in range(3)])
    assert v.verdict == "inconclusive"
    assert v.criteria[0].status == "not_reproduced"


def test_a_fix_that_loses_writes_is_a_regression():
    v = verdict_for(
        [run("before", i, r) for i, r in enumerate((10, 12, 11))]
        + [run("after", i, r, lost=1.0) for i, r in enumerate((4, 5, 4))]
    )
    assert v.verdict == "regressed"
    assert any("writes_lost" in r for r in v.regressions)


def test_different_steady_state_between_arms_is_inconclusive():
    v = verdict_for(
        [run("before", i, r, steady=40) for i, r in enumerate((10, 12, 11))]
        + [run("after", i, r, steady=400) for i, r in enumerate((4, 5, 4))]
    )
    assert v.verdict == "inconclusive"
    assert v.equivalence_problems


def test_one_valid_run_per_arm_is_never_enough():
    v = verdict_for([run("before", 1, 10), run("after", 1, 2)])
    assert v.verdict == "inconclusive"


# ---------------------------------------------------------------- verifier control flow
class FakeCompose:
    def __init__(self, fail_apply: bool = False) -> None:
        self.calls: list[str] = []
        self.applied = False
        self.fail_apply = fail_apply

    def apply(self, override: Path, services: list[str]) -> None:
        from remediation.compose import ComposeError

        self.calls.append("apply")
        if self.fail_apply:
            raise ComposeError("boom")
        assert override.exists()
        self.applied = True

    def restore(self, services: list[str]) -> None:
        self.calls.append("restore")
        self.applied = False


class FakeProbe:
    def write(self) -> CartWrite:
        return CartWrite(True, 3, session=None)

    def preserved(self, write: CartWrite) -> bool:
        return True


def approved_verifier(results_dir, config, write_telemetry, compose, run_experiment):
    write_telemetry()
    store = Store(results_dir)
    store.save_proposal(make_proposal(), SERVICES)
    store.record_decision("rem-test", "approved", "alice")
    verifier = Verifier(
        config,
        run_experiment,
        compose,
        FakeProbe(),
        store,
        cooldown_s=0,
        steady_samples=1,
        sleep=lambda _s: None,
    )
    return verifier, store


def test_verifier_runs_both_arms_and_always_restores(results_dir, config, write_telemetry):
    compose = FakeCompose()
    seen = []

    def run_experiment(exp: Experiment) -> ExperimentResult:
        seen.append((exp.id, compose.applied))
        return make_result(exp.id, recovery=4.0 if compose.applied else 10.0 + len(seen) % 2)

    verifier, store = approved_verifier(results_dir, config, write_telemetry, compose, run_experiment)
    v = verifier.verify(store.load_proposal("rem-test"))

    assert [s[0] for s in seen] == [f"rem-test-{a}-{i}" for a in ("before", "after") for i in (1, 2, 3)]
    assert [s[1] for s in seen] == [False] * 3 + [True] * 3
    assert compose.calls == ["apply", "restore"] and v.fix_restored
    assert v.verdict == "effective", v.reasons
    assert (store.dir("rem-test") / "verification.json").exists()


def test_a_kill_switch_mid_verification_restores_and_is_inconclusive(results_dir, config, write_telemetry):
    compose = FakeCompose()

    def run_experiment(exp: Experiment) -> ExperimentResult:
        result = make_result(exp.id)
        if compose.applied:
            result.verdict, result.abort_reason = "stopped", "kill switch"
        return result

    verifier, store = approved_verifier(results_dir, config, write_telemetry, compose, run_experiment)
    v = verifier.verify(store.load_proposal("rem-test"))
    assert compose.calls == ["apply", "restore"]
    assert v.verdict == "inconclusive"
    assert any("stopped early" in r for r in v.reasons)


def test_a_failed_apply_still_restores(results_dir, config, write_telemetry):
    compose = FakeCompose(fail_apply=True)
    verifier, store = approved_verifier(
        results_dir, config, write_telemetry, compose, lambda exp: make_result(exp.id)
    )
    v = verifier.verify(store.load_proposal("rem-test"))
    assert compose.calls == ["apply", "restore"]
    assert v.verdict == "inconclusive"


def test_an_unapproved_proposal_is_never_run(results_dir, config, write_telemetry):
    write_telemetry()
    store = Store(results_dir)
    store.save_proposal(make_proposal(), SERVICES)
    verifier = Verifier(
        config,
        lambda _e: pytest.fail("ran an unapproved fix"),
        FakeCompose(),
        FakeProbe(),
        store,
        cooldown_s=0,
        steady_samples=1,
        sleep=lambda _s: None,
    )
    with pytest.raises(PermissionError):
        verifier.verify(store.load_proposal("rem-test"))


# ---------------------------------------------------------------- agent plumbing
def test_agent_reads_only_real_experiment_records(results_dir):
    write_record(results_dir, make_result("kill-cart-1"))
    write_record(results_dir, make_result("rem-1-before-1"))
    (results_dir / "graph.json").write_text(json.dumps({"nodes": []}))
    dry = make_result("dry-1")
    dry.injected_at = None
    write_record(results_dir, dry)
    assert [r.experiment.id for r in experiment_records(results_dir)] == ["kill-cart-1"]


def test_agent_refuses_to_guess_when_the_reply_is_unusable():
    sent = {}

    def client(finish_reason, content):
        def create(**kwargs):
            sent.update(kwargs)
            message = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason, message=message)])

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    draft = make_draft()
    with pytest.raises(ProposalError, match="token limit"):
        GroqProposer(client=client("length", draft.model_dump_json())).propose("ctx")
    with pytest.raises(ProposalError, match="did not parse"):
        GroqProposer(client=client("stop", '{"remediable": true}')).propose("ctx")
    assert GroqProposer(client=client("stop", draft.model_dump_json())).propose("ctx") == draft
    assert sent["response_format"]["json_schema"]["strict"] is True


def test_fault_timeline_shows_container_state_per_sample(results_dir, write_telemetry, telemetry):
    from tests.conftest import make_sample

    result = make_result("kill-cart-1")
    samples = []
    for i, phase in enumerate(("running", "exited (137)", "exited (137)", "running")):
        sample = make_sample(at=result.injected_at + timedelta(seconds=2 * i - 1))
        sample["workloads"] = [
            {
                "name": "online-boutique-cartservice-1",
                "app": "cartservice",
                "phase": phase,
                "restarts": 0,
                "oom_killed": False,
            }
        ]
        samples.append(sample)
    samples[1]["recent_events"] = [
        {
            "time": result.injected_at.isoformat(),
            "action": "kill",
            "container": "online-boutique-cartservice-1",
            "exit_code": None,
        }
    ]
    write_telemetry(samples)
    text = fault_timeline(result, telemetry)
    assert "cartservice exited (137) r=0" in text
    assert "docker events:" in text and "kill cartservice" in text


def test_fault_timeline_flags_a_container_running_at_its_limit(results_dir, write_telemetry, telemetry):
    from tests.conftest import make_sample

    result = make_result("spike-frontend-1")
    sample = make_sample(at=result.injected_at + timedelta(seconds=1))
    sample["workloads"] = [
        {"name": "c-1", "app": "currencyservice", "phase": "running", "restarts": 0, "cpu_pct": 9.5},
        {"name": "p-1", "app": "productcatalogservice", "phase": "running", "restarts": 0, "cpu_pct": 3.0},
    ]
    write_telemetry([sample])
    caps = resource_caps(
        {"currencyservice": {"cpus": 0.1}, "productcatalogservice": {"cpus": 0.5}, "frontend": {}}
    )
    assert caps == {"currencyservice": {"cpu_pct": 10.0}, "productcatalogservice": {"cpu_pct": 50.0}}
    text = fault_timeline(result, telemetry, caps=caps)
    assert "currencyservice running r=0 cpu=10/10% AT LIMIT" in text
    assert "productcatalogservice" not in text


def test_agent_context_shows_resource_limits_only_where_set(results_dir, config, write_telemetry):
    from tests.conftest import make_sample

    write_telemetry([make_sample()])
    services = {
        "currencyservice": {"restart": "unless-stopped", "cpus": 0.1, "environment": {"PORT": "7000"}},
        "cartservice": {"restart": "unless-stopped", "environment": {"REDIS_ADDR": "redis-cart:6379"}},
    }
    text = build_context([make_result("kill-cart-1")], config, services, docs_path=Path("missing.yaml"))
    assert '"currencyservice":{"restart":"unless-stopped","cpus":0.1,' in text
    assert '"cartservice":{"restart":"unless-stopped","environment"' in text


def test_strict_schema_requires_every_field():
    schema = strict_schema(ProposalDraft.model_json_schema())
    exp = schema["$defs"]["ValidationExperiment"]
    assert set(exp["required"]) == set(exp["properties"]) and exp["additionalProperties"] is False

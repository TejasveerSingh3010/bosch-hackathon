"""The safety layer is what makes an autonomous planner safe, so it gets the most tests."""
from __future__ import annotations

import pytest

from safety.killswitch import KillSwitch
from safety.limits import ABSOLUTE_MAX_EXPERIMENT_S, TargetConfig, as_percent
from safety.validator import PlanRejected, merge_thresholds, validate_plan
from schemas import AbortIf, Experiment
from tests.conftest import PROJECT, make_sample


def plan(**overrides) -> Experiment:
    payload = {"id": "t1", "target": "redis-cart", "action": "kill", "duration_s": 30}
    payload.update(overrides)
    return Experiment(**payload)


# --------------------------------------------------------------------- units
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.2, 20.0), (0.05, 5.0), (1.0, 100.0), (20, 20.0), (99.5, 99.5), (None, 20.0)],
)
def test_fraction_and_percent_thresholds_both_become_percent(value, expected):
    """target.yaml uses 0.20, telemetry reports 20.0; mixing them up would be a 100x error."""
    assert as_percent(value, 20.0) == expected


def test_negative_threshold_is_rejected():
    with pytest.raises(ValueError):
        as_percent(-1, 20.0)


def test_absolute_ceiling_overrides_a_reckless_config():
    config = TargetConfig.from_dict({"max_experiment_seconds": 99999, "allowed_containers": ["x"]})
    assert config.effective_max_seconds == ABSOLUTE_MAX_EXPERIMENT_S


# ------------------------------------------------------------------ allow-list
def test_container_not_on_the_allow_list_is_rejected(config, docker, telemetry, write_telemetry):
    write_telemetry()
    containers = docker.list_containers(PROJECT)
    narrowed = TargetConfig.from_dict(
        {"compose_project": PROJECT, "allowed_containers": [f"{PROJECT}-adservice-1"],
         "max_experiment_seconds": 120},
        results_dir=config.results_dir,
    )
    with pytest.raises(PlanRejected, match="not in target.yaml allowed_containers"):
        validate_plan(plan(target="redis-cart"), narrowed, containers, telemetry)


def test_empty_allow_list_rejects_everything(config, docker, telemetry, write_telemetry):
    write_telemetry()
    empty = TargetConfig.from_dict(
        {"compose_project": PROJECT, "allowed_containers": [], "max_experiment_seconds": 120},
        results_dir=config.results_dir,
    )
    with pytest.raises(PlanRejected, match="empty allowed_containers"):
        validate_plan(plan(), empty, docker.list_containers(PROJECT), telemetry)


def test_load_generator_is_protected_even_if_allow_listed(config, docker, telemetry, write_telemetry):
    """Breaking the load generator would silently remove the traffic the experiment measures."""
    write_telemetry()
    permissive = TargetConfig.from_dict(
        {"compose_project": PROJECT, "allowed_containers": [f"{PROJECT}-loadgenerator-1"],
         "max_experiment_seconds": 120},
        results_dir=config.results_dir,
    )
    with pytest.raises(PlanRejected, match="protected infrastructure"):
        validate_plan(plan(target="loadgenerator"), permissive, docker.list_containers(PROJECT), telemetry)


# -------------------------------------------------------------------- duration
def test_duration_above_the_configured_cap_is_rejected(config, docker, telemetry, write_telemetry):
    write_telemetry()
    with pytest.raises(PlanRejected, match="exceeds the cap"):
        validate_plan(plan(duration_s=300), config, docker.list_containers(PROJECT), telemetry)


def test_schema_rejects_a_duration_beyond_any_sane_limit():
    with pytest.raises(ValueError):
        plan(duration_s=99999)


# ------------------------------------------------------------------ thresholds
def test_a_planner_cannot_loosen_the_configured_limits(config):
    """An LLM asking for a 90% error budget gets target.yaml's 20% instead."""
    thresholds, notes = merge_thresholds(
        plan(abort_if=AbortIf(error_rate=0.9, p95_ms=60000)), config
    )
    assert thresholds.error_rate_pct == 20.0
    assert thresholds.p95_ms == 2000.0
    assert len(notes) == 2 and "stricter" in notes[0]


def test_a_planner_may_tighten_the_limits(config):
    thresholds, notes = merge_thresholds(plan(abort_if=AbortIf(error_rate=0.05, p95_ms=800)), config)
    assert (thresholds.error_rate_pct, thresholds.p95_ms) == (5.0, 800.0)
    assert notes == []


# ----------------------------------------------------------------- pre-flight
def test_injecting_into_an_already_degraded_system_is_rejected(config, docker, telemetry, write_telemetry):
    write_telemetry(error_rate_pct=33.3, failed_endpoints=["Cart"])
    with pytest.raises(PlanRejected, match="already degraded"):
        validate_plan(plan(), config, docker.list_containers(PROJECT), telemetry)


def test_stale_telemetry_blocks_injection(config, docker, telemetry, write_telemetry):
    """No monitoring means no measurement and no automatic abort, so never inject."""
    from datetime import datetime, timedelta

    old = datetime.now().astimezone() - timedelta(minutes=5)
    write_telemetry(samples=[make_sample(at=old)])
    with pytest.raises(PlanRejected, match="monitor is not running"):
        validate_plan(plan(), config, docker.list_containers(PROJECT), telemetry)


def test_missing_telemetry_blocks_injection(config, docker, telemetry):
    with pytest.raises(PlanRejected, match="monitor"):
        validate_plan(plan(), config, docker.list_containers(PROJECT), telemetry)


def test_other_unready_containers_block_injection(config, docker, telemetry, write_telemetry):
    write_telemetry(not_ready=[f"{PROJECT}-emailservice-1"], ready=11)
    with pytest.raises(PlanRejected, match="not ready before the experiment"):
        validate_plan(plan(), config, docker.list_containers(PROJECT), telemetry)


def test_a_stopped_target_is_rejected(config, docker, telemetry, write_telemetry):
    write_telemetry()
    docker.containers[f"{PROJECT}-redis-cart-1"].status = "exited"
    with pytest.raises(PlanRejected, match="not running"):
        validate_plan(plan(), config, docker.list_containers(PROJECT), telemetry)


def test_a_valid_plan_resolves_the_service_to_a_container(config, docker, telemetry, write_telemetry):
    write_telemetry()
    validated = validate_plan(plan(target="redis-cart"), config, docker.list_containers(PROJECT), telemetry)
    assert validated.container_name == f"{PROJECT}-redis-cart-1"
    assert validated.service_name == "redis-cart"
    assert validated.thresholds.error_rate_pct == 20.0


def test_an_unknown_target_lists_what_is_available(config, docker, telemetry, write_telemetry):
    write_telemetry()
    with pytest.raises(PlanRejected, match="Known services"):
        validate_plan(plan(target="nosuchservice"), config, docker.list_containers(PROJECT), telemetry)


# ----------------------------------------------------------------- kill switch
def test_kill_switch_round_trip(results_dir):
    switch = KillSwitch(results_dir)
    assert not switch.engaged()
    switch.engage("because I said so")
    assert switch.engaged() and switch.reason() == "because I said so"
    switch.clear()
    assert not switch.engaged()

"""The day-2 fault actions: each must apply, and - as with every action - always roll back,
idempotently and from state, with no Docker needed (tests/fake_docker models the behaviour).
"""
from __future__ import annotations

import time

import pytest

from injector.actions import (
    MEMORY_FLOOR_BYTES,
    build_action,
    parse_memory,
)
from injector.docker_cli import DockerError
from tests.conftest import PROJECT

TARGET = f"{PROJECT}-redis-cart-1"
NETWORK = f"{PROJECT}_default"


def action_for(name, docker, **params):
    return build_action(name, docker, PROJECT, **params)


# ----------------------------------------------------------- parse_memory
@pytest.mark.parametrize(
    ("text", "expected"),
    [("256m", 256 * 1024**2), ("1g", 1024**3), ("512k", 512 * 1024), ("1048576", 1048576)],
)
def test_parse_memory(text, expected):
    assert parse_memory(text) == expected


# ----------------------------------------------------------- network_partition
def test_network_partition_disconnects_and_rollback_reconnects_with_aliases(docker):
    action = action_for("network_partition", docker)
    outcome = action.apply(TARGET)
    assert outcome.ok
    assert NETWORK not in docker.containers[TARGET].networks  # disconnected

    assert action.rollback(TARGET).ok
    assert docker.containers[TARGET].networks[NETWORK] == ["redis-cart"]  # aliases restored


def test_network_partition_rollback_is_idempotent(docker):
    action = action_for("network_partition", docker)
    action.apply(TARGET)
    action.rollback(TARGET)
    second = action.rollback(TARGET)  # already reconnected
    assert second.ok and "already on" in second.detail


def test_network_partition_refuses_a_network_the_target_is_not_on(docker):
    action = action_for("network_partition", docker, network="does-not-exist")
    outcome = action.apply(TARGET)
    assert not outcome.ok and "not on network" in outcome.detail


# ----------------------------------------------------------- network_delay
def test_network_delay_installs_and_removes_the_qdisc_via_a_sidecar(docker):
    action = action_for("network_delay", docker, delay_ms=300, loss_pct=5)
    outcome = action.apply(TARGET)
    assert outcome.ok and TARGET in docker.netem_applied
    sidecar = docker.sidecars[-1]
    assert sidecar["cap_add"] == ("NET_ADMIN",)
    assert "netem" in sidecar["command"] and "300ms" in sidecar["command"]

    assert action.rollback(TARGET).ok
    assert TARGET not in docker.netem_applied


def test_network_delay_rollback_tolerates_an_already_clean_qdisc(docker):
    action = action_for("network_delay", docker)
    action.apply(TARGET)
    docker.netem_applied.discard(TARGET)  # someone/something already cleared it
    docker.fail_on.add("fail_netem_del")  # and the del now errors like the real tc does
    outcome = action.rollback(TARGET)
    assert outcome.ok and "already cleared" in outcome.detail


# ----------------------------------------------------------- cpu_throttle
def test_cpu_throttle_sets_and_restores_the_quota(docker):
    docker.containers[TARGET].nano_cpus = 2_000_000_000  # started with a 2-core limit
    action = action_for("cpu_throttle", docker, cpus=0.1)
    assert action.apply(TARGET).ok
    assert docker.containers[TARGET].nano_cpus == 100_000_000  # 0.1 core

    assert action.rollback(TARGET).ok
    assert docker.containers[TARGET].nano_cpus == 2_000_000_000  # original restored


def test_cpu_throttle_rollback_restores_unlimited_when_there_was_no_limit(docker):
    assert docker.containers[TARGET].nano_cpus is None
    action = action_for("cpu_throttle", docker, cpus=0.2)
    action.apply(TARGET)
    action.rollback(TARGET)
    assert docker.containers[TARGET].nano_cpus is None  # back to unlimited


# ----------------------------------------------------------- memory_limit
def test_memory_limit_enforces_the_floor_and_restores(docker):
    action = action_for("memory_limit", docker, memory="1m")  # below the floor
    outcome = action.apply(TARGET)
    assert outcome.ok and docker.containers[TARGET].mem_limit == MEMORY_FLOOR_BYTES

    assert action.rollback(TARGET).ok
    assert docker.containers[TARGET].mem_limit is None  # was unlimited, restored


def test_memory_limit_respects_a_request_above_the_floor(docker):
    action = action_for("memory_limit", docker, memory="256m")
    action.apply(TARGET)
    assert docker.containers[TARGET].mem_limit == 256 * 1024**2


# ----------------------------------------------------------- flapping
def test_flapping_pauses_and_rollback_leaves_it_running(docker):
    action = action_for("flapping", docker, mode="pause", down_s=0.1, interval_s=0.1)
    assert action.apply(TARGET).ok
    time.sleep(0.35)  # let a couple of cycles run
    assert action.rollback(TARGET).ok
    assert docker.containers[TARGET].running and not docker.containers[TARGET].paused
    assert ("pause", TARGET) in docker.calls  # it really did flap


def test_flapping_refuses_a_bad_mode(docker):
    action = action_for("flapping", docker, mode="explode")
    outcome = action.apply(TARGET)
    assert not outcome.ok and "pause" in outcome.detail


def test_flapping_rollback_is_safe_when_never_started(docker):
    action = action_for("flapping", docker)
    assert action.rollback(TARGET).ok  # nothing to stop, container already running


# ----------------------------------------------------------- refusal on a stopped target
@pytest.mark.parametrize("name", ["network_partition", "network_delay", "cpu_throttle",
                                  "memory_limit", "flapping"])
def test_actions_refuse_a_stopped_target(docker, name):
    docker.containers[TARGET].status = "exited"
    outcome = action_for(name, docker).apply(TARGET)
    assert not outcome.ok and "not running" in outcome.detail


# ----------------------------------------------------------- docker error surfaces
def test_a_docker_error_during_apply_is_raised(docker):
    docker.fail_on = {"update"}
    with pytest.raises(DockerError):
        action_for("cpu_throttle", docker).apply(TARGET)

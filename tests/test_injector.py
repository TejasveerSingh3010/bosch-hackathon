"""Every action must apply, and - more importantly - must always roll back."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from injector.actions import build_action
from injector.docker_cli import DockerError
from injector.resolve import TargetError, resolve_target
from tests.conftest import PROJECT
from tests.fake_docker import FakeContainer, FakeDocker

TARGET = f"{PROJECT}-redis-cart-1"


def action_for(name: str, docker: FakeDocker, **params):
    return build_action(name, docker, PROJECT, **params)


# ------------------------------------------------------------------- resolving
def test_a_service_name_resolves_to_its_container(docker):
    assert resolve_target(docker.list_containers(PROJECT), "redis-cart").name == TARGET


def test_an_exact_container_name_resolves(docker):
    assert resolve_target(docker.list_containers(PROJECT), TARGET).name == TARGET


def test_a_partial_name_resolves_when_unambiguous(docker):
    assert resolve_target(docker.list_containers(PROJECT), "redis").name == TARGET


def test_an_ambiguous_target_is_refused_rather_than_guessed(docker):
    """Injecting into the wrong container would invalidate the experiment."""
    with pytest.raises(TargetError, match="ambiguous"):
        resolve_target(docker.list_containers(PROJECT), "service")


def test_resolving_against_no_containers_says_the_app_is_not_running():
    with pytest.raises(TargetError, match="is the application running"):
        resolve_target([], "redis-cart")


# ----------------------------------------------------------------------- kill
def test_kill_is_recovered_by_dockers_restart_policy_and_rollback_is_a_no_op(docker):
    action = action_for("kill", docker)
    assert action.apply(TARGET).ok
    assert docker.containers[TARGET].restart_count == 1

    outcome = action.rollback(TARGET)
    assert outcome.ok and "restart policy" in outcome.detail
    assert docker.containers[TARGET].running
    assert ("start", TARGET) not in docker.calls  # nothing needed starting


def test_kill_without_a_restart_policy_is_started_by_rollback(docker):
    docker.containers[TARGET].restart_policy = "no"
    docker.self_heal_on_kill = False
    action = action_for("kill", docker)
    action.apply(TARGET)
    assert not docker.containers[TARGET].running

    assert action.rollback(TARGET).ok
    assert docker.containers[TARGET].running
    assert ("start", TARGET) in docker.calls


def test_killing_a_stopped_container_is_refused_not_attempted(docker):
    docker.containers[TARGET].status = "exited"
    outcome = action_for("kill", docker).apply(TARGET)
    assert not outcome.ok and "not running" in outcome.detail
    assert docker.calls == []


# ----------------------------------------------------------------------- stop
def test_stop_keeps_the_container_down_until_rollback(docker):
    action = action_for("stop", docker)
    assert action.apply(TARGET).ok
    assert not docker.containers[TARGET].running  # `unless-stopped` must not undo a deliberate stop

    assert action.rollback(TARGET).ok
    assert docker.containers[TARGET].running


# ---------------------------------------------------------------------- pause
def test_pause_then_unpause(docker):
    action = action_for("pause", docker)
    assert action.apply(TARGET).ok
    assert docker.containers[TARGET].paused

    outcome = action.rollback(TARGET)
    assert outcome.ok and "unpaused" in outcome.detail
    assert not docker.containers[TARGET].paused


def test_pausing_an_already_paused_container_is_refused(docker):
    docker.containers[TARGET].paused = True
    assert not action_for("pause", docker).apply(TARGET).ok


def test_rollback_is_idempotent(docker):
    """Rollback runs from the normal path, aborts, the kill switch and crash recovery."""
    action = action_for("pause", docker)
    action.apply(TARGET)
    action.rollback(TARGET)
    second = action.rollback(TARGET)
    assert second.ok and "nothing to undo" in second.detail


def test_rollback_unpauses_a_container_that_was_paused_by_a_crashed_run(docker):
    """`recover` builds a fresh action with no memory of the apply call."""
    docker.containers[TARGET].paused = True
    assert action_for("pause", docker).rollback(TARGET).ok
    assert not docker.containers[TARGET].paused


def test_a_failing_docker_start_surfaces_as_an_error(docker):
    docker.containers[TARGET].restart_policy = "no"
    docker.self_heal_on_kill = False
    action = action_for("kill", docker)
    action.apply(TARGET)
    docker.fail_on = {"start"}
    with pytest.raises(DockerError):
        action.rollback(TARGET)


def test_an_unknown_action_is_refused_with_the_available_list(docker):
    with pytest.raises(ValueError, match="available"):
        build_action("nuke", docker, PROJECT)


def test_pre_state_is_recorded_so_recovery_knows_what_to_restore(docker):
    outcome = action_for("stop", docker).apply(TARGET)
    assert outcome.pre_state["running"] is True
    assert outcome.pre_state["restart_policy"] == "unless-stopped"


def test_spike_sends_until_rollback_stops_it(docker, monkeypatch):
    calls: list[str] = []

    def fake_get(url, timeout=5, allow_redirects=False):
        calls.append(url)
        response = MagicMock()
        response.status_code = 200
        return response

    monkeypatch.setattr("injector.actions.requests.get", fake_get)
    action = action_for("spike", docker, rps=20, users=2, time=5)
    outcome = action.apply(TARGET)
    assert outcome.ok and "20 req/s" in outcome.detail
    time.sleep(0.35)
    sent = len(calls)
    assert sent >= 4
    assert all(url.startswith("http://localhost:8080/") for url in calls)

    assert "stopped the traffic spike" in action.rollback(TARGET).detail
    time.sleep(0.25)
    assert len(calls) - sent <= 2
    assert "nothing to undo" in action.rollback(TARGET).detail


def test_spike_refuses_a_rate_above_the_cap(docker):
    outcome = action_for("spike", docker, rps=20_001, users=2, time=5).apply(TARGET)
    assert not outcome.ok and "rps" in outcome.detail


def test_spike_refuses_a_stopped_container(docker):
    docker.containers[TARGET].status = "exited"
    outcome = action_for("spike", docker, rps=5, users=1, time=5).apply(TARGET)
    assert not outcome.ok and "not running" in outcome.detail


def test_a_container_outside_the_project_is_not_listed(docker):
    docker.containers["other-app-db-1"] = FakeContainer("other-app-db-1", "db", project="other-app")
    names = {c.name for c in docker.list_containers(PROJECT)}
    assert "other-app-db-1" not in names

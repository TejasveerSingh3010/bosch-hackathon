"""End-to-end runner tests: the system must always end up rolled back.

A fake clock is injected so a 30-second experiment runs instantly, and the telemetry file is
rewritten between polls to simulate the application degrading and recovering.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orchestrator.runner import ExperimentRunner
from safety.validator import PlanRejected
from schemas import Experiment
from tests.conftest import PROJECT, make_sample

TARGET_SERVICE = "redis-cart"
TARGET = f"{PROJECT}-{TARGET_SERVICE}-1"


class FakeClock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.01)


class TelemetryScript:
    """Rewrites telemetry.json on each poll, so the runner sees a changing system."""

    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir
        self.phases: list[dict[str, Any]] = []
        self.index = 0

    def set(self, **kwargs: Any) -> None:
        """Freeze telemetry at one state (timestamps stay fresh on every read)."""
        self.phases = [kwargs]
        self._write(kwargs)

    def sequence(self, phases: list[dict[str, Any]]) -> None:
        self.phases = phases
        self.index = 0
        self._write(phases[0])

    def advance(self) -> None:
        if self.index < len(self.phases) - 1:
            self.index += 1
        self._write(self.phases[self.index])

    def _write(self, state: dict[str, Any]) -> None:
        now = datetime.now().astimezone()
        samples = [make_sample(at=now - timedelta(seconds=2 * (4 - i)), **state) for i in range(5)]
        (self.results_dir / "telemetry.json").write_text(json.dumps(samples), encoding="utf-8")
        with (self.results_dir / "telemetry_log.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(samples[-1], separators=(",", ":")) + "\n")


@pytest.fixture
def script(results_dir: Path) -> TelemetryScript:
    return TelemetryScript(results_dir)


def build_runner(docker, config, script: TelemetryScript, clock: FakeClock) -> ExperimentRunner:
    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        script.advance()

    return ExperimentRunner(docker, config, poll_s=2.0, clock=clock.time, sleep=sleep)


def experiment(**overrides) -> Experiment:
    payload = {"id": "exp-test", "target": TARGET_SERVICE, "action": "pause", "duration_s": 20}
    payload.update(overrides)
    return Experiment(**payload)


# ------------------------------------------------------------------ happy path
def test_a_tolerated_failure_completes_and_rolls_back(docker, config, script):
    """adservice is meant to degrade gracefully: no probe failures, so the run completes."""
    script.set()
    clock = FakeClock()
    result = build_runner(docker, config, script, clock).run(experiment(target="adservice"))

    assert result.verdict == "completed"
    assert not docker.containers[f"{PROJECT}-adservice-1"].paused
    assert result.rolled_back_at is not None
    assert any("tolerated" in note for note in result.observations)


def test_the_result_file_is_written_and_reloadable(docker, config, script, results_dir):
    script.set()
    runner = build_runner(docker, config, script, FakeClock())
    result = runner.run(experiment(id="exp-written"))

    path = results_dir / "exp-written.json"
    assert path.exists()
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["verdict"] == result.verdict
    assert reloaded["container"] == TARGET
    assert reloaded["thresholds_used"]["error_rate_pct"] == 20.0


# ----------------------------------------------------------------- abort paths
def test_the_watchdog_aborts_a_breach_and_rolls_back(docker, config, script):
    """The brief's "stop automatically if impact exceeds thresholds", proven end to end."""
    script.sequence([
        {},  # healthy pre-flight
        {"error_rate_pct": 100.0, "failed_endpoints": ["Home", "Cart", "Product"]},
        {"error_rate_pct": 100.0, "failed_endpoints": ["Home", "Cart", "Product"]},
    ])
    result = build_runner(docker, config, script, FakeClock()).run(experiment(duration_s=120))

    assert result.verdict == "aborted"
    assert "error rate" in result.abort_reason
    assert not docker.containers[TARGET].paused  # rolled back despite the abort
    assert result.impact_seconds is not None and result.impact_seconds < 120


def test_the_kill_switch_stops_a_running_experiment(docker, config, script):
    """The real scenario: someone engages it from another window while the failure is in place."""
    script.set()
    clock = FakeClock()
    runner = ExperimentRunner(docker, config, poll_s=2.0, clock=clock.time, sleep=lambda _seconds: None)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        runner.killswitch.engage("teammate pulled the plug")

    runner.sleep = sleep
    result = runner.run(experiment(duration_s=120))
    assert result.verdict == "stopped"
    assert "teammate pulled the plug" in result.abort_reason
    assert not docker.containers[TARGET].paused


def test_telemetry_disappearing_mid_experiment_aborts(docker, config, script, results_dir):
    """If monitoring dies we are blind, so the failure must not stay in place."""
    script.set()
    clock = FakeClock()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        (results_dir / "telemetry.json").unlink(missing_ok=True)

    runner = ExperimentRunner(docker, config, poll_s=2.0, clock=clock.time, sleep=sleep)
    result = runner.run(experiment(duration_s=120))

    assert result.verdict == "aborted"
    assert "unreadable" in result.abort_reason or "blind" in result.abort_reason
    assert not docker.containers[TARGET].paused


def test_a_stale_telemetry_file_aborts(docker, config, script, results_dir):
    script.set()
    clock = FakeClock()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        old = datetime.now().astimezone() - timedelta(minutes=10)
        (results_dir / "telemetry.json").write_text(json.dumps([make_sample(at=old)]), encoding="utf-8")

    runner = ExperimentRunner(docker, config, poll_s=2.0, clock=clock.time, sleep=sleep)
    result = runner.run(experiment(duration_s=120))
    assert result.verdict == "aborted" and "stale" in result.abort_reason
    assert not docker.containers[TARGET].paused


# --------------------------------------------------------------- self-healing
def test_a_kill_is_reported_as_self_healed(docker, config, script):
    script.set()
    result = build_runner(docker, config, script, FakeClock()).run(
        experiment(action="kill", target=TARGET_SERVICE)
    )
    assert result.self_healed is True
    assert result.restart_count_delta == 1
    assert docker.containers[TARGET].running


# -------------------------------------------------------------------- journal
def test_a_leftover_journal_blocks_a_new_run_and_recover_clears_it(docker, config, script):
    script.set()
    runner = build_runner(docker, config, script, FakeClock())

    # Simulate a crash: pause the container and leave a journal behind.
    from safety.journal import JournalEntry

    runner.journal.write(JournalEntry(
        experiment_id="crashed", action="pause", container=TARGET, service=TARGET_SERVICE,
        project=PROJECT, params={}, pre_state={"running": True}, injected_at=0.0, pid=1,
    ))
    docker.containers[TARGET].paused = True

    with pytest.raises(PlanRejected, match="did not finish cleanly"):
        runner.run(experiment())

    message = runner.recover()
    assert "recovered" in message
    assert not docker.containers[TARGET].paused
    assert not runner.journal.exists()
    runner.run(experiment())  # the next run is unblocked


def test_recover_restores_the_cpu_limit_a_crashed_throttle_replaced(docker, config):
    from safety.journal import JournalEntry

    runner = ExperimentRunner(docker, config)
    runner.journal.write(JournalEntry(
        experiment_id="crashed", action="throttle", container=TARGET, service=TARGET_SERVICE,
        project=PROJECT, params={"cpus": 0.05}, pre_state={"running": True, "nano_cpus": 200_000_000},
        injected_at=0.0, pid=1,
    ))
    docker.containers[TARGET].nano_cpus = 50_000_000
    assert "recovered" in runner.recover()
    assert docker.containers[TARGET].nano_cpus == 200_000_000


def test_recover_is_safe_when_there_is_nothing_to_do(docker, config):
    assert "nothing to recover" in ExperimentRunner(docker, config).recover()


def test_the_journal_is_cleared_after_a_normal_run(docker, config, script):
    script.set()
    runner = build_runner(docker, config, script, FakeClock())
    runner.run(experiment())
    assert not runner.journal.exists()


def test_a_stale_kill_switch_does_not_block_the_next_run(docker, config, script):
    script.set()
    runner = build_runner(docker, config, script, FakeClock())
    runner.killswitch.engage("from a previous run")
    runner.run(experiment())  # engaged -> stopped, but the switch is cleared at the start

    runner2 = build_runner(docker, config, script, FakeClock())
    assert runner2.run(experiment(id="exp-after")).verdict == "completed"


# ------------------------------------------------------------------- dry run
def test_dry_run_changes_nothing(docker, config, script):
    script.set()
    runner = build_runner(docker, config, script, FakeClock())
    plan = runner.dry_run(experiment())

    assert plan.container_name == TARGET
    assert docker.calls == []
    assert not runner.journal.exists()


def test_dry_run_still_rejects_an_invalid_plan(docker, config, script):
    script.set()
    runner = build_runner(docker, config, script, FakeClock())
    with pytest.raises(PlanRejected):
        runner.dry_run(experiment(target="loadgenerator"))


# ---------------------------------------------------------- injection failure
def test_a_failed_injection_does_not_leave_a_journal_behind(docker, config, script):
    script.set()
    runner = build_runner(docker, config, script, FakeClock())
    docker.fail_on = {"pause"}

    result = runner.run(experiment())
    assert result.verdict == "failed"
    assert not runner.journal.exists()

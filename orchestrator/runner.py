"""Run one experiment end to end: validate, inject, watch, roll back, measure, record.

The order of operations is chosen so the system can always be put back:

    validate -> journal -> inject -> watch loop -> rollback -> wait for recovery -> clear journal

Rollback runs in a `finally` block, so an exception, a Ctrl+C, a watchdog abort and a kill switch
all take the same path out. The journal is written *before* injection and cleared *after* a
confirmed rollback, which is what makes `orchestrator recover` possible.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from injector.actions import ActionOutcome, FailureAction, build_action
from injector.docker_cli import Docker, DockerError
from safety.journal import Journal, JournalEntry
from safety.killswitch import KillSwitch
from safety.limits import TargetConfig
from safety.telemetry import TelemetryReader, TelemetryUnavailable, healthy, summarise_phase
from safety.validator import PlanRejected, ValidatedPlan, explain_plan, validate_plan
from safety.watchdog import Watchdog
from schemas import Experiment, ExperimentResult, Phase

log = logging.getLogger("orchestrator")

#: How long to wait for the system to come back after rollback before giving up on measuring it.
RECOVERY_TIMEOUT_S = 90.0
#: Healthy samples in a row required before declaring recovery (one good sample can be luck).
RECOVERY_CONFIRM_SAMPLES = 2


class ExperimentRunner:
    def __init__(
        self,
        docker: Docker,
        config: TargetConfig,
        telemetry: TelemetryReader | None = None,
        poll_s: float = 2.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.docker = docker
        self.config = config
        self.results_dir = Path(config.results_dir)
        self.telemetry = telemetry or TelemetryReader(self.results_dir)
        self.poll_s = poll_s
        self.clock = clock
        self.sleep = sleep
        self.journal = Journal(self.results_dir)
        self.killswitch = KillSwitch(self.results_dir)

    # ------------------------------------------------------------------ helpers
    def _containers(self) -> list[Any]:
        return self.docker.list_containers(self.config.compose_project)  # type: ignore[attr-defined]

    def _latest(self) -> tuple[dict[str, Any] | None, float | None]:
        """The newest sample and its age, or (None, None) if telemetry is unreadable."""
        try:
            sample = self.telemetry.latest()
            return sample, self.telemetry.age_seconds(sample)
        except (TelemetryUnavailable, ValueError, KeyError):
            return None, None

    def _write_result(self, result: ExperimentResult) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        path = self.results_dir / f"{result.experiment.id}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return path

    # ------------------------------------------------------------------ public
    def dry_run(self, experiment: Experiment) -> ValidatedPlan:
        """Validate and explain without touching anything. Raises PlanRejected if not allowed."""
        plan = validate_plan(experiment, self.config, self._containers(), self.telemetry)
        log.info("DRY RUN - nothing will be injected\n%s", explain_plan(plan))
        return plan

    def run(self, experiment: Experiment) -> ExperimentResult:
        """Validate and run one experiment. Always leaves the system rolled back."""
        if self.journal.exists():
            entry = self.journal.read()
            detail = f"{entry.action} on {entry.container}" if entry else "unknown"
            raise PlanRejected(
                f"a previous experiment ({detail}) did not finish cleanly. "
                f"Run `python -m orchestrator recover` first."
            )
        self.killswitch.clear()  # a stale STOP file must not block a fresh run

        plan = validate_plan(experiment, self.config, self._containers(), self.telemetry)
        log.info("plan accepted\n%s", explain_plan(plan))

        action = build_action(experiment.action, self.docker, self.config.compose_project,
                              **experiment.params)
        watchdog = Watchdog(plan.thresholds, target_container=plan.container_name)
        result = ExperimentResult(
            experiment=experiment,
            verdict="failed",
            container=plan.container_name,
            service=plan.service_name,
            thresholds_used=plan.thresholds.as_dict(),
            baseline=self._baseline_summary(),
            observations=list(plan.notes),
        )

        before_start = datetime.now().astimezone() - timedelta(seconds=60)
        result.started_at = datetime.now().astimezone()

        applied: ActionOutcome | None = None
        try:
            applied = self._inject(plan, action, result)
            if applied is not None:
                self._watch(plan, watchdog, result)
        except KeyboardInterrupt:
            result.verdict = "stopped"
            result.abort_reason = "interrupted from the keyboard (Ctrl+C)"
            log.warning("interrupted - rolling back")
        except DockerError as exc:
            result.verdict = "failed"
            result.errors.append(f"docker error: {exc}")
            log.error("docker error: %s", exc)
        finally:
            # Roll back whenever the failure may be in place. `applied is None` means injection
            # never took effect and the journal has already been cleared, so there is nothing to undo.
            if applied is not None:
                self._rollback(plan, action, result)
        return self._finish(result, before_start)

    # ------------------------------------------------------------------ phases
    def _baseline_summary(self) -> dict[str, Any] | None:
        baseline = self.telemetry.baseline()
        if not baseline:
            return None
        return {
            "captured_at": baseline.get("captured_at"),
            "window": baseline.get("window"),
            "overall": baseline.get("overall"),
            "quality_warnings": baseline.get("quality_warnings"),
        }

    def _inject(self, plan: ValidatedPlan, action: FailureAction,
                result: ExperimentResult) -> ActionOutcome | None:
        """Journal, then inject. Returns None when nothing took effect (journal already cleared)."""
        entry = JournalEntry(
            experiment_id=plan.experiment.id,
            action=plan.experiment.action,
            container=plan.container_name,
            service=plan.service_name,
            project=self.config.compose_project,
            params=dict(plan.experiment.params),
            pre_state=None,
            injected_at=self.clock(),
            pid=os.getpid(),
        )
        self.journal.write(entry)  # written first: a crash now is still recoverable

        try:
            outcome = action.apply(plan.container_name)
        except DockerError as exc:
            # The command failed, so the container may or may not have been touched. Roll back
            # defensively (every rollback is idempotent and state-based) and clear the journal,
            # so a docker hiccup does not block the next run.
            result.verdict = "failed"
            result.errors.append(f"injection failed: {exc}")
            log.error("injection failed: %s", exc)
            try:
                action.rollback(plan.container_name)
            except DockerError as rollback_exc:
                result.errors.append(f"ROLLBACK after the failed injection also failed: {rollback_exc}")
                log.error("rollback after failed injection also failed: %s", rollback_exc)
                return None  # keep the journal so `recover` can retry
            self.journal.clear()
            return None

        if not outcome.ok:
            # Refused before touching anything (for example the target was already stopped).
            result.verdict = "failed"
            result.errors.append(f"injection did not happen: {outcome.detail}")
            self.journal.clear()
            log.error("injection failed: %s", outcome.detail)
            return None

        entry.pre_state = outcome.pre_state
        self.journal.write(entry)
        result.injected_at = datetime.now().astimezone()
        log.info("injected: %s", outcome.detail)
        return outcome

    def _watch(self, plan: ValidatedPlan, watchdog: Watchdog, result: ExperimentResult) -> None:
        """Poll telemetry until the duration elapses, the watchdog aborts or someone stops us."""
        deadline = self.clock() + plan.duration_s
        result.verdict = "completed"
        while self.clock() < deadline:
            if self.killswitch.engaged():
                result.verdict = "stopped"
                result.abort_reason = f"kill switch: {self.killswitch.reason()}"
                log.warning("kill switch engaged - rolling back")
                return

            sample, age = self._latest()
            if sample is None:
                result.verdict = "aborted"
                result.abort_reason = "telemetry became unreadable - stopping to avoid running blind"
                log.warning(result.abort_reason)
                return

            decision = watchdog.check(sample, age)
            if decision.abort:
                result.verdict = "aborted"
                result.abort_reason = decision.reason
                log.warning("watchdog abort: %s", decision.reason)
                return
            if decision.reason and not decision.ignored:
                log.info("watchdog: %s", decision.reason)

            remaining = deadline - self.clock()
            self.sleep(min(self.poll_s, max(0.0, remaining)))

        if watchdog.first_degraded_at is None:
            result.observations.append(
                "no probe failure was observed while the failure was in place - the application "
                "tolerated it (or the probes do not cover the affected path)"
            )
        result.detected_at = watchdog.first_degraded_at

    def _rollback(self, plan: ValidatedPlan, action: FailureAction, result: ExperimentResult) -> None:
        try:
            container = self.docker.get(plan.container_name, self.config.compose_project)
            pre = (self.journal.read().pre_state if self.journal.read() else None) or {}
            if pre.get("restart_count") is not None:
                result.restart_count_delta = container.restart_count - int(pre["restart_count"])
            result.self_healed = bool(
                container.running and not container.paused and plan.experiment.action == "kill"
            )
        except DockerError as exc:
            result.errors.append(f"could not inspect the container before rollback: {exc}")

        try:
            outcome = action.rollback(plan.container_name)
            result.rolled_back_at = datetime.now().astimezone()
            result.observations.append(f"rollback: {outcome.detail}")
            log.info("rolled back: %s", outcome.detail)
            self.journal.clear()
        except DockerError as exc:
            result.errors.append(f"ROLLBACK FAILED: {exc}")
            log.error("ROLLBACK FAILED: %s - the journal is kept; run `orchestrator recover`", exc)

    def _wait_for_recovery(self, result: ExperimentResult) -> None:
        """Measure how long the application takes to look healthy again after rollback."""
        if result.rolled_back_at is None:
            return
        deadline = self.clock() + RECOVERY_TIMEOUT_S
        confirmed = 0
        while self.clock() < deadline:
            sample, _ = self._latest()
            if sample is not None and not sample.get("simulated") and healthy(sample):
                confirmed += 1
                if confirmed >= RECOVERY_CONFIRM_SAMPLES:
                    try:
                        result.recovered_at = datetime.fromisoformat(sample["timestamp"])
                    except (KeyError, ValueError):
                        result.recovered_at = datetime.now().astimezone()
                    return
            else:
                confirmed = 0
            self.sleep(self.poll_s)
        result.observations.append(
            f"the application was still not fully healthy {RECOVERY_TIMEOUT_S:.0f}s after rollback - "
            f"worth reporting as a weakness"
        )

    def _finish(self, result: ExperimentResult, before_start: datetime) -> ExperimentResult:
        """Measure recovery, compute timings, summarise the phases and write the result file."""
        self._wait_for_recovery(result)
        result.ended_at = datetime.now().astimezone()

        # timings
        if result.injected_at and result.detected_at:
            result.detection_seconds = round((result.detected_at - result.injected_at).total_seconds(), 1)
        if result.injected_at and result.rolled_back_at:
            result.impact_seconds = round((result.rolled_back_at - result.injected_at).total_seconds(), 1)
        if result.rolled_back_at and result.recovered_at:
            result.recovery_seconds = round(
                max(0.0, (result.recovered_at - result.rolled_back_at).total_seconds()), 1
            )

        # phases
        if result.injected_at:
            result.before = Phase(**summarise_phase(
                self.telemetry.between(before_start, result.injected_at)))
            end_during = result.rolled_back_at or result.ended_at or result.injected_at
            result.during = Phase(**summarise_phase(
                self.telemetry.between(result.injected_at, end_during)))
            if result.rolled_back_at:
                result.after = Phase(**summarise_phase(
                    self.telemetry.between(result.rolled_back_at,
                                           result.recovered_at or result.ended_at or result.rolled_back_at)))

        path = self._write_result(result)
        log.info("result written to %s", path)
        return result

    # ------------------------------------------------------------------ recovery
    def recover(self) -> str:
        """Undo whatever a crashed run left behind. Safe to call when there is nothing to do."""
        entry = self.journal.read()
        if entry is None:
            self.killswitch.clear()
            return "nothing to recover: no active experiment journal."

        action = build_action(entry.action, self.docker, entry.project, **entry.params)
        try:
            outcome = action.rollback(entry.container)
        except DockerError as exc:
            return (f"recovery FAILED for {entry.action} on {entry.container}: {exc}\n"
                    f"Fix it by hand (for example `docker start {entry.container}` or "
                    f"`docker unpause {entry.container}`), then delete {self.journal.path.name}.")
        self.journal.clear()
        self.killswitch.clear()
        return (f"recovered from experiment {entry.experiment_id} "
                f"({entry.action} on {entry.container}, {entry.age_s():.0f}s ago): {outcome.detail}")


__all__ = ["RECOVERY_CONFIRM_SAMPLES", "RECOVERY_TIMEOUT_S", "ExperimentRunner"]

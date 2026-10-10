"""Decide whether an experiment is allowed to run - before anything touches Docker.

This is the layer that makes an autonomous planner safe to use: on day 2 the LLM may propose any
experiment it likes, and every one of them passes through `validate_plan` first. The rules are
deliberately dumb and deterministic, because a check that an AI could talk its way past is not a
safety control.

Rules enforced here:
  1. the resolved container is on target.yaml's `allowed_containers` list;
  2. the action is one the injector actually implements;
  3. the duration is within target.yaml's cap and the absolute ceiling;
  4. the target is not infrastructure the experiment depends on (the monitor or load generator);
  5. abort thresholds are present and not looser than target.yaml's;
  6. the system is healthy and telemetry is fresh *right now* (pre-flight).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from injector.actions import ACTIONS
from injector.docker_cli import Container
from injector.resolve import TargetError, resolve_target
from schemas import Experiment

from .limits import ABSOLUTE_MAX_EXPERIMENT_S, TargetConfig, Thresholds, as_percent
from .telemetry import TelemetryReader, TelemetryUnavailable, is_degraded

#: Never a valid target: breaking these would blind or distort the experiment itself.
PROTECTED_SUBSTRINGS = ("loadgenerator", "dashboard", "monitor", "prometheus", "grafana")


class PlanRejected(RuntimeError):
    """The experiment is not allowed to run. The message says exactly why."""


@dataclass
class ValidatedPlan:
    """An experiment that passed every check, with everything the runner needs resolved."""

    experiment: Experiment
    container: Container
    thresholds: Thresholds
    duration_s: int
    notes: list[str] = field(default_factory=list)

    @property
    def container_name(self) -> str:
        return self.container.name

    @property
    def service_name(self) -> str | None:
        return self.container.service


def merge_thresholds(experiment: Experiment, config: TargetConfig) -> tuple[Thresholds, list[str]]:
    """Combine the experiment's abort_if with target.yaml, never loosening the configured limits.

    A planner that asks for a 90% error-rate tolerance does not get it: the stricter of the two
    values wins, and the clamp is reported so the behaviour is visible in the result file.
    """
    notes: list[str] = []
    base = config.thresholds
    abort = experiment.abort_if

    error_rate = as_percent(abort.error_rate, base.error_rate_pct)
    if error_rate > base.error_rate_pct:
        notes.append(
            f"experiment error_rate {error_rate:.1f}% is looser than target.yaml "
            f"({base.error_rate_pct:.1f}%); using the stricter limit"
        )
        error_rate = base.error_rate_pct

    p95 = float(abort.p95_ms) if abort.p95_ms is not None else base.p95_ms
    if p95 > base.p95_ms:
        notes.append(
            f"experiment p95_ms {p95:.0f} is looser than target.yaml ({base.p95_ms:.0f}); "
            f"using the stricter limit"
        )
        p95 = base.p95_ms

    collateral = (abort.max_collateral_unready
                  if abort.max_collateral_unready is not None else base.max_collateral_unready)
    collateral = min(int(collateral), base.max_collateral_unready)

    return Thresholds(error_rate_pct=error_rate, p95_ms=p95, max_collateral_unready=collateral), notes


def validate_plan(
    experiment: Experiment,
    config: TargetConfig,
    containers: list[Container],
    telemetry: TelemetryReader | None = None,
    skip_health_check: bool = False,
) -> ValidatedPlan:
    """Raise PlanRejected unless every rule passes. Returns the resolved plan."""
    notes: list[str] = []

    # 2. known action (the schema also checks this; kept so dict-built plans are covered too)
    if experiment.action not in ACTIONS:
        raise PlanRejected(f"unknown action {experiment.action!r}; available: {', '.join(sorted(ACTIONS))}")

    # resolve the target to exactly one container
    try:
        container = resolve_target(containers, experiment.target)
    except TargetError as exc:
        raise PlanRejected(str(exc)) from exc

    # 4. protected infrastructure
    lowered = container.name.lower()
    for marker in PROTECTED_SUBSTRINGS:
        if marker in lowered:
            raise PlanRejected(
                f"{container.name} is protected infrastructure ({marker}); "
                f"breaking it would distort or blind the experiment"
            )

    # 1. allow-list
    if not config.allowed_containers:
        raise PlanRejected(
            "target.yaml has an empty allowed_containers list. Fill it with the names from "
            "`docker ps` before running experiments."
        )
    if container.name not in config.allowed_containers:
        allowed = "\n  ".join(sorted(config.allowed_containers))
        raise PlanRejected(
            f"{container.name} is not in target.yaml allowed_containers. Allowed:\n  {allowed}"
        )

    # 3. duration
    duration = int(experiment.duration_s)
    cap = config.effective_max_seconds
    if duration > cap:
        raise PlanRejected(
            f"duration_s {duration} exceeds the cap of {cap}s "
            f"(target.yaml max_experiment_seconds={config.max_experiment_seconds}, "
            f"absolute ceiling {ABSOLUTE_MAX_EXPERIMENT_S}s)"
        )

    # target must be running, or there is no failure to inject
    if not container.running:
        raise PlanRejected(
            f"{container.name} is not running (status {container.status}); nothing to break"
        )
    if container.paused:
        raise PlanRejected(f"{container.name} is already paused; roll back the previous experiment first")

    # 5. thresholds
    thresholds, threshold_notes = merge_thresholds(experiment, config)
    notes.extend(threshold_notes)

    # 6. pre-flight health
    if not skip_health_check:
        if telemetry is None:
            raise PlanRejected("no telemetry reader supplied; cannot confirm the system is healthy")
        try:
            sample = telemetry.require_fresh()
        except TelemetryUnavailable as exc:
            raise PlanRejected(str(exc)) from exc
        summary = sample.get("summary") or {}
        if is_degraded(sample):
            raise PlanRejected(
                f"the system is already degraded (error rate {summary.get('error_rate_pct')}%); "
                f"wait for it to recover so the experiment measures one cause"
            )
        not_ready = [n for n in (summary.get("not_ready") or []) if n != container.name]
        if not_ready:
            raise PlanRejected(
                f"these containers are not ready before the experiment starts: {', '.join(not_ready)}"
            )
        p95 = float(summary.get("p95_latency_ms") or 0.0)
        if p95 > thresholds.p95_ms:
            raise PlanRejected(
                f"p95 latency is already {p95:.0f}ms, above the abort limit "
                f"({thresholds.p95_ms:.0f}ms); the watchdog would abort immediately"
            )

    if not container.self_healing and experiment.action == "kill":
        notes.append(
            f"{container.name} has restart policy {container.restart_policy!r}, so Docker will not "
            f"restart it by itself; rollback will start it explicitly"
        )

    return ValidatedPlan(
        experiment=experiment,
        container=container,
        thresholds=thresholds,
        duration_s=duration,
        notes=notes,
    )


def explain_plan(plan: ValidatedPlan) -> str:
    """Human-readable summary, used by --dry-run and printed before every real run."""
    exp = plan.experiment
    lines = [
        f"experiment    {exp.id}",
        f"action        {exp.action}  ({ACTIONS[exp.action].description})",
        f"target        {exp.target} -> container {plan.container_name}"
        + (f" (service {plan.service_name})" if plan.service_name else ""),
        f"duration      {plan.duration_s}s",
        f"abort if      error rate > {plan.thresholds.error_rate_pct:.1f}%"
        f" | p95 > {plan.thresholds.p95_ms:.0f}ms"
        f" | > {plan.thresholds.max_collateral_unready} other container(s) unready",
        f"restart policy {plan.container.restart_policy}"
        + (" (Docker should self-heal a crash)" if plan.container.self_healing else ""),
    ]
    if exp.reason:
        lines.append(f"reason        {exp.reason}")
    for note in plan.notes:
        lines.append(f"note          {note}")
    return "\n".join(lines)


__all__ = ["PlanRejected", "ValidatedPlan", "explain_plan", "merge_thresholds", "validate_plan"]

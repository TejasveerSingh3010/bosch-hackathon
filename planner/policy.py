"""The pre-execution policy check: independent, deterministic, and run before every injection.

This is the gate that makes an autonomous planner safe. It does not trust the agent: whatever the
agent asks for, the request is re-checked here against fixed rules, and only an accepted plan is
offered for human approval. It rejects, with a specific reason:

  * an unapproved experiment type (not one of the eight catalogue attacks);
  * unsafe parameters (out of the injector's validated ranges);
  * an unknown or protected target, or one not on target.yaml's allow-list;
  * an excessive duration (past target.yaml's cap or the absolute ceiling);
  * an unhealthy or stale cluster (the pre-flight check in `safety.validate_plan`).

The target/duration/protected/health rules are delegated to `safety.validate_plan`, the same code
the runner itself uses, so the agent cannot reach Docker by any path that skips them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from injector.docker_cli import Container
from safety.limits import TargetConfig
from safety.telemetry import TelemetryReader
from safety.validator import PlanRejected, ValidatedPlan, validate_plan

from .catalogue import ATTACK_NAMES, CATALOGUE
from .plan import AttackPlan


@dataclass
class PolicyDecision:
    """Why a plan was accepted or rejected. Recorded in the audit log verbatim."""

    allowed: bool
    experiment_id: str
    reasons: list[str] = field(default_factory=list)  # why it was rejected (empty when allowed)
    notes: list[str] = field(default_factory=list)  # clamps/warnings from the safety layer
    validated: ValidatedPlan | None = None  # resolved plan when allowed

    @property
    def summary(self) -> str:
        if self.allowed:
            tail = f" ({'; '.join(self.notes)})" if self.notes else ""
            return f"ACCEPTED {self.experiment_id}{tail}"
        return f"REJECTED {self.experiment_id}: {'; '.join(self.reasons)}"


def check_plan(
    plan: AttackPlan,
    config: TargetConfig,
    containers: list[Container],
    telemetry: TelemetryReader | None = None,
    skip_health_check: bool = False,
) -> PolicyDecision:
    """Judge one plan. Never raises for a bad plan: the reasons come back on the decision."""
    # 1. approved experiment type
    if plan.action not in CATALOGUE:
        return PolicyDecision(
            False, plan.experiment_id,
            reasons=[f"unapproved experiment type {plan.action!r}; "
                     f"allowed: {', '.join(ATTACK_NAMES)}"],
        )

    # 2. safe parameters + a schema-valid experiment
    try:
        experiment = plan.to_experiment()
    except (ValueError, KeyError) as exc:
        return PolicyDecision(False, plan.experiment_id, reasons=[f"unsafe parameters: {exc}"])

    # 3-5. target, duration, protected infra and pre-flight health (shared with the runner)
    try:
        validated = validate_plan(experiment, config, containers, telemetry, skip_health_check)
    except PlanRejected as exc:
        return PolicyDecision(False, plan.experiment_id, reasons=[str(exc)])

    return PolicyDecision(True, plan.experiment_id, notes=list(validated.notes), validated=validated)


__all__ = ["PolicyDecision", "check_plan"]

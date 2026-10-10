"""Human approval for every fault injection.

The planner never injects a fault that a human has not approved. An approver sees a plan only after
it has passed the policy check, and its decision is recorded in the audit log. The default is an
interactive y/N prompt; `pre_approved` ids (the CLI's `--approve`) stand in for a human who has
already said yes to specific experiments, for non-interactive runs. `AutoApprover` exists only for a
genuinely isolated test environment and must be chosen explicitly; `DenyAll` is the safe default for
dry runs and tests.
"""
from __future__ import annotations

# ruff: noqa: ARG002  (AutoApprover/DenyAll implement the Approver protocol but ignore its arguments)
import sys
from dataclasses import dataclass
from typing import Protocol

from .plan import AttackPlan
from .policy import PolicyDecision


@dataclass
class Approval:
    """A human (or a stand-in) deciding whether one approved-by-policy plan may run."""

    approved: bool
    by: str  # "human", "pre-approved", "auto", "denied", "non-interactive"
    reason: str = ""


class Approver(Protocol):
    def decide(self, plan: AttackPlan, decision: PolicyDecision) -> Approval: ...


def render_plan(plan: AttackPlan, decision: PolicyDecision) -> str:
    """The plan a human sees before approving, in plain text."""
    lines = [
        f"  experiment   {plan.experiment_id}",
        f"  cluster      {plan.cluster}",
        f"  attack       {plan.action} -> {plan.target}",
        f"  params       {plan.params or '(none)'}",
        f"  max duration {plan.max_duration_s}s",
        f"  hypothesis   {plan.hypothesis}",
        f"  expect       {plan.expected_observations}",
        f"  recovery     {plan.recovery_criteria}",
    ]
    if decision.validated is not None:
        t = decision.validated.thresholds
        lines.append(f"  target       container {decision.validated.container_name}")
        lines.append(f"  abort if     error > {t.error_rate_pct:.0f}% | p95 > {t.p95_ms:.0f}ms")
    for note in decision.notes:
        lines.append(f"  note         {note}")
    return "\n".join(lines)


class CLIApprover:
    """Ask a human y/N on the terminal, unless the experiment id was pre-approved on the command line."""

    def __init__(self, pre_approved: set[str] | None = None, assume_no: bool = False) -> None:
        self.pre_approved = set(pre_approved or ())
        #: When stdin is not a terminal and nothing is pre-approved, deny rather than block forever.
        self.assume_no = assume_no

    def decide(self, plan: AttackPlan, decision: PolicyDecision) -> Approval:
        if plan.experiment_id in self.pre_approved:
            return Approval(True, "pre-approved", "approved on the command line (--approve)")
        print("\nPlan awaiting approval:\n" + render_plan(plan, decision))
        if not sys.stdin or not sys.stdin.isatty() or self.assume_no:
            print("  -> no interactive terminal; denying (pass --approve to run non-interactively)")
            return Approval(False, "non-interactive", "no terminal and not pre-approved")
        try:
            answer = input(f"Inject this fault on {plan.target}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return Approval(False, "denied", "input aborted")
        if answer in ("y", "yes"):
            return Approval(True, "human", "approved at the prompt")
        return Approval(False, "denied", f"answered {answer!r}")


class AutoApprover:
    """Approve every policy-accepted plan. Only for an isolated test environment; choose it on purpose."""

    def decide(self, plan: AttackPlan, decision: PolicyDecision) -> Approval:
        return Approval(True, "auto", "auto-approved (isolated test environment)")


class DenyAll:
    """Approve nothing. The default for dry runs and tests."""

    def decide(self, plan: AttackPlan, decision: PolicyDecision) -> Approval:
        return Approval(False, "denied", "approval withheld (dry run / deny-all)")


__all__ = ["Approval", "Approver", "AutoApprover", "CLIApprover", "DenyAll", "render_plan"]

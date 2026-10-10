"""Deterministic safety controls for chaos experiments.

Nothing in this package calls an LLM, and nothing in it can be configured away by a planner. The
orchestrator must pass every experiment through `validate_plan` before the injector runs, and must
poll `Watchdog.check` while a failure is in place.

Pieces:
  * `limits`     - target.yaml loading, unit normalisation, absolute ceilings
  * `validator`  - allow-list, duration cap, protected infrastructure, pre-flight health
  * `watchdog`   - automatic abort on error rate, latency, blast radius or lost telemetry
  * `killswitch` - the manual `results/STOP` file
  * `journal`    - crash recovery, so an injected failure is never left behind
  * `telemetry`  - read-only access to the monitor's output
"""
from .journal import Journal, JournalEntry
from .killswitch import KillSwitch
from .limits import ABSOLUTE_MAX_EXPERIMENT_S, MAX_TELEMETRY_AGE_S, TargetConfig, Thresholds, as_percent
from .telemetry import TelemetryReader, TelemetryUnavailable, healthy, is_degraded, summarise_phase
from .validator import PlanRejected, ValidatedPlan, explain_plan, merge_thresholds, validate_plan
from .watchdog import Decision, Watchdog

__all__ = [
    "ABSOLUTE_MAX_EXPERIMENT_S",
    "MAX_TELEMETRY_AGE_S",
    "Decision",
    "Journal",
    "JournalEntry",
    "KillSwitch",
    "PlanRejected",
    "TargetConfig",
    "TelemetryReader",
    "TelemetryUnavailable",
    "Thresholds",
    "ValidatedPlan",
    "Watchdog",
    "as_percent",
    "explain_plan",
    "healthy",
    "is_degraded",
    "merge_thresholds",
    "summarise_phase",
    "validate_plan",
]

"""Runs experiments end to end: validate, inject, watch, roll back, measure, record.

The orchestrator is the only package that talks to both `injector` and `safety`, and it never
skips the safety layer: `ExperimentRunner.run` calls `safety.validate_plan` before building an
action, and polls `safety.Watchdog` for as long as a failure is in place.

The shared data shapes live in the top-level `schemas` module rather than here, so the safety
layer can validate an `Experiment` without importing the orchestrator (which would be a cycle).

On day 2 the planner proposes `Experiment` objects and the analyst reads `ExperimentResult`
files; neither needs any change here.
"""
from .runner import ExperimentRunner

__all__ = ["ExperimentRunner"]

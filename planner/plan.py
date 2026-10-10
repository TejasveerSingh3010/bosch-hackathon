"""The planner's output: a structured, inspectable plan for one experiment.

This is what Agent 1 produces and a human reads before approving. It carries everything Phase 3
asks for - the experiment id, the target and cluster, the parameters, the hypothesis, the expected
observations, the maximum duration and the recovery criteria - and nothing free-form that could hide
an instruction. `to_experiment()` turns it into the `schemas.Experiment` the safety layer validates,
so the plan and the thing that actually runs can never drift apart.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas import Experiment

from .catalogue import DEFAULT_DURATION_S, get_attack


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


class AttackPlan(BaseModel):
    """One planned experiment, in the planner's own vocabulary (validated, not free-form)."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    cluster: str = Field(description="the system under test, e.g. the Compose app name")
    target: str = Field(min_length=1, description="the service the fault is injected into")
    action: str = Field(description="one of the catalogue attack names")
    params: dict[str, Any] = Field(default_factory=dict)
    hypothesis: str = Field(min_length=1, max_length=500)
    expected_observations: str = Field(min_length=1, max_length=500)
    max_duration_s: int = Field(gt=0, le=600)
    recovery_criteria: str = Field(min_length=1, max_length=500)

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        get_attack(value)  # raises for an unknown attack
        return value

    def to_experiment(self) -> Experiment:
        """The validated `Experiment` the runner executes. Params are re-normalised through the
        catalogue so the experiment carries exactly what the injector expects."""
        attack = get_attack(self.action)
        return Experiment(
            id=self.experiment_id,
            target=self.target,
            action=self.action,
            params=attack.normalise(self.params),
            duration_s=self.max_duration_s,
            reason=self.hypothesis,
        )

    @classmethod
    def build(
        cls,
        action: str,
        target: str,
        cluster: str,
        params: dict[str, Any] | None = None,
        duration_s: int = DEFAULT_DURATION_S,
        hypothesis: str = "",
        experiment_id: str | None = None,
    ) -> AttackPlan:
        """Assemble a plan, filling hypothesis / observations / recovery from the catalogue entry.

        Raises KeyError for an unknown attack and ValueError for bad parameters, before any plan is
        built - the first line of the pre-execution policy check.
        """
        attack = get_attack(action)
        normalised = attack.normalise(params)
        when = datetime.now().strftime("%H%M%S")
        exp_id = experiment_id or _slug(f"{action}-{target}-{when}")
        fill = {"target": target}
        return cls(
            experiment_id=exp_id,
            cluster=cluster,
            target=target,
            action=action,
            params=normalised,
            hypothesis=hypothesis or attack.hypothesis.format(**fill),  # caller text used verbatim
            expected_observations=attack.expected_observations.format(**fill),
            max_duration_s=duration_s,
            recovery_criteria=attack.recovery_criteria.format(**fill),
        )


__all__ = ["AttackPlan"]

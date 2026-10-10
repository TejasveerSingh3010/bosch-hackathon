"""Pydantic shapes the three agents read and write.

These are the agent-layer contracts, kept separate from the top-level `schemas.py` (which owns the
engine's `Experiment` / `ExperimentResult`). Every LLM response is validated against one of these,
and every deterministic fallback builds the same shapes, so the two paths are interchangeable and a
downstream agent cannot tell which produced its input.

Design rules that show up throughout:
  * every claim carries `evidence_ids`, so "evidence or silence" (constraint 5) is structural, not
    a matter of discipline;
  * observed facts and inferences are different types (`Observation.kind`), never mixed;
  * a `RankedAttack` embeds a fully valid `Experiment`, so the thing a human approves is exactly the
    thing the runner executes - no translation step where meaning could drift.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas import Experiment

Source = Literal["deterministic", "llm"]
AppState = Literal["healthy", "degraded", "unknown"]
PreflightVerdict = Literal["allow", "refuse"]
ClaimKind = Literal["observed", "inferred"]

#: The fixed vocabulary the advisor must classify every failure into (Agent 3 spec).
RootCause = Literal[
    "single_point_of_failure",
    "no_health_check",
    "no_timeout_or_retry",
    "no_resource_limits",
    "no_replica",
    "no_graceful_degradation",
    "unbounded_load",
    "slow_detection",
    "slow_recovery",
]
#: Where a fix can live. The upstream images are pinned to v0.10.7 and unbuildable here, so a fix is
#: either a Compose/config change or an explicit "this needs an upstream code change".
FixScope = Literal["compose", "config", "upstream_code"]
Effort = Literal["S", "M", "L"]
Risk = Literal["low", "medium", "high"]


# ----------------------------------------------------------------- Agent 1: Attack Ranker
class PredictedEffects(BaseModel):
    """What Agent 1 expects an attack to do, before it runs. All predictions, never measurements."""

    model_config = ConfigDict(extra="forbid")

    failed_endpoints: list[str] = Field(default_factory=list)
    blast_radius: list[str] = Field(default_factory=list, description="services expected to degrade")
    recovery_difficulty: Literal["easy", "moderate", "hard", "unknown"] = "unknown"


class RankedAttack(BaseModel):
    """One entry in the ranking: a runnable experiment plus why it ranks where it does."""

    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    experiment: Experiment
    predicted_harm: float = Field(ge=0, le=100, description="0..100; the sort key, highest first")
    predicted_effects: PredictedEffects = Field(default_factory=PredictedEffects)
    confidence: float = Field(ge=0, le=1, default=0.5)
    rationale: str = Field(default="", max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list)
    preflight_verdict: PreflightVerdict
    preflight_reason: str = Field(default="")
    prior_run_id: str | None = Field(default=None, description="an earlier results/<id>.json, if any")


class Ranking(BaseModel):
    """`results/ranking.json`: the whole catalogue, sorted by predicted harm, highest first."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    source: Source = "deterministic"
    model: str | None = None
    app_state: AppState = "unknown"
    attacks: list[RankedAttack] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------- Agent 2: Reporter
class EvidenceRef(BaseModel):
    """One item in the evidence pack, as it appears in the report's index."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="E1, E2, ... - referenced by every claim")
    source: str = Field(description="result | telemetry | docker_event | logs | timeline | graph | baseline")
    at: str | None = Field(default=None, description="ISO timestamp, when the item has one")
    summary: str


class Observation(BaseModel):
    """A single statement, labelled as a measured fact or an inference, with its evidence."""

    model_config = ConfigDict(extra="forbid")

    statement: str
    kind: ClaimKind
    evidence_ids: list[str] = Field(default_factory=list)


class FailureOrigin(BaseModel):
    """The first component to degrade. `is_injected_target` says whether it is the thing we broke."""

    model_config = ConfigDict(extra="forbid")

    component: str | None = None
    at: str | None = None
    is_injected_target: bool | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""


class PropagationStep(BaseModel):
    """One hop in the failure spreading out: target -> its callers -> the pages that broke."""

    model_config = ConfigDict(extra="forbid")

    component: str
    role: Literal["target", "caller", "page", "service"]
    degraded_at: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Impact(BaseModel):
    """What users and the system saw, each number tied to its evidence and to the baseline."""

    model_config = ConfigDict(extra="forbid")

    failed_endpoints: list[str] = Field(default_factory=list)
    error_types: dict[str, int] = Field(default_factory=dict)
    peak_error_rate_pct: float | None = None
    peak_p95_ms: float | None = None
    baseline_p95_ms: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Timings(BaseModel):
    """Detection and recovery, and whether Docker's restart policy did the recovering."""

    model_config = ConfigDict(extra="forbid")

    detection_seconds: float | None = None
    recovery_seconds: float | None = None
    self_healed: bool | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Report(BaseModel):
    """`reports/<id>.json`: the structured failure report Agent 3 reads."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    source: Source = "deterministic"
    verdict: str = "unknown"
    action: str | None = None
    target: str | None = None
    service: str | None = None
    summary: str = ""

    failure_origin: FailureOrigin = Field(default_factory=FailureOrigin)
    propagation: list[PropagationStep] = Field(default_factory=list)
    impact: Impact = Field(default_factory=Impact)
    timings: Timings = Field(default_factory=Timings)

    what_worked: list[Observation] = Field(default_factory=list)
    anomalies: list[Observation] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)

    evidence: list[EvidenceRef] = Field(default_factory=list)


# ----------------------------------------------------------------- Agent 3: Fix Recommender
class Verification(BaseModel):
    """How to prove a fix worked: which attack to re-run and what result would count as success."""

    model_config = ConfigDict(extra="forbid")

    attack: Experiment | None = None
    attack_summary: str = Field(default="", description="human text when no full Experiment is given")
    success_criteria: str = ""


class Recommendation(BaseModel):
    """One prioritised, deduplicated fix. Recommendation only - nothing here is ever applied."""

    model_config = ConfigDict(extra="forbid")

    id: str
    priority: int = Field(ge=1, description="1 = do first; ordered by user impact removed per effort")
    problem: str
    root_cause: RootCause
    fix: str
    scope: FixScope = "compose"
    expected_effect: str = ""
    metric: str = Field(default="", description="the telemetry number the fix should move")
    effort: Effort = "M"
    risk: Risk = "medium"
    confidence: float = Field(ge=0, le=1, default=0.5)
    needs_more_evidence: bool = False
    verification: Verification = Field(default_factory=Verification)
    report_ids: list[str] = Field(default_factory=list, description="every report this fix covers")
    evidence_refs: list[str] = Field(default_factory=list)


class Praise(BaseModel):
    """Something that held up and must not be touched, kept short and evidenced."""

    model_config = ConfigDict(extra="forbid")

    statement: str
    evidence_refs: list[str] = Field(default_factory=list)


class Recommendations(BaseModel):
    """`reports/recommendations.json`: the advisor's whole output."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    source: Source = "deterministic"
    model: str | None = None
    recommendations: list[Recommendation] = Field(default_factory=list)
    praise: list[Praise] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


__all__ = [
    "AppState",
    "ClaimKind",
    "Effort",
    "EvidenceRef",
    "Experiment",
    "FailureOrigin",
    "FixScope",
    "Impact",
    "Observation",
    "Praise",
    "PredictedEffects",
    "PreflightVerdict",
    "PropagationStep",
    "RankedAttack",
    "Ranking",
    "Recommendation",
    "Recommendations",
    "Report",
    "Risk",
    "RootCause",
    "Source",
    "Timings",
    "Verification",
]

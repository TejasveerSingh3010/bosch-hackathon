"""Shapes for remediation proposals and their verification.

`ProposalDraft` is what the LLM fills in (via structured outputs) or a human writes by hand: a
Compose configuration change. `CodeProposalDraft` is its counterpart for a weakness only code can
fix: exact edits to the pinned upstream source. Neither carries numeric or length constraints:
structured outputs cannot enforce them, so every rule lives in `remediation.policy`, which applies
the same checks to both authors.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas import ActionName, Experiment

#: The only Compose settings a proposal may change. Each one is reversible by recreating the
#: container from the original Compose file, and each maps to a measurable failure mode.
Setting = Literal["restart", "cpus", "mem_limit", "mem_reservation", "environment"]

#: Outcome metrics, all "lower is better". `writes_lost` comes from the cart consistency probe.
Metric = Literal[
    "during_error_rate_mean",
    "during_error_rate_max",
    "during_p95_max",
    "recovery_seconds",
    "failed_endpoint_count",
    "writes_lost",
]
METRICS: tuple[str, ...] = Metric.__args__  # type: ignore[attr-defined]

ProposalVerdict = Literal["effective", "ineffective", "inconclusive", "regressed"]


class ConfigChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(description="Compose service name, e.g. 'frontend'")
    setting: Setting
    value: str = Field(description="New value. For 'environment' use KEY=value")


class Evidence(BaseModel):
    """A claim tied to a recorded number, so a reviewer (and the policy check) can look it up."""

    model_config = ConfigDict(extra="forbid")

    result_id: str = Field(description="Experiment id; the record is results/<result_id>.json")
    field: str = Field(description="Dotted path inside that record, e.g. 'during.error_rate_pct_max'")
    observed: str = Field(description="The value at that path, copied exactly")
    interpretation: str


class SuccessCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: Metric
    min_improvement_pct: float = Field(description="Required median reduction, percent (5-100)")


class ValidationExperiment(BaseModel):
    """The fault to replay before and after the fix. Turned into a `schemas.Experiment`."""

    model_config = ConfigDict(extra="forbid")

    target: str
    action: ActionName
    duration_s: int
    rps: int | None = Field(default=None, description="spike only")
    users: int | None = Field(default=None, description="spike only")
    cpus: float | None = Field(default=None, description="throttle only: the CPU limit to impose")
    repeats: int = Field(default=3, description="runs per arm (2-5)")

    def to_experiment(self, experiment_id: str, reason: str) -> Experiment:
        params: dict[str, Any] = {}
        if self.action == "spike":
            params = {"rps": self.rps, "users": self.users, "time": self.duration_s}
        elif self.action == "throttle":
            params = {"cpus": self.cpus}
        return Experiment(
            id=experiment_id,
            target=self.target,
            action=self.action,
            duration_s=self.duration_s,
            params=params,
            reason=reason[:500],
        )


class ProposalDraft(BaseModel):
    """What the agent (or a human) proposes. Every field is required reading for the reviewer."""

    model_config = ConfigDict(extra="forbid")

    remediable: bool = Field(description="False if no allowed change can plausibly help")
    declined_reason: str = Field(default="", description="Why not, when remediable is false")
    diagnosed_problem: str
    evidence: list[Evidence]
    changes: list[ConfigChange]
    expected_benefit: str
    success_criteria: list[SuccessCriterion]
    side_effects: list[str]
    rollback_procedure: str
    validation_experiment: ValidationExperiment


class Proposal(ProposalDraft):
    id: str
    author: str = Field(description="'llm:<model>' or 'human'")
    created_at: datetime
    source_results: list[str] = Field(default_factory=list)
    policy_problems: list[str] = Field(default_factory=list)
    trace_id: str | None = Field(default=None, description="Langfuse trace of the LLM call that wrote it")


class CodeEdit(BaseModel):
    """Replace one exact snippet of the pinned upstream source with new code."""

    model_config = ConfigDict(extra="forbid")

    service: str = Field(description="Service whose source is edited, e.g. 'frontend'")
    file: str = Field(description="Path inside that service's source directory, e.g. 'rpc.go'")
    function: str = Field(
        default="",
        description="Top-level function containing the snippet, e.g. 'homeHandler'; '' for file level",
    )
    original: str = Field(description="Exact text to replace, copied verbatim; must occur exactly once")
    replacement: str = Field(description="The new text")
    purpose: str = Field(description="What this edit changes and why")


class CodeProposalDraft(BaseModel):
    """A code-change recommendation for a weakness no Compose setting can fix."""

    model_config = ConfigDict(extra="forbid")

    remediable: bool = Field(description="False if no small, safe code change can plausibly help")
    declined_reason: str = Field(default="", description="Why not, when remediable is false")
    diagnosed_problem: str
    evidence: list[Evidence]
    edits: list[CodeEdit]
    expected_benefit: str
    success_criteria: list[SuccessCriterion]
    side_effects: list[str]
    validation_experiment: ValidationExperiment


class BuildCheck(BaseModel):
    status: Literal["passed", "failed", "skipped"]
    detail: str = ""


class CodeProposal(CodeProposalDraft):
    id: str
    author: str = Field(description="'llm:<model>' or 'human'")
    created_at: datetime
    source_results: list[str] = Field(default_factory=list)
    responds_to: str = Field(description="The declined config proposal this answers")
    source_commit: str = Field(description="Upstream commit the edits and patch apply to")
    patch: str = Field(default="", description="Unified diff generated from the edits")
    build: BuildCheck | None = None
    policy_problems: list[str] = Field(default_factory=list)
    trace_id: str | None = Field(default=None, description="Langfuse trace of the LLM call that wrote it")
    critique: list[str] = Field(default_factory=list, description="What the review pass found in the draft")
    revised: bool = Field(default=False, description="Rewritten once after the review pass")


class Critique(BaseModel):
    """A second model's reading of a code recommendation. It can only point out problems."""

    model_config = ConfigDict(extra="forbid")

    fixes_diagnosis: bool = Field(description="Does the patch remove the diagnosed cause?")
    issues: list[str] = Field(
        description="Concrete problems, each naming the file and what is wrong; empty if none"
    )


AnyProposal = Proposal | CodeProposal


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reviewer: str
    decided_at: datetime
    note: str = ""
    #: Hashes of exactly what was reviewed. Editing either file afterwards voids the approval.
    proposal_sha256: str
    artifact: str = Field(description="The change file reviewed: override.yml or patch.diff")
    artifact_sha256: str


class RunRecord(BaseModel):
    """One experiment run inside a verification, plus the consistency probe around it."""

    model_config = ConfigDict(extra="forbid")

    arm: Literal["before", "after"]
    experiment_id: str
    verdict: str
    valid: bool
    invalid_reason: str | None = None
    metrics: dict[str, float | None] = Field(default_factory=dict)
    steady_p95_ms: float | None = None
    steady_error_rate_pct: float | None = None
    write_acknowledged: bool | None = None
    write_preserved: bool | None = None


class CriterionOutcome(BaseModel):
    metric: str
    before: list[float]
    after: list[float]
    before_median: float
    after_median: float
    improvement_pct: float
    required_pct: float
    separated: bool = Field(description="every after-run beat every before-run")
    status: Literal["met", "unmet", "not_separated", "not_reproduced", "insufficient_data"]


class Verification(BaseModel):
    proposal_id: str
    verdict: ProposalVerdict
    reasons: list[str] = Field(default_factory=list)
    started_at: datetime
    ended_at: datetime | None = None
    repeats: int
    experiment: Experiment
    runs: list[RunRecord] = Field(default_factory=list)
    criteria: list[CriterionOutcome] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    equivalence_problems: list[str] = Field(default_factory=list)
    resource_cost: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    fix_restored: bool = False

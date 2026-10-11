"""Validated data shapes shared by the injector, safety layer, runner and (on day 2) the planner.

`Experiment` matches docs/experiment.example.json field for field, so a plan written by hand, by
the heuristic planner or by an LLM all go through the same validation. Using pydantic here means a
malformed or hallucinated plan is rejected with a clear message before anything touches Docker.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from injector.actions import ACTIONS, spike_params, throttle_params

ActionName = Literal[
    "kill", "stop", "pause", "spike", "throttle",
    "memory_limit", "network_partition", "network_delay", "flapping",
]
Verdict = Literal["completed", "aborted", "stopped", "failed", "dry_run"]


class AbortIf(BaseModel):
    """Per-experiment abort limits. Omitted fields fall back to target.yaml.

    `error_rate` is a fraction (0.2 = 20%), matching docs/experiment.example.json;
    safety.limits.as_percent converts it before the watchdog uses it.
    """

    model_config = ConfigDict(extra="forbid")

    error_rate: float | None = Field(default=None, ge=0, description="fraction, e.g. 0.2 for 20%")
    p95_ms: float | None = Field(default=None, gt=0)
    max_collateral_unready: int | None = Field(default=None, ge=0)


class Experiment(BaseModel):
    """One controlled failure, start to finish."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    target: str = Field(min_length=1, description="Compose service or container name")
    action: ActionName
    params: dict[str, Any] = Field(default_factory=dict)
    duration_s: int = Field(gt=0, le=600)
    abort_if: AbortIf = Field(default_factory=AbortIf)
    rollback: str = Field(default="auto", description="Declared intent; the action class performs it")
    reason: str = Field(default="", max_length=500, description="Why this experiment is worth running")

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in ACTIONS:
            raise ValueError(f"unknown action {value!r}; available: {', '.join(sorted(ACTIONS))}")
        return value

    @model_validator(mode="after")
    def _spike_params_fit_the_approved_window(self) -> Experiment:
        if self.action != "spike":
            return self
        rps, users, hold = spike_params(self.params)
        if hold > self.duration_s:
            raise ValueError("spike params.time cannot be longer than duration_s")
        self.params = {"rps": rps, "users": users, "time": hold}
        return self

    @model_validator(mode="after")
    def _throttle_has_a_cpu_limit(self) -> Experiment:
        if self.action == "throttle":
            self.params = {"cpus": throttle_params(self.params)}
        return self


class TelemetryPoint(BaseModel):
    """One monitor sample, reduced to the numbers the watchdog and report care about."""

    model_config = ConfigDict(extra="ignore")

    timestamp: datetime
    error_rate_pct: float
    p95_latency_ms: float
    mean_latency_ms: float
    ready: int | None = None
    total: int | None = None
    not_ready: list[str] = Field(default_factory=list)
    error_types: dict[str, int] = Field(default_factory=dict)
    failed_endpoints: list[str] = Field(default_factory=list)
    simulated: bool = False

    @classmethod
    def from_sample(cls, sample: dict[str, Any]) -> TelemetryPoint:
        summary = sample.get("summary") or {}
        metrics = sample.get("metrics") or {}
        return cls(
            timestamp=sample["timestamp"],
            error_rate_pct=float(summary.get("error_rate_pct") or 0.0),
            p95_latency_ms=float(summary.get("p95_latency_ms") or 0.0),
            mean_latency_ms=float(summary.get("mean_latency_ms") or 0.0),
            ready=summary.get("ready_pods"),
            total=summary.get("total_pods"),
            not_ready=list(summary.get("not_ready") or []),
            error_types=dict(summary.get("error_types") or {}),
            failed_endpoints=sorted(name for name, m in metrics.items() if not m.get("success", True)),
            simulated=bool(sample.get("simulated")),
        )


class Phase(BaseModel):
    """Aggregated telemetry for one phase of an experiment (before / during / after)."""

    model_config = ConfigDict(extra="forbid")

    samples: int = 0
    seconds: float = 0.0
    error_rate_pct_mean: float = 0.0
    error_rate_pct_max: float = 0.0
    p95_ms_mean: float = 0.0
    p95_ms_max: float = 0.0
    failed_endpoints: list[str] = Field(default_factory=list)
    error_types: dict[str, int] = Field(default_factory=dict)


class ExperimentResult(BaseModel):
    """The artifact the reporter (Agent 2) and remediation agent read. One file per experiment in results/."""

    model_config = ConfigDict(extra="forbid")

    experiment: Experiment
    verdict: Verdict
    container: str | None = None
    service: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None

    injected_at: datetime | None = None
    detected_at: datetime | None = None
    rolled_back_at: datetime | None = None
    recovered_at: datetime | None = None

    detection_seconds: float | None = Field(default=None, description="injection -> first degraded sample")
    impact_seconds: float | None = Field(default=None, description="how long the failure was in place")
    recovery_seconds: float | None = Field(default=None, description="rollback -> healthy again")

    abort_reason: str | None = None
    self_healed: bool | None = Field(default=None, description="Docker restarted it before rollback")
    restart_count_delta: int | None = None

    before: Phase = Field(default_factory=Phase)
    during: Phase = Field(default_factory=Phase)
    after: Phase = Field(default_factory=Phase)

    thresholds_used: dict[str, float] = Field(default_factory=dict)
    baseline: dict[str, Any] | None = None
    observations: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

"""Shapes for Agent 2: the evidence pack it reads and the report it writes.

`EvidencePack` is built by code (reporter/evidence.py), never by the model. `Analysis` is the part
a model (today the stub, later an LLM) fills in, and every claim in it must cite evidence ids that
exist in the pack. `Report` is what lands in reports/<id>.json for Agent 3.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Basis = Literal["observed", "inferred", "unknown"]
Severity = Literal["none", "minor", "major", "critical"]
Source = Literal["runner", "telemetry", "docker", "logs"]


class TimelineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # E1, E2, ... in time order
    t: datetime | None = None
    source: Source
    event: str
    key: str = ""  # stable lookup key, e.g. "page_fail:Home"


class LogFinding(BaseModel):
    """One kind of error found in one container's logs, deduplicated."""

    model_config = ConfigDict(extra="forbid")

    container: str
    service: str | None = None
    kind: str  # connection_refused, timeout, deadline_exceeded, ...
    count: int
    first_at: datetime | None = None
    first_line: str = ""
    last_line: str = ""


class EvidencePack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["experiment", "incident"]
    subject: str  # experiment id or incident id
    window_start: datetime | None = None
    window_end: datetime | None = None
    experiment: dict[str, Any] | None = None  # the ExperimentResult facts that matter
    timeline: list[TimelineItem] = Field(default_factory=list)
    refs: dict[str, str] = Field(default_factory=dict)  # lookup key -> evidence id
    log_findings: list[LogFinding] = Field(default_factory=list)
    graph: dict[str, Any] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)  # what evidence is missing, and why


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    evidence: list[str] = Field(default_factory=list)
    basis: Basis = "observed"


class Origin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str
    first_degraded_at: datetime | None = None
    basis: Basis = "observed"
    evidence: list[str] = Field(default_factory=list)
    note: str = ""


class Hop(BaseModel):
    """One step of the propagation path: origin -> services -> pages."""

    model_config = ConfigDict(extra="forbid")

    component: str
    kind: Literal["origin", "service", "page"]
    at: datetime | None = None
    seconds_after_origin: float | None = None
    evidence: list[str] = Field(default_factory=list)
    note: str = ""


class Analysis(BaseModel):
    """What the narrator (stub now, LLM later) produces from the evidence pack."""

    model_config = ConfigDict(extra="forbid")

    headline: str
    severity: Severity
    summary: str
    failure_point: str = Field(description="where exactly it failed, in one or two sentences")
    origin: Origin | None = None
    propagation: list[Hop] = Field(default_factory=list)
    what_worked: list[Claim] = Field(default_factory=list)
    anomalies: list[Claim] = Field(default_factory=list)


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["experiment", "incident"]
    generated_at: datetime
    narrator: str  # "stub" today, "llm" later
    title: str
    verdict: str | None = None
    target: str | None = None
    action: str | None = None
    timings: dict[str, float | None] = Field(default_factory=dict)
    phases: dict[str, Any] = Field(default_factory=dict)
    analysis: Analysis
    evidence: EvidencePack

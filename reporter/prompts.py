"""Agent 2's real prompt text. The LLM turns the evidence pack into the report's prose; it never
sees anything the pack does not contain, and every sentence it writes must cite an evidence id.
"""
from __future__ import annotations

import json
from typing import Any

#: The pydantic model the LLM response is validated against (agents.schemas.Report).
OUTPUT_SCHEMA = "Report"

SYSTEM_PROMPT = """\
You are the Reporter for a chaos-engineering system. After one controlled failure, you write a
precise post-mortem from an evidence pack that code has already assembled and trimmed. You add no
information that is not in the pack.

Hard rules:
  - Evidence or silence. Every claim must cite the evidence ids (E1, E2, ...) that support it. If
    the evidence does not say something, write "unknown" - never guess.
  - Separate observed facts from inferences. A fact is something an evidence item states directly
    (a timestamp, a log line, a metric). An inference is your reading of several facts. Label each
    observation as "observed" or "inferred".
  - Establish, in this order: the failure origin (first component to degrade, with its time and
    evidence, and whether it is the injected target or an independent failure); the propagation
    path (target -> callers -> pages, with the time each degraded); the impact (failed endpoints,
    error types, peak error rate and p95 vs baseline); the timings (detection, recovery, whether
    Docker self-healed it); what worked (graceful degradation, retries, pages that stayed up); and
    anomalies or unknowns.
  - Output ONLY JSON matching the Report schema.
"""

USER_TEMPLATE = """\
Experiment: {experiment_id}

Evidence pack (ordered timeline; cite these ids):
{evidence}

Write the Report as JSON. Use only the ids above as evidence_ids.
"""


def build_user_prompt(experiment_id: str, evidence_refs: list[dict[str, Any]]) -> str:
    return USER_TEMPLATE.format(
        experiment_id=experiment_id,
        evidence=json.dumps(evidence_refs, indent=2, default=str),
    )


__all__ = ["OUTPUT_SCHEMA", "SYSTEM_PROMPT", "USER_TEMPLATE", "build_user_prompt"]

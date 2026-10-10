"""Agent 1's real prompt text, written now so only the client wiring is left for later.

The LLM ranking stage (stage 2) sees the *features* of each candidate - never raw logs - and returns
a predicted harm score, predicted effects, a confidence and an evidence-citing rationale for each.
Deterministic code keeps the Experiments and the pre-flight verdicts; the model only re-scores. The
output is validated against `LLMRanking` (see ranker.py), and the final artifact is `Ranking` in
agents/schemas.py.
"""
from __future__ import annotations

import json
from typing import Any

#: The pydantic model the LLM response is validated against (lives in planner/ranker.py).
OUTPUT_SCHEMA = "LLMRanking"

SYSTEM_PROMPT = """\
You are the Attack Ranker for a chaos-engineering system running against a local copy of Google's
Online Boutique. You rank candidate failure injections from most to least harmful for the app's
CURRENT state. You never execute anything: a human approves one entry and a deterministic safety
layer runs it.

Definition of HARM, in strict priority order:
  1. user-facing impact - pages or checkout failing, error rate, p95 latency;
  2. breadth - how many services are affected (blast radius);
  3. expected recovery difficulty.

Rules:
  - Score only from the features you are given. You do NOT see raw logs or container internals.
  - Every rationale must cite the evidence ids provided for that candidate (e.g. "graph:redis-cart",
    "result:kill-adservice-1"). If the evidence does not support a claim, do not make it.
  - Prefer attacks that reveal unknowns. An attack already run and survived is less valuable than an
    untested one of similar harm; say so when the evidence shows a prior run.
  - Return a harm score 0-100, predicted failed endpoints, predicted blast radius (service names),
    a confidence 0-1, and the rationale. Do not invent services or endpoints not in the features.
  - Output ONLY JSON matching the schema. No prose outside the JSON.
"""

USER_TEMPLATE = """\
App state: {app_state}

Rank these candidate attacks. For each, use candidate_id EXACTLY as given.

Candidates (JSON):
{candidates}

Return JSON of the form:
{{"entries": [
  {{"candidate_id": "<id>", "predicted_harm": <0-100>,
    "predicted_failed_endpoints": ["Home", ...], "predicted_blast_radius": ["service", ...],
    "confidence": <0-1>, "rationale": "<cites evidence ids>", "evidence_refs": ["graph:...", ...]}}
]}}
"""


def build_user_prompt(app_state: str, candidates: list[dict[str, Any]]) -> str:
    """Render the user prompt from the per-candidate feature payloads."""
    return USER_TEMPLATE.format(
        app_state=app_state,
        candidates=json.dumps(candidates, indent=2, default=str),
    )


__all__ = ["OUTPUT_SCHEMA", "SYSTEM_PROMPT", "USER_TEMPLATE", "build_user_prompt"]

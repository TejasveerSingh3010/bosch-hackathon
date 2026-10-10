"""Shared plumbing for the three agents (planner, reporter, advisor).

This package holds everything the agents have in common and nothing agent-specific:

  * `llm`      - the provider-neutral LLM seam. A `StubClient` is the Phase 1 default; a real
                 client is a documented placeholder that raises `NotConfigured` until a key is set.
                 All structured-output validation, the retry-once-on-error policy and the audit log
                 live here, so every agent gets them for free.
  * `schemas`  - the pydantic shapes the agents read and write: RankedAttack / Ranking (planner),
                 Report (reporter) and Recommendation(s) (advisor).

The agents never call Docker and never touch `safety/`. The planner proposes `schemas.Experiment`
objects that still go through `safety.validate_plan`; the reporter and advisor are read-only.
"""
from .llm import (
    AgentLog,
    LLMClient,
    LLMOutputError,
    NotConfigured,
    RealClient,
    StubClient,
    get_client,
)

__all__ = [
    "AgentLog",
    "LLMClient",
    "LLMOutputError",
    "NotConfigured",
    "RealClient",
    "StubClient",
    "get_client",
]

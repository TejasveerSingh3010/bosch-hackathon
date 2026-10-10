"""The one place an LLM gets plugged in.

Phase 1 ships with `StubClient` only: every agent runs end to end offline, and its "model output"
is produced by a deterministic handler registered with `@stub_handler(task)`. Nothing here makes a
network call and nothing needs an API key.

When the real model is ready, only this file changes:

  1. implement `RealClient.complete_json` (TODO(llm) below) with your provider's SDK;
  2. make `get_client()` return it when LLM_API_KEY is set.

Agents never import a provider SDK. They call `client.complete_json(...)` with the prompt text,
the pydantic schema they expect back, and `payload` (the same evidence as a dict, which only the
stub reads). The real client sends `system` + `user` and ignores `payload`.
"""

# ruff: noqa: ARG002  (the interface passes every argument; the stub and the placeholder ignore some)
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class NotConfigured(RuntimeError):
    """The requested LLM backend has no key, or has not been wired in yet."""


class LLMClient(Protocol):
    name: str

    def complete_json(
        self, *, task: str, system: str, user: str, schema: type[T], payload: dict[str, Any]
    ) -> T: ...


# --------------------------------------------------------------------------- stub
_STUB_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def stub_handler(
    task: str,
) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Callable[[dict[str, Any]], dict[str, Any]]]:
    """Register the deterministic stand-in for one LLM task, e.g. `reporter.analyse`."""

    def register(
        func: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        _STUB_HANDLERS[task] = func
        return func

    return register


class StubClient:
    """Returns hardcoded, rule-based output. The default in Phase 1."""

    name = "stub"

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: type[T],  # noqa: ARG002
        payload: dict[str, Any],
    ) -> T:
        handler = _STUB_HANDLERS.get(task)
        if handler is None:
            raise NotConfigured(f"the stub has no handler for task {task!r}")
        return schema.model_validate(handler(payload))


class RealClient:
    """Placeholder for the real model. Not wired in yet."""

    name = "llm"

    def __init__(self) -> None:
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.provider = os.getenv("LLM_PROVIDER", "")
        self.model = os.getenv("LLM_MODEL", "")
        if not self.api_key:
            raise NotConfigured("LLM_API_KEY is not set")

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: type[T],  # noqa: ARG002
        payload: dict[str, Any],
    ) -> T:
        # TODO(llm): call the provider with `system` and `user`, ask for JSON matching
        # `schema.model_json_schema()`, then `return schema.model_validate_json(text)`.
        # Retry once with the validation error appended, then raise. Ignore `payload`.
        raise NotConfigured(f"RealClient is not wired in yet (task {task!r})")


def get_client() -> LLMClient:
    """Phase 1: always the stub. TODO(llm): return RealClient() when LLM_API_KEY is set."""
    return StubClient()


# --------------------------------------------------------------------------- audit log
def log_call(
    results_dir: Path,
    *,
    agent: str,
    task: str,
    client: str,
    system: str,
    user: str,
    evidence_ids: list[str] | None = None,
) -> None:
    """Append one line to results/agent_log.jsonl. Never raises: logging must not break an agent."""
    entry = {
        "at": datetime.now().astimezone().isoformat(),
        "agent": agent,
        "task": task,
        "client": client,
        "prompt_sha256": hashlib.sha256((system + "\n" + user).encode("utf-8")).hexdigest()[:16],
        "evidence_ids": evidence_ids or [],
    }
    try:
        Path(results_dir).mkdir(parents=True, exist_ok=True)
        with (Path(results_dir) / "agent_log.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


__all__ = ["LLMClient", "NotConfigured", "RealClient", "StubClient", "get_client", "log_call", "stub_handler"]

"""The one place an LLM gets plugged in.

`get_client()` returns `RealClient` (Groq, `REPORTER_MODEL` = openai/gpt-oss-20b, overridable with
LLM_MODEL) when LLM_API_KEY is set in the environment or .env, and `StubClient` otherwise. The stub's
"model output" is produced by a deterministic handler registered with `@stub_handler(task)` and makes
no network call. Callers fall back to it when the real model fails (`LLMError`), so a report is
always written. Tests set LLM_API_KEY to "" so they stay offline.

Agents never import a provider SDK. They call `client.complete_json(...)` with the prompt text,
the pydantic schema they expect back, and `payload` (the same evidence as a dict, which only the
stub reads). The real client sends `system` + `user` and ignores `payload`.
"""

# ruff: noqa: ARG002  (the interface passes every argument; the stub and the placeholder ignore some)
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTER_MODEL = "openai/gpt-oss-20b"
MAX_COMPLETION_TOKENS = 4000
log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The real model could not produce a valid answer; callers fall back to the stub."""


class NotConfigured(LLMError):
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


def _api_key() -> str:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")  # never overrides a variable already set (tests set it to "")
    key = os.getenv("LLM_API_KEY", "")
    return "" if key == "your-key-here" else key


class RealClient:
    """Groq chat completions with a strict JSON schema, sized and traced like the remediation agent.

    A reply that does not match the schema gets one retry with the validation error appended; any
    other failure raises `LLMError`.
    """

    def __init__(self, model: str | None = None, client: Any = None) -> None:
        self.model = model or os.getenv("LLM_MODEL") or REPORTER_MODEL
        self.name = f"llm:{self.model}"
        if client is None:
            key = _api_key()
            if not key:
                raise NotConfigured("LLM_API_KEY is not set")
            import groq

            client = groq.Groq(api_key=key)
        self.client = client
        from remediation.agent import MinuteBudget

        self.budget = MinuteBudget()

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: type[T],
        payload: dict[str, Any],
    ) -> T:
        from agents import tracing
        from remediation.agent import GROQ_TPM, REASONING_EFFORT, estimate_request, wire_schema

        system, wire = wire_schema(schema, system)
        size = estimate_request(system, user, json.dumps(wire))
        if size > GROQ_TPM:
            raise LLMError(f"{task}: the request is about {size} tokens, over Groq's {GROQ_TPM:,} a minute")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        params = {"max_completion_tokens": MAX_COMPLETION_TOKENS, "reasoning_effort": REASONING_EFFORT}
        for attempt in (1, 2):
            self.budget.wait_for(size)
            try:
                with tracing.generation(task, self.model, messages, params) as traced:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        response_format={
                            "type": "json_schema",
                            "json_schema": {"name": task.replace(".", "_"), "schema": wire, "strict": True},
                        },
                        **params,
                    )
                    traced.finish(response)
            except Exception as exc:  # SDK errors: auth, rate limit, connection, schema refusal
                raise LLMError(f"{task}: {self.model} request failed: {exc}") from exc
            usage = getattr(response, "usage", None)
            self.budget.record(int(getattr(usage, "total_tokens", 0) or size))
            choice = response.choices[0]
            text = choice.message.content or ""
            if choice.finish_reason == "length":
                raise LLMError(f"{task}: the answer was cut off at {MAX_COMPLETION_TOKENS} tokens")
            try:
                return schema.model_validate_json(text)
            except ValueError as exc:
                if attempt == 2:
                    raise LLMError(f"{task}: the reply did not match {schema.__name__}: {exc}") from exc
                log.info("%s: invalid reply, retrying once: %s", task, exc)
                fix = f"That JSON is invalid: {exc}. Return corrected JSON only."
                messages = [*messages, {"role": "assistant", "content": text},
                            {"role": "user", "content": fix}]
        raise AssertionError("unreachable")


def get_client(model: str | None = None) -> LLMClient:
    """The real model when LLM_API_KEY is set (environment or .env), else the stub.

    `model` picks the Groq model (the planner asks for openai/gpt-oss-120b); without it the
    reporter's default applies, overridable with LLM_MODEL.
    """
    try:
        return RealClient(model)
    except NotConfigured:
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


__all__ = [
    "LLMClient", "LLMError", "NotConfigured", "RealClient", "StubClient",
    "get_client", "log_call", "stub_handler",
]

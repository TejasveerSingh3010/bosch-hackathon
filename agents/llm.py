"""The LLM seam: one place the real model is wired in later, and the only place it ever will be.

Phase 1 ships with **no real LLM**. The whole three-agent pipeline runs offline because every agent
has a deterministic implementation (see each agent's module); the LLM, when it arrives, replaces one
ranking/writing step behind this interface and never the safety layer.

What this module guarantees, so the agents do not each reinvent it:

  * **A provider-neutral interface.** `LLMClient.generate(system, user, schema)` takes the agent's
    prompt text and a pydantic model and returns a validated instance of that model. Nothing above
    this line knows which provider is in use.
  * **Structured output only.** The raw completion must be JSON that validates against `schema`.
  * **Retry once, then fail loudly.** On a validation error the call is retried a single time with
    the error appended to the prompt; a second failure raises `LLMOutputError` and the raw text is
    saved to `results/` so a human can see exactly what came back.
  * **An audit trail.** Every call is appended to `results/agent_log.jsonl` with the agent name,
    model, a hash of the prompt, the evidence ids that went in and the output that came out.
  * **Lazy configuration.** `LLM_API_KEY`, `LLM_PROVIDER` and `LLM_MODEL` are read from the
    environment / `.env` only when a client is actually built, never at import time, so importing an
    agent never requires a key.

The seam for the real integration is deliberately small: `RealClient._complete` is the single
`TODO(llm)` method to fill in. Everything else - validation, retry, logging - already works.
"""
from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_DIR = REPO_ROOT / "results"
ENV_FILE = REPO_ROOT / ".env"

#: Low temperature and a capped token budget keep the agents reproducible and cheap (constraint 7).
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 1536

T = TypeVar("T", bound=BaseModel)


class NotConfigured(RuntimeError):
    """A real LLM call was attempted but no provider is wired in yet (the Phase 1 state).

    This is never raised during normal Phase 1 operation: with no key set, `get_client` returns
    None and the agents take their deterministic path. It is raised only if someone sets a key
    before the real client is implemented, which is exactly the seam left for later.
    """


class LLMOutputError(RuntimeError):
    """The model's output did not validate even after one retry. The raw text has been saved."""


def _load_env_file(path: Path | None = None) -> dict[str, str]:
    """Minimal `.env` reader, used lazily. Avoids importing python-dotenv at module import time."""
    values: dict[str, str] = {}
    try:
        text = Path(path or ENV_FILE).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env(name: str, env_file: dict[str, str]) -> str:
    """Process environment wins over `.env`, matching how python-dotenv is normally configured."""
    return (os.getenv(name) or env_file.get(name) or "").strip()


def _prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()[:16]


class AgentLog:
    """Append-only audit of every LLM call, one JSON object per line (constraint 7)."""

    def __init__(self, results_dir: Path | str = DEFAULT_RESULTS_DIR) -> None:
        self.path = Path(results_dir) / "agent_log.jsonl"

    def record(
        self,
        *,
        agent: str,
        model: str,
        prompt_hash: str,
        evidence_ids: Sequence[str],
        output: Any,
        error: str | None = None,
    ) -> None:
        entry = {
            "at": datetime.now().astimezone().isoformat(),
            "agent": agent,
            "model": model,
            "prompt_hash": prompt_hash,
            "evidence_ids": list(evidence_ids),
            "output": output,
        }
        if error:
            entry["error"] = error
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass  # the audit log must never break an agent


class LLMClient(ABC):
    """Provider-neutral structured-output client.

    Subclasses implement only `_complete` (system + user prompt -> raw text). This base class owns
    JSON parsing, pydantic validation, the single retry, the raw-output dump and the audit log, so
    the behaviour is identical whatever provider is plugged in later.
    """

    model: str = "unset"

    def __init__(self, results_dir: Path | str = DEFAULT_RESULTS_DIR) -> None:
        self.results_dir = Path(results_dir)
        self.log = AgentLog(self.results_dir)

    @abstractmethod
    def _complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> str:
        """Return the model's raw text for one prompt. The only method a real provider must add."""
        raise NotImplementedError

    def generate(
        self,
        *,
        agent: str,
        system: str,
        user: str,
        schema: type[T],
        evidence_ids: Sequence[str] = (),
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> T:
        """Run one structured call: complete -> validate -> (retry once) -> validate, then log.

        Raises `LLMOutputError` if the output still does not validate after the retry; the raw text
        is written to `results/<agent>.raw.txt` so the failure is inspectable, never swallowed.
        """
        prompt_hash = _prompt_hash(system, user)
        raw = self._complete(system, user, temperature=temperature, max_tokens=max_tokens)
        try:
            parsed = schema.model_validate_json(_json_slice(raw))
        except (ValidationError, ValueError) as first_error:
            retry_user = (
                f"{user}\n\nYour previous response did not validate against the required schema:\n"
                f"{first_error}\n\nReturn only JSON that satisfies the schema."
            )
            raw = self._complete(system, retry_user, temperature=temperature, max_tokens=max_tokens)
            try:
                parsed = schema.model_validate_json(_json_slice(raw))
            except (ValidationError, ValueError) as second_error:
                self._dump_raw(agent, raw)
                self.log.record(
                    agent=agent, model=self.model, prompt_hash=prompt_hash,
                    evidence_ids=evidence_ids, output=None, error=str(second_error),
                )
                raise LLMOutputError(
                    f"{agent}: LLM output failed validation twice; raw output saved to "
                    f"{self._raw_path(agent)}. Last error: {second_error}"
                ) from second_error

        self.log.record(
            agent=agent, model=self.model, prompt_hash=prompt_hash,
            evidence_ids=evidence_ids, output=parsed.model_dump(mode="json"),
        )
        return parsed

    def _raw_path(self, agent: str) -> Path:
        return self.results_dir / f"{agent}.raw.txt"

    def _dump_raw(self, agent: str, raw: str) -> None:
        try:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            self._raw_path(agent).write_text(raw, encoding="utf-8")
        except OSError:
            pass


def _json_slice(raw: str) -> str:
    """Trim a completion to the outermost JSON object, tolerating code fences or stray prose.

    Models wrap JSON in ```json fences or add a sentence around it. We never *repair* JSON (that
    would hide a malformed response), only take the first `{` through the last `}` so the real
    validation runs on the real payload.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.lstrip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


class StubClient(LLMClient):
    """The Phase 1 default and the test double. Returns scripted output; never touches a network.

    Two ways to drive it:
      * `StubClient(responder=fn)` - `fn(system, user) -> str` computes the raw text. This is how a
        test exercises the validation/retry path deterministically.
      * `StubClient(scripted=[...])` - return these raw strings in order, one per `_complete` call,
        which is handy for testing the retry (give a bad string then a good one).

    With neither, it returns `"{}"`, which is a *deliberate* non-answer: the agents never rely on a
    stub to produce real analysis - that is what their deterministic implementations are for.
    """

    model = "stub"

    def __init__(
        self,
        responder: Callable[[str, str], str] | None = None,
        scripted: Sequence[str] | None = None,
        results_dir: Path | str = DEFAULT_RESULTS_DIR,
    ) -> None:
        super().__init__(results_dir)
        self._responder = responder
        self._scripted = list(scripted) if scripted is not None else None
        self.calls: list[tuple[str, str]] = []

    def _complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> str:  # noqa: ARG002
        self.calls.append((system, user))
        if self._scripted is not None:
            return self._scripted.pop(0) if self._scripted else "{}"
        if self._responder is not None:
            return self._responder(system, user)
        return "{}"


class RealClient(LLMClient):
    """Placeholder for the real provider. Everything is wired except the one network call.

    When the LLM is added, implement `_complete` against the chosen provider's SDK using
    `self.provider`, `self.model` and `self.api_key` (all read from `.env`), and delete the
    `raise NotConfigured`. No other code changes: validation, retry, logging and the agent call
    sites already treat this exactly like the stub.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        results_dir: Path | str = DEFAULT_RESULTS_DIR,
    ) -> None:
        super().__init__(results_dir)
        self.provider = provider or "unset"
        self.model = model or "unset"
        self.api_key = api_key

    def _complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> str:  # noqa: ARG002
        # TODO(llm): call self.provider's chat/messages API with system+user, temperature and
        # max_tokens, and return the raw assistant text. This is the only integration point.
        raise NotConfigured(
            f"the real LLM client for provider {self.provider!r} is not implemented yet "
            f"(Phase 1 ships with the stub only). Unset LLM_API_KEY to use the deterministic path."
        )


def get_client(results_dir: Path | str = DEFAULT_RESULTS_DIR) -> LLMClient | None:
    """Build the configured client, or return None to mean "use the deterministic fallback".

    Configuration is read lazily here, never at import. In the Phase 1 state - no `LLM_API_KEY` -
    this returns None and the agents rank/report/recommend deterministically. Once a key is set it
    returns a `RealClient`, whose first call raises `NotConfigured` until the real provider is
    wired in. That deterministic path stays as the permanent fallback if the LLM is unreachable, so
    a demo never blocks (constraint 8).
    """
    env_file = _load_env_file()
    api_key = _env("LLM_API_KEY", env_file)
    if not api_key:
        return None
    provider = _env("LLM_PROVIDER", env_file)
    model = _env("LLM_MODEL", env_file)
    return RealClient(provider=provider, model=model, api_key=api_key, results_dir=results_dir)


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "AgentLog",
    "LLMClient",
    "LLMOutputError",
    "NotConfigured",
    "RealClient",
    "StubClient",
    "get_client",
]

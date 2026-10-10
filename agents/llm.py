"""The LLM seam: one place the real model is wired in, and the only place it ever will be.

Phase 1 ships a deterministic pipeline that runs with no key. When a key is set, this module makes
the real call behind a provider-neutral interface and never touches the safety layer.

What this module guarantees, so the agents do not each reinvent it:

  * **A provider-neutral interface.** `LLMClient.generate(system, user, schema)` takes the agent's
    prompt text and a pydantic model and returns a validated instance of that model. Nothing above
    this line knows which provider is in use.
  * **Structured output only.** The raw completion must be JSON that validates against `schema`.
  * **Retry once, then fail loudly.** On a validation error the call is retried a single time with
    the error appended to the prompt; a second failure raises `LLMOutputError` and the raw text is
    saved to `results/` so a human can see exactly what came back.
  * **Provider failover.** `RealClient` tries the primary provider, then an optional fallback, on a
    rate limit (HTTP 429) or an unavailable server (5xx / network). When every provider fails the
    call raises, and each agent catches that and keeps its deterministic result (constraint 8), so
    a demo never blocks even when the free-tier quota is exhausted.
  * **An audit trail.** Every call is appended to `results/agent_log.jsonl` with the agent name,
    model, a hash of the prompt, the evidence ids that went in and the output that came out.
  * **Lazy configuration.** `LLM_API_KEY`, `LLM_PROVIDER` and `LLM_MODEL` (and the optional
    `LLM_FALLBACK_*` trio) are read from the environment / `.env` only when a client is actually
    built, never at import time, so importing an agent never requires a key.

Supported providers are `gemini` (Google AI Studio) and `groq`; any other name raises
`NotConfigured`, leaving the seam open for later providers.
"""
from __future__ import annotations

import hashlib
import json
import logging
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

#: Where each provider's OpenAI-compatible chat endpoint lives. Gemini has its own shape below.
_OPENAI_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
}
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: (connect timeout, read timeout) in seconds. Reads are short because max_tokens is capped.
_HTTP_TIMEOUT = (10, 90)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class NotConfigured(RuntimeError):
    """A real LLM call was attempted for a provider that is not implemented.

    With no key set, `get_client` returns None and the agents take their deterministic path. This is
    raised only when `LLM_PROVIDER` names something other than a supported provider, which is the
    seam left for adding more providers later.
    """


class LLMOutputError(RuntimeError):
    """The model's output did not validate even after one retry. The raw text has been saved."""


class LLMProviderError(RuntimeError):
    """Every configured provider failed (rate limit, network error, or a bad response).

    Each agent catches this and keeps its deterministic result, so the pipeline never blocks.
    """


class _Transient(RuntimeError):
    """An internal signal that a provider failed in a way worth failing over for (429 / 5xx)."""


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
    """The real provider client: gemini (Google AI Studio) and groq, with failover between them.

    `generate`, validation, the single retry, the raw dump and the audit log all live in the base
    class and are unchanged. This subclass only turns one prompt into raw assistant text, trying the
    primary provider and then the optional fallback. Networking uses `requests` (already a project
    dependency) and the providers' REST APIs directly, so no provider SDK is needed.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        results_dir: Path | str = DEFAULT_RESULTS_DIR,
        *,
        fallback_provider: str = "",
        fallback_model: str = "",
        fallback_api_key: str = "",
    ) -> None:
        super().__init__(results_dir)
        self.provider = provider or "unset"
        self.model = model or "unset"
        self.api_key = api_key
        self.fallback_provider = fallback_provider or ""
        self.fallback_model = fallback_model or ""
        self.fallback_api_key = fallback_api_key or ""

    def _providers(self) -> list[dict[str, str]]:
        """The ordered list of providers to try: primary, then the fallback if its key is set."""
        chain = [{
            "kind": self.provider.lower(),
            "name": self.provider,
            "model": self.model,
            "api_key": self.api_key,
        }]
        if self.fallback_api_key:
            chain.append({
                "kind": self.fallback_provider.lower(),
                "name": self.fallback_provider or "fallback",
                "model": self.fallback_model,
                "api_key": self.fallback_api_key,
            })
        return chain

    def _complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> str:
        chain = self._providers()
        errors: list[str] = []
        for index, prov in enumerate(chain):
            is_last = index == len(chain) - 1
            try:
                if prov["kind"] in ("gemini", "google"):
                    return self._complete_gemini(prov, system, user, temperature, max_tokens)
                if prov["kind"] in _OPENAI_BASE_URLS:
                    return self._complete_openai(prov, system, user, temperature, max_tokens)
                raise NotConfigured(
                    f"the real LLM client for provider {prov['name']!r} is not implemented yet "
                    f"(supported: gemini, groq). Unset LLM_API_KEY to use the deterministic path."
                )
            except NotConfigured:
                if is_last:
                    raise
                errors.append(f"{prov['name']}: provider not implemented")
            except _Transient as exc:
                log.warning("LLM provider %s unavailable (will fail over if possible): %s",
                            prov["name"], exc)
                errors.append(f"{prov['name']}: {exc}")
                if is_last:
                    raise LLMProviderError("every LLM provider failed: " + "; ".join(errors)) from exc
            except Exception as exc:  # noqa: BLE001 - surface as a provider error; agents fall back
                log.warning("LLM provider %s failed: %s", prov["name"], exc)
                errors.append(f"{prov['name']}: {exc}")
                if is_last:
                    raise LLMProviderError("every LLM provider failed: " + "; ".join(errors)) from exc
        # Unreachable: the last provider always either returns or raises above.
        raise LLMProviderError("every LLM provider failed: " + "; ".join(errors))

    @staticmethod
    def _check_transient(status_code: int, body: str) -> None:
        """A 429 (rate limit) or any 5xx is worth failing over for; raise `_Transient` for those."""
        if status_code == 429 or status_code >= 500:
            raise _Transient(f"HTTP {status_code}: {body[:200]}")

    def _complete_gemini(self, prov: dict[str, str], system: str, user: str,
                         temperature: float, max_tokens: int) -> str:
        import requests

        url = f"{_GEMINI_BASE_URL}/models/{prov['model']}:generateContent"
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        try:
            resp = requests.post(url, params={"key": prov["api_key"]}, json=body,
                                 timeout=_HTTP_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise _Transient(f"request error: {exc}") from exc
        self._check_transient(resp.status_code, resp.text)
        if resp.status_code >= 400:
            raise LLMProviderError(f"gemini HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMProviderError("gemini returned no candidates"
                                   + (f" (blocked: {blocked})" if blocked else ""))
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts)
        if not text.strip():
            raise LLMProviderError("gemini returned an empty completion")
        return text

    def _complete_openai(self, prov: dict[str, str], system: str, user: str,
                         temperature: float, max_tokens: int) -> str:
        import requests

        base = _OPENAI_BASE_URLS.get(prov["kind"], _OPENAI_BASE_URLS["groq"])
        url = f"{base}/chat/completions"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        payload: dict[str, Any] = {
            "model": prov["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {prov['api_key']}"}
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=_HTTP_TIMEOUT)
            # Some models reject json_object unless the word "json" is in the prompt. The agents'
            # prompts are JSON-oriented, but retry once without the hint if a provider objects.
            if resp.status_code == 400 and "json" in resp.text.lower():
                payload.pop("response_format", None)
                resp = requests.post(url, headers=headers, json=payload, timeout=_HTTP_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise _Transient(f"request error: {exc}") from exc
        self._check_transient(resp.status_code, resp.text)
        if resp.status_code >= 400:
            raise LLMProviderError(f"{prov['name']} HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMProviderError(f"{prov['name']} returned no choices")
        text = ((choices[0].get("message") or {}).get("content")) or ""
        if not text.strip():
            raise LLMProviderError(f"{prov['name']} returned an empty completion")
        return text


def get_client(results_dir: Path | str = DEFAULT_RESULTS_DIR) -> LLMClient | None:
    """Build the configured client, or return None to mean "use the deterministic fallback".

    Configuration is read lazily here, never at import. With no `LLM_API_KEY` this returns None and
    the agents rank/report/recommend deterministically. With a key it returns a `RealClient` wired
    to the primary provider, plus an optional fallback provider when the `LLM_FALLBACK_*` trio is
    set. The deterministic path stays as the permanent fallback if every provider is unreachable,
    so a demo never blocks (constraint 8).
    """
    env_file = _load_env_file()
    api_key = _env("LLM_API_KEY", env_file)
    if not api_key:
        return None
    return RealClient(
        provider=_env("LLM_PROVIDER", env_file),
        model=_env("LLM_MODEL", env_file),
        api_key=api_key,
        results_dir=results_dir,
        fallback_provider=_env("LLM_FALLBACK_PROVIDER", env_file),
        fallback_model=_env("LLM_FALLBACK_MODEL", env_file),
        fallback_api_key=_env("LLM_FALLBACK_API_KEY", env_file),
    )


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "AgentLog",
    "LLMClient",
    "LLMOutputError",
    "LLMProviderError",
    "NotConfigured",
    "RealClient",
    "StubClient",
    "get_client",
]

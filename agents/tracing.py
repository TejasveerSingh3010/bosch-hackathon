"""Langfuse tracing for the agent graph: one trace per pipeline run.

What lands in Langfuse, nested under one root span per run:
- every graph node (each agent turn, its ToolNode, its handoff, the escalation), via the LangChain
  `CallbackHandler`, with the tool calls, their arguments and what each tool returned;
- every LLM call. Calls made through LangChain (an agent driven by a chat model) are captured by
  the handler; calls made with the Groq SDK directly (`remediation.agent.GroqProposer`) are
  recorded here by `generation()`, with token usage and cost in USD from `PRICES`.

Langfuse computes cost itself only for models it has a price for; `register_prices` (CLI:
`python -m agents langfuse-prices`) gives it ours, so LangChain-captured generations get a cost
too. Tracing is on when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set (LANGFUSE_BASE_URL
picks the server; default Langfuse Cloud EU). Without them every function here is a no-op.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

#: USD per million tokens. Groq list prices for the models this project uses; check
#: https://groq.com/pricing when changing them (launch pricing for gpt-oss-120b was 0.15 / 0.75).
PRICES: dict[str, dict[str, float]] = {
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
    "openai/gpt-oss-20b": {"input": 0.075, "output": 0.30},
}


def enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def cost(model: str, usage: dict[str, int]) -> dict[str, float] | None:
    """Cost in USD per usage type, or None for a model without a price (Langfuse then leaves it blank)."""
    price = PRICES.get(model) or PRICES.get(model.removeprefix("openai/"))
    if price is None:
        return None
    parts = {k: usage.get(k, 0) * price[k] / 1_000_000 for k in ("input", "output")}
    return {**parts, "total": sum(parts.values())}


def usage_of(response: Any) -> tuple[dict[str, int], dict[str, Any]]:
    """Token usage from an OpenAI-shaped response (Groq SDK), plus the details worth keeping."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}, {}
    counts = {
        "input": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output": int(getattr(usage, "completion_tokens", 0) or 0),
    }
    counts["total"] = int(getattr(usage, "total_tokens", 0) or counts["input"] + counts["output"])
    details = {}
    reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    if reasoning is not None:
        details["reasoning_tokens"] = reasoning  # billed as output; already inside "output"
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
    if cached:
        details["cached_input_tokens"] = cached
    for key in ("queue_time", "prompt_time", "completion_time", "total_time"):
        if (value := getattr(usage, key, None)) is not None:
            details[key] = value
    return counts, details


class Generation:
    """Handle for one LLM call inside `generation()`; `finish` records the reply, usage and cost."""

    def __init__(self, observation: Any, model: str, trace_id: str | None = None) -> None:
        self.observation = observation
        self.model = model
        self.trace_id = trace_id  # where this call's scores go (filing, review)

    def finish(self, response: Any) -> None:
        if self.observation is None:
            return
        counts, details = usage_of(response)
        choice = (getattr(response, "choices", None) or [None])[0]
        message = getattr(choice, "message", None)
        self.observation.update(
            output=getattr(message, "content", None),
            usage_details=counts or None,
            cost_details=cost(self.model, counts) if counts else None,
            metadata={"finish_reason": getattr(choice, "finish_reason", None), **details},
        )


@contextmanager
def generation(
    name: str, model: str, input: Any, model_parameters: dict[str, Any] | None = None
) -> Iterator[Generation]:
    """Record one direct (non-LangChain) LLM call as a Langfuse generation under the current span."""
    if not enabled():
        yield Generation(None, model)
        return
    from langfuse import get_client

    client = get_client()
    with client.start_as_current_observation(
        name=name, as_type="generation", model=model, input=input, model_parameters=model_parameters
    ) as observation:
        try:
            yield Generation(observation, model, client.get_current_trace_id())
        except Exception as exc:
            observation.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            raise


class Span:
    def __init__(self, observation: Any = None, trace_id: str | None = None) -> None:
        self.observation = observation
        self.trace_id = trace_id

    def finish(self, output: Any = None, **metadata: Any) -> None:
        if self.observation is not None:
            self.observation.update(output=output, metadata=metadata or None)


@contextmanager
def span(name: str, input: Any = None) -> Iterator[Span]:
    """Group several LLM calls (draft, review, revision) in one observation - and so one trace when
    run outside the graph, which is where the proposal's scores land."""
    if not enabled():
        yield Span()
        return
    from langfuse import get_client

    client = get_client()
    with client.start_as_current_observation(name=name, as_type="chain", input=input) as observation:
        handle = Span(observation, client.get_current_trace_id())
        try:
            yield handle
        except Exception as exc:
            observation.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            raise
    client.flush()


class Run:
    """What the graph needs from one traced run: the callbacks to pass in, and where to look after."""

    def __init__(self, root: Any = None, callbacks: list[Any] | None = None) -> None:
        self.root = root
        self.callbacks = callbacks or []
        self.url: str | None = None

    def finish(self, output: Any) -> None:
        if self.root is None:
            return
        self.root.update(output=output)


@contextmanager
def run(
    name: str, input: Any, session_id: str | None = None, tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Run]:
    """One trace for one pipeline run: a root span, with session, tags and metadata on every child."""
    if not enabled():
        yield Run()
        return
    from langfuse import get_client, propagate_attributes
    from langfuse.langchain import CallbackHandler

    client = get_client()
    attributes = propagate_attributes(
        trace_name=name,
        session_id=session_id,
        tags=tags,
        metadata={k: str(v) for k, v in (metadata or {}).items()},
    )
    with client.start_as_current_observation(name=name, as_type="agent", input=input) as root, attributes:
        traced = Run(root, [CallbackHandler()])
        try:
            yield traced
        except Exception as exc:
            root.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            try:
                traced.url = client.get_trace_url()
            except Exception:  # the URL is a convenience; never fail a run over it
                traced.url = None
    client.flush()


def score(
    trace_id: str | None,
    name: str,
    value: float | str,
    comment: str | None = None,
    data_type: str = "NUMERIC",
) -> bool:
    """Attach a score to the trace that produced something; False (and no error) if it cannot be sent.

    Scores are how recommendation quality is measured over time: the policy verdict at filing, and
    the human decision at review, on the very trace (prompt, context, tokens) that produced it.
    """
    if not trace_id or not enabled():
        return False
    from langfuse import get_client

    try:
        client = get_client()
        client.create_score(trace_id=trace_id, name=name, value=value, comment=comment, data_type=data_type)
        client.flush()
    except Exception as exc:  # observability must never break filing or review
        log.warning("could not send score %s to Langfuse: %s", name, exc)
        return False
    return True


def register_prices(prices: dict[str, dict[str, float]] = PRICES) -> list[str]:
    """Create Langfuse model definitions for `prices` that it does not already have; returns the new ones."""
    from langfuse import get_client

    client = get_client()
    existing = {m.model_name for m in client.api.models.list(limit=100).data}
    created = []
    for model, price in prices.items():
        if model in existing:
            continue
        bare = model.split("/", 1)[-1]
        client.api.models.create(
            model_name=model,
            match_pattern=rf"(?i)^(openai/)?{bare.replace('.', '[.]')}$",
            unit="TOKENS",
            input_price=price["input"] / 1_000_000,
            output_price=price["output"] / 1_000_000,
        )
        created.append(model)
    return created

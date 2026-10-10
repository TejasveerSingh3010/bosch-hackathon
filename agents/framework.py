"""The shape every agent in the graph shares.

An agent is three nodes:

    <name>          one agent turn: the driver returns an AIMessage, possibly with tool calls
    <name>_tools    a ToolNode over the agent's own tools (results go back as ToolMessages)
    <name>_handoff  turns the finished conversation into state for the next stage

with a conditional edge `<name>` -> `<name>_tools` | `<name>_handoff` (tool calls pending and
rounds left, or not), a normal edge `<name>_tools` -> `<name>`, and the spec's own conditional
edge out of `<name>_handoff` to the next agent or straight to escalation.

The driver is what decides the tool calls. `llm_driver` binds a LangChain chat model to the tools
(for agents that reason with an LLM); `scripted_driver` emits fixed tool calls (a stand-in for an
agent not built yet, or one whose LLM work happens inside its tools). Both go through the same
ToolNode and edges, so tracing and tool handling are identical either way.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

from .state import PipelineState

#: (state, the agent's conversation so far) -> the agent's next message.
Driver = Callable[[PipelineState, list[AnyMessage]], AIMessage]
#: (state, the agent's conversation so far) -> tool calls to make next as (name, args), or None when done.
Script = Callable[[PipelineState, list[AnyMessage]], list[tuple[str, dict[str, Any]]] | None]


@dataclass
class AgentSpec:
    name: str
    tools: list[BaseTool]
    driver: Driver
    brief: Callable[[PipelineState], str]  # the first message the agent sees
    handoff: Callable[[PipelineState, list[AnyMessage]], dict[str, Any]]  # state update when done
    route: Callable[[PipelineState], str]  # next node after the handoff
    destinations: tuple[str, ...]  # every node `route` can return (drawn in the graph)
    max_tool_rounds: int = 6
    description: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.name}_messages"


def tool_rounds(messages: list[AnyMessage]) -> int:
    return sum(1 for m in messages if isinstance(m, AIMessage) and m.tool_calls)


def tool_results(messages: list[AnyMessage]) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


def artifacts(messages: list[AnyMessage], tool: str | None = None) -> list[dict[str, Any]]:
    """Structured results the tools attached (`response_format="content_and_artifact"`), oldest first."""
    return [
        m.artifact
        for m in tool_results(messages)
        if isinstance(m.artifact, dict) and (tool is None or m.name == tool)
    ]


def tool_errors(name: str, messages: list[AnyMessage]) -> list[str]:
    return [f"{name}: {m.name} failed: {m.content}" for m in tool_results(messages) if m.status == "error"]


def add_agent(builder: StateGraph, spec: AgentSpec) -> None:
    def agent(state: PipelineState) -> dict[str, Any]:
        history = list(state.get(spec.key) or [])
        seed = [] if history else [HumanMessage(spec.brief(state))]
        reply = spec.driver(state, history + seed)
        return {spec.key: seed + [reply]}

    def after_turn(state: PipelineState) -> str:
        messages = state.get(spec.key) or []
        last = messages[-1] if messages else None
        pending = isinstance(last, AIMessage) and bool(last.tool_calls)
        if pending and tool_rounds(messages) <= spec.max_tool_rounds:
            return f"{spec.name}_tools"
        return f"{spec.name}_handoff"

    def handoff(state: PipelineState) -> dict[str, Any]:
        messages = state.get(spec.key) or []
        update = dict(spec.handoff(state, messages))
        notes = tool_errors(spec.name, messages)
        last = messages[-1] if messages else None
        if isinstance(last, AIMessage) and last.tool_calls:
            notes.append(f"{spec.name}: stopped after {spec.max_tool_rounds} tool rounds")
        if notes:
            update["notes"] = list(update.get("notes") or []) + notes
        return update

    meta = {"agent": spec.name}
    builder.add_node(spec.name, agent, metadata=meta)
    tools = ToolNode(spec.tools, messages_key=spec.key, handle_tool_errors=True)
    builder.add_node(f"{spec.name}_tools", tools, metadata=meta)
    builder.add_node(f"{spec.name}_handoff", handoff, metadata=meta)
    builder.add_conditional_edges(spec.name, after_turn, [f"{spec.name}_tools", f"{spec.name}_handoff"])
    builder.add_edge(f"{spec.name}_tools", spec.name)
    builder.add_conditional_edges(f"{spec.name}_handoff", spec.route, list(spec.destinations))


def scripted_driver(script: Script, summary: Callable[[PipelineState, list[AnyMessage]], str]) -> Driver:
    """A driver that makes the tool calls `script` names, and reports `summary` when it has none."""

    def drive(state: PipelineState, messages: list[AnyMessage]) -> AIMessage:
        calls = script(state, messages)
        if calls:
            return AIMessage(
                content="",
                name="scripted",
                tool_calls=[
                    {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "tool_call"}
                    for name, args in calls
                ],
            )
        return AIMessage(content=summary(state, messages), name="scripted")

    return drive


def llm_driver(model: BaseChatModel, system_prompt: str, tools: list[BaseTool]) -> Driver:
    """A driver backed by a chat model that may call `tools`. Usage is traced by the Langfuse handler."""
    bound = model.bind_tools(tools) if tools else model

    def drive(_state: PipelineState, messages: list[AnyMessage]) -> AIMessage:
        return bound.invoke([SystemMessage(system_prompt), *messages])

    return drive


def groq_chat_model(model: str | None = None, **kwargs: Any) -> BaseChatModel:
    """The project's LLM (Groq, key LLM_API_KEY in .env) as a LangChain chat model, for `llm_driver`."""
    import os

    from dotenv import load_dotenv
    from langchain_groq import ChatGroq

    from remediation.agent import MODEL
    from safety.limits import REPO_ROOT

    load_dotenv(REPO_ROOT / ".env")
    key = os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY")
    return ChatGroq(model=model or MODEL, api_key=key, **kwargs)

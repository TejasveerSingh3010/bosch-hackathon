"""The state that flows through the graph. Each agent keeps its own message history; what one agent
hands the next is a few plain fields (result ids, findings, proposal ids), not its conversation."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class PipelineState(TypedDict, total=False):
    # --- input
    goal: str  # what this run is for, in words (the attacker's brief)
    plan: dict[str, Any] | None  # an Experiment as JSON; the attacker's stand-in runs it as given
    dry_run: bool  # validate the plan only, inject nothing
    allow_code: bool  # let remediation answer a declined config proposal with a code change

    # --- per-agent conversations (agent turns, tool calls, tool results)
    attacker_messages: Annotated[list[AnyMessage], add_messages]
    reporter_messages: Annotated[list[AnyMessage], add_messages]
    remediation_messages: Annotated[list[AnyMessage], add_messages]

    # --- handoffs
    result_ids: list[str]  # attacker -> reporter: experiment records written to results/
    findings: list[str]  # reporter -> remediation: the reported records worth a recommendation
    proposal_ids: list[str]  # remediation -> escalation: what the human is asked to review
    notes: Annotated[list[str], operator.add]  # why a stage ended early, tool errors, ...

    # --- output
    escalation: dict[str, Any]  # the recommendation for human escalation; always the last write

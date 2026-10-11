"""The pipeline graph: attacker -> analyst -> remediation -> escalate.

START is always the attacker and the last node before END is always `escalate`, which writes the
recommendation for human escalation. Every early exit (plan rejected, dry run, nothing found, an
LLM error) still routes through it, so a run always ends with something a human can act on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from remediation.models import CodeProposal
from remediation.store import Store

from . import analyst, attacker, remediation, tracing
from .deps import Deps
from .framework import add_agent
from .state import PipelineState


def escalation(state: PipelineState, store: Store) -> dict[str, Any]:
    """The hand-over to a human: what was filed, where each stands, and the command to decide it."""
    items = []
    for pid in state.get("proposal_ids") or []:
        proposal = store.load_proposal(pid)
        status = store.status(pid)
        reviewable = proposal.remediable and not proposal.policy_problems
        items.append(
            {
                "proposal_id": pid,
                "kind": "code" if isinstance(proposal, CodeProposal) else "config",
                "status": status,
                "diagnosis": proposal.diagnosed_problem,
                "reviewable": reviewable,
                "action": f"python -m remediation review {pid}" if reviewable else None,
                "review_sheet": str(store.dir(pid) / "review.md"),
            }
        )
    awaiting = [i for i in items if i["reviewable"]]
    if awaiting:
        headline = f"{len(awaiting)} recommendation(s) await a human decision"
    elif items:
        headline = "nothing to approve: every recommendation was declined or rejected by policy"
    else:
        headline = "no recommendation was filed"
    return {
        "headline": headline,
        "experiments": list(state.get("result_ids") or []),
        "findings": list(state.get("findings") or []),
        "recommendations": items,
        "notes": list(state.get("notes") or []),
        "at": datetime.now().astimezone().isoformat(),
    }


def build_graph(deps: Deps) -> CompiledStateGraph:
    builder = StateGraph(PipelineState)
    for spec in (attacker.spec(deps), analyst.spec(deps), remediation.spec(deps)):
        add_agent(builder, spec)
    store = Store(deps.config.results_dir)
    builder.add_node("escalate", lambda state: {"escalation": escalation(state, store)})
    builder.add_edge(START, "attacker")
    builder.add_edge("escalate", END)
    return builder.compile(name="chaos-pipeline")


def run_pipeline(
    deps: Deps, inputs: PipelineState, session_id: str | None = None, tags: list[str] | None = None
) -> tuple[PipelineState, str | None]:
    """Run the graph once, traced as one Langfuse trace. Returns the final state and the trace URL."""
    graph = build_graph(deps)
    plan = inputs.get("plan") or {}
    with tracing.run(
        "chaos-pipeline",
        input={k: v for k, v in inputs.items() if not k.endswith("_messages")},
        session_id=session_id,
        tags=["chaos-pipeline", *(tags or [])],
        metadata={k: plan.get(k) for k in ("id", "target", "action")} | {"dry_run": inputs.get("dry_run")},
    ) as traced:
        final = graph.invoke(
            inputs,
            config={"callbacks": traced.callbacks, "run_name": "chaos-pipeline", "recursion_limit": 60},
        )
        traced.finish(final.get("escalation"))
    return final, traced.url

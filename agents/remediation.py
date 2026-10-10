"""The remediation agent in the graph: findings in, recommendations filed for human review out.

Its LLM work happens inside its tools (`remediation.recommend`, one structured Groq call per fault,
traced as a Langfuse generation with usage and cost), so its driver is scripted: recommend a
configuration fix for each fault; for each one declined as needing code, and if code is allowed,
recommend a code change answering it. Groq's free tier (8,000 tokens a minute) is why it does not
loop an LLM over the tools: each call already carries a dense context.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AnyMessage
from langchain_core.tools import BaseTool, tool

from remediation.agent import ProposalError
from remediation.models import CodeProposal
from remediation.recommend import Filed, recommend_code, recommend_config
from remediation.source import SourceError

from .deps import Deps
from .framework import AgentSpec, artifacts, scripted_driver, tool_results
from .state import PipelineState


def describe(proposal: Any) -> tuple[str, dict[str, Any]]:
    kind = "code" if isinstance(proposal, CodeProposal) else "config"
    if not proposal.remediable:
        outcome = f"DECLINED: {proposal.declined_reason}"
    elif proposal.policy_problems:
        outcome = "REJECTED by policy: " + "; ".join(proposal.policy_problems)
    elif kind == "code":
        outcome = "edits " + ", ".join(sorted({f"{e.service}/{e.file}" for e in proposal.edits}))
    else:
        outcome = "changes " + ", ".join(f"{c.service}.{c.setting}={c.value}" for c in proposal.changes)
    sources = ", ".join(proposal.source_results)
    text = f"{kind} proposal {proposal.id} ({sources})\nproblem {proposal.diagnosed_problem}\n{outcome}"
    return text, {
        "proposal_id": proposal.id,
        "kind": kind,
        "remediable": proposal.remediable,
        "policy_problems": list(proposal.policy_problems),
    }


def report(filed: Filed) -> tuple[str, dict[str, Any]]:
    """One tool result for everything a recommend_* call filed (one proposal per fault)."""
    described = [describe(p) for p in filed.proposals]
    text = "\n\n".join([t for t, _ in described] + [f"ERROR: {e}" for e in filed.errors])
    if filed.skipped:
        text += f"\n\nnot asked this run: {', '.join(filed.skipped)}"
    return text or "nothing filed", {"filed": [a for _, a in described], "errors": list(filed.errors)}


def make_tools(deps: Deps) -> list[BaseTool]:
    @tool(response_format="content_and_artifact")
    def recommend_config_fix(result_ids: list[str]) -> tuple[str, dict[str, Any]]:
        """Ask the remediation LLM for a Compose configuration fix per fault in these experiment
        records, policy-check each and file it for human review. It may decline when only code can
        fix it."""
        try:
            filed = recommend_config(deps.proposer, deps.config, deps.services(), result_ids)
        except ProposalError as exc:
            return f"ERROR: {exc}", {"filed": [], "errors": [str(exc)]}
        return report(filed)

    @tool(response_format="content_and_artifact")
    def recommend_code_fix(declined_id: str) -> tuple[str, dict[str, Any]]:
        """Answer a declined configuration proposal with exact edits to the pinned upstream source,
        policy- and build-checked, filed for human review."""
        try:
            filed = recommend_code(deps.proposer, deps.config, declined_id, deps.build)
        except (ProposalError, SourceError) as exc:
            return f"ERROR: {exc}", {"filed": [], "errors": [str(exc)]}
        return report(filed)

    return [recommend_config_fix, recommend_code_fix]


def filed_so_far(messages: list[AnyMessage]) -> list[dict[str, Any]]:
    return [entry for a in artifacts(messages) for entry in a.get("filed") or []]


def script(state: PipelineState, messages: list[AnyMessage]) -> list[tuple[str, dict[str, Any]]] | None:
    """Config fixes first; then a code recommendation for each fault whose config fix was declined."""
    if not tool_results(messages):
        return [("recommend_config_fix", {"result_ids": list(state.get("findings") or [])})]
    # By tool name, not artifact: a tool that failed leaves no artifact and must not be called again.
    asked_code = any(m.name == "recommend_code_fix" for m in tool_results(messages))
    if not state.get("allow_code", True) or asked_code:
        return None
    declined = [
        f["proposal_id"] for f in filed_so_far(messages) if f["kind"] == "config" and not f["remediable"]
    ]
    return [("recommend_code_fix", {"declined_id": pid}) for pid in declined] or None


def summary(_state: PipelineState, messages: list[AnyMessage]) -> str:
    return "\n\n".join(str(m.content) for m in tool_results(messages))


def handoff(_state: PipelineState, messages: list[AnyMessage]) -> dict[str, Any]:
    update: dict[str, Any] = {"proposal_ids": [f["proposal_id"] for f in filed_so_far(messages)]}
    errors = [f"remediation: {e}" for a in artifacts(messages) for e in a.get("errors") or []]
    if errors:
        update["notes"] = errors
    return update


def spec(deps: Deps) -> AgentSpec:
    return AgentSpec(
        name="remediation",
        tools=make_tools(deps),
        driver=scripted_driver(script, summary),
        brief=lambda state: f"Recommend fixes for {', '.join(state.get('findings') or [])}.",
        handoff=handoff,
        route=lambda _state: "escalate",
        destinations=("escalate",),
        description="files config / code recommendations for human review, one per fault",
    )

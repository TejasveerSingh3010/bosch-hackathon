"""Agent 2 in the graph: the reporter, between the attacker and the remediator.

The attacker hands it the experiments it ran; for each, Agent 2 (`reporter.service.Reporter`) builds
a failure report from the monitoring telemetry and the container logs, saves it to `reports/` (where
the dashboard shows it and the remediator reads it), and passes the experiment on as a finding. Its
LLM work (the narrator, Groq `openai/gpt-oss-20b`, with a rule-based fallback) happens inside the
tool, so the driver is scripted: write one report per experiment. A reporting failure is recorded as
a note and the experiment is still forwarded, so one bad report never sinks the run.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AnyMessage
from langchain_core.tools import BaseTool, tool

from .deps import Deps
from .framework import AgentSpec, artifacts, scripted_driver
from .state import PipelineState


def make_tools(deps: Deps) -> list[BaseTool]:
    @tool(response_format="content_and_artifact")
    def write_report(result_id: str) -> tuple[str, dict[str, Any]]:
        """Generate Agent 2's failure report for a finished experiment from its telemetry and the
        container logs, and save it for the remediator and the dashboard."""
        try:
            report = deps.reporter.report_experiment_id(result_id)
        except Exception as exc:  # noqa: BLE001 - reporting touches docker, files and an LLM; never sink the run
            return f"report for {result_id} failed: {exc}", {"result_id": result_id, "error": str(exc)}
        a = report.analysis
        return (
            f"report {report.id} [{a.severity}] {a.headline}\nwhere: {a.failure_point}\n"
            f"narrator: {report.narrator}",
            {"result_id": result_id, "report_id": report.id, "severity": a.severity,
             "narrator": report.narrator},
        )

    return [write_report]


def script(state: PipelineState, messages: list[AnyMessage]) -> list[tuple[str, dict[str, Any]]] | None:
    """Write a report for each experiment the attacker produced, once (errors carry an artifact too)."""
    done = {a["result_id"] for a in artifacts(messages, "write_report")}
    todo = [r for r in state.get("result_ids") or [] if r not in done]
    return [("write_report", {"result_id": r}) for r in todo] or None


def _reported(messages: list[AnyMessage]) -> list[dict[str, Any]]:
    return [a for a in artifacts(messages, "write_report") if "report_id" in a]


def summary(_state: PipelineState, messages: list[AnyMessage]) -> str:
    done = _reported(messages)
    return "Reports: " + (", ".join(f"{a['result_id']} [{a['severity']}]" for a in done) or "none")


def handoff(_state: PipelineState, messages: list[AnyMessage]) -> dict[str, Any]:
    update: dict[str, Any] = {"findings": [a["result_id"] for a in _reported(messages)]}
    failed = [a for a in artifacts(messages, "write_report") if "error" in a]
    if failed:
        update["notes"] = [f"reporter: {a['result_id']} - {a['error']}" for a in failed]
    return update


def route(state: PipelineState) -> str:
    return "remediation" if state.get("findings") else "escalate"


def spec(deps: Deps) -> AgentSpec:
    return AgentSpec(
        name="reporter",
        tools=make_tools(deps),
        driver=scripted_driver(script, summary),
        brief=lambda state: f"Report on experiments {', '.join(state.get('result_ids') or [])}.",
        handoff=handoff,
        route=route,
        destinations=("remediation", "escalate"),
        description="Agent 2: writes the failure report from telemetry and logs, per experiment",
    )

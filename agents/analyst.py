"""The analyst: reads what the attacker's experiments did and decides what is worth remediating.

NOT BUILT YET. The tools are real; the stand-in reads every record the attacker produced and hands
all of them on as findings. To build it, replace `driver` with an `llm_driver` and make `handoff`
read the findings the model reports.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AnyMessage
from langchain_core.tools import BaseTool, tool

from remediation.agent import experiment_records, fault_timeline, record_digest
from safety.telemetry import TelemetryReader

from .deps import Deps
from .framework import AgentSpec, artifacts, scripted_driver
from .state import PipelineState


def make_tools(deps: Deps) -> list[BaseTool]:
    results_dir = deps.config.results_dir

    @tool(response_format="content_and_artifact")
    def read_experiment_record(result_id: str) -> tuple[str, dict[str, Any]]:
        """One experiment record: verdict, phase metrics and a sample-by-sample fault timeline."""
        record = experiment_records(results_dir, [result_id])[0]
        timeline = fault_timeline(record, TelemetryReader(results_dir))
        return f"{record_digest(record)}\ntimeline:\n{timeline}", {
            "result_id": result_id,
            "verdict": record.verdict,
            "injected": record.injected_at is not None,
        }

    @tool
    def recent_experiments(limit: int = 8) -> str:
        """The most recent real experiment records: id, target, action, verdict."""
        records = experiment_records(results_dir, limit=limit)
        lines = [f"{r.experiment.id}: {r.experiment.action} on {r.experiment.target} -> {r.verdict}"
                 for r in records]
        return "\n".join(lines) or "no experiment records"

    return [read_experiment_record, recent_experiments]


def script(state: PipelineState, messages: list[AnyMessage]) -> list[tuple[str, dict[str, Any]]] | None:
    """Stand-in: read each record the attacker produced, once."""
    read = {a["result_id"] for a in artifacts(messages, "read_experiment_record")}
    todo = [r for r in state.get("result_ids") or [] if r not in read]
    return [("read_experiment_record", {"result_id": r}) for r in todo] or None


def summary(_state: PipelineState, messages: list[AnyMessage]) -> str:
    found = [a["result_id"] for a in artifacts(messages, "read_experiment_record") if a["injected"]]
    return f"Records worth a recommendation: {', '.join(found) or 'none'}."


def handoff(_state: PipelineState, messages: list[AnyMessage]) -> dict[str, Any]:
    # A record without an injection (the fault never took effect) has nothing to remediate.
    found = [a["result_id"] for a in artifacts(messages, "read_experiment_record") if a["injected"]]
    return {"findings": found}


def route(state: PipelineState) -> str:
    return "remediation" if state.get("findings") else "escalate"


def spec(deps: Deps) -> AgentSpec:
    return AgentSpec(
        name="analyst",
        tools=make_tools(deps),
        driver=scripted_driver(script, summary),
        brief=lambda state: f"Analyse experiments {', '.join(state.get('result_ids') or [])}.",
        handoff=handoff,
        route=route,
        destinations=("remediation", "escalate"),
        description="reads the records and picks findings (stand-in: forwards every injected record)",
    )

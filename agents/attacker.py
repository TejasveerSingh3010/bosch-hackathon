"""The attacker: decides which failure to inject and runs it. Always the graph's entry point.

NOT BUILT YET. Its tools are real (every plan still goes through `safety.validate_plan` inside the
runner); the decision-making is a stand-in that runs the plan given as input, once. To build it,
replace `driver` with `framework.llm_driver(groq_chat_model(), SYSTEM_PROMPT, tools)`.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AnyMessage
from langchain_core.tools import BaseTool, tool

from injector.actions import describe_actions
from injector.docker_cli import DockerError
from safety.telemetry import TelemetryUnavailable
from safety.validator import PlanRejected, explain_plan
from schemas import Experiment

from .deps import Deps
from .framework import AgentSpec, artifacts, scripted_driver, tool_results
from .state import PipelineState


def make_tools(deps: Deps) -> list[BaseTool]:
    @tool
    def list_targets() -> str:
        """The containers the safety layer allows as targets, and the failure actions available."""
        allowed = ", ".join(sorted(deps.config.allowed_containers))
        return f"allowed targets: {allowed}\nactions:\n{describe_actions()}"

    @tool(response_format="content_and_artifact")
    def run_experiment(plan: Experiment, dry_run: bool = False) -> tuple[str, dict[str, Any]]:
        """Validate one experiment against the safety layer and run it (or only validate it, with
        dry_run). The system is always rolled back afterwards; the record lands in results/<id>.json."""
        plan = Experiment.model_validate(plan) if isinstance(plan, dict) else plan
        runner = deps.runner
        try:
            if dry_run:
                return f"DRY RUN, nothing injected:\n{explain_plan(runner.dry_run(plan))}", {"dry_run": True}
            result = runner.run(plan)
        except PlanRejected as exc:
            return f"REJECTED by the safety layer: {exc}", {"rejected": str(exc)}
        except (DockerError, TelemetryUnavailable) as exc:
            return f"ERROR: {exc}", {"error": str(exc)}
        lines = [f"verdict {result.verdict}"]
        if result.abort_reason:
            lines.append(f"abort reason {result.abort_reason}")
        for label in ("detection_seconds", "impact_seconds", "recovery_seconds"):
            if (value := getattr(result, label)) is not None:
                lines.append(f"{label} {value:g}")
        lines.append(f"record results/{plan.id}.json")
        return "\n".join(lines), {"result_id": plan.id, "verdict": result.verdict}

    return [list_targets, run_experiment]


def brief(state: PipelineState) -> str:
    return state.get("goal") or "Find the most damaging failure the safety layer allows, and run it."


def script(state: PipelineState, messages: list[AnyMessage]) -> list[tuple[str, dict[str, Any]]] | None:
    """Stand-in: run the input plan once."""
    if tool_results(messages) or not state.get("plan"):
        return None
    return [("run_experiment", {"plan": state["plan"], "dry_run": bool(state.get("dry_run"))})]


def summary(state: PipelineState, messages: list[AnyMessage]) -> str:
    if not state.get("plan"):
        return "No plan given, and the attacker agent that would choose one is not built yet."
    return next((m.content for m in reversed(tool_results(messages))), "")


def handoff(state: PipelineState, messages: list[AnyMessage]) -> dict[str, Any]:
    done = [a for a in artifacts(messages, "run_experiment") if "result_id" in a]
    notes = [f"attacker: {a['rejected']}" for a in artifacts(messages, "run_experiment") if "rejected" in a]
    if not state.get("plan"):
        notes.append("attacker: no plan given")
    if any(a.get("dry_run") for a in artifacts(messages, "run_experiment")):
        notes.append("attacker: dry run, nothing was injected")
    return {"result_ids": [a["result_id"] for a in done], "notes": notes}


def route(state: PipelineState) -> str:
    return "analyst" if state.get("result_ids") else "escalate"


def spec(deps: Deps) -> AgentSpec:
    tools = make_tools(deps)
    return AgentSpec(
        name="attacker",
        tools=tools,
        driver=scripted_driver(script, summary),
        brief=brief,
        handoff=handoff,
        route=route,
        destinations=("analyst", "escalate"),
        description="chooses and runs a failure experiment (stand-in: runs the given plan)",
    )

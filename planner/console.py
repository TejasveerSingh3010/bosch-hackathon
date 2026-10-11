"""The attack console: a typed request -> one policy-checked plan -> (after confirmation) a run.

The dashboard's console calls this through the CLI in two separate steps, so a human always sees
the exact plan before anything is injected:

    python -m planner interpret "slow cartservice down by 400 ms for 20 s" --out REQUEST.json
    python -m planner execute REQUEST.json --confirm

`interpret` maps the request onto the catalogue (openai/gpt-oss-120b when LLM_API_KEY is set, a
keyword parser otherwise), builds the `AttackPlan` and runs the policy check, but injects nothing.
`execute` re-runs the policy check on the saved plan (health may have changed) and, with the
operator's confirmation, sends it through the same runner every injection uses.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agents.llm import LLMError, get_client, stub_handler

from . import prompts
from .agent import DEFAULT_PARAMS, PLANNER_MODEL, AttackParams
from .approval import Approval
from .catalogue import CATALOGUE, DEFAULT_DURATION_S
from .interface import ToolInterface, allowed_services
from .plan import AttackPlan
from .policy import PolicyDecision


class PromptedAttack(BaseModel):
    """One attack read from the operator's request."""

    model_config = ConfigDict(extra="forbid")

    understood: bool = Field(description="false when the request cannot be mapped onto one catalogue attack")
    action: str = Field(description="catalogue attack name")
    target: str = Field(description="allowed service name")
    params: AttackParams = Field(default_factory=AttackParams)
    duration_s: int = DEFAULT_DURATION_S
    note: str = Field(default="", description="one sentence on how the request was read")


#: Words that name an attack, checked in this order (more specific first).
KEYWORDS: tuple[tuple[str, str], ...] = (
    ("network_partition", r"partition|disconnect|isolat|cut off"),
    ("network_delay", r"delay|latency|lag|slow|packet loss|lossy|\bloss\b"),
    ("flapping", r"flap|intermittent|on and off"),
    ("memory_limit", r"memory|\bmem\b|oom|\bram\b"),
    ("throttle", r"throttl|cpu|starv"),
    ("pause", r"pause|freeze|frozen|hang|unresponsive"),
    ("stop", r"\bstop|take down|outage|shut ?down"),
    ("kill", r"\bkill|crash|sigkill"),
)


def _number(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text)
    return float(match.group(1)) if match else None


@stub_handler(prompts.INTERPRET_TASK)
def parse(payload: dict[str, Any]) -> dict[str, Any]:
    """Offline reading of a request: keywords for the attack, a service name for the target."""
    text = str(payload.get("prompt") or "").lower()
    services: list[str] = list(payload.get("services") or [])
    action = next((name for name, pattern in KEYWORDS if re.search(pattern, text)), "")
    squashed = text.replace(" ", "")
    # "cartservice", "cart service" or the word "cart" all name cartservice ("ad" inside "load" does not)
    words = {s: rf"\b{re.escape(s.removesuffix('service') or s)}\b" for s in services}
    by_length = sorted(services, key=len, reverse=True)
    target = (next((s for s in by_length if s in squashed), "")  # a full name ("redis-cart") wins
              or next((s for s in by_length if re.search(words[s], text)), ""))
    params: dict[str, Any] = dict(DEFAULT_PARAMS.get(action, {}))
    found = {
        "delay_ms": _number(r"(\d+)\s*ms\b", text),
        "loss_pct": _number(r"(\d+(?:\.\d+)?)\s*%", text),
        "memory_mb": _number(r"(\d+)\s*(?:mb|mib)\b", text),
        "cpus": _number(r"(\d*\.\d+|\d+)\s*(?:cpus?|cores?)\b", text),
    }
    params.update({k: v for k, v in found.items() if v is not None and k in params})
    for key in ("memory_mb", "delay_ms"):
        if key in params:
            params[key] = int(params[key])
    seconds = _number(r"(\d+)\s*(?:s|sec|secs|seconds?)\b", text)
    minutes = _number(r"(\d+)\s*(?:min|mins|minutes?)\b", text)
    duration_s = int(seconds if seconds is not None else (minutes * 60 if minutes else DEFAULT_DURATION_S))
    missing = [label for label, value in (("an attack", action), ("a target", target)) if not value]
    return {
        "understood": not missing,
        "action": action, "target": target, "params": params, "duration_s": duration_s,
        "note": (f"could not find {' or '.join(missing)} in the request" if missing
                 else f"keyword match: {action} on {target}"),
    }


def _catalogue_text() -> str:
    return "\n".join(f"- {name}: {attack.description} [{json.dumps(DEFAULT_PARAMS.get(name, {}))}]"
                     + (f" params: {attack.params_help}" if attack.params_help else "")
                     for name, attack in CATALOGUE.items())


def interpret(tools: ToolInterface, prompt: str) -> dict[str, Any]:
    """Read the request, build the plan and policy-check it. Injects nothing."""
    services = allowed_services(tools.config)
    tools.auditor.tool_request("interpret", {"prompt": prompt})
    user = prompts.INTERPRET_USER_TEMPLATE.format(
        prompt=prompt, services=", ".join(services), catalogue=_catalogue_text(),
        schema=prompts.INTERPRET_SCHEMA,
    )
    client = get_client(PLANNER_MODEL)
    payload = {"prompt": prompt, "services": services}
    try:
        read = client.complete_json(task=prompts.INTERPRET_TASK, system=prompts.INTERPRET_SYSTEM,
                                    user=user, schema=PromptedAttack, payload=payload)
        interpreted_by, error = client.name, None
    except LLMError as exc:
        read, interpreted_by, error = PromptedAttack(**parse(payload)), "stub", str(exc)
    out: dict[str, Any] = {"prompt": prompt, "interpreted_by": interpreted_by, "note": read.note,
                           "understood": read.understood, "plan": None, "allowed": False, "reasons": []}
    if error:
        out["llm_error"] = error
    if not read.understood:
        out["reasons"] = [read.note or "the request could not be mapped onto one catalogue attack"]
        return out
    try:
        plan = AttackPlan.build(read.action, read.target, tools.config.app_name, read.params.given(),
                                read.duration_s)
    except (KeyError, ValueError) as exc:
        out["reasons"] = [f"could not build a plan: {exc}"]
        return out
    tools.auditor.plan(plan)
    decision = tools.check(plan)
    tools.auditor.policy(decision)
    out.update(plan=plan.model_dump(), allowed=decision.allowed, reasons=decision.reasons,
               notes=decision.notes)
    return out


class ConsoleApproval:
    """The operator confirmed this exact plan in the dashboard console (the --confirm flag)."""

    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id

    def decide(self, plan: AttackPlan, decision: PolicyDecision) -> Approval:  # noqa: ARG002
        if plan.experiment_id == self.experiment_id:
            return Approval(True, "operator", "confirmed in the dashboard attack console")
        return Approval(False, "denied", "not the plan the operator confirmed")


def load_plan(request_file: Path) -> AttackPlan | None:
    request = json.loads(Path(request_file).read_text(encoding="utf-8"))
    return AttackPlan.model_validate(request["plan"]) if request.get("plan") else None


def execute(tools: ToolInterface, plan: AttackPlan) -> dict[str, Any]:
    """Run a saved plan, re-checking the policy first. The tools' approver decides."""
    tools.auditor.tool_request("execute", {"experiment_id": plan.experiment_id})
    return {"experiment_id": plan.experiment_id, **tools.execute_plan(plan)}


__all__ = ["KEYWORDS", "ConsoleApproval", "PromptedAttack", "execute", "interpret", "load_plan", "parse"]

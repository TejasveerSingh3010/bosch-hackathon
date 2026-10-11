"""Agent 1: choose resilience experiments from the catalogue and drive them through the tools.

The planner selects a ranked set of experiments (an LLM when LLM_API_KEY is set, a deterministic
blast-radius ranker otherwise - the same seam the reporter uses), then runs each one through the
constrained tool interface: policy check, human approval, runner, audit. It ends on an
evidence-based conclusion drawn from what actually ran, not from its own reasoning.

Selection cannot escape the safety layer: whatever is chosen, every injection is re-validated and
needs approval. An unknown action or disallowed target is simply rejected by the policy check and
recorded, which is exactly the independent-validation property Phase 3 asks for.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agents.llm import LLMError, get_client, stub_handler
from safety.limits import REPO_ROOT

from . import prompts
from .catalogue import ATTACK_NAMES, CATALOGUE, DEFAULT_DURATION_S, get_attack
from .interface import ToolInterface, allowed_services

#: Default parameters the ranker fills in for attacks that need them.
DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "throttle": {"cpus": 0.1},
    "memory_limit": {"memory_mb": 128},
    "network_delay": {"delay_ms": 300, "loss_pct": 5},
    "flapping": {"down_s": 2, "up_s": 3, "cycles": 3},
}
#: The order the deterministic ranker reaches for attacks: hangs and slow/partial failures first,
#: because those are the ones systems most often handle badly; the plain crash last.
PRIORITY_ATTACKS = ("pause", "stop", "network_delay", "throttle", "memory_limit",
                    "network_partition", "flapping", "kill")


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    hypothesis: str = ""


class Selection(BaseModel):
    """The planner's chosen experiments, most informative first."""

    model_config = ConfigDict(extra="forbid")

    selections: list[Candidate] = Field(default_factory=list)


@functools.lru_cache(maxsize=1)
def blast_radius(services_file: str) -> dict[str, int]:
    """How many frontend pages fail when each service is lost, from docs/services.yaml."""
    try:
        data = yaml.safe_load(Path(services_file).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    calls = ((data.get("services") or {}).get("frontend") or {}).get("calls") or {}
    scores: dict[str, int] = {}
    for info in calls.values():
        for service in (info or {}).get("fatal", []):
            scores[service] = scores.get(service, 0) + 1
    return scores


def rank(services: list[str], blast: dict[str, int], limit: int) -> list[Candidate]:
    """Pick up to `limit` (attack, service) pairs: highest blast radius first, hangs before crashes."""
    ordered = sorted(services, key=lambda s: (-blast.get(s, 0), s))
    out: list[Candidate] = []
    for i, service in enumerate(ordered):
        if len(out) >= limit:
            break
        action = PRIORITY_ATTACKS[i % len(PRIORITY_ATTACKS)]
        attack = get_attack(action)
        out.append(Candidate(
            action=action, target=service, params=dict(DEFAULT_PARAMS.get(action, {})),
            hypothesis=attack.hypothesis.format(target=service),
        ))
    return out


@stub_handler(prompts.TASK)
def _deterministic(payload: dict[str, Any]) -> dict[str, Any]:
    """Offline selection: the blast-radius ranker. Also the LLM's fallback when it has no key."""
    health = payload.get("health") or {}
    if health.get("available") and (health.get("degraded") or not health.get("fresh", True)):
        return {"selections": []}  # unsafe to inject now
    services = list(payload.get("services") or [])
    blast = {k: int(v) for k, v in (payload.get("blast") or {}).items()}
    limit = int(payload.get("limit") or 1)
    return {"selections": [c.model_dump() for c in rank(services, blast, limit)]}


def _catalogue_text() -> str:
    lines = []
    for name, a in CATALOGUE.items():
        params = ", ".join(f"{k} ({v})" for k, v in a.params_help.items()) or "none"
        lines.append(f"- {name}: {a.description} params: {params}")
    return "\n".join(lines)


def select(tools: ToolInterface, goal: str, limit: int,
           services_file: str | None = None) -> tuple[Selection, str]:
    """Choose experiments. Returns the selection and the narrator name (llm:... or stub)."""
    services = allowed_services(tools.config)
    blast = blast_radius(services_file or str(REPO_ROOT / "docs" / "services.yaml"))
    health = tools.get_cluster_health()
    payload = {"goal": goal, "limit": limit, "services": services, "blast": blast, "health": health}
    user = prompts.USER_TEMPLATE.format(
        goal=goal, limit=limit, health=yaml.safe_dump(health, sort_keys=False).strip(),
        catalogue=_catalogue_text(),
        blast=", ".join(f"{s}:{blast.get(s, 0)}" for s in services) or "(unknown)",
        schema=prompts.OUTPUT_SCHEMA,
    )
    client = get_client()
    try:
        selection = client.complete_json(
            task=prompts.TASK, system=prompts.SYSTEM, user=user, schema=Selection, payload=payload
        )
        return selection, client.name
    except LLMError:
        return Selection(**_deterministic(payload)), "stub"


class AttackPlanner:
    """Run one planning session: select, execute each through the tools, conclude."""

    def __init__(self, tools: ToolInterface, services_file: str | None = None) -> None:
        self.tools = tools
        self.services_file = services_file

    def run(self, goal: str, limit: int = 3) -> dict[str, Any]:
        auditor = self.tools.auditor
        recovery = self.tools.check_recovery()
        if not recovery["clean"] and recovery.get("pending_experiment"):
            return auditor.conclusion(
                "did not run: a previous experiment is still open; recover first",
                {"recovery": recovery},
            )

        selection, narrator = select(self.tools, goal, limit, self.services_file)
        auditor.tool_request("select", {"narrator": narrator, "goal": goal, "limit": limit,
                                        "selections": [c.model_dump() for c in selection.selections]})
        if not selection.selections:
            return auditor.conclusion(
                "no experiments selected (cluster unhealthy/stale, or none chosen)",
                {"narrator": narrator, "health": self.tools.get_cluster_health()},
            )

        outcomes = [self._execute(c) for c in selection.selections[:limit]]
        return auditor.conclusion(self._summarise(outcomes), {
            "narrator": narrator, "goal": goal,
            "selected": [c.model_dump() for c in selection.selections[:limit]],
            "outcomes": outcomes,
        })

    def _execute(self, candidate: Candidate) -> dict[str, Any]:
        import inspect

        tool = {
            "kill": self.tools.inject_kill, "stop": self.tools.inject_stop,
            "pause": self.tools.inject_pause, "throttle": self.tools.inject_cpu_throttle,
            "memory_limit": self.tools.inject_memory_limit,
            "network_partition": self.tools.inject_network_partition,
            "network_delay": self.tools.inject_network_delay, "flapping": self.tools.inject_flapping,
        }.get(candidate.action)
        tag = {"action": candidate.action, "target": candidate.target}
        if tool is None:
            return {**tag, "status": "rejected",
                    "detail": f"unapproved experiment type {candidate.action!r}"}
        # Only pass parameters the tool actually takes; a stray one is a planner error, not a run.
        allowed = set(inspect.signature(tool).parameters) - {"target", "duration_s", "hypothesis"}
        extra = set(candidate.params) - allowed
        if extra:
            return {**tag, "status": "error",
                    "detail": f"parameters {sorted(extra)} are not valid for {candidate.action}"}
        result = tool(target=candidate.target, hypothesis=candidate.hypothesis, **candidate.params)
        return {**tag, **result}

    @staticmethod
    def _summarise(outcomes: list[dict[str, Any]]) -> str:
        ran = [o for o in outcomes if o["status"] == "ran"]
        by_status = {s: sum(1 for o in outcomes if o["status"] == s)
                     for s in ("ran", "rejected", "not_approved", "error")}
        parts = [f"{by_status['ran']} ran", f"{by_status['rejected']} rejected by policy",
                 f"{by_status['not_approved']} not approved", f"{by_status['error']} errored"]
        weaknesses = [f"{o['action']} {o['target']} -> {o.get('verdict')}"
                      for o in ran if o.get("verdict") in ("aborted", "failed")]
        tail = f"; weaknesses: {', '.join(weaknesses)}" if weaknesses else ""
        return f"{len(outcomes)} experiment(s): " + ", ".join(parts) + tail


__all__ = ["ATTACK_NAMES", "DEFAULT_DURATION_S", "AttackPlanner", "Candidate", "Selection",
           "blast_radius", "rank", "select"]

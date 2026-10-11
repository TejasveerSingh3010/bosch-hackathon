"""Agent 1's threat board: the five deadliest attacks the catalogue allows, given current health.

Kill, pause and stop are not ranked (see EXCLUDED). Nothing is injected. The planner model
(openai/gpt-oss-120b) is shown every service's live state
(down, health-check latency, CPU, memory, restarts), each service's blast radius from
docs/services.yaml and the catalogue, and asked to rank attacks by user-visible damage. Its answer
is checked like a plan would be: unknown attacks and disallowed targets are dropped, parameters go
through the catalogue's own validation, one entry per target. Gaps are filled by the deterministic
scorer below, which is also the whole answer when there is no LLM key.

The result lands in results/attack_ranking.json, which the dashboard shows next to the chaos tests.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agents.llm import LLMError, get_client, stub_handler
from safety.limits import REPO_ROOT

from . import prompts
from .agent import DEFAULT_PARAMS, PLANNER_MODEL, AttackParams, blast_radius
from .catalogue import CATALOGUE, get_attack
from .interface import ToolInterface, allowed_services

RANKING_FILE = "attack_ranking.json"
TOP_N = 5

#: The plain outages (kill, pause, stop) are left off the board: their damage is just the blast
#: radius, so they would crowd out the subtler faults worth a human's attention.
EXCLUDED = frozenset({"kill", "pause", "stop"})
RANKABLE: tuple[str, ...] = tuple(a for a in CATALOGUE if a not in EXCLUDED)

#: How damaging each fault is to an otherwise healthy service, 0-1. The frontend sets no deadline on
#: backend calls (docs/services.yaml), so isolation and intermittent hangs hurt most.
SEVERITY: dict[str, float] = {
    "network_partition": 0.9, "flapping": 0.8, "network_delay": 0.7, "memory_limit": 0.5, "throttle": 0.4,
}
SLOW_HEALTH_MS = 500  # a health check slower than this means the service is already struggling
BUSY_CPU_PCT = 50
FULL_MEM_PCT = 50


class RankedAttack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    target: str
    params: AttackParams = Field(default_factory=AttackParams)
    deadliness: int = Field(description="0-100: user-visible damage if injected now")
    rationale: str = Field(description="one sentence citing the health data or blast radius used")


class Ranking(BaseModel):
    """The deadliest attacks right now, most deadly first."""

    model_config = ConfigDict(extra="forbid")

    ranked: list[RankedAttack] = Field(default_factory=list)


def page_count(services_file: str) -> int:
    """How many frontend pages there are (each `calls` entry in docs/services.yaml)."""
    try:
        data = yaml.safe_load(Path(services_file).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return 0
    return len(((data.get("services") or {}).get("frontend") or {}).get("calls") or {})


def score(action: str, reach: float, health: dict[str, Any]) -> tuple[float, str]:
    """Deadliness 0-1 of one attack on a service with this blast radius (0-1) and health, and why."""
    value = SEVERITY.get(action, 0.5) * reach
    why: list[str] = []
    if health.get("down"):
        value *= 0.3
        why.append(f"already down ({health.get('reason') or 'unhealthy'}), so little new damage")
        return value, "; ".join(why)
    if action in ("pause", "network_delay", "flapping") and (health.get("health_ms") or 0) >= SLOW_HEALTH_MS:
        value *= 1.3
        why.append(f"health check already {health['health_ms']:.0f} ms, so callers hit deadlines sooner")
    if action == "throttle" and (health.get("cpu_pct") or 0) >= BUSY_CPU_PCT:
        value *= 1.6
        why.append(f"already at {health['cpu_pct']:.0f}% CPU")
    if action == "memory_limit":
        cap = DEFAULT_PARAMS["memory_limit"]["memory_mb"]
        if (health.get("mem_mb") or 0) >= 0.8 * cap:
            value *= 2.2  # OOM-killed and restarted into the same cap: a crash loop
            why.append(f"uses {health['mem_mb']:.0f} MiB, so a {cap} MiB cap likely OOM-kills it in a loop")
        elif (health.get("mem_pct") or 0) >= FULL_MEM_PCT:
            value *= 1.4
            why.append(f"already at {health['mem_pct']:.0f}% of its memory limit")
    if health.get("restarts") or health.get("oom_killed"):
        value *= 1.2
        why.append("has restarted before")
    return min(value, 1.0), "; ".join(why)


def _lost(service: str, blast: dict[str, int], pages: int) -> int:
    """Frontend pages lost with the service; the frontend loses them all."""
    return pages if service == "frontend" else blast.get(service, 0)


def deterministic(services: list[str], blast: dict[str, int], pages: int,
                  health: dict[str, dict[str, Any]], limit: int) -> list[RankedAttack]:
    """Score every (attack, service) pair, keep each service's worst, return the top `limit`."""
    total = max(pages, max(blast.values(), default=0), 1)
    best: dict[str, tuple[float, RankedAttack]] = {}
    for service in services:
        lost = _lost(service, blast, pages)
        reach = max(lost, 0.25) / total  # a tolerated dependency still costs something (a missing ad)
        for action in RANKABLE:
            value, why = score(action, reach, health.get(service, {}))
            if service in best and best[service][0] >= value:
                continue
            title = get_attack(action).title
            base = (f"{title} on {service} takes down {lost}/{total} pages" if lost
                    else f"{title} on {service}: every page tolerates it")
            best[service] = (value, RankedAttack(
                action=action, target=service, params=AttackParams(**DEFAULT_PARAMS.get(action, {})),
                deadliness=round(100 * value), rationale=base + (f"; {why}" if why else ""),
            ))
    ordered = sorted(best.values(), key=lambda pair: (-pair[0], pair[1].target))
    return [attack for _, attack in ordered[:limit]]


@stub_handler(prompts.RANK_TASK)
def _stub(payload: dict[str, Any]) -> dict[str, Any]:
    ranked = deterministic(payload["services"], payload["blast"], payload["pages"],
                           payload["health"], payload["limit"])
    return {"ranked": [r.model_dump() for r in ranked]}


def clean(ranking: Ranking, services: list[str], fallback: list[RankedAttack],
          limit: int) -> list[RankedAttack]:
    """Keep only what the catalogue and target list allow, one per target, topped up from `fallback`."""
    out: list[RankedAttack] = []
    for item in ranking.ranked:
        if item.action not in RANKABLE or item.target not in services:
            continue
        if any(o.target == item.target for o in out):
            continue
        attack = get_attack(item.action)
        try:
            params = attack.normalise(item.params.given())
        except ValueError:
            params = attack.normalise(DEFAULT_PARAMS.get(item.action, {}))
        out.append(item.model_copy(update={"params": AttackParams(**params),
                                           "deadliness": max(0, min(100, item.deadliness))}))
    for item in fallback:
        if len(out) >= limit:
            break
        if not any(o.target == item.target for o in out):
            out.append(item)
    return sorted(out[:limit], key=lambda r: -r.deadliness)


def _catalogue_text() -> str:
    return "\n".join(f"- {name}: {attack.description} [{json.dumps(DEFAULT_PARAMS.get(name, {}))}]"
                     for name, attack in CATALOGUE.items() if name in RANKABLE)


def rank_deadliest(tools: ToolInterface, limit: int = TOP_N,
                   services_file: str | None = None) -> dict[str, Any]:
    """Rank the `limit` deadliest attacks against the cluster as it is now. Returns the document."""
    services_file = services_file or str(REPO_ROOT / "docs" / "services.yaml")
    services = allowed_services(tools.config)
    blast, pages = blast_radius(services_file), page_count(services_file)
    health = tools.get_cluster_health()
    per_service = health.get("services") or {}
    cluster = {k: v for k, v in health.items() if k != "services"}
    payload = {"services": services, "blast": blast, "pages": pages, "health": per_service, "limit": limit}
    fallback = deterministic(services, blast, pages, per_service, limit)

    user = prompts.RANK_USER_TEMPLATE.format(
        limit=limit, health=yaml.safe_dump(cluster, sort_keys=False).strip(),
        services="\n".join(f"- {s}: {json.dumps(per_service.get(s, {}))}" for s in services),
        blast=", ".join(f"{s}:{blast.get(s, 0)}" for s in services if s != "frontend"),
        pages=pages, catalogue=_catalogue_text(), schema=prompts.RANK_SCHEMA,
    )
    client = get_client(PLANNER_MODEL)
    try:
        answer = client.complete_json(task=prompts.RANK_TASK, system=prompts.RANK_SYSTEM, user=user,
                                      schema=Ranking, payload=payload)
        narrator, error = client.name, None
    except LLMError as exc:
        answer, narrator, error = Ranking(ranked=fallback), "stub", str(exc)
    ranked = clean(answer, services, fallback, limit)
    tools.auditor.tool_request("rank_deadliest", {"narrator": narrator, "ranked": [
        {**r.model_dump(exclude={"params"}), "params": r.params.given()} for r in ranked]})
    doc: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "narrator": narrator,
        "safe_to_inject": bool(health.get("available") and health.get("fresh")
                               and not health.get("degraded")),
        "health": cluster,
        "ranked": [_entry(i, r) for i, r in enumerate(ranked, 1)],
    }
    if error:
        doc["llm_error"] = error
    return doc


def _entry(rank: int, item: RankedAttack) -> dict[str, Any]:
    """One board entry: the ranking plus the catalogue text a human reads before approving."""
    attack, fill = get_attack(item.action), {"target": item.target}
    return {
        "rank": rank, "title": attack.title, **item.model_dump(exclude={"params"}),
        "params": item.params.given(), "description": attack.description,
        "hypothesis": attack.hypothesis.format(**fill),
        "expected_observations": attack.expected_observations.format(**fill),
        "recovery_criteria": attack.recovery_criteria.format(**fill),
    }


def write_ranking(doc: dict[str, Any], results_dir: Path) -> Path:
    """Write atomically, so the dashboard never reads half a file."""
    path = Path(results_dir) / RANKING_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


__all__ = ["EXCLUDED", "RANKABLE", "RANKING_FILE", "SEVERITY", "TOP_N", "RankedAttack", "Ranking", "clean",
           "deterministic", "page_count", "rank_deadliest", "score", "write_ranking"]

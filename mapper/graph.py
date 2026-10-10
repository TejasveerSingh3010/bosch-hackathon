"""Turn parsed compose edges into a graph with per-service criticality.

Criticality is what makes the day-2 planner useful: it is the difference between "break something"
and "break the thing most likely to reveal a weakness". It is computed from the graph alone, so it
needs no AI and is fully reproducible:

  * `dependents`        - services that transitively depend on this one (blast radius)
  * `dependencies`      - services this one transitively needs
  * `on_checkout_path`  - whether a purchase passes through it (revenue-critical)
  * `user_facing`       - whether it is reachable by the user directly
  * `single_point`      - no sibling provides the same capability (nothing can take over)
  * `score`             - 0..100, combining the above

The score measures **internal blast radius** - how much of the system sits behind a service -
weighted towards the checkout path and stored state. It is deliberately not a measure of how
visible a failure would be: `frontend` scores low because nothing else depends on it, even though
killing it is a total outage. That is the right bias for choosing experiments, because a frontend
crash reveals nothing anyone doubted, while a `redis-cart` crash tells you whether the cart
degrades gracefully.

The score is a weighted sum, not a learned model, so the planner can quote the reason:
"redis-cart scores 85 because 3 services depend on it, it is on the checkout path and it holds
state".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from .compose import Edge, ParsedCompose, cross_check

#: Entry points: a request from a real user arrives here.
USER_ENTRY = "frontend"
#: The purchase path. Anything reachable from here is revenue-critical.
CHECKOUT_ROOT = "checkoutservice"
#: Not part of the application under test; excluded from scoring and never a target.
INFRASTRUCTURE = {"loadgenerator", "dashboard", "monitor"}

#: Score weights (they sum to 100 at their maxima).
W_DEPENDENTS = 40.0
W_CHECKOUT = 25.0
W_USER_FACING = 15.0
W_SINGLE_POINT = 10.0
W_STATEFUL = 10.0

#: Services holding state: losing one can lose data, not just availability.
STATEFUL_HINTS = ("redis", "db", "database", "postgres", "mysql", "mongo", "cache")


@dataclass
class ServiceInfo:
    name: str
    dependencies: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    direct_dependencies: list[str] = field(default_factory=list)
    direct_dependents: list[str] = field(default_factory=list)
    on_checkout_path: bool = False
    user_facing: bool = False
    stateful: bool = False
    single_point: bool = False
    infrastructure: bool = False
    restart_policy: str = "no"
    score: float = 0.0
    rationale: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 1),
            "rationale": self.rationale,
            "dependents": self.dependents,
            "dependencies": self.dependencies,
            "direct_dependents": self.direct_dependents,
            "direct_dependencies": self.direct_dependencies,
            "on_checkout_path": self.on_checkout_path,
            "user_facing": self.user_facing,
            "stateful": self.stateful,
            "single_point_of_failure": self.single_point,
            "infrastructure": self.infrastructure,
            "restart_policy": self.restart_policy,
        }


class DependencyGraph:
    """A directed graph where an edge `a -> b` means "a calls b"."""

    def __init__(self, parsed: ParsedCompose) -> None:
        self.parsed = parsed
        self.graph = nx.DiGraph()
        self.graph.add_nodes_from(parsed.services)
        for edge in parsed.edges:
            if edge.evidence == "env" or not self.graph.has_edge(edge.src, edge.dst):
                self.graph.add_edge(edge.src, edge.dst, type=edge.type,
                                    evidence=edge.evidence, port=edge.port)
        self.services: dict[str, ServiceInfo] = self._analyse()

    # ---------------------------------------------------------------- analysis
    def _analyse(self) -> dict[str, ServiceInfo]:
        graph = self.graph
        checkout_reach: set[str] = set()
        if CHECKOUT_ROOT in graph:
            checkout_reach = {CHECKOUT_ROOT} | nx.descendants(graph, CHECKOUT_ROOT)

        out: dict[str, ServiceInfo] = {}
        for name in graph.nodes:
            dependents = sorted(nx.ancestors(graph, name) - INFRASTRUCTURE)
            dependencies = sorted(nx.descendants(graph, name))
            info = ServiceInfo(
                name=name,
                dependencies=dependencies,
                dependents=dependents,
                direct_dependencies=sorted(graph.successors(name)),
                direct_dependents=sorted(n for n in graph.predecessors(name) if n not in INFRASTRUCTURE),
                on_checkout_path=name in checkout_reach,
                user_facing=name == USER_ENTRY,
                stateful=any(hint in name.lower() for hint in STATEFUL_HINTS),
                infrastructure=name in INFRASTRUCTURE,
                restart_policy=self.parsed.restart_policies.get(name, "no"),
            )
            out[name] = info

        # A service is a single point of failure when at least one other service needs it and
        # nothing else offers the same capability (no sibling shares all of its dependents).
        app_nodes = [n for n, i in out.items() if not i.infrastructure]
        for name in app_nodes:
            info = out[name]
            if info.user_facing:
                # Nothing else serves users, so the entry point is always a single point of failure
                # even though no other service calls it.
                info.single_point = True
                continue
            if not info.direct_dependents:
                info.single_point = False
                continue
            info.single_point = not any(
                other != name
                and set(out[other].direct_dependents) >= set(info.direct_dependents)
                and out[other].direct_dependencies == info.direct_dependencies
                for other in app_nodes
            )

        max_dependents = max((len(out[n].dependents) for n in app_nodes), default=0) or 1
        for name in app_nodes:
            info = out[name]
            reasons = []
            score = W_DEPENDENTS * len(info.dependents) / max_dependents
            if info.dependents:
                reasons.append(f"{len(info.dependents)} service(s) depend on it")
            if info.on_checkout_path:
                score += W_CHECKOUT
                reasons.append("on the checkout path")
            if info.user_facing:
                score += W_USER_FACING
                reasons.append("user-facing entry point")
            if info.single_point:
                score += W_SINGLE_POINT
                reasons.append("single point of failure")
            if info.stateful:
                score += W_STATEFUL
                reasons.append("holds state")
            if info.restart_policy in ("no", ""):
                reasons.append("no restart policy, so it will not recover by itself")
            info.score = min(100.0, score)
            info.rationale = "; ".join(reasons) or "no other service depends on it"
        return out

    # ---------------------------------------------------------------- outputs
    def ranked(self, include_infrastructure: bool = False) -> list[ServiceInfo]:
        """Services ordered by criticality - the day-2 planner's starting point."""
        items = [i for i in self.services.values() if include_infrastructure or not i.infrastructure]
        return sorted(items, key=lambda i: (-i.score, i.name))

    def cycles(self) -> list[list[str]]:
        return [sorted(c) for c in nx.simple_cycles(self.graph)]

    def to_json(self) -> dict[str, Any]:
        """Superset of docs/graph.example.json: same `services` and `edges`, plus analysis."""
        edges = [Edge(src, dst, data.get("type", "grpc"), data.get("evidence", "env"), data.get("port"))
                 for src, dst, data in self.graph.edges(data=True)]
        warnings = list(self.parsed.warnings)
        cycles = self.cycles()
        if cycles:
            warnings.append("dependency cycle(s): " + "; ".join("->".join(c) for c in cycles))
        return {
            "generated_from": "docker-compose.yml (*_ADDR environment variables and depends_on)",
            "project": self.parsed.project,
            "services": sorted(self.graph.nodes),
            "edges": [e.to_json() for e in sorted(edges, key=lambda e: (e.src, e.dst))],
            "analysis": {name: info.to_json() for name, info in sorted(self.services.items())},
            "ranked_targets": [i.name for i in self.ranked()],
            "cross_check": cross_check(self.parsed),
            "warnings": warnings,
        }

    def to_text(self) -> str:
        lines = [f"services: {len(self.graph.nodes)}   call edges: {self.graph.number_of_edges()}", ""]
        lines.append(f"{'SERVICE':<24} {'SCORE':>6}  WHY")
        for info in self.ranked():
            lines.append(f"{info.name:<24} {info.score:>6.1f}  {info.rationale}")
        infra = [i.name for i in self.services.values() if i.infrastructure]
        if infra:
            lines.append(f"\nexcluded as infrastructure: {', '.join(sorted(infra))}")
        for note in cross_check(self.parsed):
            lines.append(f"cross-check: {note}")
        for warning in self.parsed.warnings:
            lines.append(f"warning: {warning}")
        return "\n".join(lines)


__all__ = ["CHECKOUT_ROOT", "INFRASTRUCTURE", "USER_ENTRY", "DependencyGraph", "ServiceInfo"]

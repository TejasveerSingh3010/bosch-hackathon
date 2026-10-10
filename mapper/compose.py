"""Derive the service dependency graph from docker-compose.yml.

Two signals are read, because neither is sufficient on its own:

  * **`*_ADDR` environment variables** - the real call edges. Online Boutique wires every
    dependency as `PRODUCT_CATALOG_SERVICE_ADDR: productcatalogservice:3550`, so these give the
    actual runtime topology.
  * **`depends_on`** - only a startup ordering hint, and incomplete in this compose file
    (`recommendationservice` depends on productcatalogservice but several others list nothing).
    It is recorded separately as a weaker signal and never used alone.

`REFERENCE_EDGES` holds the topology from the upstream project's architecture diagram. The parsed
graph is checked against it so the map can honestly be described as derived automatically *and*
cross-checked, and so a drift between the compose file and the documentation shows up as a warning
rather than a silent wrong answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: Env var suffixes that name a dependency.
ADDR_SUFFIX = "_ADDR"

#: Services that speak plain TCP rather than gRPC/HTTP (used to label edges).
TCP_SERVICES = {"redis-cart"}

#: Edges from the upstream Online Boutique architecture diagram, used only as a cross-check.
REFERENCE_EDGES: set[tuple[str, str]] = {
    ("frontend", "adservice"),
    ("frontend", "cartservice"),
    ("frontend", "checkoutservice"),
    ("frontend", "currencyservice"),
    ("frontend", "productcatalogservice"),
    ("frontend", "recommendationservice"),
    ("frontend", "shippingservice"),
    ("checkoutservice", "cartservice"),
    ("checkoutservice", "currencyservice"),
    ("checkoutservice", "emailservice"),
    ("checkoutservice", "paymentservice"),
    ("checkoutservice", "productcatalogservice"),
    ("checkoutservice", "shippingservice"),
    ("recommendationservice", "productcatalogservice"),
    ("cartservice", "redis-cart"),
    ("loadgenerator", "frontend"),
}


@dataclass
class Edge:
    """One dependency: `src` calls `dst`."""

    src: str
    dst: str
    type: str = "grpc"
    #: "env" when derived from a *_ADDR variable, "depends_on" when only an ordering hint.
    evidence: str = "env"
    port: int | None = None

    def key(self) -> tuple[str, str]:
        return (self.src, self.dst)

    def to_json(self) -> dict[str, Any]:
        """Matches docs/graph.example.json, with the extra fields appended."""
        out: dict[str, Any] = {"from": self.src, "to": self.dst, "type": self.type}
        if self.port:
            out["port"] = self.port
        out["evidence"] = self.evidence
        return out


@dataclass
class ParsedCompose:
    services: list[str] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    images: dict[str, str] = field(default_factory=dict)
    ports: dict[str, list[str]] = field(default_factory=dict)
    restart_policies: dict[str, str] = field(default_factory=dict)
    project: str | None = None
    warnings: list[str] = field(default_factory=list)


def _service_from_addr(value: str) -> tuple[str | None, int | None]:
    """`productcatalogservice:3550` -> ("productcatalogservice", 3550)."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    host, _, port = value.strip().partition(":")
    host = host.strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        return None, None
    try:
        return host, int(port) if port else None
    except ValueError:
        return host, None


def _as_env_dict(env: Any) -> dict[str, str]:
    """Compose allows a mapping or a `KEY=value` list; normalise both."""
    if isinstance(env, dict):
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    if isinstance(env, list):
        out = {}
        for item in env:
            key, _, value = str(item).partition("=")
            out[key.strip()] = value.strip()
        return out
    return {}


def parse_compose(path: Path) -> ParsedCompose:
    """Read a compose file and return its services and dependency edges."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"compose file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    services_raw: dict[str, Any] = raw.get("services") or {}
    if not services_raw:
        raise ValueError(f"{path} has no `services:` section")

    result = ParsedCompose(services=sorted(services_raw), project=raw.get("name"))
    known = set(services_raw)

    for name, spec in services_raw.items():
        spec = spec or {}
        if spec.get("image"):
            result.images[name] = str(spec["image"])
        if spec.get("ports"):
            result.ports[name] = [str(p) for p in spec["ports"]]
        result.restart_policies[name] = str(spec.get("restart") or "no")

        env = _as_env_dict(spec.get("environment"))
        for key, value in env.items():
            if not key.endswith(ADDR_SUFFIX):
                continue
            host, port = _service_from_addr(value)
            if host is None:
                continue
            if host not in known:
                result.warnings.append(
                    f"{name} is configured to call {host!r} (via {key}), but no such service exists "
                    f"in this compose file - the dependency is unsatisfiable and is left out of the graph"
                )
                continue
            if host == name:
                continue
            edge_type = "tcp" if host in TCP_SERVICES else "grpc"
            result.edges.append(Edge(name, host, edge_type, "env", port))

        for dep in spec.get("depends_on") or []:
            dep = str(dep)
            if dep in known and dep != name:
                result.edges.append(Edge(name, dep, "tcp" if dep in TCP_SERVICES else "grpc", "depends_on"))

    # de-duplicate, preferring env evidence over depends_on
    best: dict[tuple[str, str], Edge] = {}
    for edge in result.edges:
        existing = best.get(edge.key())
        if existing is None or (existing.evidence == "depends_on" and edge.evidence == "env"):
            best[edge.key()] = edge
    result.edges = sorted(best.values(), key=lambda e: (e.src, e.dst))
    return result


def cross_check(parsed: ParsedCompose, reference: set[tuple[str, str]] | None = None) -> list[str]:
    """Compare the parsed call edges with the documented architecture."""
    reference = REFERENCE_EDGES if reference is None else reference
    found = {e.key() for e in parsed.edges if e.evidence == "env"}
    known_services = set(parsed.services)
    notes: list[str] = []

    missing = {e for e in reference if e not in found and set(e) <= known_services}
    extra = {e for e in found if e not in reference}
    if missing:
        notes.append("documented but not found in the compose file: "
                     + ", ".join(f"{a}->{b}" for a, b in sorted(missing)))
    if extra:
        notes.append("found in the compose file but not in the documented diagram: "
                     + ", ".join(f"{a}->{b}" for a, b in sorted(extra)))
    if not missing and not extra:
        notes.append(f"all {len(found)} call edges match the documented architecture")
    return notes


__all__ = ["ADDR_SUFFIX", "REFERENCE_EDGES", "Edge", "ParsedCompose", "cross_check", "parse_compose"]

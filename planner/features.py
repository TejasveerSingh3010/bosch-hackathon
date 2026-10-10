"""Turn one catalogue entry into the feature set the ranker scores (and the LLM would see).

These are facts about the attack and the app's current state, never raw logs: the LLM ranking stage
is given only this, by design (the raw evidence is Agent 2's job, after an attack has run). Every
feature traces to a source - the dependency graph, the service-to-page table, the telemetry sample,
or an earlier result - so the rationale a ranking produces can cite where each number came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .catalogue import AttackSpec

#: Which page probes fail when a service is down, from the README's service-to-probe table
#: (monitor.boutique_core.SERVICE_IMPACT for v0.10.7). Replicated here to keep the planner
#: import-light; it is static because the upstream images are pinned.
SERVICE_PAGES: dict[str, list[str]] = {
    "frontend": ["Home", "Product", "Cart", "Checkout"],
    "productcatalogservice": ["Home", "Product", "Checkout"],
    "currencyservice": ["Home", "Product", "Cart", "Checkout"],
    "cartservice": ["Home", "Product", "Cart", "Checkout"],
    "redis-cart": ["Home", "Product", "Cart", "Checkout"],
    "shippingservice": ["Cart", "Checkout"],
    "checkoutservice": ["Checkout"],
    "paymentservice": ["Checkout"],
    "emailservice": [],
    "adservice": [],
    "recommendationservice": [],
}


@dataclass
class AttackFeatures:
    """Everything the ranker needs about one candidate attack. All facts, each with a source."""

    criticality: float = 0.0
    criticality_reason: str = ""
    pages_affected: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    on_checkout_path: bool = False
    single_point: bool = False
    stateful: bool = False
    self_heal_expected: bool = False
    app_degraded: bool = False
    prior_run_id: str | None = None
    prior_verdict: str | None = None
    preflight_verdict: str = "allow"
    preflight_reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    @property
    def recovery_difficulty(self) -> str:
        if self.stateful:
            return "hard"
        if self.preflight_verdict == "refuse":
            return "unknown"
        if not self.self_heal_expected:
            return "moderate"
        return "easy"


def compute_features(
    spec: AttackSpec,
    graph: dict[str, Any] | None,
    prior_results: dict[str, dict[str, Any]],
    app_degraded: bool,
    preflight: tuple[str, str],
) -> AttackFeatures:
    """Assemble the feature set for one attack from the graph, the page table and prior runs."""
    analysis = ((graph or {}).get("analysis") or {}).get(spec.target, {}) or {}
    evidence: list[str] = []
    if analysis:
        evidence.append(f"graph:{spec.target}")
    pages = SERVICE_PAGES.get(spec.target, [])
    if spec.target in SERVICE_PAGES:
        evidence.append("pages-table")

    prior = prior_results.get(spec.key)
    if prior:
        evidence.append(f"result:{prior.get('id')}")

    verdict, reason = preflight
    evidence.append("telemetry:app_state")

    return AttackFeatures(
        criticality=float(analysis.get("score") or 0.0),
        criticality_reason=str(analysis.get("rationale") or "no dependency-graph data"),
        pages_affected=list(pages),
        dependents=list(analysis.get("dependents") or []),
        on_checkout_path=bool(analysis.get("on_checkout_path")),
        single_point=bool(analysis.get("single_point_of_failure")),
        stateful=bool(analysis.get("stateful")),
        self_heal_expected=spec.self_heal_expected,
        app_degraded=app_degraded,
        prior_run_id=(prior or {}).get("id"),
        prior_verdict=(prior or {}).get("verdict"),
        preflight_verdict=verdict,
        preflight_reason=reason,
        evidence_refs=evidence,
    )


__all__ = ["SERVICE_PAGES", "AttackFeatures", "compute_features"]

"""Agent 3's engine: turn failure reports into a prioritised, deduplicated list of fixes.

Recommendations only - it never edits the Compose file, config or code. The deterministic path (the
Phase 1 default and permanent fallback) maps each report's failure signature to a root cause and a
concrete Compose/config fix, grounded against what the Compose file already declares; the LLM
replaces this behind the same interface (TODO(llm)) and must cite the same report/evidence ids.

Inputs are the reports and the Compose file plus graph.json - never raw logs or telemetry. The
reports' evidence ids are the trail back to those.
"""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agents.llm import LLMClient
from agents.schemas import (
    Praise,
    Recommendation,
    Recommendations,
    RootCause,
    Verification,
)
from schemas import Experiment

from . import prompts

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"
DEFAULT_COMPOSE = REPO_ROOT / "app" / "docker-compose.yml"
DEFAULT_GRAPH = REPO_ROOT / "results" / "graph.json"

EFFORT_COST = {"S": 1, "M": 2, "L": 3}

#: How each fault's failure maps to a root cause and a fix, when the report shows real impact.
#: (root_cause, fix text, scope, metric the fix should move, effort, risk).
FAULT_FIX: dict[str, tuple[RootCause, str, str, str, str, str]] = {
    "stop": ("single_point_of_failure",
             "Run a second replica of this service (compose `deploy.replicas: 2` behind the "
             "frontend's client-side load balancing), or add a graceful fallback so its outage "
             "does not fail the page.", "compose", "error_rate_pct on the affected pages", "L", "medium"),
    "kill": ("slow_recovery",
             "Add a Compose `healthcheck` so readiness is gated on the service actually serving, "
             "which shortens detection and keeps traffic off it until it is back.",
             "compose", "detection_seconds / recovery_seconds", "S", "low"),
    "pause": ("no_timeout_or_retry",
              "Give callers a short client timeout and a retry/circuit-breaker so a hung dependency "
              "fails fast instead of tying up connections (timeouts live in the caller's code, so "
              "this needs an upstream change; a Compose `healthcheck` with `depends_on: condition: "
              "service_healthy` mitigates it meanwhile).",
              "upstream_code", "p95_latency_ms, error_rate_pct", "M", "medium"),
    "network_partition": ("no_graceful_degradation",
                          "Add a fallback path or cached response so an unreachable dependency "
                          "degrades the feature, not the whole page; add a `healthcheck` so it is "
                          "detected quickly.", "config", "error_rate_pct, failed pages", "M", "medium"),
    "network_delay": ("no_timeout_or_retry",
                      "Set client timeouts so a slow link fails fast rather than dragging p95 up "
                      "across callers (caller-side, so upstream).",
                      "upstream_code", "p95_latency_ms", "M", "medium"),
    "cpu_throttle": ("no_resource_limits",
                     "Declare CPU reservations/limits in Compose `deploy.resources` so this service "
                     "is guaranteed enough CPU and cannot be starved by a noisy neighbour.",
                     "compose", "p95_latency_ms", "S", "low"),
    "memory_limit": ("no_resource_limits",
                     "Declare a memory reservation/limit in Compose `deploy.resources` sized above "
                     "steady-state, and investigate growth if it is still OOM-killed.",
                     "compose", "restart_count, error_rate_pct", "S", "low"),
    "flapping": ("no_health_check",
                 "Add a Compose `healthcheck` so an intermittently-failing instance is marked "
                 "unready and taken out of rotation instead of serving errors between flaps.",
                 "compose", "error_rate_pct, detection_seconds", "S", "low"),
    "spike": ("unbounded_load",
              "Protect the frontend from surges: add rate limiting / connection caps and CPU "
              "reservations so a traffic spike sheds load gracefully instead of breaching the SLA.",
              "config", "p95_latency_ms, error_rate_pct", "M", "medium"),
}


@dataclass
class _RawRec:
    service: str
    action: str
    root_cause: RootCause
    fix: str
    scope: str
    metric: str
    effort: str
    risk: str
    impact: float
    confidence: float
    needs_more_evidence: bool
    report_ids: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    problem: str = ""


def _load_reports(reports_dir: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(reports_dir.glob("*.json")):
        if path.name == "recommendations.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "experiment_id" in data:
            reports.append(data)
    return reports


def _compose_facts(compose_path: Path) -> dict[str, dict[str, Any]]:
    """Per service: what it already declares, so we do not recommend what is already there."""
    try:
        doc = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    facts: dict[str, dict[str, Any]] = {}
    for name, svc in (doc.get("services") or {}).items():
        svc = svc or {}
        deploy = svc.get("deploy") or {}
        facts[name] = {
            "has_healthcheck": "healthcheck" in svc,
            "has_resources": bool(deploy.get("resources")),
            "replicas": int(deploy.get("replicas", 1) or 1),
            "restart": svc.get("restart", "no"),
        }
    return facts


def _impact_score(report: dict[str, Any], analysis: dict[str, Any]) -> float:
    impact = report.get("impact") or {}
    pages = len(impact.get("failed_endpoints") or [])
    err = float(impact.get("peak_error_rate_pct") or 0.0)
    checkout = 2.0 if analysis.get("on_checkout_path") else 1.0
    spof = 1.5 if analysis.get("single_point_of_failure") else 1.0
    return (pages * 10 + err) * checkout * spof


def _raw_rec(report: dict[str, Any], graph: dict[str, Any] | None,
             compose: dict[str, dict[str, Any]]) -> _RawRec | None:
    service = report.get("service") or "unknown"
    action = report.get("action") or report.get("experiment_id", "").split("-")[0]
    analysis = ((graph or {}).get("analysis") or {}).get(service, {}) or {}
    impact = report.get("impact") or {}
    failed = impact.get("failed_endpoints") or []
    verdict = report.get("verdict")

    report_id = report.get("experiment_id")
    evidence = [report_id] + list((report.get("failure_origin") or {}).get("evidence_ids") or [])
    evidence += list(impact.get("evidence_ids") or [])

    # A tolerated run is not a problem to fix - it becomes praise elsewhere.
    origin_component = (report.get("failure_origin") or {}).get("component")
    degraded = bool(failed) or verdict in ("aborted", "stopped") or bool(origin_component)
    if not degraded:
        return None

    base = FAULT_FIX.get(action)
    if base is None:
        return None
    root_cause, fix, scope, metric, effort, risk = base

    # Impact override: a single point of failure on the checkout path is the headline problem.
    spof_on_checkout = analysis.get("single_point_of_failure") and analysis.get("on_checkout_path")
    if spof_on_checkout and action in ("stop", "pause"):
        root_cause = "single_point_of_failure"

    # Ground against Compose: drop a health-check suggestion if one already exists, etc.
    facts = compose.get(service, {})
    notes = []
    if root_cause == "no_health_check" and facts.get("has_healthcheck"):
        notes.append("a healthcheck already exists; tune its thresholds instead")
        confidence = 0.4
    elif root_cause == "no_resource_limits" and facts.get("has_resources"):
        notes.append("resource limits already exist; raise the reservation")
        confidence = 0.5
    else:
        confidence = 0.75

    # Thin evidence -> recommend gathering more rather than guessing a fix.
    unknowns = report.get("unknowns") or []
    needs_more = len(evidence) <= 1 or (not failed and bool(unknowns))
    if needs_more:
        confidence = min(confidence, 0.35)

    problem = (f"A {action} on {service} caused "
               + (f"{', '.join(failed)} to fail" if failed else f"a {verdict} verdict")
               + (f" ({'; '.join(notes)})" if notes else ""))

    return _RawRec(
        service=service, action=action, root_cause=root_cause,
        fix=fix + ((" Note: " + "; ".join(notes)) if notes else ""),
        scope=scope, metric=metric, effort=effort, risk=risk,
        impact=_impact_score(report, analysis), confidence=confidence,
        needs_more_evidence=needs_more, report_ids=[report_id],
        evidence_refs=[e for e in evidence if e], problem=problem,
    )


def _verification(action: str, service: str) -> Verification:
    """Re-run the same fault and state what result would prove the fix worked."""
    params: dict[str, Any] = {}
    duration = 30
    if action == "spike":
        params = {"rps": 50, "users": 10, "time": duration}
    attack: Experiment | None = None
    with contextlib.suppress(Exception):
        attack = Experiment(id=f"verify-{action}-{service}".replace("_", "-"),
                            target=service, action=action, params=params, duration_s=duration,
                            reason=f"verify the fix for {action} on {service}")
    criteria = ("re-run this attack; the fix works if the previously-failed pages stay up, the peak "
                "error rate stays under the abort threshold, and recovery is no slower than before")
    return Verification(attack=attack, attack_summary=f"{action} on {service}", success_criteria=criteria)


def _merge(raws: list[_RawRec]) -> list[_RawRec]:
    """Deduplicate: one fix per (service, root_cause), linking every report it covers."""
    merged: dict[tuple[str, str], _RawRec] = {}
    for raw in raws:
        key = (raw.service, raw.root_cause)
        if key not in merged:
            merged[key] = raw
            continue
        existing = merged[key]
        existing.report_ids += [r for r in raw.report_ids if r not in existing.report_ids]
        existing.evidence_refs += [e for e in raw.evidence_refs if e not in existing.evidence_refs]
        existing.impact = max(existing.impact, raw.impact)
        existing.confidence = max(existing.confidence, raw.confidence)
        existing.needs_more_evidence = existing.needs_more_evidence and raw.needs_more_evidence
    return list(merged.values())


def _praise(reports: list[dict[str, Any]]) -> list[Praise]:
    seen: set[str] = set()
    out: list[Praise] = []
    for report in reports:
        for obs in report.get("what_worked") or []:
            statement = obs.get("statement", "")
            if statement and statement not in seen:
                seen.add(statement)
                out.append(Praise(statement=statement,
                                  evidence_refs=[report["experiment_id"], *obs.get("evidence_ids", [])]))
    return out[:6]  # keep it brief


def build_recommendations(
    reports_dir: Path | str = DEFAULT_REPORTS_DIR,
    compose_path: Path | str = DEFAULT_COMPOSE,
    graph_path: Path | str = DEFAULT_GRAPH,
    client: LLMClient | None = None,
) -> Recommendations:
    reports = _load_reports(Path(reports_dir))
    compose = _compose_facts(Path(compose_path))
    graph = None
    with contextlib.suppress(OSError, json.JSONDecodeError):
        graph = json.loads(Path(graph_path).read_text(encoding="utf-8"))

    recs = _deterministic(reports, graph, compose)
    if client is not None:
        with contextlib.suppress(Exception):  # deterministic result is the permanent fallback
            recs = _llm(reports, compose, client)  # TODO(llm)
    return recs


def _deterministic(reports: list[dict[str, Any]], graph: dict[str, Any] | None,
                   compose: dict[str, dict[str, Any]]) -> Recommendations:
    raws = [r for r in (_raw_rec(rep, graph, compose) for rep in reports) if r is not None]
    merged = _merge(raws)
    merged.sort(key=lambda r: r.impact / EFFORT_COST.get(r.effort, 2), reverse=True)

    recommendations: list[Recommendation] = []
    for index, raw in enumerate(merged, start=1):
        rec_id = f"R{index}-{raw.root_cause}"
        expected = f"removes or reduces the failure a {raw.action} on {raw.service} caused"
        if raw.needs_more_evidence:
            fix = (f"Evidence is thin. Before changing anything, re-run a {raw.action} on "
                   f"{raw.service} with logging on to confirm the failure mode. Candidate fix: {raw.fix}")
        else:
            fix = raw.fix
        recommendations.append(Recommendation(
            id=rec_id, priority=index, problem=raw.problem, root_cause=raw.root_cause,
            fix=fix, scope=raw.scope, expected_effect=expected, metric=raw.metric,
            effort=raw.effort, risk=raw.risk, confidence=raw.confidence,
            needs_more_evidence=raw.needs_more_evidence,
            verification=_verification(raw.action, raw.service),
            report_ids=raw.report_ids, evidence_refs=raw.evidence_refs,
        ))

    notes = []
    if not reports:
        notes.append("no reports found in reports/; run some experiments and `python -m reporter <id>` first")
    return Recommendations(source="deterministic", recommendations=recommendations,
                           praise=_praise(reports), notes=notes)


def _llm(reports: list[dict[str, Any]], compose: dict[str, dict[str, Any]],
         client: LLMClient) -> Recommendations:
    """Single Agent 3 model call. TODO(llm): client handles validation/retry/logging."""
    evidence_ids = [r.get("experiment_id") for r in reports]
    recs = client.generate(
        agent="advisor",
        system=prompts.SYSTEM_PROMPT,
        user=prompts.build_user_prompt(reports, compose),
        schema=Recommendations,
        evidence_ids=[e for e in evidence_ids if e],
    )
    recs.source = "llm"
    return recs


# --------------------------------------------------------------------- rendering + writing
def render_markdown(recs: Recommendations) -> str:
    lines = [
        "# Fix recommendations",
        "",
        f"*Source: {recs.source} · {len(recs.recommendations)} recommendation(s). "
        f"Recommendations only - nothing here has been applied.*",
        "",
    ]
    for note in recs.notes:
        lines.append(f"> note: {note}")
    if recs.notes:
        lines.append("")
    for rec in recs.recommendations:
        lines += [
            f"## {rec.priority}. {rec.problem}",
            "",
            f"- **Root cause:** {rec.root_cause}",
            f"- **Fix ({rec.scope}):** {rec.fix}",
            f"- **Expected effect:** {rec.expected_effect} (watch {rec.metric})",
            f"- **Effort:** {rec.effort} · **Risk:** {rec.risk} · **Confidence:** {rec.confidence}",
            f"- **Verify:** {rec.verification.attack_summary} - {rec.verification.success_criteria}",
            f"- **Covers reports:** {', '.join(rec.report_ids)}",
            f"- **Evidence:** {', '.join(rec.evidence_refs) or 'none'}",
            "",
        ]
    if recs.praise:
        lines += ["## What held up (do not touch)", ""]
        lines += [f"- {p.statement} [{', '.join(p.evidence_refs)}]" for p in recs.praise]
        lines.append("")
    return "\n".join(lines)


def write_recommendations(recs: Recommendations,
                          reports_dir: Path | str = DEFAULT_REPORTS_DIR) -> tuple[Path, Path]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "recommendations.json"
    md_path = reports_dir / "recommendations.md"
    json_path.write_text(recs.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(recs), encoding="utf-8")
    return md_path, json_path


def generate_recommendations(
    reports_dir: Path | str = DEFAULT_REPORTS_DIR,
    compose_path: Path | str = DEFAULT_COMPOSE,
    graph_path: Path | str = DEFAULT_GRAPH,
    client: LLMClient | None = None,
) -> Recommendations:
    recs = build_recommendations(reports_dir, compose_path, graph_path, client=client)
    write_recommendations(recs, reports_dir)
    return recs


__all__ = [
    "FAULT_FIX",
    "build_recommendations",
    "generate_recommendations",
    "render_markdown",
    "write_recommendations",
]

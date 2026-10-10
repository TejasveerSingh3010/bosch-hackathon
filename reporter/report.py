"""Agent 2's report builder: evidence pack in, Report (and markdown) out.

The deterministic path - the Phase 1 default and the permanent offline fallback - states only facts
drawn from the pack, each citing its evidence ids, with no narrative. When a client is configured
the LLM writes the prose instead (TODO(llm)), but it is handed only the same pack and must cite the
same ids, so the two paths make the same kind of claims.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.llm import LLMClient
from agents.schemas import (
    EvidenceRef,
    FailureOrigin,
    Impact,
    Observation,
    PropagationStep,
    Report,
    Timings,
)

from . import prompts
from .evidence import EvidencePack, build_pack, component_degradation

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"
PAGES = ("Home", "Cart", "Product", "Checkout")


def build_report(pack: EvidencePack, client: LLMClient | None = None) -> Report:
    """Build the Report. Deterministic unless a client is supplied (then LLM narrative, TODO(llm))."""
    report = _deterministic_report(pack)
    if client is not None:
        # The deterministic report is the permanent fallback if the LLM call fails for any reason.
        with contextlib.suppress(Exception):
            report = _llm_report(pack, client)  # TODO(llm)
    return report


def _deterministic_report(pack: EvidencePack) -> Report:
    result = pack.result
    service = result.get("service")
    during = result.get("during") or {}
    analysis = ((pack.graph or {}).get("analysis") or {}).get(service or "", {}) or {}
    callers = set(analysis.get("direct_dependents") or analysis.get("dependents") or [])
    degradation = component_degradation(pack.window)

    # --- failure origin ---
    origin = FailureOrigin(note="no degradation was observed in the evidence window")
    first = pack.first_degraded()
    if first is not None:
        component = first.data.get("component")
        origin = FailureOrigin(
            component=component,
            at=first.at.isoformat() if first.at else None,
            is_injected_target=(component == service),
            evidence_ids=[first.id],
            note=("the injected target degraded first"
                  if component == service else
                  f"{component} degraded first; it is not the injected target ({service})"),
        )

    # --- propagation ---
    propagation: list[PropagationStep] = []
    for kind, name, at in degradation:
        role = "target" if name == service else (
            "caller" if name in callers else ("page" if kind == "page" else "service"))
        propagation.append(PropagationStep(
            component=name, role=role, degraded_at=at.isoformat(),
            evidence_ids=pack.ids_where(component=name, kind="degraded"),
        ))

    # --- impact ---
    baseline_p95 = ((pack.baseline or {}).get("overall") or {}).get("sample_p95_ms", {}).get("mean")
    impact = Impact(
        failed_endpoints=list(during.get("failed_endpoints") or []),
        error_types=dict(during.get("error_types") or {}),
        peak_error_rate_pct=during.get("error_rate_pct_max"),
        peak_p95_ms=during.get("p95_ms_max"),
        baseline_p95_ms=baseline_p95,
        evidence_ids=(pack.ids_where(kind="peak_error") + pack.ids_where(kind="peak_p95")
                      + pack.ids_where(kind="result")),
    )

    # --- timings ---
    timings = Timings(
        detection_seconds=result.get("detection_seconds"),
        recovery_seconds=result.get("recovery_seconds"),
        self_healed=result.get("self_healed"),
        evidence_ids=pack.ids_where(kind="result"),
    )

    # --- what worked (observed facts) ---
    known_pages = {p for s in pack.window for p in (s.get("metrics") or {})}
    failed = set(during.get("failed_endpoints") or [])
    stayed_up = sorted(known_pages - failed)
    what_worked: list[Observation] = []
    if stayed_up:
        what_worked.append(Observation(
            statement=f"these pages kept working throughout: {', '.join(stayed_up)}",
            kind="observed", evidence_ids=pack.ids_where(kind="result"),
        ))
    if result.get("self_healed"):
        what_worked.append(Observation(
            statement="Docker's restart policy recovered the container before rollback",
            kind="observed", evidence_ids=pack.ids_where(kind="timeline") or pack.ids_where(kind="result"),
        ))
    if result.get("verdict") == "completed" and not failed:
        what_worked.append(Observation(
            statement="the application tolerated the fault: no page probe failed while it was in place",
            kind="observed", evidence_ids=pack.ids_where(kind="result"),
        ))

    # --- anomalies (inferences) and unknowns ---
    anomalies: list[Observation] = []
    for kind, name, _at in degradation:
        if kind == "service" and name != service and name not in callers:
            anomalies.append(Observation(
                statement=f"{name} degraded but neither is the target nor a known caller of it - "
                          f"possibly an independent failure",
                kind="inferred", evidence_ids=pack.ids_where(component=name, kind="degraded"),
            ))
    unknowns: list[str] = []
    if not pack.window:
        unknowns.append("no telemetry samples were found for the evidence window")
    if not pack.ids_for_source("logs"):
        unknowns.append("no container logs were captured, so the underlying error text is unknown")

    summary = _facts_summary(result, service, first, impact)
    return Report(
        experiment_id=pack.experiment_id,
        source="deterministic",
        verdict=result.get("verdict", "unknown"),
        target=result.get("container"),
        service=service,
        summary=summary,
        failure_origin=origin,
        propagation=propagation,
        impact=impact,
        timings=timings,
        what_worked=what_worked,
        anomalies=anomalies,
        unknowns=unknowns,
        evidence=[EvidenceRef(**item.ref()) for item in pack.items],
    )


def _facts_summary(result: dict, service: str | None, first: Any, impact: Impact) -> str:
    action = (result.get("experiment") or {}).get("action", "?")
    bits = [f"{action} on {service}: verdict {result.get('verdict')}"]
    if first is not None:
        bits.append(f"first degradation: {first.data.get('component')} [{first.id}]")
    else:
        bits.append("no degradation observed")
    if impact.failed_endpoints:
        bits.append(f"pages failed: {', '.join(impact.failed_endpoints)}")
    return "; ".join(bits)


def _llm_report(pack: EvidencePack, client: LLMClient) -> Report:
    """Single Agent 2 model call. TODO(llm): the client handles validation/retry/logging."""
    refs = [item.ref() for item in pack.items]
    evidence_ids = [item.id for item in pack.items]
    report = client.generate(
        agent="reporter",
        system=prompts.SYSTEM_PROMPT,
        user=prompts.build_user_prompt(pack.experiment_id, refs),
        schema=Report,
        evidence_ids=evidence_ids,
    )
    report.source = "llm"
    report.experiment_id = pack.experiment_id
    if not report.evidence:
        report.evidence = [EvidenceRef(**item.ref()) for item in pack.items]
    return report


# --------------------------------------------------------------------- rendering
def render_markdown(report: Report) -> str:
    r = report
    lines = [
        f"# Failure report: {r.experiment_id}",
        "",
        f"*Source: {r.source} · verdict: **{r.verdict}** · target: {r.service or r.target}*",
        "",
        r.summary,
        "",
        "## Failure origin",
    ]
    o = r.failure_origin
    if o.component:
        lines.append(f"- **{o.component}** degraded first at {o.at} {_cite(o.evidence_ids)}")
        lines.append(f"- {o.note}")
    else:
        lines.append(f"- {o.note}")

    lines += ["", "## Propagation path"]
    if r.propagation:
        for step in r.propagation:
            lines.append(f"- `{step.role}` **{step.component}** degraded at {step.degraded_at} "
                         f"{_cite(step.evidence_ids)}")
    else:
        lines.append("- nothing propagated: no component degraded in the window")

    lines += ["", "## Impact"]
    i = r.impact
    lines.append(f"- failed endpoints: {', '.join(i.failed_endpoints) or 'none'}")
    lines.append(f"- error types: {i.error_types or 'none'}")
    lines.append(f"- peak error rate: {i.peak_error_rate_pct}% · peak p95: {i.peak_p95_ms}ms "
                 f"(baseline p95 {i.baseline_p95_ms}ms) {_cite(i.evidence_ids)}")

    lines += ["", "## Timings"]
    t = r.timings
    lines.append(f"- detection: {t.detection_seconds}s · recovery: {t.recovery_seconds}s · "
                 f"self-healed: {t.self_healed} {_cite(t.evidence_ids)}")

    lines += ["", "## What worked"]
    worked = [f"- {o.statement} ({o.kind}) {_cite(o.evidence_ids)}" for o in r.what_worked]
    lines += worked or ["- (nothing recorded)"]

    lines += ["", "## Anomalies"]
    anomalies = [f"- {o.statement} ({o.kind}) {_cite(o.evidence_ids)}" for o in r.anomalies]
    lines += anomalies or ["- none"]

    lines += ["", "## Unknowns"]
    lines += [f"- {u}" for u in r.unknowns] or ["- none"]

    lines += ["", "## Evidence"]
    for e in r.evidence:
        when = f" @ {e.at[11:19]}" if e.at else ""
        lines.append(f"- **{e.id}** ({e.source}{when}) {e.summary}")
    lines.append("")
    return "\n".join(lines)


def _cite(ids: list[str]) -> str:
    return f"[{', '.join(ids)}]" if ids else "[no evidence]"


# --------------------------------------------------------------------- writing
def write_report(report: Report, reports_dir: Path | str = DEFAULT_REPORTS_DIR) -> tuple[Path, Path]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{report.experiment_id}.json"
    md_path = reports_dir / f"{report.experiment_id}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return md_path, json_path


def generate_report(
    results_dir: Path | str,
    experiment_id: str,
    reports_dir: Path | str = DEFAULT_REPORTS_DIR,
    client: LLMClient | None = None,
) -> Report:
    """Top-level: build the pack, build the report, write both files, refresh the campaign table."""
    pack = build_pack(results_dir, experiment_id)
    report = build_report(pack, client=client)
    write_report(report, reports_dir)
    refresh_campaign(reports_dir)
    return report


def refresh_campaign(reports_dir: Path | str = DEFAULT_REPORTS_DIR) -> Path:
    """Rewrite reports/campaign.md: one row per report, with the failure origin and verdict."""
    import json

    reports_dir = Path(reports_dir)
    rows: list[tuple[str, str, str]] = []
    for path in sorted(reports_dir.glob("*.json")):
        if path.name in ("recommendations.json",):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "experiment_id" not in data:
            continue
        origin = (data.get("failure_origin") or {}).get("component") or "none"
        rows.append((data["experiment_id"], data.get("verdict", "?"), origin))

    lines = [
        "# Campaign summary",
        "",
        f"*{len(rows)} experiment(s), regenerated {datetime.now().astimezone():%Y-%m-%d %H:%M}*",
        "",
        "| Experiment | Verdict | Failure origin |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {eid} | {verdict} | {origin} |" for eid, verdict, origin in rows]
    lines.append("")
    path = reports_dir / "campaign.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


__all__ = [
    "DEFAULT_REPORTS_DIR",
    "build_report",
    "generate_report",
    "refresh_campaign",
    "render_markdown",
    "write_report",
]

"""Turn a Report into markdown (for people) and a short console block (for the terminal)."""

from __future__ import annotations

from datetime import datetime

from .models import Claim, Report

SEVERITY_LABEL = {"none": "NONE", "minor": "MINOR", "major": "MAJOR", "critical": "CRITICAL"}


def _clock(value: datetime | None) -> str:
    return value.astimezone().strftime("%H:%M:%S") if value else "-"


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _cites(ids: list[str]) -> str:
    return ", ".join(ids) if ids else "-"


def _claims(items: list[Claim]) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {c.text} _[{c.basis}; evidence {_cites(c.evidence)}]_" for c in items]


def _fmt(value: float | None, unit: str = "") -> str:
    return "not measured" if value is None else f"{value:g}{unit}"


def to_markdown(report: Report) -> str:
    a, e = report.analysis, report.evidence
    lines = [
        f"# {report.title}",
        "",
        f"**Severity: {SEVERITY_LABEL[a.severity]}** | {report.kind} | generated "
        f"{report.generated_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')} | narrator: {report.narrator}",
        "",
        f"> {a.headline}",
        "",
        "## Where the failure was",
        "",
        a.failure_point,
        "",
        "## Summary",
        "",
        a.summary,
        "",
    ]

    if a.origin:
        lines += [
            "## Failure origin",
            "",
            f"- **Component:** `{a.origin.component}`",
            f"- **First degraded:** {_clock(a.origin.first_degraded_at)} ({a.origin.basis})",
            f"- **Evidence:** {_cites(a.origin.evidence)}",
            f"- **Note:** {a.origin.note}",
            "",
        ]

    if a.propagation:
        lines += [
            "## How it spread",
            "",
            "| Step | Component | Time | +s | What happened | Evidence |",
            "|---|---|---|---|---|---|",
        ]
        for number, hop in enumerate(a.propagation, start=1):
            offset = "-" if hop.seconds_after_origin is None else f"{hop.seconds_after_origin:+.1f}"
            lines.append(
                f"| {number} | {_cell(hop.component)} ({hop.kind}) | {_clock(hop.at)} | {offset} | "
                f"{_cell(hop.note)} | {_cites(hop.evidence)} |"
            )
        lines.append("")

    if report.timings or report.phases:
        lines += ["## Impact and timings", ""]
        if report.phases:
            lines += [
                "| Phase | Samples | Error rate mean / peak | p95 mean / peak (ms) | Failed pages |",
                "|---|---|---|---|---|",
            ]
            for name in ("before", "during", "after"):
                p = report.phases.get(name) or {}
                if not p:
                    continue
                lines.append(
                    f"| {name} | {p.get('samples', 0)} | {p.get('error_rate_pct_mean', 0)}% / "
                    f"{p.get('error_rate_pct_max', 0)}% | "
                    f"{p.get('p95_ms_mean', 0)} / {p.get('p95_ms_max', 0)} | "
                    f"{', '.join(p.get('failed_endpoints') or []) or 'none'} |"
                )
            lines.append("")
        if report.timings:
            lines += [
                f"- Detection: {_fmt(report.timings.get('detection_seconds'), ' s after injection')}",
                f"- Failure in place: {_fmt(report.timings.get('impact_seconds'), ' s')}",
                f"- Recovery: {_fmt(report.timings.get('recovery_seconds'), ' s after rollback')}",
                "",
            ]

    lines += [
        "## What worked",
        "",
        *_claims(a.what_worked),
        "",
        "## Anomalies and unknowns",
        "",
        *_claims(a.anomalies),
        "",
    ]

    if e.log_findings:
        lines += [
            "## Container log findings",
            "",
            "| Container | Kind | Count | First seen | First line |",
            "|---|---|---|---|---|",
        ]
        for f in e.log_findings:
            lines.append(
                f"| {_cell(f.container)} | {f.kind} | {f.count} | {_clock(f.first_at)} | "
                f"{_cell(f.first_line)} |"
            )
        lines.append("")

    lines += ["## Evidence timeline", "", "| ID | Time | Source | Event |", "|---|---|---|---|"]
    lines += [f"| {i.id} | {_clock(i.t)} | {i.source} | {_cell(i.event)} |" for i in e.timeline]
    lines += [
        "",
        "---",
        "",
        f"_Evidence window {_clock(e.window_start)} to {_clock(e.window_end)}; "
        f"{e.facts.get('samples', 0)} telemetry samples. Narrator: {report.narrator}"
        + (" (hardcoded rules, no LLM yet)" if report.narrator == "stub" else "")
        + "._",
        "",
    ]
    return "\n".join(lines)


def to_console(report: Report, path: str | None = None) -> str:
    a = report.analysis
    bar = "=" * 72
    rows = [
        bar,
        f"AGENT 2 REPORT [{SEVERITY_LABEL[a.severity]}] {report.id}",
        bar,
        a.headline,
        "",
        "WHERE IT FAILED",
        a.failure_point,
    ]
    if a.propagation:
        rows += ["", "HOW IT SPREAD"]
        for hop in a.propagation:
            offset = "" if hop.seconds_after_origin is None else f" ({hop.seconds_after_origin:+.0f}s)"
            rows.append(f"  {_clock(hop.at)}{offset:<8} {hop.component} [{hop.kind}]")
    if a.anomalies:
        rows += ["", "ANOMALIES"] + [f"  - {c.text}" for c in a.anomalies[:4]]
    if path:
        rows += ["", f"full report: {path}"]
    rows.append(bar)
    return "\n".join(rows)

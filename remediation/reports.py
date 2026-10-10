"""Reading Agent 2's (the reporter's) output: reports/<experiment id>.json.

Read as plain JSON, not through the reporter's models, so either agent can change shape without the
other failing; anything missing is just left out.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from safety.limits import REPO_ROOT

#: A caveat saying the experiment's data is not from a real run.
SYNTHETIC = re.compile(r"\b(synthetic|simulated|demo data)\b", re.IGNORECASE)


def default_reports_dir() -> Path:
    """Where the reporter writes; the same default and override as `reporter.service`."""
    return Path(os.getenv("BOUTIQUE_REPORTS_DIR") or REPO_ROOT / "reports")


def load_report(experiment_id: str, reports_dir: Path | None = None) -> dict[str, Any] | None:
    path = Path(reports_dir or default_reports_dir()) / f"{experiment_id}.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def caveats(report: dict[str, Any]) -> list[str]:
    """The report's notes on missing or unusual evidence, and its anomalies, deduplicated."""
    analysis = report.get("analysis") or {}
    evidence = report.get("evidence") or {}
    found = [*(evidence.get("notes") or []), *(c.get("text", "") for c in analysis.get("anomalies") or [])]
    return [c for c in dict.fromkeys(found) if c]


def synthetic_sources(experiment_ids: list[str], reports_dir: Path | None = None) -> dict[str, str]:
    """Experiment id -> the caveat that says its data is synthetic, for each id whose report says so."""
    flagged = {}
    for experiment_id in dict.fromkeys(experiment_ids):
        report = load_report(experiment_id, reports_dir)
        hit = next((c for c in caveats(report) if SYNTHETIC.search(c)), None) if report else None
        if hit:
            flagged[experiment_id] = hit
    return flagged


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def report_digest(experiment_id: str, reports_dir: Path | None = None, max_logs: int = 4) -> str | None:
    """The report on one experiment in a few dense lines, for an agent's context; None if there is none."""
    report = load_report(experiment_id, reports_dir)
    if report is None:
        return None
    analysis = report.get("analysis") or {}
    evidence = report.get("evidence") or {}
    narrator, severity = report.get("narrator", "?"), analysis.get("severity")
    lines = [f"reporter (Agent 2, narrator {narrator}): severity {severity}"]
    if origin := analysis.get("origin"):
        lines.append(f"  origin: {origin.get('component')} ({origin.get('basis')})")
    spread = [
        f"{h.get('component')} +{h['seconds_after_origin']:g}s"
        for h in analysis.get("propagation") or []
        if h.get("kind") != "origin" and h.get("seconds_after_origin") is not None
    ]
    if spread:
        lines.append(f"  spread: {', '.join(spread)}")
    if analysis.get("failure_point"):
        lines.append(f"  failure point: {_clip(analysis['failure_point'], 320)}")
    findings = sorted(evidence.get("log_findings") or [], key=lambda f: -int(f.get("count") or 0))
    for f in findings[:max_logs]:
        lines.append(
            f"  log: {f.get('service') or f.get('container')} {f.get('kind')} x{f.get('count')}: "
            f"{_clip(f.get('first_line', ''), 140)}"
        )
    if len(findings) > max_logs:
        lines.append(f"  log: ... {len(findings) - max_logs} more kind(s) not shown")
    if found := caveats(report):
        lines.append(f"  caveats: {'; '.join(_clip(c, 160) for c in found)}")
    return "\n".join(lines)

"""Read-only dashboard panel for the three agents' output.

Renders what the agents wrote to results/ and reports/ - Agent 1's attack
ranking (results/ranking.json), Agent 2's failure reports
(reports/<id>.md and reports/campaign.md) and Agent 3's fix recommendations
(reports/recommendations.md). It only reads those files; it never runs an
agent and never touches Docker.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

_REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = _REPO_ROOT / "results"
REPORTS_DIR = _REPO_ROOT / "reports"


def _mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return "?"


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        st.warning(f"Could not read {path.name}: {exc}")
        return None


def _read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        st.warning(f"Could not read {path.name}: {exc}")
        return None


def _ranking() -> None:
    st.markdown("### 1 - Attack Ranker")
    path = RESULTS_DIR / "ranking.json"
    if not path.exists():
        st.info("No ranking yet. Run `python -m planner rank` to write results/ranking.json.")
        return
    data = _load_json(path)
    if not data:
        return
    st.caption(f"{path.name} - source: {data.get('source', '?')}"
               + (f" - model: {data['model']}" if data.get("model") else "")
               + f" - app state: {data.get('app_state', '?')} - updated {_mtime(path)}")
    attacks = data.get("attacks") or []
    if not attacks:
        st.info("The ranking is empty.")
    else:
        rows = []
        for a in attacks:
            exp = a.get("experiment") or {}
            rows.append({
                "Rank": a.get("rank"),
                "Harm": a.get("predicted_harm"),
                "Target": exp.get("target"),
                "Action": exp.get("action"),
                "Pre-flight": a.get("preflight_verdict"),
                "Confidence": a.get("confidence"),
                "Rationale": a.get("rationale") or a.get("preflight_reason") or "",
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df.style.map(
                lambda v: "color: #b91c1c; font-weight: bold" if v == "refuse" else (
                    "color: #15803d" if v == "allow" else ""),
                subset=["Pre-flight"],
            ),
            width="stretch", hide_index=True,
        )
    for note in data.get("notes") or []:
        st.caption(f"- {note}")


def _reports() -> None:
    st.markdown("### 2 - Reporter")
    if not REPORTS_DIR.is_dir():
        st.info("No reports yet. They appear after an approved attack, "
                "or run `python -m reporter <experiment-id>`.")
        return
    campaign = REPORTS_DIR / "campaign.md"
    if campaign.exists():
        with st.expander(f"Campaign summary - updated {_mtime(campaign)}", expanded=True):
            text = _read_text(campaign)
            if text:
                st.markdown(text)
    report_files = sorted(
        (p for p in REPORTS_DIR.glob("*.md")
         if p.name not in ("campaign.md", "recommendations.md")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not report_files:
        st.info("No per-experiment reports yet.")
        return
    labels = [f"{p.stem}  ({_mtime(p)})" for p in report_files]
    choice = st.selectbox("Experiment report", range(len(report_files)),
                          format_func=lambda i: labels[i], key="reports_pick")
    text = _read_text(report_files[choice])
    if text:
        st.markdown(text)


def _recommendations() -> None:
    st.markdown("### 3 - Fix Recommender")
    md = REPORTS_DIR / "recommendations.md"
    if not md.exists():
        st.info("No recommendations yet. Run `python -m advisor` after at least one report exists.")
        return
    st.caption(f"{md.name} - updated {_mtime(md)}")
    text = _read_text(md)
    if text:
        st.markdown(text)


def render() -> None:
    """Draw the whole agent-reports panel. Safe to call every rerun."""
    st.subheader("Agent reports")
    st.caption("Read-only view of what the three agents wrote to results/ and reports/. "
               "This panel never runs an agent - use the CLI (planner / reporter / advisor) for that. "
               "New files appear on the next refresh.")
    _ranking()
    st.divider()
    _reports()
    st.divider()
    _recommendations()

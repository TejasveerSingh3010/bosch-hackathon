"""Agent 2: the Reporter.

After an approved attack, it documents what happened: where the failure started, how it spread, how
the app behaved, and how long detection and recovery took. It is read-only - it touches no
containers and writes only to reports/ - and every claim it makes cites an evidence id.
"""
from .evidence import EvidencePack, build_pack
from .report import build_report, generate_report, refresh_campaign, render_markdown, write_report

__all__ = [
    "EvidencePack",
    "build_pack",
    "build_report",
    "generate_report",
    "refresh_campaign",
    "render_markdown",
    "write_report",
]

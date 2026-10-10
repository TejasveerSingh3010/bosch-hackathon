"""Agent 3: the Fix Recommender.

Turns Agent 2's failure reports into a prioritised, deduplicated list of fixes - recommendations
only. It never edits the Compose file, config or code, and never applies anything. It reads the
reports and the Compose file (and graph.json), not raw logs or telemetry; the reports' evidence ids
are its trail back to those.
"""
from .recommender import (
    build_recommendations,
    generate_recommendations,
    render_markdown,
    write_recommendations,
)

__all__ = [
    "build_recommendations",
    "generate_recommendations",
    "render_markdown",
    "write_recommendations",
]

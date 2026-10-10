"""Agent 3 CLI: regenerate the fix recommendations from every report.

    python -m advisor
    python -m advisor --reports-dir reports --compose app/docker-compose.yml

Reads reports/*.json, the Compose file and graph.json; writes reports/recommendations.json and
reports/recommendations.md. Recommendations only - nothing is applied.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents.llm import get_client
from safety.limits import DEFAULT_RESULTS_DIR

from .recommender import (
    DEFAULT_COMPOSE,
    DEFAULT_REPORTS_DIR,
    generate_recommendations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m advisor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument("--graph", type=Path, default=DEFAULT_RESULTS_DIR / "graph.json")
    args = parser.parse_args(argv)

    recs = generate_recommendations(
        args.reports_dir, args.compose, args.graph, client=get_client(DEFAULT_RESULTS_DIR)
    )
    print(f"recommendations written: {args.reports_dir / 'recommendations.md'}  (source: {recs.source})")
    for rec in recs.recommendations:
        print(f"  {rec.priority}. [{rec.root_cause}] {rec.problem}  "
              f"(effort {rec.effort}, confidence {rec.confidence})")
    if not recs.recommendations:
        print("  (no recommendations - no failing reports found)")
    for note in recs.notes:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

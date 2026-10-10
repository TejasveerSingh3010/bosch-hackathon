"""Agent 2 CLI: document one experiment.

    python -m reporter <experiment-id>
    python -m reporter <experiment-id> --results-dir results --reports-dir reports

Reads the experiment's result, telemetry window, Docker events, captured logs, runner timeline,
graph and baseline; writes reports/<id>.md and reports/<id>.json; and refreshes reports/campaign.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents.llm import get_client
from safety.limits import DEFAULT_RESULTS_DIR

from .report import DEFAULT_REPORTS_DIR, generate_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m reporter", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("experiment_id", help="the id of a results/<id>.json record")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    args = parser.parse_args(argv)

    result_path = args.results_dir / f"{args.experiment_id}.json"
    if not result_path.exists():
        print(f"no result at {result_path}", file=sys.stderr)
        return 1

    report = generate_report(
        args.results_dir, args.experiment_id, args.reports_dir,
        client=get_client(args.results_dir),
    )
    origin = report.failure_origin.component or "none"
    print(f"report written: {args.reports_dir / (args.experiment_id + '.md')}")
    print(f"  source         {report.source}")
    print(f"  verdict        {report.verdict}")
    print(f"  failure origin {origin}")
    print(f"  propagation    {' -> '.join(s.component for s in report.propagation) or 'none'}")
    print(f"  campaign       {args.reports_dir / 'campaign.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

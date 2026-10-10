"""Agent 2 (Reporter) command line: `python -m reporter ...`

python -m reporter demo                    show a report for a synthetic failure (no Docker needed)
python -m reporter demo --scenario kill    the other built-in scenario
python -m reporter watch                   watch live telemetry; report automatically on any failure
python -m reporter report <experiment-id>  (re)build the report for a finished experiment
python -m reporter latest                  print the newest report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from safety.limits import TargetConfig

from .demo import SCENARIOS, run_demo
from .render import to_console
from .service import Reporter, default_reports_dir
from .watch import watch


def _config(args: argparse.Namespace) -> TargetConfig | None:
    try:
        return TargetConfig.load(args.target_file, args.results_dir)
    except FileNotFoundError:
        return None


def _reporter(args: argparse.Namespace) -> Reporter:
    config = _config(args)
    results_dir = (
        Path(args.results_dir) if args.results_dir else (config.results_dir if config else Path("results"))
    )
    project = config.compose_project if config else None
    return Reporter(results_dir, Path(args.reports_dir) if args.reports_dir else None, project=project)


def cmd_demo(args: argparse.Namespace) -> int:
    results = Path(args.results_dir) / "demo" if args.results_dir else Path("results") / "demo"
    report, path = run_demo(args.scenario, results, Path(args.reports_dir) if args.reports_dir else None)
    print(to_console(report, str(path)))
    print(
        f"\nThe dashboard shows this under 'Failure report (Agent 2)'. Index: {path.parent / 'campaign.md'}"
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    reporter = _reporter(args)
    report = reporter.report_experiment_id(args.experiment)
    print(to_console(report, str(reporter.reports_dir / f"{report.id}.md")))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    reporter = _reporter(args)
    try:
        watch(reporter, poll_s=args.poll, replay=args.replay)
    except KeyboardInterrupt:
        print("\nAgent 2 stopped watching.")
    return 0


def cmd_latest(args: argparse.Namespace) -> int:
    path = (Path(args.reports_dir) if args.reports_dir else default_reports_dir()) / "latest.md"
    if not path.exists():
        print(f"no report yet ({path}). Try: python -m reporter demo", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m reporter", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target-file", type=Path, default=None, help="path to target.yaml")
    parser.add_argument("--results-dir", type=Path, default=None, help="where telemetry and results live")
    parser.add_argument(
        "--reports-dir", type=Path, default=None, help="where reports are written (default reports/)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="report on a synthetic failure")
    demo.add_argument("--scenario", choices=sorted(SCENARIOS), default="pause")
    demo.set_defaults(func=cmd_demo)

    rep = sub.add_parser("report", help="build the report for a finished experiment")
    rep.add_argument("experiment", help="experiment id (results/<id>.json) or a path to its result file")
    rep.set_defaults(func=cmd_report)

    watcher = sub.add_parser("watch", help="watch live telemetry and report on failures automatically")
    watcher.add_argument("--poll", type=float, default=2.0, help="seconds between telemetry reads")
    watcher.add_argument(
        "--replay", action="store_true", help="process the samples already in telemetry.json once, then exit"
    )
    watcher.set_defaults(func=cmd_watch)

    latest = sub.add_parser("latest", help="print the newest report")
    latest.set_defaults(func=cmd_latest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

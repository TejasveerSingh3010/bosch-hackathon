"""Command line entry point: `python -m agents ...`

python -m agents run PLAN.json [--dry-run] [--no-code] [--no-build]   one traced pipeline run
python -m agents graph                                                 the graph as Mermaid
python -m agents langfuse-prices                                       give Langfuse our model prices
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from safety.limits import REPO_ROOT, TargetConfig

from . import tracing
from .deps import Deps
from .graph import build_graph, run_pipeline


def make_deps(args: argparse.Namespace) -> Deps:
    from injector.docker_cli import DockerCli
    from remediation.agent import GroqProposer
    from remediation.compose import load_services
    from remediation.source import build_check

    return Deps(
        config=TargetConfig.load(args.target_file, args.results_dir),
        docker=DockerCli(),
        make_proposer=lambda: GroqProposer(args.model) if args.model else GroqProposer(),
        build=None if getattr(args, "no_build", False) else build_check,
        services=load_services,
    )


def cmd_run(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    inputs = {"plan": plan, "dry_run": args.dry_run, "allow_code": not args.no_code, "notes": []}
    if args.goal:
        inputs["goal"] = args.goal
    if not tracing.enabled():
        print("tracing off: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env to trace this run")
    final, url = run_pipeline(make_deps(args), inputs, session_id=args.session)
    report = final["escalation"]
    print("\n" + "=" * 72)
    print(f"HUMAN ESCALATION  {report['headline']}")
    for item in report["recommendations"]:
        print(f"  {item['proposal_id']:<22} {item['kind']:<6} {item['status']}")
        if item["action"]:
            print(f"    decide: {item['action']}")
    for note in report["notes"]:
        print(f"  note: {note}")
    if url:
        print(f"trace   {url}")
    print("=" * 72)
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    print(build_graph(make_deps(args)).get_graph().draw_mermaid())
    return 0


def cmd_prices(_args: argparse.Namespace) -> int:
    if not tracing.enabled():
        print("set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env first", file=sys.stderr)
        return 1
    created = tracing.register_prices()
    print(f"created: {', '.join(created)}" if created else "Langfuse already has every model price")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(
        prog="python -m agents", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target-file", type=Path, default=None)
    parser.add_argument("--results-dir", type=Path, default=None)
    parser.add_argument("--model", default=None, help="remediation model (default: the agent's own)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="one pipeline run, attacker to human escalation")
    p.add_argument("plan", help="an Experiment JSON (the attacker stand-in runs it as given)")
    p.add_argument("--dry-run", action="store_true", help="validate the plan only")
    p.add_argument("--no-code", action="store_true", help="no code recommendation for a declined config fix")
    p.add_argument("--no-build", action="store_true", help="skip the build check of code recommendations")
    p.add_argument("--goal", help="the attacker's brief")
    p.add_argument("--session", help="Langfuse session id, to group runs")
    p.set_defaults(func=cmd_run)
    sub.add_parser("graph", help="print the graph as Mermaid").set_defaults(func=cmd_graph)
    sub.add_parser("langfuse-prices", help="register model prices in Langfuse").set_defaults(func=cmd_prices)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

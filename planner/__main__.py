"""Agent 1 (the Planner) command line: `python -m planner ...`

python -m planner list                       the eight attacks and the services they may target
python -m planner health                     the current cluster-health snapshot
python -m planner details <attack>           one attack's hypothesis, parameters, recovery criteria
python -m planner rank [--limit N]           the N (default 5) deadliest attacks given current health;
                                             writes results/attack_ranking.json for the dashboard
python -m planner run --goal "..."           plan and run experiments (asks y/N before each injection)
    --limit N            at most N experiments (default 3)
    --approve ID ...     pre-approve these experiment ids (non-interactive)
    --auto               approve every policy-accepted plan (isolated test environment only)
    --dry-run            select and policy-check only; never inject
    --session NAME       audit-trail folder name (default: a timestamp)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from safety.limits import REPO_ROOT, TargetConfig

from .agent import AttackPlanner
from .approval import AutoApprover, CLIApprover, DenyAll
from .audit import Auditor
from .catalogue import get_attack
from .interface import ToolInterface


def _tools(args: argparse.Namespace, approver_default: bool = True) -> ToolInterface:
    from injector.docker_cli import DockerCli

    config = TargetConfig.load(args.target_file, args.results_dir)
    session = args.session or datetime.now().strftime("%Y%m%d-%H%M%S")
    auditor = Auditor(Path(config.results_dir) / "plans", session)
    if not approver_default or getattr(args, "dry_run", False):
        approver = DenyAll()
    elif getattr(args, "auto", False):
        approver = AutoApprover()
    else:
        approver = CLIApprover(pre_approved=set(getattr(args, "approve", None) or ()))
    return ToolInterface(config, DockerCli(), auditor, approver=approver)


def cmd_list(args: argparse.Namespace) -> int:
    print(json.dumps(_tools(args, approver_default=False).list_experiments(), indent=2))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    print(json.dumps(_tools(args, approver_default=False).get_cluster_health(), indent=2))
    return 0


def cmd_details(args: argparse.Namespace) -> int:
    try:
        get_attack(args.attack)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(_tools(args, approver_default=False).get_experiment_details(args.attack), indent=2))
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    from .ranking import rank_deadliest, write_ranking

    if args.session is None:
        args.session = "rankings"  # one audit folder for every ranking, not one per refresh
    tools = _tools(args, approver_default=False)
    doc = rank_deadliest(tools, limit=args.limit)
    path = write_ranking(doc, Path(tools.config.results_dir))
    safe = "" if doc["safe_to_inject"] else "  (cluster degraded or telemetry stale: not safe to inject now)"
    print(f"Deadliest attacks right now, by {doc['narrator']}{safe}")
    for item in doc["ranked"]:
        params = f" {json.dumps(item['params'])}" if item["params"] else ""
        head = f"  {item['rank']}. [{item['deadliness']:>3}] {item['action']:<18} {item['target']:<22}"
        print(head + params)
        print(f"       {item['rationale']}")
    if doc.get("llm_error"):
        print(f"  (model failed, deterministic ranking used: {doc['llm_error']})")
    print(f"written {path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    tools = _tools(args)
    planner = AttackPlanner(tools)
    report = planner.run(goal=args.goal, limit=args.limit)
    print("\n" + "=" * 72)
    print(f"PLANNER CONCLUSION  {report['conclusion']}")
    for outcome in report.get("outcomes", []):
        line = f"  {outcome['action']:<18} {outcome['target']:<22} {outcome['status']}"
        if outcome.get("verdict"):
            line += f" ({outcome['verdict']})"
        print(line)
        if outcome.get("reasons"):
            for reason in outcome["reasons"]:
                print(f"      reason: {reason}")
    print(f"audit   {tools.auditor.dir}")
    print("=" * 72)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(
        prog="python -m planner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target-file", type=Path, default=None)
    parser.add_argument("--results-dir", type=Path, default=None)
    parser.add_argument("--session", default=None, help="audit-trail folder name")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the attacks and targets").set_defaults(func=cmd_list)
    sub.add_parser("health", help="cluster-health snapshot").set_defaults(func=cmd_health)
    p_details = sub.add_parser("details", help="one attack's details")
    p_details.add_argument("attack", help="attack name, e.g. memory_limit")
    p_details.set_defaults(func=cmd_details)

    p_rank = sub.add_parser("rank", help="rank the deadliest attacks given current health")
    p_rank.add_argument("--limit", type=int, default=5, help="how many to rank (default 5)")
    p_rank.set_defaults(func=cmd_rank)

    p_run = sub.add_parser("run", help="plan and run experiments")
    p_run.add_argument("--goal", default="Find the most damaging failure the safety layer allows.")
    p_run.add_argument("--limit", type=int, default=3, help="at most this many experiments")
    p_run.add_argument("--approve", nargs="*", help="experiment ids to pre-approve (non-interactive)")
    p_run.add_argument("--auto", action="store_true", help="approve every policy-accepted plan")
    p_run.add_argument("--dry-run", action="store_true", help="select and policy-check only; no injection")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

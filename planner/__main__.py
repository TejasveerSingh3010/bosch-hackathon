"""Agent 1 (the Planner) command line: `python -m planner ...`

python -m planner list                       the eight attacks and the services they may target
python -m planner health                     the current cluster-health snapshot
python -m planner details <attack>           one attack's hypothesis, parameters, recovery criteria
python -m planner rank [--limit N]           the N (default 5) deadliest attacks given current health;
                                             writes results/attack_ranking.json for the dashboard
python -m planner interpret "REQUEST" [--out F] one attack from a plain-words request, policy-checked,
                                             never injected (the dashboard's attack console)
python -m planner execute F --confirm        run the plan `interpret` saved in F (policy re-checked)
                                             then Agent 2 reports on it and the remediator recommends
                                             a fix (--no-report / --no-remediation skip; also for run)
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


def _report(args: argparse.Namespace, tools: ToolInterface, experiment_ids: list[str]) -> None:
    """Agent 2 writes the failure report for each experiment that ran, as the orchestrator CLI does.
    The runner writes results/<id>.json only after rollback and recovery, so this runs on a
    recovered system. A reporter problem never fails the command."""
    if args.no_report:
        return
    from reporter.service import report_after_experiment
    from schemas import ExperimentResult

    results_dir = Path(tools.config.results_dir)
    for experiment_id in experiment_ids:
        try:
            result = ExperimentResult.model_validate_json(
                (results_dir / f"{experiment_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"no report for {experiment_id}: {exc}")
            continue
        report_after_experiment(result, results_dir, tools.config.compose_project)


def _remediate(args: argparse.Namespace, tools: ToolInterface, experiment_ids: list[str],
               proposer: object | None = None) -> None:
    """The remediator after the report, as the agent graph runs it: a config recommendation per
    experiment, then a code recommendation for each one the agent declined. It only files proposals
    for human review. A remediator problem never fails the command."""
    if args.no_remediation or not experiment_ids:
        return
    from agents import tracing
    from remediation.agent import GroqProposer
    from remediation.compose import load_services
    from remediation.models import Proposal
    from remediation.recommend import recommend_code, recommend_config
    from remediation.source import build_check

    def show(filed) -> list[str]:
        for proposal in filed.proposals:
            state = "declined" if not proposal.remediable else (
                "rejected by policy" if proposal.policy_problems else "awaiting review")
            print(f"remediation {proposal.id}: {state}")
        for error in filed.errors:
            print(f"remediation error: {error}")
        return [p.id for p in filed.proposals if isinstance(p, Proposal) and not p.remediable]

    try:
        proposer = proposer or GroqProposer()
        print("\nThe remediator is writing its recommendation...")
        with tracing.span(f"remediation {' '.join(experiment_ids)}", {"result_ids": experiment_ids}):
            declined = show(recommend_config(proposer, tools.config, load_services(), experiment_ids))
            for declined_id in declined:
                show(recommend_code(proposer, tools.config, declined_id,
                                    None if args.no_build else build_check))
    except Exception as exc:  # noqa: BLE001 - the attack is over; a recommendation problem is a note
        print(f"the remediator could not finish: {type(exc).__name__}: {exc}")


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


def cmd_interpret(args: argparse.Namespace) -> int:
    from .console import interpret

    if args.session is None:
        args.session = "console"
    result = interpret(_tools(args, approver_default=False), args.request)
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".tmp").write_text(text, encoding="utf-8")
        args.out.with_suffix(".tmp").replace(args.out)
    print(text)
    return 0 if result["allowed"] else 2


def cmd_execute(args: argparse.Namespace) -> int:
    from .console import ConsoleApproval, execute, load_plan

    if args.session is None:
        args.session = "console"
    plan = load_plan(args.request_file)
    if plan is None:
        print("the request file holds no plan", file=sys.stderr)
        return 1
    tools = _tools(args, approver_default=False)
    if args.confirm:
        tools.approver = ConsoleApproval(plan.experiment_id)
    result = execute(tools, plan)
    print(json.dumps(result, indent=2))
    if result["status"] == "ran":
        _report(args, tools, [plan.experiment_id])
        _remediate(args, tools, [plan.experiment_id])
    return 0 if result["status"] == "ran" else 2


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
    ran = [o["experiment_id"] for o in report.get("outcomes", [])
           if o.get("status") == "ran" and o.get("experiment_id")]
    _report(args, tools, ran)
    _remediate(args, tools, ran)
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

    p_int = sub.add_parser("interpret", help="turn a plain-words request into one policy-checked plan")
    p_int.add_argument("request", help='e.g. "add 400 ms latency to cartservice for 20 s"')
    p_int.add_argument("--out", type=Path, help="also write the result here (for execute)")
    p_int.set_defaults(func=cmd_interpret)
    p_exe = sub.add_parser("execute", help="run a plan saved by interpret")
    p_exe.add_argument("request_file", type=Path)
    p_exe.add_argument("--confirm", action="store_true",
                       help="the operator confirms this plan; without it the run is denied")
    p_exe.add_argument("--no-report", action="store_true", help="skip the automatic Agent 2 failure report")
    p_exe.add_argument("--no-remediation", action="store_true",
                       help="skip the remediator's recommendation after the report")
    p_exe.add_argument("--no-build", action="store_true", help="skip the build check of code recommendations")
    p_exe.set_defaults(func=cmd_execute)

    p_run = sub.add_parser("run", help="plan and run experiments")
    p_run.add_argument("--goal", default="Find the most damaging failure the safety layer allows.")
    p_run.add_argument("--limit", type=int, default=3, help="at most this many experiments")
    p_run.add_argument("--approve", nargs="*", help="experiment ids to pre-approve (non-interactive)")
    p_run.add_argument("--auto", action="store_true", help="approve every policy-accepted plan")
    p_run.add_argument("--dry-run", action="store_true", help="select and policy-check only; no injection")
    p_run.add_argument("--no-report", action="store_true", help="skip the automatic Agent 2 failure report")
    p_run.add_argument("--no-remediation", action="store_true",
                       help="skip the remediator's recommendation after the report")
    p_run.add_argument("--no-build", action="store_true", help="skip the build check of code recommendations")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

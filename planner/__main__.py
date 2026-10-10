"""Agent 1 CLI: rank every attack, and approve exactly one for a human-confirmed run.

    python -m planner rank                     build results/ranking.json and print the table
    python -m planner approve <rank>           show the plan, require a typed confirmation, run it
    python -m planner approve <rank> --dry-run  validate and explain, change nothing
    python -m planner approve <rank> --confirm <id>   supply the confirmation non-interactively

Agent 1 never executes anything itself: `approve` prints `explain_plan`, requires you to type the
experiment id back, and only then calls `ExperimentRunner.run`, which re-checks pre-flight against
fresh telemetry. A rank-1 attack that the watchdog later aborts is reported, not hidden.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents.llm import get_client
from agents.schemas import RankedAttack, Ranking
from injector.docker_cli import DockerCli, DockerError
from orchestrator.runner import ExperimentRunner
from safety.limits import TargetConfig
from safety.telemetry import TelemetryUnavailable
from safety.validator import PlanRejected, explain_plan, validate_plan

from .ranker import Ranker, write_ranking

EXIT_OK, EXIT_ERROR, EXIT_REJECTED, EXIT_ABORTED = 0, 1, 2, 3


def _load_ranking(results_dir: Path) -> Ranking:
    path = results_dir / "ranking.json"
    if not path.exists():
        raise SystemExit("no results/ranking.json - run `python -m planner rank` first")
    return Ranking.model_validate_json(path.read_text(encoding="utf-8"))


def cmd_rank(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    runner = ExperimentRunner(DockerCli(), config)
    ranker = Ranker(runner, config, client=get_client(config.results_dir))
    ranking = ranker.rank()
    path = write_ranking(ranking, config.results_dir)

    print(f"Attack ranking  (source: {ranking.source}, app state: {ranking.app_state})")
    for note in ranking.notes:
        print(f"  note: {note}")
    print()
    print(f"{'#':>3}  {'HARM':>5}  {'TARGET':<22} {'ACTION':<17} {'PAGES':<5} {'PRE-FLIGHT':<9} PRIOR")
    for entry in ranking.attacks:
        pages = len(entry.predicted_effects.failed_endpoints)
        prior = entry.prior_run_id or "-"
        print(f"{entry.rank:>3}  {entry.predicted_harm:>5.1f}  {entry.experiment.target:<22} "
              f"{entry.experiment.action:<17} {pages:<5} {entry.preflight_verdict:<9} {prior}")
    print(f"\nwrote {path}")
    print("approve one with:  python -m planner approve <#>")
    return EXIT_OK


def _entry_for_rank(ranking: Ranking, rank: int) -> RankedAttack:
    for entry in ranking.attacks:
        if entry.rank == rank:
            return entry
    raise SystemExit(f"no attack at rank {rank} (there are {len(ranking.attacks)})")


def cmd_approve(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    ranking = _load_ranking(config.results_dir)
    entry = _entry_for_rank(ranking, args.rank)
    experiment = entry.experiment
    runner = ExperimentRunner(DockerCli(), config, poll_s=args.poll)

    # Show exactly what would run, using the safety layer's own explanation.
    try:
        plan = validate_plan(experiment, config, runner._containers(), runner.telemetry)
        print(explain_plan(plan))
    except PlanRejected as exc:
        print(f"\nthis attack is REJECTED by the safety layer right now:\n  {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except (DockerError, TelemetryUnavailable) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"\npredicted harm {entry.predicted_harm} | {entry.rationale}")
    if entry.preflight_verdict == "refuse":
        print(f"NOTE: the ranking marked this 'pre-flight would refuse': {entry.preflight_reason}")

    if args.dry_run:
        print("\nDRY RUN: the plan is valid and nothing was changed.")
        return EXIT_OK

    # Human approval: the experiment id must be typed back, exactly. --confirm supplies it for
    # scripted demos; without it, we prompt interactively. No confirmation means nothing runs.
    typed = args.confirm if args.confirm is not None else _prompt(experiment.id)
    if typed != experiment.id:
        print(f"\nnot confirmed (you typed {typed!r}, expected {experiment.id!r}); nothing ran.")
        return EXIT_ERROR

    try:
        result = runner.run(experiment)
    except PlanRejected as exc:
        print(f"\nREJECTED at run time by the safety layer:\n  {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except (DockerError, TelemetryUnavailable) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    # Record which ranking entry was approved, for the audit trail and Agent 2/3.
    result.ranked_from = entry.model_dump(mode="json")
    (config.results_dir / f"{result.experiment.id}.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )

    tail = f" - {result.abort_reason}" if result.abort_reason else ""
    print(f"\nverdict {result.verdict.upper()}{tail}")
    if result.verdict == "aborted":
        print("(an abort is a valid, expected outcome - the safety layer did its job)")
    print(f"full record: {config.results_dir / (result.experiment.id + '.json')}")
    if result.errors:
        return EXIT_ERROR
    return EXIT_ABORTED if result.verdict in ("aborted", "stopped") else EXIT_OK


def _prompt(experiment_id: str) -> str:
    try:
        return input(f"\nType the experiment id to confirm and RUN it ({experiment_id}): ").strip()
    except EOFError:
        return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m planner", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target-file", type=Path, default=None)
    parser.add_argument("--results-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    rank = sub.add_parser("rank", help="rank every attack and write results/ranking.json")
    rank.set_defaults(func=cmd_rank)

    approve = sub.add_parser("approve", help="approve one ranked attack and run it (with confirmation)")
    approve.add_argument("rank", type=int, help="the rank number from `planner rank`")
    approve.add_argument("--dry-run", action="store_true", help="validate and explain, change nothing")
    approve.add_argument("--confirm", default=None,
                         help="supply the typed confirmation (the experiment id) non-interactively")
    approve.add_argument("--poll", type=float, default=2.0, help="watchdog poll interval, seconds")
    approve.set_defaults(func=cmd_approve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

"""Command line entry point: `python -m orchestrator ...`

    python -m orchestrator actions                         list the failure modes
    python -m orchestrator targets                         list containers and whether each is allowed
    python -m orchestrator run --target adservice --action kill --duration 30
    python -m orchestrator run --target frontend --action spike --rps 20 --users 5 --duration 30
    python -m orchestrator run docs/experiment.example.json
    python -m orchestrator run ... --dry-run               validate and explain, change nothing
    python -m orchestrator run ... --no-report             skip the automatic Agent 2 failure report
    python -m orchestrator stop                            engage the kill switch (from another window)
    python -m orchestrator recover                          undo what a crashed run left behind
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from injector.actions import ACTIONS, describe_actions
from injector.docker_cli import DockerCli, DockerError
from safety.killswitch import KillSwitch
from safety.limits import TargetConfig
from safety.telemetry import TelemetryReader, TelemetryUnavailable
from safety.validator import PlanRejected
from schemas import Experiment

from .runner import ExperimentRunner

EXIT_OK, EXIT_REJECTED, EXIT_ABORTED, EXIT_ERROR = 0, 2, 3, 1


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _experiment_from_args(args: argparse.Namespace) -> Experiment:
    """Build an experiment from a JSON file or from the individual flags."""
    if args.plan:
        path = Path(args.plan)
        if not path.exists():
            raise SystemExit(f"plan file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if args.target:
            payload["target"] = args.target
        if args.action:
            payload["action"] = args.action
        if args.duration:
            payload["duration_s"] = args.duration
        _merge_spike_flags(payload, args)
        return Experiment.model_validate(payload)

    if not (args.target and args.action):
        raise SystemExit("give a plan file, or both --target and --action (see --help)")
    stamp = datetime.now().strftime("%H%M%S")
    duration = args.duration or 30
    payload = {
        "id": args.id or f"{args.action}-{args.target}-{stamp}".replace("_", "-"),
        "target": args.target,
        "action": args.action,
        "duration_s": duration,
        "reason": args.reason or "run from the command line",
    }
    _merge_spike_flags(payload, args)
    return Experiment.model_validate(payload)


def _merge_spike_flags(payload: dict, args: argparse.Namespace) -> None:
    """Copy --rps, --users and --duration into a spike plan. Time is the experiment duration."""
    action = payload.get("action")
    if action != "spike" and args.rps is None and args.users is None:
        return
    params = dict(payload.get("params") or {})
    if args.rps is not None:
        params["rps"] = args.rps
    if args.users is not None:
        params["users"] = args.users
    if args.duration:
        params["time"] = args.duration
    elif "time" not in params and payload.get("duration_s"):
        params["time"] = payload["duration_s"]
    if action == "spike" and ("rps" not in params or "users" not in params or "time" not in params):
        raise SystemExit("spike requires --rps, --users and --duration (requests/second, users, seconds)")
    if params and action == "spike":
        payload["params"] = params


def cmd_actions(_: argparse.Namespace) -> int:
    print("Failure modes this injector implements:\n")
    print(describe_actions())
    print("\nEach one has a matching rollback, performed automatically.")
    return EXIT_OK


def cmd_targets(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    docker = DockerCli()
    containers = docker.list_containers(config.compose_project)
    if not containers:
        print(f"no containers found for Compose project {config.compose_project!r}. Is the app running?")
        return EXIT_ERROR
    allowed = set(config.allowed_containers)
    width = max(len(c.name) for c in containers)
    print(f"Compose project: {config.compose_project}    (allow-list from {config.source})\n")
    print(f"{'CONTAINER':<{width}}  {'SERVICE':<24} {'STATUS':<10} {'POLICY':<15} ALLOWED")
    for container in sorted(containers, key=lambda c: c.name):
        mark = "yes" if container.name in allowed else "NO"
        print(f"{container.name:<{width}}  {(container.service or '-'):<24} "
              f"{container.status:<10} {container.restart_policy:<15} {mark}")
    missing = allowed - {c.name for c in containers}
    if missing:
        print(f"\nwarning: in allowed_containers but not running: {', '.join(sorted(missing))}")
    return EXIT_OK


def cmd_stop(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    switch = KillSwitch(config.results_dir)
    switch.engage(args.reason or "stopped from the command line")
    print(f"kill switch engaged ({switch.path}). A running experiment will roll back on its next tick.")
    return EXIT_OK


def cmd_recover(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    runner = ExperimentRunner(DockerCli(), config)
    message = runner.recover()
    print(message)
    return EXIT_ERROR if "FAILED" in message else EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    reader = TelemetryReader(config.results_dir)
    try:
        sample = reader.latest()
    except TelemetryUnavailable as exc:
        print(f"telemetry: {exc}")
        return EXIT_ERROR
    summary = sample.get("summary") or {}
    age = reader.age_seconds(sample)
    print(f"sample age     {age:.1f}s  ({'fresh' if age < 15 else 'STALE - is the monitor running?'})")
    print(f"error rate     {summary.get('error_rate_pct')}%")
    print(f"p95 latency    {summary.get('p95_latency_ms')} ms")
    print(f"ready          {summary.get('ready_pods')}/{summary.get('total_pods')}")
    if summary.get("not_ready"):
        print(f"not ready      {', '.join(summary['not_ready'])}")
    baseline = reader.baseline()
    baseline_state = "present" if baseline else r"MISSING - run scripts\baseline.bat"
    print(f"baseline       {baseline_state}")
    journal = config.results_dir / "active_experiment.json"
    if journal.exists():
        print(f"journal        {journal.name} present - run `python -m orchestrator recover`")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    experiment = _experiment_from_args(args)
    runner = ExperimentRunner(DockerCli(), config, poll_s=args.poll)

    try:
        if args.dry_run:
            runner.dry_run(experiment)
            print("\nDRY RUN: the plan is valid and nothing was changed.")
            return EXIT_OK
        result = runner.run(experiment)
    except PlanRejected as exc:
        print(f"\nREJECTED by the safety layer:\n  {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except (DockerError, TelemetryUnavailable) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print("\n" + "=" * 68)
    print(f"verdict           {result.verdict.upper()}")
    if result.abort_reason:
        print(f"reason            {result.abort_reason}")
    print(f"target            {result.container}")
    for label, value, unit in (
        ("detection", result.detection_seconds, "s after injection"),
        ("impact window", result.impact_seconds, "s"),
        ("recovery", result.recovery_seconds, "s after rollback"),
    ):
        print(f"{label:<17} {'not measured' if value is None else f'{value}{unit}'}")
    print(f"error rate        before {result.before.error_rate_pct_mean}% -> "
          f"during {result.during.error_rate_pct_max}% (peak) -> "
          f"after {result.after.error_rate_pct_mean}%")
    print(f"p95 latency       before {result.before.p95_ms_mean}ms -> "
          f"during {result.during.p95_ms_max}ms (peak) -> after {result.after.p95_ms_mean}ms")
    if result.during.failed_endpoints:
        print(f"pages that failed {', '.join(result.during.failed_endpoints)}")
    if result.during.error_types:
        print(f"error types       {result.during.error_types}")
    if result.self_healed:
        print("self-healed       yes - Docker's restart policy recovered it before rollback")
    for note in result.observations:
        print(f"note              {note}")
    for err in result.errors:
        print(f"ERROR             {err}")
    print(f"\nfull record       {config.results_dir / (result.experiment.id + '.json')}")
    print("=" * 68)

    if not args.no_report:
        # Agent 2 writes the failure report as soon as the experiment is over and rolled back.
        from reporter.service import report_after_experiment

        report_after_experiment(result, config.results_dir, config.compose_project)

    if result.errors:
        return EXIT_ERROR
    return EXIT_ABORTED if result.verdict in ("aborted", "stopped") else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target-file", type=Path, default=None, help="path to target.yaml")
    parser.add_argument("--results-dir", type=Path, default=None, help="where telemetry and results live")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one experiment")
    run.add_argument("plan", nargs="?", help="experiment JSON file (see docs/experiment.example.json)")
    run.add_argument("--target", help="Compose service or container name")
    run.add_argument("--action", choices=sorted(ACTIONS))
    run.add_argument("--duration", type=int, help="seconds to hold the failure (default 30); for spike this is the time")
    run.add_argument("--rps", type=int, help="spike only: requests per second, shared across --users")
    run.add_argument("--users", type=int, help="spike only: number of concurrent clients")
    run.add_argument("--id", help="experiment id, used for the result filename")
    run.add_argument("--reason", help="why this experiment is worth running")
    run.add_argument("--poll", type=float, default=2.0, help="watchdog poll interval, seconds")
    run.add_argument("--dry-run", action="store_true", help="validate and explain, change nothing")
    run.add_argument("--no-report", action="store_true", help="skip the automatic Agent 2 failure report")
    run.set_defaults(func=cmd_run)

    for name, func, help_text in (
        ("actions", cmd_actions, "list the failure modes"),
        ("targets", cmd_targets, "list containers and whether each is allowed"),
        ("status", cmd_status, "show monitor health at a glance"),
        ("recover", cmd_recover, "undo what a crashed run left behind"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)

    stop = sub.add_parser("stop", help="engage the kill switch")
    stop.add_argument("--reason")
    stop.set_defaults(func=cmd_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
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

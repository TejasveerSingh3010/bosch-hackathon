"""Command line entry point: `python -m remediation ...`

The agent only recommends; accepting a recommendation is the human escalation step.

python -m remediation propose [RESULT_ID ...]        ask the agent for a recommendation (latest records)
python -m remediation propose --from-file draft.json  check and file a hand-written proposal
python -m remediation propose-code <DECLINED_ID>      recommend a code change for a declined proposal
python -m remediation review <ID>                     human escalation: read the sheet, accept or reject
python -m remediation verify <ID> [--repeats N]       optional, after acceptance: before/after runs
python -m remediation list                            every proposal and where it stands
python -m remediation [-v] eval                       re-check every proposal with today's checks
python -m remediation recover                         undo a fix a crashed verification left applied
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from injector.docker_cli import DockerCli, DockerError
from orchestrator.runner import ExperimentRunner
from safety.limits import REPO_ROOT, TargetConfig

from .agent import (
    MODEL,
    GroqProposer,
    ProposalError,
    second_api_key,
)
from .compose import ComposeCli, ComposeError, FixJournal, load_services
from .consistency import CartProbe
from .evaluate import evaluate, previous, save, summarise
from .models import CodeProposal, CodeProposalDraft, Proposal, ProposalDraft
from .policy import SERVICE_DOCS, check_proposal
from .recommend import (
    MAX_FAULTS,
    Filed,
    file_code_proposal,
    file_config_proposal,
    recommend_code,
    recommend_config,
)
from .source import SourceError, build_check, ensure_checkout
from .store import Store
from .verify import Verifier

EXIT_OK, EXIT_ERROR, EXIT_REJECTED, EXIT_NOT_EFFECTIVE = 0, 1, 2, 3


def show_config(proposal: Proposal, store: Store) -> int:
    folder = store.dir(proposal.id)
    print(f"\nproposal {proposal.id}  ({proposal.author}; {', '.join(proposal.source_results)})")
    if not proposal.remediable:
        print("DECLINED by the agent - no configuration change can fix this:")
        print(f"  {proposal.declined_reason}")
        print(f"diagnosis {proposal.diagnosed_problem}")
        print(f"recorded in {folder}")
        return EXIT_OK
    if proposal.policy_problems:
        print("REJECTED by policy - it cannot be approved:")
        for problem in proposal.policy_problems:
            print(f"  - {problem}")
        print(f"recorded in {folder}")
        return EXIT_REJECTED
    print(f"problem   {proposal.diagnosed_problem}")
    for change in proposal.changes:
        print(f"change    {change.service}.{change.setting} = {change.value}")
    print(f"escalated for a human decision: python -m remediation review {proposal.id}")
    print(f"review sheet: {folder / 'review.md'}")
    return EXIT_OK


def show_code(proposal: CodeProposal, store: Store) -> int:
    folder = store.dir(proposal.id)
    print(
        f"\ncode recommendation {proposal.id}  ({proposal.author}, answering {proposal.responds_to}; "
        f"{', '.join(proposal.source_results)})"
    )
    if not proposal.remediable:
        print("DECLINED by the agent - no small code change can fix this:")
        print(f"  {proposal.declined_reason}")
        print(f"recorded in {folder}")
        return EXIT_OK
    if proposal.policy_problems:
        print("REJECTED by policy - it cannot be accepted:")
        for problem in proposal.policy_problems:
            print(f"  - {problem}")
        print(f"recorded in {folder}")
        return EXIT_REJECTED
    print(f"problem   {proposal.diagnosed_problem}")
    for edit in proposal.edits:
        print(f"edit      {edit.service}/{edit.file}: {edit.purpose}")
    build = proposal.build
    summary = f"{build.status} - {build.detail.splitlines()[0]}" if build else "not checked (--no-build)"
    print(f"build     {summary}")
    print(f"escalated for a human decision: python -m remediation review {proposal.id}")
    print(f"review sheet: {folder / 'review.md'}   patch: {folder / 'patch.diff'}")
    return EXIT_OK


def show_filed(filed: Filed, store: Store, show: Callable[[Any, Store], int]) -> int:
    codes = [show(p, store) for p in filed.proposals]
    for error in filed.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if filed.skipped:
        left = ", ".join(filed.skipped)
        print(f"\nnot asked this run (at most {MAX_FAULTS} faults; name them to include): {left}")
    if filed.errors and not filed.proposals:
        return EXIT_ERROR
    return EXIT_REJECTED if EXIT_REJECTED in codes else EXIT_ERROR if filed.errors else EXIT_OK


def asking(ids: list[str]) -> None:
    more = f" (+{len(ids) - 1} more run(s) of it)" if len(ids) > 1 else ""
    print(f"\nasking the agent about fault {ids[0]}{more}")


def cmd_propose(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    services = load_services()
    store = Store(config.results_dir)
    if args.from_file:
        draft = ProposalDraft.model_validate_json(Path(args.from_file).read_text(encoding="utf-8"))
        sources = sorted({e.result_id for e in draft.evidence})
        proposal = file_config_proposal(draft, "human", sources, config, services, store, args.reports_dir)
        store.save_proposal(proposal, services)
        return show_config(proposal, store)
    try:
        proposer = GroqProposer(args.model)
    except ProposalError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    filed = recommend_config(
        proposer, config, services, args.result_ids or None, args.reports_dir, MAX_FAULTS, asking
    )
    return show_filed(filed, store, show_config)


def cmd_propose_code(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    store = Store(config.results_dir)
    declined = store.load_proposal(args.declined_id)
    if not isinstance(declined, Proposal) or declined.remediable:
        print(f"{args.declined_id} is not a declined configuration proposal", file=sys.stderr)
        return EXIT_ERROR
    docs_raw = yaml.safe_load(SERVICE_DOCS.read_text(encoding="utf-8")) or {}
    try:
        checkout = ensure_checkout(docs_raw["source"])
    except SourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    build = None if args.no_build else build_check
    if args.from_file:
        draft = CodeProposalDraft.model_validate_json(Path(args.from_file).read_text(encoding="utf-8"))
        proposal = file_code_proposal(
            draft, "human", declined, config, checkout, docs_raw, build, store, args.reports_dir
        )
        store.save_proposal(proposal, {})
        return show_code(proposal, store)
    try:
        second = second_api_key()
        proposer = GroqProposer(args.model, api_key=second)  # code + rewrite on the second key
        critic = GroqProposer(args.model) if second else proposer  # critique on the primary key
    except ProposalError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if second:
        print("using a second Groq key for the code stage (config/critique on the primary key)")
    if build:
        print("edits are checked in each service's own build image (the first run pulls it)")
    filed = recommend_code(proposer, config, declined.id, build, checkout, args.reports_dir, asking,
                           critic=critic)
    return show_filed(filed, store, show_code)


def cmd_review(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    store = Store(config.results_dir)
    proposal = store.load_proposal(args.id)
    print((store.dir(args.id) / "review.md").read_text(encoding="utf-8"))
    if proposal.policy_problems or not proposal.remediable:
        print("This proposal failed the policy checks and cannot be approved.")
        return EXIT_REJECTED

    decision = "approved" if args.approve else "rejected" if args.reject else None
    try:
        if decision is None:
            answer = (
                input("Type 'approve' to approve this exact change, 'reject' to reject it: ").strip().lower()
            )
            decision = {"approve": "approved", "reject": "rejected"}.get(answer)
            if decision is None:
                print("no decision recorded")
                return EXIT_ERROR
        reviewer = (
            args.reviewer or input(f"Reviewer name [{getpass.getuser()}]: ").strip() or getpass.getuser()
        )
        note = args.note if args.note is not None else input("Note (optional): ").strip()
    except EOFError:
        print("\nno terminal to ask on - pass --approve/--reject and --reviewer", file=sys.stderr)
        return EXIT_ERROR
    store.record_decision(args.id, decision, reviewer, note)
    print(f"\n{decision} by {reviewer}.")
    if decision == "approved" and isinstance(proposal, CodeProposal):
        print("Adopting it is a code change: apply patch.diff to the upstream source and rebuild the image.")
    elif decision == "approved":
        print("Adopting it is a human edit to app/docker-compose.yml.")
        print(f"To measure it first (optional): python -m remediation verify {args.id}")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    store = Store(config.results_dir)
    proposal = store.load_proposal(args.id)
    if isinstance(proposal, CodeProposal):
        print(f"{args.id} is a code recommendation; verify measures Compose fixes only", file=sys.stderr)
        return EXIT_ERROR
    # Re-check against today's rules: the policy or docs/services.yaml may have tightened since filing.
    problems = check_proposal(
        ProposalDraft(**proposal.model_dump(include=set(ProposalDraft.model_fields))), load_services(), config
    )
    if problems:
        print(f"{args.id} no longer passes the policy checks:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_REJECTED
    docker = DockerCli()

    def loadgen_running() -> bool:
        return any(
            c.service == "loadgenerator" and c.running and not c.paused
            for c in docker.list_containers(config.compose_project)
        )

    verifier = Verifier(
        config,
        ExperimentRunner(docker, config).run,
        ComposeCli(config.compose_project),
        CartProbe(config.frontend_url),
        store,
        loadgen_running=loadgen_running,
        cooldown_s=args.cooldown,
    )
    try:
        v = verifier.verify(proposal, args.repeats)
    except (PermissionError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_REJECTED if isinstance(exc, PermissionError) else EXIT_ERROR

    print("\n" + "=" * 72)
    print(f"verdict     {v.verdict.upper()}   ({proposal.id})")
    print(
        f"experiment  {v.experiment.action} on {v.experiment.target}, {v.experiment.duration_s}s, "
        f"{v.repeats} run(s) per arm"
    )
    for run in v.runs:
        lost = {True: "kept", False: "LOST", None: "n/a"}[run.write_preserved]
        print(
            f"  {run.arm:<6} {run.experiment_id:<34} {run.verdict:<9} cart write {lost}"
            + ("" if run.valid else f"  INVALID: {run.invalid_reason}")
        )
    for reason in v.reasons:
        print(f"- {reason}")
    print(f"configuration restored: {'yes' if v.fix_restored else 'NO - run python -m remediation recover'}")
    print(f"record      {store.dir(proposal.id) / 'verification.json'}")
    print("=" * 72)
    return EXIT_OK if v.verdict == "effective" else EXIT_NOT_EFFECTIVE


def cmd_eval(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    docs_raw = yaml.safe_load(SERVICE_DOCS.read_text(encoding="utf-8")) or {}
    try:
        checkout = ensure_checkout(docs_raw["source"])
    except SourceError as exc:
        print(f"code proposals not re-checked: {exc}", file=sys.stderr)
        checkout = None
    rows = evaluate(config, load_services(), checkout, docs_raw.get("services") or {}, args.reports_dir)
    for r in rows:
        if not r.remediable:
            verdict = "declined by agent"
        else:
            verdict = ("clean" if r.clean_now else f"{len(r.problems_now)} problem(s)") + (
                "" if r.clean_now == r.clean_then else f" (was {'clean' if r.clean_then else 'flagged'})"
            )
        print(f"{r.id:<22} {r.kind:<6} {r.decision or '-':<9} {verdict}")
        if args.verbose:
            for problem in r.problems_now:
                print(f"    - {problem}")
    summary = summarise(rows)
    path = save(rows, summary, config.results_dir)
    last = previous(config.results_dir, path)
    print(
        f"\n{summary['offered_fixes']} offered fixes: {summary['clean_now']} pass today's checks "
        f"(at filing: {summary['clean_then']})"
        + (f"; last run: {last['clean_now']}" if last else "")
    )
    if summary["newly_flagged"]:
        print(f"newly flagged by today's checks: {', '.join(summary['newly_flagged'])}")
    if summary["human_decisions"]:
        odd = summary["disagreements"]
        print(
            f"checks agree with {summary['checks_agree_with_humans']}/{summary['human_decisions']} "
            "human decisions" + (f" (disagree: {', '.join(odd)})" if odd else "")
        )
    print(f"saved {path}")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    store = Store(TargetConfig.load(args.target_file, args.results_dir).results_dir)
    ids = store.ids()
    if not ids:
        print("no proposals yet - python -m remediation propose")
    for pid in ids:
        proposal = store.load_proposal(pid)
        if isinstance(proposal, CodeProposal):
            files = sorted({f"{e.service}/{e.file}" for e in proposal.edits})
            changes = f"code: {', '.join(files)}" if files else "code: -"
        else:
            changes = ", ".join(f"{c.service}.{c.setting}={c.value}" for c in proposal.changes) or "-"
        print(f"{pid:<22} {store.status(pid):<26} {proposal.author:<24} {changes}")
    return EXIT_OK


def cmd_recover(args: argparse.Namespace) -> int:
    config = TargetConfig.load(args.target_file, args.results_dir)
    journal = FixJournal(config.results_dir)
    entry = journal.read()
    if entry is None:
        print("nothing to recover: no fix is recorded as applied.")
        return EXIT_OK
    try:
        ComposeCli(config.compose_project).restore(entry["services"])
    except ComposeError as exc:
        print(f"recovery FAILED: {exc}", file=sys.stderr)
        return EXIT_ERROR
    journal.clear()
    Store(config.results_dir).log("recovered", entry["proposal_id"], services=entry["services"])
    print(f"restored {', '.join(entry['services'])} to app/docker-compose.yml (fix {entry['proposal_id']}).")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m remediation",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target-file", type=Path, default=None)
    parser.add_argument("--results-dir", type=Path, default=None)
    parser.add_argument(
        "--reports-dir", type=Path, default=None, help="Agent 2 reports (default: reports/)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("propose", help="ask the agent for a fix, or file a hand-written one")
    p.add_argument("result_ids", nargs="*", help="experiment ids to diagnose (default: the latest records)")
    p.add_argument("--from-file", help="a ProposalDraft JSON written by hand")
    p.add_argument("--model", default=MODEL)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("propose-code", help="recommend a code change for a declined config proposal")
    p.add_argument("declined_id", help="id of the declined configuration proposal to answer")
    p.add_argument("--from-file", help="a CodeProposalDraft JSON written by hand")
    p.add_argument("--no-build", action="store_true", help="skip the compile check")
    p.add_argument("--model", default=MODEL)
    p.set_defaults(func=cmd_propose_code)

    p = sub.add_parser("review", help="human escalation: read a recommendation, accept or reject it")
    p.add_argument("id")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")
    p.add_argument("--reviewer")
    p.add_argument("--note")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("verify", help="optional: measure an accepted fix with before/after runs")
    p.add_argument("id")
    p.add_argument("--repeats", type=int, help="runs per arm (default: the proposal's own)")
    p.add_argument("--cooldown", type=float, default=20.0, help="seconds to settle before each run")
    p.set_defaults(func=cmd_verify)

    sub.add_parser("list", help="every proposal and its status").set_defaults(func=cmd_list)
    sub.add_parser("eval", help="re-check every proposal with today's checks (no LLM)").set_defaults(
        func=cmd_eval
    )
    sub.add_parser("recover", help="undo a fix a crashed verification left applied").set_defaults(
        func=cmd_recover
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Model output can contain any Unicode; a Windows console (cp1252) would crash printing it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    load_dotenv(REPO_ROOT / ".env")  # LANGFUSE_* too: review and filing post scores
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return int(args.func(args))
    except (FileNotFoundError, DockerError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

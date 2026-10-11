"""Turning a draft into a filed recommendation: shared by the CLI and the agent graph's tools.

Filing never applies anything. It policy-checks the draft, renders its artifact and saves it under
results/remediation/, where it waits for the human decision (`python -m remediation review`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agents import tracing
from safety.limits import TargetConfig

from .agent import (
    ProposalError,
    Proposer,
    build_code_context,
    build_context,
    critique_context,
    experiment_records,
    fault_groups,
    revision_note,
)
from .models import BuildCheck, CodeProposal, CodeProposalDraft, Proposal, ProposalDraft
from .policy import SERVICE_DOCS, check_code_proposal, check_proposal
from .source import build_check, ensure_checkout, render_patch
from .store import Store

Build = Callable[..., tuple[BuildCheck, dict[str, tuple[str, str]]]]

#: Faults answered per run when no records are named: each is one LLM call (and up to a minute's wait).
MAX_FAULTS = 3


@dataclass
class Filed:
    """What one recommend_* call filed, one proposal per fault, and the faults it could not answer."""

    proposals: list[Any] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # faults left out by MAX_FAULTS


def new_id(store: Store | None = None) -> str:
    """`rem-<timestamp>`, suffixed if a proposal filed in the same second already took it."""
    base = f"rem-{datetime.now():%Y%m%d-%H%M%S}"
    pid, n = base, 1
    while store is not None and store.dir(pid).exists():
        n += 1
        pid = f"{base}-{n}"
    return pid


def file_config_proposal(
    draft: ProposalDraft,
    author: str,
    sources: list[str],
    config: TargetConfig,
    services: dict[str, dict[str, Any]],
    store: Store | None = None,
    reports_dir: Path | None = None,
) -> Proposal:
    return Proposal(
        **draft.model_dump(),
        id=new_id(store),
        author=author,
        created_at=datetime.now().astimezone(),
        source_results=sources,
        policy_problems=check_proposal(draft, services, config, reports_dir),
    )


def file_code_proposal(
    draft: CodeProposalDraft,
    author: str,
    declined: Proposal,
    config: TargetConfig,
    checkout: Path,
    docs_raw: dict[str, Any],
    build: Build | None = build_check,
    store: Store | None = None,
    reports_dir: Path | None = None,
    sources: list[str] | None = None,
) -> CodeProposal:
    """Policy-check a code draft, render its patch and (unless `build` is None) compile it."""
    docs = docs_raw.get("services") or {}
    problems, edited = check_code_proposal(draft, config, checkout, docs, reports_dir)
    files = edited.files if draft.remediable and not edited.problems else {}
    result = None
    if files and not problems and build is not None:
        for service in sorted({e.service for e in draft.edits}):
            doc = docs[service]
            mine = {p: t for p, t in files.items() if p.startswith(doc["code"]["dir"] + "/")}
            result, formatted = build(checkout, doc["code"]["dir"], doc.get("language", ""), mine)
            files = {**files, **formatted}
            if result.status == "failed":
                problems.append(f"the patched {service} fails its build check (output in the review sheet)")
                break
    patch = render_patch(files)
    return CodeProposal(
        **draft.model_dump(),
        id=new_id(store),
        author=author,
        created_at=datetime.now().astimezone(),
        source_results=sources or declined.source_results,
        responds_to=declined.id,
        source_commit=(docs_raw.get("source") or {}).get("commit", ""),
        patch=patch,
        build=result,
        policy_problems=problems,
    )


def recommend_config(
    proposer: Proposer,
    config: TargetConfig,
    services: dict[str, dict[str, Any]],
    result_ids: list[str] | None = None,
    reports_dir: Path | None = None,
    max_faults: int | None = MAX_FAULTS,
    progress: Callable[[list[str]], None] | None = None,
) -> Filed:
    """Ask the agent about experiment records, one fault at a time, and file each answer.

    Named records are all answered; otherwise the `max_faults` most recent faults are.
    """
    records = experiment_records(config.results_dir, result_ids or None)
    if not records:
        raise FileNotFoundError("no experiment records to work from - run some experiments first")
    groups = fault_groups(records)
    filed = Filed()
    if not result_ids and max_faults is not None and len(groups) > max_faults:
        filed.skipped = [g[0].experiment.id for g in groups[max_faults:]]
        groups = groups[:max_faults]
    store = Store(config.results_dir)
    for group in groups:
        ids = [r.experiment.id for r in group]
        if progress:
            progress(ids)
        with tracing.span(f"config-recommendation {ids[0]}", input={"experiments": ids}) as traced:
            try:
                draft = proposer.propose(build_context(group, config, services, reports_dir=reports_dir))
            except ProposalError as exc:
                filed.errors.append(f"{', '.join(ids)}: {exc}")
                traced.finish({"error": str(exc)})
                continue
            proposal = file_config_proposal(
                draft, f"llm:{proposer.model_name}", ids, config, services, store, reports_dir
            )
            proposal.trace_id = traced.trace_id or getattr(proposer, "last_trace_id", None)
            store.save_proposal(proposal, services)
            traced.finish({"proposal": proposal.id, "remediable": proposal.remediable})
        score_filing(proposal)
        filed.proposals.append(proposal)
    return filed


#: Policy problems a rewrite cannot fix: they are about the data behind the proposal, not its code.
UNREVISABLE = ("built on synthetic data",)


def review_and_revise(
    proposer: Proposer,
    draft: CodeProposalDraft,
    declined: Proposal,
    group: list[Any],
    config: TargetConfig,
    checkout: Path,
    docs: dict[str, Any],
    reports_dir: Path | None,
    critic: Proposer | None = None,
) -> tuple[CodeProposalDraft, list[str], bool]:
    """One review pass over a code draft, and at most one rewrite: (final draft, issues found, revised).

    The automated checks run first (cheap, certain); the reviewer model sees their findings and the
    patch, nothing more. Any issue from either earns one rewrite with the issues attached. The
    reviewer cannot approve anything, and its issues go on the review sheet for the human.
    """
    reviewer = critic or proposer
    if not draft.remediable or not hasattr(reviewer, "critique_code"):
        return draft, [], False
    problems, edited = check_code_proposal(draft, config, checkout, docs, reports_dir)
    if any(p.startswith(UNREVISABLE) for p in problems):
        return draft, [], False  # no rewrite can make the data real; save the tokens
    patch = render_patch(edited.files) if not edited.problems else ""
    try:
        critique = reviewer.critique_code(critique_context(draft, declined, patch, problems, group))
    except ProposalError as exc:
        return draft, [f"review pass failed: {exc}"], False
    issues = list(problems) + [f"review: {i}" for i in critique.issues]
    if not critique.fixes_diagnosis and not critique.issues:
        issues.append("review: the patch does not remove the diagnosed cause")
    if not issues:
        return draft, [], False
    note = revision_note(draft, patch, issues)
    try:
        revised = proposer.propose_code(
            build_code_context(declined, group, checkout, reports_dir=reports_dir, extra=note)
        )
    except ProposalError as exc:
        return draft, [*issues, f"rewrite failed, first draft kept: {exc}"], False
    return revised, issues, True


def score_filing(proposal: Any) -> None:
    """Scores on the proposal's trace: did it pass policy, and (code) did it build and need a rewrite."""
    if not proposal.trace_id:
        return
    clean = proposal.remediable and not proposal.policy_problems
    tracing.score(
        proposal.trace_id, "policy_clean", 1 if clean else 0, "; ".join(proposal.policy_problems) or None
    )
    tracing.score(proposal.trace_id, "remediable", 1 if proposal.remediable else 0)
    if isinstance(proposal, CodeProposal):
        if proposal.build:
            tracing.score(proposal.trace_id, "build", proposal.build.status, data_type="CATEGORICAL")
        issues = "; ".join(proposal.critique) or None
        tracing.score(proposal.trace_id, "revised", 1 if proposal.revised else 0, issues)


def recommend_code(
    proposer: Proposer,
    config: TargetConfig,
    declined_id: str,
    build: Build | None = build_check,
    checkout: Path | None = None,
    reports_dir: Path | None = None,
    progress: Callable[[list[str]], None] | None = None,
    critic: Proposer | None = None,
) -> Filed:
    """Answer a declined configuration proposal with a code recommendation per fault, and file them.

    The code draft and any rewrite run on `proposer`; the review pass runs on `critic` when one is
    given, so with two Groq keys the code and critique calls draw on separate tokens-a-minute budgets
    and the config -> code -> critique -> rewrite sequence alternates keys instead of waiting."""
    store = Store(config.results_dir)
    declined = store.load_proposal(declined_id)
    if not isinstance(declined, Proposal) or declined.remediable:
        raise ValueError(f"{declined_id} is not a declined configuration proposal")
    docs_raw = yaml.safe_load(SERVICE_DOCS.read_text(encoding="utf-8")) or {}
    checkout = checkout or ensure_checkout(docs_raw["source"])
    docs = docs_raw.get("services") or {}
    filed = Filed()
    for group in fault_groups(experiment_records(config.results_dir, declined.source_results)):
        ids = [r.experiment.id for r in group]
        if progress:
            progress(ids)
        name = f"code-recommendation {ids[0]}"
        with tracing.span(name, input={"experiments": ids, "answers": declined.id}) as traced:
            try:
                draft = proposer.propose_code(
                    build_code_context(declined, group, checkout, reports_dir=reports_dir)
                )
            except ProposalError as exc:
                filed.errors.append(f"{', '.join(ids)}: {exc}")
                traced.finish({"error": str(exc)})
                continue
            draft, issues, revised = review_and_revise(
                proposer, draft, declined, group, config, checkout, docs, reports_dir, critic
            )
            proposal = file_code_proposal(
                draft, f"llm:{proposer.model_name}", declined, config, checkout, docs_raw, build, store,
                reports_dir, ids,
            )
            proposal.critique, proposal.revised = issues, revised
            proposal.trace_id = traced.trace_id or getattr(proposer, "last_trace_id", None)
            store.save_proposal(proposal, {})
            traced.finish({"proposal": proposal.id, "revised": revised, "issues": issues})
        score_filing(proposal)
        filed.proposals.append(proposal)
    return filed

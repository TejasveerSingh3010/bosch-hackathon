"""On-disk record of every proposal, decision and verification under results/remediation/.

results/remediation/<id>/proposal.json      what was proposed (and any policy problems)
results/remediation/<id>/override.yml       the exact Compose change (config proposals)
results/remediation/<id>/patch.diff         the exact source change (code proposals; `git apply`)
results/remediation/<id>/review.md          the human-readable review sheet, with the diff
results/remediation/<id>/approval.json      reviewer decision + hashes of what was reviewed
results/remediation/<id>/verification.json  before/after runs, comparison and verdict
results/remediation/ledger.jsonl            append-only history, including failed attempts
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import tracing

from .compose import config_diff, render_override
from .models import AnyProposal, Approval, CodeProposal, Proposal, Verification


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Store:
    def __init__(self, results_dir: Path) -> None:
        self.root = Path(results_dir) / "remediation"

    def dir(self, proposal_id: str) -> Path:
        return self.root / proposal_id

    def log(self, event: str, proposal_id: str, **detail: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now().astimezone().isoformat(),
            "event": event,
            "proposal_id": proposal_id,
            **detail,
        }
        with (self.root / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    # ---- proposals
    def save_proposal(self, proposal: AnyProposal, services: dict[str, dict[str, Any]]) -> Path:
        folder = self.dir(proposal.id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "proposal.json").write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
        if isinstance(proposal, CodeProposal):
            if proposal.remediable and proposal.patch:
                (folder / "patch.diff").write_text(proposal.patch, encoding="utf-8", newline="\n")
            sheet = code_review_sheet(proposal)
        else:
            if proposal.remediable and proposal.changes:
                (folder / "override.yml").write_text(
                    render_override(proposal.changes, proposal.id), encoding="utf-8"
                )
            sheet = review_sheet(proposal, services)
        (folder / "review.md").write_text(sheet, encoding="utf-8")
        self.log(
            "declined"
            if not proposal.remediable
            else "rejected_by_policy"
            if proposal.policy_problems
            else "proposed",
            proposal.id,
            author=proposal.author,
            problems=proposal.policy_problems,
        )
        return folder

    def load_proposal(self, proposal_id: str) -> AnyProposal:
        path = self.dir(proposal_id) / "proposal.json"
        if not path.exists():
            raise FileNotFoundError(f"no proposal {proposal_id!r} under {self.root}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return CodeProposal.model_validate(data) if "edits" in data else Proposal.model_validate(data)

    def artifact(self, proposal_id: str) -> Path:
        """The change file a reviewer approves: the Compose override or the source patch."""
        patch = self.dir(proposal_id) / "patch.diff"
        return patch if patch.exists() else self.dir(proposal_id) / "override.yml"

    def ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "proposal.json").exists())

    # ---- review
    def record_decision(self, proposal_id: str, decision: str, reviewer: str, note: str = "") -> Approval:
        folder = self.dir(proposal_id)
        artifact = self.artifact(proposal_id)
        approval = Approval(
            decision=decision,
            reviewer=reviewer,
            note=note,
            decided_at=datetime.now().astimezone(),
            proposal_sha256=sha256(folder / "proposal.json"),
            artifact=artifact.name,
            artifact_sha256=sha256(artifact) if artifact.exists() else "",
        )
        (folder / "approval.json").write_text(approval.model_dump_json(indent=2), encoding="utf-8")
        self.log(f"review_{decision}", proposal_id, reviewer=reviewer, note=note)
        # The human decision is the quality signal: score it on the trace that wrote the proposal.
        trace_id = json.loads((folder / "proposal.json").read_text(encoding="utf-8")).get("trace_id")
        tracing.score(trace_id, "human_decision", 1 if decision == "approved" else 0, note or None)
        return approval

    def load_approval(self, proposal_id: str) -> Approval | None:
        path = self.dir(proposal_id) / "approval.json"
        return Approval.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None

    def approval_problem(self, proposal_id: str) -> str | None:
        """None if a human approved exactly the files that are on disk now."""
        approval = self.load_approval(proposal_id)
        folder = self.dir(proposal_id)
        if approval is None:
            return "not reviewed yet - run `python -m remediation review <id>`"
        if approval.decision != "approved":
            return f"rejected by {approval.reviewer}: {approval.note or 'no note'}"
        if sha256(folder / "proposal.json") != approval.proposal_sha256:
            return "proposal.json changed after it was approved - review it again"
        artifact = folder / approval.artifact
        if not artifact.exists() or sha256(artifact) != approval.artifact_sha256:
            return f"{approval.artifact} changed after it was approved - review it again"
        return None

    # ---- verification
    def save_verification(self, verification: Verification) -> Path:
        path = self.dir(verification.proposal_id) / "verification.json"
        path.write_text(verification.model_dump_json(indent=2), encoding="utf-8")
        self.log(
            "verified", verification.proposal_id, verdict=verification.verdict, reasons=verification.reasons
        )
        return path

    def load_verification(self, proposal_id: str) -> Verification | None:
        path = self.dir(proposal_id) / "verification.json"
        return Verification.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None

    def status(self, proposal_id: str) -> str:
        proposal = self.load_proposal(proposal_id)
        if not proposal.remediable:
            return "declined by agent"
        if proposal.policy_problems:
            return "rejected by policy"
        verification = self.load_verification(proposal_id)
        if verification:
            return f"verified: {verification.verdict.upper()}"
        approval = self.load_approval(proposal_id)
        if approval is None:
            return "awaiting review"
        return "accepted by reviewer" if approval.decision == "approved" else "rejected by reviewer"


def review_sheet(proposal: Proposal, services: dict[str, dict[str, Any]]) -> str:
    exp = proposal.validation_experiment
    lines = [
        f"# Remediation proposal {proposal.id}",
        "",
        f"Author: {proposal.author} - created {proposal.created_at:%Y-%m-%d %H:%M}",
        "",
    ]
    if not proposal.remediable:
        lines += [
            "## Declined by the agent - no configuration fix",
            "",
            proposal.declined_reason,
            "",
            "## Diagnosed problem",
            "",
            proposal.diagnosed_problem,
            "",
        ]
        return "\n".join(lines)
    if proposal.policy_problems:
        lines += ["## REJECTED BY POLICY (cannot be approved)", ""]
        lines += [f"- {p}" for p in proposal.policy_problems] + [""]
    lines += ["## Diagnosed problem", "", proposal.diagnosed_problem, "", "## Evidence", ""]
    lines += [f"- `{e.result_id}` `{e.field}` = {e.observed} - {e.interpretation}" for e in proposal.evidence]
    lines += [
        "",
        "## Proposed change",
        "",
        "```diff",
        config_diff(services, proposal.changes).rstrip(),
        "```",
        "",
        "## Expected benefit",
        "",
        proposal.expected_benefit,
        "",
        "## Success criteria",
        "",
    ]
    lines += [
        f"- median `{c.metric}` falls by at least {c.min_improvement_pct:g}%"
        for c in proposal.success_criteria
    ]
    lines += ["", "## Side effects", ""] + [f"- {s}" for s in proposal.side_effects]
    lines += [
        "",
        "## Rollback",
        "",
        proposal.rollback_procedure,
        "",
        "Mechanical rollback: `python -m remediation recover` (recreates the services from "
        "app/docker-compose.yml without the override).",
        "",
        "## Validation experiment",
        "",
        f"`{exp.action}` on `{exp.target}` for {exp.duration_s}s"
        + (f" at {exp.rps} rps / {exp.users} users" if exp.action == "spike" else "")
        + f", {exp.repeats} runs on the original configuration, then {exp.repeats} with the fix.",
        "",
    ]
    return "\n".join(lines)


def code_review_sheet(proposal: CodeProposal) -> str:
    exp = proposal.validation_experiment
    lines = [
        f"# Code recommendation {proposal.id}",
        "",
        f"Author: {proposal.author} - created {proposal.created_at:%Y-%m-%d %H:%M}",
        f"Answers declined config proposal {proposal.responds_to}; "
        f"applies to upstream commit {proposal.source_commit[:10]}.",
        "",
    ]
    if not proposal.remediable:
        lines += [
            "## Declined by the agent - no small code fix",
            "",
            proposal.declined_reason,
            "",
            "## Diagnosed problem",
            "",
            proposal.diagnosed_problem,
            "",
        ]
        return "\n".join(lines)
    if proposal.policy_problems:
        lines += ["## REJECTED BY POLICY (cannot be accepted)", ""]
        lines += [f"- {p}" for p in proposal.policy_problems] + [""]
    lines += ["## Diagnosed problem", "", proposal.diagnosed_problem, "", "## Evidence", ""]
    lines += [f"- `{e.result_id}` `{e.field}` = {e.observed} - {e.interpretation}" for e in proposal.evidence]
    lines += ["", "## Proposed change", ""]
    lines += [f"{n}. `{e.service}/{e.file}`: {e.purpose}" for n, e in enumerate(proposal.edits, 1)]
    lines += ["", "```diff", proposal.patch.rstrip() or "(no patch - see policy problems)", "```", ""]
    if proposal.build:
        lines += [f"Build check: **{proposal.build.status}** - {proposal.build.detail.splitlines()[0]}", ""]
        if proposal.build.status == "failed":
            lines += ["```", proposal.build.detail, "```", ""]
    if proposal.critique:
        verb = "were found in the first draft, which was then rewritten once" if proposal.revised else (
            "were found by the review pass (not rewritten)"
        )
        lines += [f"## Review pass: these issues {verb}", ""]
        lines += [f"- {issue}" for issue in proposal.critique] + [""]
        if proposal.revised:
            lines += ["Check that the change above actually addresses them.", ""]
    lines += ["## Expected benefit", "", proposal.expected_benefit, "", "## Side effects", ""]
    lines += [f"- {s}" for s in proposal.side_effects]
    lines += ["", "## How it could be measured", ""]
    lines += [
        f"- median `{c.metric}` falls by at least {c.min_improvement_pct:g}%"
        for c in proposal.success_criteria
    ]
    lines += [
        f"- by replaying `{exp.action}` on `{exp.target}` for {exp.duration_s}s, {exp.repeats} runs "
        f"with the current image and {exp.repeats} with an image built from the patch",
        "",
        "## Adopting it",
        "",
        f"In a checkout of the upstream source at {proposal.source_commit[:10]}: `git apply patch.diff`, "
        "then rebuild the service image. Reverting is `git apply -R patch.diff`.",
        "",
    ]
    return "\n".join(lines)

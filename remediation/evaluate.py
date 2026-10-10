"""Replay every filed proposal against today's checks: the regression set for recommendation quality.

No LLM and no build: each stored draft is re-checked with the current policy and codelint, and
compared with how it was judged when filed and, where a human decided, with that decision. Run it
after changing a prompt, a check or the context, and compare the summary with the last one
(results/remediation/eval/<timestamp>.json).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from safety.limits import TargetConfig

from .models import CodeProposal, CodeProposalDraft, ProposalDraft
from .policy import check_code_proposal, check_proposal
from .store import Store


@dataclass
class Row:
    id: str
    kind: str
    status: str
    decision: str | None  # the human's, if any
    remediable: bool
    clean_then: bool  # passed the checks when it was filed
    problems_now: list[str] = field(default_factory=list)

    @property
    def clean_now(self) -> bool:
        return self.remediable and not self.problems_now


def evaluate(
    config: TargetConfig,
    services: dict[str, dict[str, Any]],
    checkout: Path | None,
    docs: dict[str, Any],
    reports_dir: Path | None = None,
) -> list[Row]:
    store = Store(config.results_dir)
    rows = []
    for pid in store.ids():
        proposal = store.load_proposal(pid)
        approval = store.load_approval(pid)
        code = isinstance(proposal, CodeProposal)
        row = Row(
            id=pid,
            kind="code" if code else "config",
            status=store.status(pid),
            decision=approval.decision if approval else None,
            remediable=proposal.remediable,
            clean_then=proposal.remediable and not proposal.policy_problems,
        )
        if proposal.remediable:
            if code and checkout is not None:
                draft = CodeProposalDraft(**proposal.model_dump(include=set(CodeProposalDraft.model_fields)))
                row.problems_now = check_code_proposal(draft, config, checkout, docs, reports_dir)[0]
                if proposal.build and proposal.build.status == "failed":  # not rebuilt here: too slow
                    row.problems_now.append("failed its build check when filed (not re-run)")
            elif not code:
                draft = ProposalDraft(**proposal.model_dump(include=set(ProposalDraft.model_fields)))
                row.problems_now = check_proposal(draft, services, config, reports_dir)
        rows.append(row)
    return rows


def summarise(rows: list[Row]) -> dict[str, Any]:
    offered = [r for r in rows if r.remediable]
    decided = [r for r in offered if r.decision]
    agree = [r for r in decided if r.clean_now == (r.decision == "approved")]
    return {
        "at": datetime.now().astimezone().isoformat(),
        "proposals": len(rows),
        "declined_by_agent": len(rows) - len(offered),
        "offered_fixes": len(offered),
        "clean_then": sum(r.clean_then for r in offered),
        "clean_now": sum(r.clean_now for r in offered),
        "newly_flagged": [r.id for r in offered if r.clean_then and not r.clean_now],
        "newly_clean": [r.id for r in offered if not r.clean_then and r.clean_now],
        "human_decisions": len(decided),
        "checks_agree_with_humans": len(agree),
        "disagreements": [r.id for r in decided if r not in agree],
    }


def save(rows: list[Row], summary: dict[str, Any], results_dir: Path) -> Path:
    folder = Path(results_dir) / "remediation" / "eval"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    payload = {"summary": summary, "rows": [{**asdict(r), "clean_now": r.clean_now} for r in rows]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def previous(results_dir: Path, before: Path) -> dict[str, Any] | None:
    """The summary of the last saved run before `before`, to compare with."""
    folder = Path(results_dir) / "remediation" / "eval"
    older = sorted(p for p in folder.glob("*.json") if p.name < before.name)
    if not older:
        return None
    return json.loads(older[-1].read_text(encoding="utf-8")).get("summary")

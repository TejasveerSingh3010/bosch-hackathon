"""The audit trail: every tool request, policy decision, approval, result and conclusion.

One directory per planner session under `results/plans/<session>/`:

  * `audit.jsonl` - an ordered, timestamped line for every event, so a run can be replayed;
  * `<experiment_id>.plan.json` - each plan as it was proposed;
  * `conclusion.json` - the evidence-based summary the planner ends on.

Evidence, not reasoning: the log records what was requested, what the policy decided and what
happened, never the agent's private deliberation. Writing never raises - an audit failure must not
stop a rollback or crash a run - but write errors are surfaced on the conclusion.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .approval import Approval
from .plan import AttackPlan
from .policy import PolicyDecision


class Auditor:
    def __init__(self, plans_dir: Path, session_id: str) -> None:
        self.dir = Path(plans_dir) / session_id
        self.session_id = session_id
        self.log_path = self.dir / "audit.jsonl"
        self.errors: list[str] = []
        self._event("session_start", {"session": session_id})

    # -- events --
    def tool_request(self, tool: str, args: dict[str, Any]) -> None:
        self._event("tool_request", {"tool": tool, "args": _plain(args)})

    def plan(self, plan: AttackPlan) -> None:
        self._write(f"{plan.experiment_id}.plan.json", plan.model_dump())
        self._event("plan", plan.model_dump())

    def policy(self, decision: PolicyDecision) -> None:
        self._event("policy_decision", {
            "experiment_id": decision.experiment_id,
            "allowed": decision.allowed,
            "reasons": decision.reasons,
            "notes": decision.notes,
        })

    def approval(self, experiment_id: str, approval: Approval) -> None:
        self._event("approval", {"experiment_id": experiment_id, **asdict(approval)})

    def result(self, experiment_id: str, data: dict[str, Any]) -> None:
        self._event("result", {"experiment_id": experiment_id, **_plain(data)})

    def conclusion(self, text: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"session": self.session_id, "conclusion": text, **(data or {})}
        if self.errors:
            payload["audit_errors"] = list(self.errors)
        self._write("conclusion.json", payload)
        self._event("conclusion", {"conclusion": text})
        return payload

    # -- internals --
    def _event(self, kind: str, data: dict[str, Any]) -> None:
        entry = {"at": datetime.now().astimezone().isoformat(), "event": kind, **data}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
        except OSError as exc:
            self.errors.append(f"{kind}: {exc}")

    def _write(self, name: str, data: dict[str, Any]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / name).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            self.errors.append(f"{name}: {exc}")


def _plain(value: Any) -> Any:
    """Make dataclasses and other odd values JSON-friendly for the log."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


__all__ = ["Auditor"]

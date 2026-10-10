"""Crash recovery: remember what is currently broken, so it can be put back.

If the runner is killed mid-experiment (Ctrl+C twice, a closed window, a power cut) nothing else
would know a container had been deliberately paused or stopped. The journal is written before the
failure is injected and deleted only after a successful rollback, so

    python -m orchestrator recover

can always restore the system - and the runner refuses to start a new experiment while a journal
from a previous one is still present.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

JOURNAL_FILENAME = "active_experiment.json"


@dataclass
class JournalEntry:
    """The minimum needed to undo an injected failure after a crash."""

    experiment_id: str
    action: str
    container: str
    service: str | None
    project: str | None
    params: dict[str, Any]
    pre_state: dict[str, Any] | None
    injected_at: float
    pid: int

    def age_s(self) -> float:
        return max(0.0, time.time() - self.injected_at)


class Journal:
    """A single-slot journal at `<results_dir>/active_experiment.json`."""

    def __init__(self, results_dir: Path) -> None:
        self.path = Path(results_dir) / JOURNAL_FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    def write(self, entry: JournalEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(entry), indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def read(self) -> JournalEntry | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return JournalEntry(**data)
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


__all__ = ["Journal", "JournalEntry"]

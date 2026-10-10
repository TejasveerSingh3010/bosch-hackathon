"""The manual kill switch: a file anyone can create to stop an experiment immediately.

A file is used rather than a signal or an API because it works from everywhere at once - the
dashboard button, a second terminal (`type nul > results\\STOP`), a teammate on the same machine,
or a script. The runner checks for it on every watchdog tick and rolls back when it appears.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

STOP_FILENAME = "STOP"


class KillSwitch:
    """Presence of `<results_dir>/STOP` means: stop and roll back now."""

    def __init__(self, results_dir: Path) -> None:
        self.path = Path(results_dir) / STOP_FILENAME

    def engaged(self) -> bool:
        return self.path.exists()

    def engage(self, reason: str = "manual stop") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(reason, encoding="utf-8")

    def reason(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip() or "manual stop"
        except OSError:
            return "manual stop"

    def clear(self) -> None:
        """Remove the switch. The runner does this at startup so a stale file cannot block a run."""
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)

"""Capture container logs for the evidence window, so a report can show the *actual* error.

Telemetry says a service degraded and when; the logs say *why* - "connection refused",
"context deadline exceeded", an OOM, a panic. Those lines are not collected anywhere else, so this
module adds them. It is deliberately engine-level (not in an agent package): the runner calls it at
injection and at rollback, and Agent 2 only reads what it wrote.

What it does, per the Agent 2 spec:
  * grabs `docker logs` for the target and its direct callers,
  * keeps the first line of each *kind* of error with a running count, plus the last few lines,
  * caps each container at about 200 lines,
  * saves everything under `results/<id>.logs/`, and deduplicates across the injection and
    rollback captures so an error is counted once with the right total.

It must never break an experiment: every docker call is wrapped, and a failure is recorded as a
note rather than raised.
"""
from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from injector.docker_cli import Docker, DockerError

#: Error signatures, most specific first. Each maps a log line to one error *kind*, so repeated
#: identical failures collapse to "first line + count" instead of flooding the report.
ERROR_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("connection refused", "connection_refused"),
    ("no such host", "dns"),
    ("name resolution", "dns"),
    ("context deadline exceeded", "timeout"),
    ("deadline exceeded", "timeout"),
    ("timed out", "timeout"),
    ("timeout", "timeout"),
    ("out of memory", "oom"),
    ("oomkill", "oom"),
    ("connection reset", "connection_reset"),
    ("broken pipe", "connection_reset"),
    ("unavailable", "unavailable"),
    ("panic", "crash"),
    ("fatal", "crash"),
    ("traceback", "crash"),
    ("exception", "error"),
    ("error", "error"),
)

MAX_LINES = 200
TAIL_LINES = 5


def classify_log_line(line: str) -> str | None:
    """Return the error kind a log line matches, or None if it looks benign."""
    lowered = line.lower()
    for needle, kind in ERROR_SIGNATURES:
        if needle in lowered:
            return kind
    return None


@dataclass
class ErrorKind:
    kind: str
    first_line: str
    count: int = 0


@dataclass
class ContainerLogs:
    """The deduplicated summary for one container, accumulated across capture phases."""

    container: str
    errors: dict[str, ErrorKind] = field(default_factory=dict)
    tail: list[str] = field(default_factory=list)
    total_lines: int = 0
    phases: list[str] = field(default_factory=list)
    note: str = ""

    def add_lines(self, lines: list[str]) -> None:
        self.total_lines += len(lines)
        for line in lines:
            kind = classify_log_line(line)
            if kind is None:
                continue
            if kind not in self.errors:
                self.errors[kind] = ErrorKind(kind=kind, first_line=line.strip()[:300])
            self.errors[kind].count += 1
        self.tail = [line.rstrip() for line in lines][-TAIL_LINES:]

    def to_json(self) -> dict:
        return {
            "container": self.container,
            "phases": self.phases,
            "total_lines": self.total_lines,
            "errors": [
                {"kind": e.kind, "first_line": e.first_line, "count": e.count}
                for e in self.errors.values()
            ],
            "tail": self.tail,
            "note": self.note,
        }


class LogCapture:
    """One capture session for one experiment. Call `capture()` at injection and at rollback."""

    def __init__(self, docker: Docker, results_dir: Path | str, experiment_id: str) -> None:
        self.docker = docker
        self.dir = Path(results_dir) / f"{experiment_id}.logs"
        self.experiment_id = experiment_id
        self.by_container: dict[str, ContainerLogs] = {}

    def capture(self, containers: list[str], phase: str, since: str | None = None,
                tail: int = MAX_LINES) -> None:
        """Capture logs for each container for one phase. Best-effort; never raises."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for name in containers:
            entry = self.by_container.setdefault(name, ContainerLogs(container=name))
            if phase not in entry.phases:
                entry.phases.append(phase)
            try:
                raw = self.docker.logs(name, since=since, tail=tail)  # type: ignore[attr-defined]
            except DockerError as exc:
                entry.note = f"could not read logs: {exc}"
                continue
            lines = [ln for ln in raw.splitlines() if ln.strip()][-MAX_LINES:]
            entry.add_lines(lines)
            self._write_raw(name, phase, lines)

    def finalize(self) -> dict[str, dict]:
        """Write the per-experiment summary and return it. Safe to call with nothing captured."""
        summary = {name: entry.to_json() for name, entry in self.by_container.items()}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "summary.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
        return summary

    def _write_raw(self, name: str, phase: str, lines: list[str]) -> None:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        with contextlib.suppress(OSError):
            (self.dir / f"{safe}.{phase}.log").write_text("\n".join(lines), encoding="utf-8")


__all__ = [
    "MAX_LINES",
    "TAIL_LINES",
    "ContainerLogs",
    "ErrorKind",
    "LogCapture",
    "classify_log_line",
]

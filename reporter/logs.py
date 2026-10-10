"""Container log capture for one time window, reduced to a few findings.

Raw logs are noisy, so the reporter never reads them whole. This module pulls `docker logs` for the
target and the services that call it, classifies the error lines, and keeps one finding per
(container, kind): how many times it happened, when it first happened, and the first and last line.
The trimmed raw log is also saved under results/<id>.logs/ for humans.

Everything here is read-only. If Docker is unavailable the result is an empty list and the caller
records why, so a report never fails because logs are missing.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .models import LogFinding

#: Raw lines kept on disk per container (first half + last half), and the longest line kept.
MAX_SAVED_LINES = 200
MAX_LINE_CHARS = 220
DOCKER_TIMEOUT_S = 15

#: Order matters: the first pattern that matches a line decides its kind.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("oom", re.compile(r"out of memory|oom", re.I)),
    ("panic", re.compile(r"\bpanic\b|fatal|segfault|unhandled exception", re.I)),
    ("deadline_exceeded", re.compile(r"deadline ?exceeded|context deadline", re.I)),
    ("timeout", re.compile(r"timed? ?out|timeout", re.I)),
    ("connection_refused", re.compile(r"connection refused|econnrefused|connect: refused", re.I)),
    ("unavailable", re.compile(r"unavailable|no such host|name resolution|dial tcp|connection reset", re.I)),
    ("error", re.compile(r"\berror\b|\bfailed\b|\bfailure\b|exception", re.I)),
]

#: (command) -> (stdout+stderr text, return code). Injectable so tests need no Docker.
Runner = Callable[[list[str]], tuple[str, int]]


def classify(line: str) -> str | None:
    for kind, pattern in PATTERNS:
        if pattern.search(line):
            return kind
    return None


def _default_runner(command: list[str]) -> tuple[str, int]:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",  # noqa: S603
        timeout=DOCKER_TIMEOUT_S,
        check=False,
    )
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode


def _split_timestamp(line: str) -> tuple[datetime | None, str]:
    """`docker logs --timestamps` prefixes each line with an RFC3339 time (nanoseconds, UTC)."""
    head, _, rest = line.partition(" ")
    match = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", head)
    if not match:
        return None, line
    base, fraction, zone = match.groups()
    fraction = (fraction or ".0")[:7].ljust(7, "0")  # keep microseconds
    zone = "+00:00" if zone in (None, "Z") else zone
    try:
        return datetime.fromisoformat(f"{base}{fraction}{zone}"), rest
    except ValueError:
        return None, line


def _trim(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 3] + "..."


def _utc(value: datetime) -> str:
    stamp = value if value.tzinfo else value.astimezone()
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def findings_from_text(container: str, service: str | None, text: str) -> list[LogFinding]:
    """Deduplicate classified error lines into findings. Pure function, easy to test."""
    found: dict[str, LogFinding] = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        stamp, message = _split_timestamp(raw)
        kind = classify(message)
        if kind is None:
            continue
        line = _trim(message)
        existing = found.get(kind)
        if existing is None:
            found[kind] = LogFinding(
                container=container,
                service=service,
                kind=kind,
                count=1,
                first_at=stamp,
                first_line=line,
                last_line=line,
            )
        else:
            existing.count += 1
            existing.last_line = line
    return sorted(
        found.values(), key=lambda f: (f.first_at is None, f.first_at or datetime.max.replace(tzinfo=UTC))
    )


def capture_logs(
    containers: dict[str, str | None],
    start: datetime,
    end: datetime,
    out_dir: Path | None = None,
    runner: Runner | None = None,
    binary: str = "docker",
) -> tuple[list[LogFinding], list[str]]:
    """Return (findings, notes). `containers` maps container name -> service name."""
    run = runner or _default_runner
    findings: list[LogFinding] = []
    notes: list[str] = []
    for name, service in containers.items():
        command = [binary, "logs", "--timestamps", "--since", _utc(start), "--until", _utc(end), name]
        try:
            text, code = run(command)
        except FileNotFoundError:
            notes.append("container logs not captured: the docker CLI was not found")
            return findings, notes
        except subprocess.TimeoutExpired:
            notes.append(f"container logs for {name} not captured: docker logs timed out")
            continue
        if code != 0:
            notes.append(f"container logs for {name} not captured: {_trim(text) or 'docker logs failed'}")
            continue
        if out_dir is not None:
            _save_raw(out_dir, name, text)
        found = findings_from_text(name, service, text)
        if not found and text.strip():
            notes.append(f"{name}: no error lines in the window ({len(text.splitlines())} lines read)")
        findings.extend(found)
    return findings, notes


def _save_raw(out_dir: Path, container: str, text: str) -> None:
    lines = text.splitlines()
    if len(lines) > MAX_SAVED_LINES:
        half = MAX_SAVED_LINES // 2
        lines = lines[:half] + [f"... {len(lines) - MAX_SAVED_LINES} lines omitted ..."] + lines[-half:]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{container}.log").write_text(
            "\n".join(_trim(line) for line in lines) + "\n", encoding="utf-8"
        )
    except OSError:
        pass

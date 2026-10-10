"""Read the container logs captured during a run into a form the evidence pack can cite.

The capture itself happens at injection and rollback in `monitor.log_capture` (called by the
runner), which writes `results/<id>.logs/`. This module is the *reader*: it loads that summary -
the deduplicated first-error-of-each-kind-with-a-count and the tail per container - so Agent 2 can
turn "connection refused x12 in cartservice" into an evidence item. If the summary is missing it
falls back to re-reading the raw `.log` files, so an older run without a summary still reports.
"""
from __future__ import annotations

import json
from pathlib import Path

from monitor.log_capture import classify_log_line


def read_captured_logs(results_dir: Path | str, experiment_id: str) -> dict[str, dict]:
    """Return {container: {errors, tail, phases, note}} for the experiment, or {} if none."""
    log_dir = Path(results_dir) / f"{experiment_id}.logs"
    if not log_dir.is_dir():
        return {}
    summary_path = log_dir / "summary.json"
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return _rebuild_from_raw(log_dir)


def _rebuild_from_raw(log_dir: Path) -> dict[str, dict]:
    """Reconstruct the per-container summary from the raw `<container>.<phase>.log` files."""
    out: dict[str, dict] = {}
    for path in sorted(log_dir.glob("*.log")):
        name, _, _phase = path.stem.rpartition(".")
        name = name or path.stem
        entry = out.setdefault(name, {"container": name, "errors": [], "tail": [], "phases": [],
                                      "total_lines": 0, "note": ""})
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        seen: dict[str, dict] = {e["kind"]: e for e in entry["errors"]}
        for line in lines:
            kind = classify_log_line(line)
            if kind is None:
                continue
            if kind not in seen:
                seen[kind] = {"kind": kind, "first_line": line.strip()[:300], "count": 0}
            seen[kind]["count"] += 1
        entry["errors"] = list(seen.values())
        entry["tail"] = [ln.rstrip() for ln in lines][-5:]
        entry["total_lines"] += len(lines)
    return out


__all__ = ["read_captured_logs"]

"""Read the monitor's telemetry files.

The monitor owns these files; nothing here writes to them. Two sources are used:

  * `telemetry.json` - the latest 60 samples, for live watching.
  * `telemetry_log.jsonl` - the append-only history, for the before/after phases of an experiment
    (the 60-sample window is only ~2 minutes, which a 120-second experiment would overrun).

Freshness is treated as a safety signal: if telemetry stops updating we have gone blind, and the
runner aborts rather than keeping a failure in place with nobody watching.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .limits import MAX_TELEMETRY_AGE_S


class TelemetryUnavailable(RuntimeError):
    """Telemetry is missing, unreadable or too old to act on."""


class TelemetryReader:
    def __init__(self, results_dir: Path) -> None:
        self.results_dir = Path(results_dir)
        self.snapshot_path = self.results_dir / "telemetry.json"
        self.log_path = self.results_dir / "telemetry_log.jsonl"
        self.baseline_path = self.results_dir / "baseline.json"

    # ---- reads ----
    def latest(self) -> dict[str, Any]:
        """The most recent sample. Raises TelemetryUnavailable if there is none."""
        samples = self.recent(limit=1)
        if not samples:
            raise TelemetryUnavailable(
                f"no samples in {self.snapshot_path}. Start the monitor first "
                f"(scripts\\monitor.bat or python monitor/headless_monitor.py)."
            )
        return samples[-1]

    def recent(self, limit: int = 60) -> list[dict[str, Any]]:
        if not self.snapshot_path.exists():
            raise TelemetryUnavailable(f"{self.snapshot_path} does not exist - is the monitor running?")
        try:
            data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TelemetryUnavailable(f"could not read {self.snapshot_path}: {exc}") from exc
        if not isinstance(data, list):
            raise TelemetryUnavailable(f"{self.snapshot_path} is not a list of samples")
        return data[-limit:]

    def between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Samples with `start <= timestamp <= end`, from the history log when available."""
        samples = self._all_history() or self.recent(limit=60)
        out = []
        for sample in samples:
            try:
                stamp = datetime.fromisoformat(sample["timestamp"])
            except (KeyError, ValueError):
                continue
            if start <= stamp <= end:
                out.append(sample)
        return out

    def _all_history(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in (self.log_path.with_name(self.log_path.name + ".1"), self.log_path):
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a half-written final line
        return out

    def baseline(self) -> dict[str, Any] | None:
        if not self.baseline_path.exists():
            return None
        try:
            return json.loads(self.baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ---- freshness ----
    def age_seconds(self, sample: dict[str, Any] | None = None) -> float:
        sample = sample or self.latest()
        stamp = datetime.fromisoformat(sample["timestamp"])
        now = datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.now()
        return (now - stamp).total_seconds()

    def require_fresh(self, max_age_s: float = MAX_TELEMETRY_AGE_S) -> dict[str, Any]:
        """Return the latest sample, or raise if the monitor has stopped updating it."""
        sample = self.latest()
        age = self.age_seconds(sample)
        if age > max_age_s:
            raise TelemetryUnavailable(
                f"telemetry is {age:.0f}s old (limit {max_age_s:.0f}s) - the monitor is not running. "
                f"Refusing to inject a failure with no monitoring."
            )
        return sample

    def wait_for_fresh(self, timeout_s: float = 20.0, poll_s: float = 1.0) -> dict[str, Any]:
        """Wait briefly for a fresh sample, so a just-started monitor is not a hard error."""
        deadline = time.time() + timeout_s
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                return self.require_fresh()
            except TelemetryUnavailable as exc:
                last_error = exc
                time.sleep(poll_s)
        raise TelemetryUnavailable(str(last_error))


def is_degraded(sample: dict[str, Any], error_rate_pct: float = 0.0) -> bool:
    """Any probe failure (or an error rate above `error_rate_pct`) counts as degraded."""
    summary = sample.get("summary") or {}
    return float(summary.get("error_rate_pct") or 0.0) > error_rate_pct


def healthy(sample: dict[str, Any], p95_limit_ms: float | None = None) -> bool:
    """No probe errors, every container ready, and (optionally) p95 under a limit."""
    summary = sample.get("summary") or {}
    if float(summary.get("error_rate_pct") or 0.0) > 0:
        return False
    if summary.get("not_ready"):
        return False
    ready, total = summary.get("ready_pods"), summary.get("total_pods")
    if ready is not None and total is not None and ready != total:
        return False
    return not (p95_limit_ms is not None
                and float(summary.get("p95_latency_ms") or 0.0) > p95_limit_ms)


def summarise_phase(samples: list[dict[str, Any]], seconds: float | None = None) -> dict[str, Any]:
    """Aggregate a list of samples into the Phase shape (ignoring simulated samples)."""
    real = [s for s in samples if not s.get("simulated")]
    if not real:
        return {"samples": 0, "seconds": round(seconds or 0.0, 1)}
    errors = [float((s.get("summary") or {}).get("error_rate_pct") or 0.0) for s in real]
    p95s = [float((s.get("summary") or {}).get("p95_latency_ms") or 0.0) for s in real]
    failed: set[str] = set()
    types: dict[str, int] = {}
    for sample in real:
        for name, metric in (sample.get("metrics") or {}).items():
            if not metric.get("success", True):
                failed.add(name)
        for key, count in ((sample.get("summary") or {}).get("error_types") or {}).items():
            types[key] = types.get(key, 0) + int(count)
    if seconds is None:
        stamps = [datetime.fromisoformat(s["timestamp"]) for s in real]
        seconds = (max(stamps) - min(stamps)).total_seconds() if len(stamps) > 1 else 0.0
    return {
        "samples": len(real),
        "seconds": round(seconds, 1),
        "error_rate_pct_mean": round(sum(errors) / len(errors), 2),
        "error_rate_pct_max": round(max(errors), 2),
        "p95_ms_mean": round(sum(p95s) / len(p95s), 2),
        "p95_ms_max": round(max(p95s), 2),
        "failed_endpoints": sorted(failed),
        "error_types": dict(sorted(types.items())),
    }


__all__ = [
    "TelemetryReader",
    "TelemetryUnavailable",
    "healthy",
    "is_degraded",
    "summarise_phase",
]

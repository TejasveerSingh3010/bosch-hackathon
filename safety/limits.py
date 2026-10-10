"""Load target.yaml and normalise its limits.

One unit conversion lives here and nowhere else: `target.yaml` and experiment `abort_if` blocks
express error rate as a **fraction** (0.20), while the monitor's telemetry reports a
**percentage** (20.0). Mixing those up would make the watchdog 100x too slow to abort, so every
threshold is converted to a percentage at the edge and only percentages are used after that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET_FILE = REPO_ROOT / "target.yaml"
DEFAULT_RESULTS_DIR = REPO_ROOT / "results"

#: Hard ceiling, independent of target.yaml, so a bad config cannot authorise a long outage.
ABSOLUTE_MAX_EXPERIMENT_S = 600
#: Telemetry older than this means the monitor is not running - never inject blind.
MAX_TELEMETRY_AGE_S = 15.0


def as_percent(value: float | int | None, default: float) -> float:
    """Accept a fraction (0.2) or a percentage (20) and always return a percentage.

    Values at or below 1 are read as fractions, which is safe here because a 1% error-rate
    threshold is below the noise floor of a 3-4 probe sample and nobody would configure it.
    """
    if value is None:
        return default
    value = float(value)
    if value < 0:
        raise ValueError(f"threshold cannot be negative: {value}")
    return value * 100.0 if value <= 1.0 else value


@dataclass(frozen=True)
class Thresholds:
    """Abort limits, always as percentages / milliseconds."""

    error_rate_pct: float = 20.0
    p95_ms: float = 2000.0
    #: Abort if this many containers *other than the target* stop being ready (blast radius).
    max_collateral_unready: int = 1

    def as_dict(self) -> dict[str, float]:
        return {
            "error_rate_pct": self.error_rate_pct,
            "p95_ms": self.p95_ms,
            "max_collateral_unready": self.max_collateral_unready,
        }


@dataclass(frozen=True)
class TargetConfig:
    """Everything the safety layer needs to judge an experiment."""

    app_name: str
    compose_project: str | None
    frontend_url: str
    allowed_containers: tuple[str, ...]
    thresholds: Thresholds
    max_experiment_seconds: int
    results_dir: Path = field(default=DEFAULT_RESULTS_DIR)
    source: Path | None = None

    @property
    def effective_max_seconds(self) -> int:
        return min(self.max_experiment_seconds, ABSOLUTE_MAX_EXPERIMENT_S)

    @classmethod
    def load(cls, path: Path | None = None, results_dir: Path | None = None) -> TargetConfig:
        path = Path(path or DEFAULT_TARGET_FILE)
        if not path.exists():
            raise FileNotFoundError(f"{path} not found - copy it from the repo root or pass --target")
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw, source=path, results_dir=results_dir)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source: Path | None = None,
                  results_dir: Path | None = None) -> TargetConfig:
        thresholds_raw = raw.get("thresholds") or {}
        allowed = tuple(raw.get("allowed_containers") or ())
        max_seconds = int(raw.get("max_experiment_seconds") or 120)
        if max_seconds <= 0:
            raise ValueError("max_experiment_seconds must be positive")
        return cls(
            app_name=raw.get("app_name") or "unknown",
            compose_project=raw.get("compose_project") or None,
            frontend_url=(raw.get("frontend_url") or "http://localhost:8080").rstrip("/"),
            allowed_containers=allowed,
            thresholds=Thresholds(
                error_rate_pct=as_percent(thresholds_raw.get("error_rate"), 20.0),
                p95_ms=float(thresholds_raw.get("p95_ms") or 2000.0),
                max_collateral_unready=int(thresholds_raw.get("max_collateral_unready", 1)),
            ),
            max_experiment_seconds=max_seconds,
            results_dir=Path(results_dir or DEFAULT_RESULTS_DIR),
            source=source,
        )

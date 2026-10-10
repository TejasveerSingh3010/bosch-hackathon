"""The automatic stop: watches telemetry during an experiment and says when to abort.

The watchdog is a pure decision function over samples, with no Docker access and no timers of its
own, so every abort rule is directly unit-testable (see tests/test_watchdog.py). The runner owns
the loop and performs the rollback.

Four reasons to abort:
  * error rate above the limit;
  * p95 latency above the limit;
  * too many containers *other than the target* unready (blast radius);
  * telemetry gone stale - we have lost visibility, so stop rather than continue blind.

Simulated samples (the dashboard's "test alerting" button) are ignored, so a monitor self-test can
never abort a real experiment.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .limits import MAX_TELEMETRY_AGE_S, Thresholds

#: Consecutive breaching samples required before aborting, so one noisy probe is not enough.
DEFAULT_BREACH_STREAK = 2


@dataclass
class Decision:
    """The watchdog's verdict for one sample."""

    abort: bool = False
    reason: str | None = None
    degraded: bool = False
    #: Set when the sample is unusable (stale or simulated) rather than a measurement.
    ignored: bool = False


class Watchdog:
    """Evaluates samples one at a time and decides whether to stop the experiment."""

    def __init__(
        self,
        thresholds: Thresholds,
        target_container: str | None = None,
        breach_streak: int = DEFAULT_BREACH_STREAK,
        max_telemetry_age_s: float = MAX_TELEMETRY_AGE_S,
    ) -> None:
        self.thresholds = thresholds
        self.target_container = target_container
        self.breach_streak = max(1, int(breach_streak))
        self.max_telemetry_age_s = max_telemetry_age_s
        self._streak = 0
        self._last_breach: str | None = None
        self.first_degraded_at: datetime | None = None
        self.samples_seen = 0

    # ---- internals ----
    def _breach(self, sample: dict[str, Any]) -> str | None:
        """Which threshold this single sample breaches, if any."""
        summary = sample.get("summary") or {}
        error_rate = float(summary.get("error_rate_pct") or 0.0)
        if error_rate > self.thresholds.error_rate_pct:
            return (f"error rate {error_rate:.1f}% exceeded the limit "
                    f"{self.thresholds.error_rate_pct:.1f}%")

        p95 = float(summary.get("p95_latency_ms") or 0.0)
        if p95 > self.thresholds.p95_ms:
            return f"p95 latency {p95:.0f}ms exceeded the limit {self.thresholds.p95_ms:.0f}ms"

        collateral = [n for n in (summary.get("not_ready") or []) if n != self.target_container]
        if len(collateral) > self.thresholds.max_collateral_unready:
            return (f"blast radius too large: {len(collateral)} container(s) other than the target "
                    f"are unready ({', '.join(sorted(collateral))}), limit "
                    f"{self.thresholds.max_collateral_unready}")
        return None

    # ---- interface ----
    def check(self, sample: dict[str, Any], age_s: float | None = None) -> Decision:
        """Judge one telemetry sample."""
        if sample.get("simulated"):
            return Decision(ignored=True, reason="simulated sample ignored")

        if age_s is not None and age_s > self.max_telemetry_age_s:
            return Decision(
                abort=True,
                ignored=True,
                reason=(f"telemetry went stale ({age_s:.0f}s old, limit "
                        f"{self.max_telemetry_age_s:.0f}s) - monitoring lost, stopping to be safe"),
            )

        self.samples_seen += 1
        degraded = float((sample.get("summary") or {}).get("error_rate_pct") or 0.0) > 0.0
        if degraded and self.first_degraded_at is None:
            try:
                self.first_degraded_at = datetime.fromisoformat(sample["timestamp"])
            except (KeyError, ValueError):
                self.first_degraded_at = None

        breach = self._breach(sample)
        if breach is None:
            self._streak = 0
            self._last_breach = None
            return Decision(degraded=degraded)

        self._streak += 1
        self._last_breach = breach
        if self._streak >= self.breach_streak:
            return Decision(
                abort=True,
                degraded=degraded,
                reason=f"{breach} for {self._streak} consecutive samples",
            )
        return Decision(degraded=degraded, reason=f"{breach} (sample {self._streak}/{self.breach_streak})")

    def note_detection(self, sample: dict[str, Any]) -> None:
        """Record the first degraded sample without affecting the abort streak."""
        if self.first_degraded_at is None and float(
            (sample.get("summary") or {}).get("error_rate_pct") or 0.0
        ) > 0.0:
            with contextlib.suppress(KeyError, ValueError):
                self.first_degraded_at = datetime.fromisoformat(sample["timestamp"])


__all__ = ["DEFAULT_BREACH_STREAK", "Decision", "Watchdog"]

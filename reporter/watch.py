"""Live watcher: notices a failure the moment it shows up in telemetry and reports on it when it ends.

    python -m reporter watch

An incident starts at the first sample with a probe error or a service down, and ends after a few
healthy samples in a row. Failures that belong to an experiment are skipped here (the orchestrator
already triggers a report when the experiment finishes), so each failure gets exactly one report.
"""

from __future__ import annotations

import contextlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from safety.telemetry import TelemetryUnavailable

from .evidence import parse_ts
from .render import to_console
from .service import Reporter

CLEAR_AFTER_SAMPLES = 3  # healthy samples in a row that end an incident
CONTEXT_SAMPLES = 15  # samples kept from before the incident started
MAX_INCIDENT_S = 600  # a longer incident is reported in pieces


def is_failing(sample: dict[str, Any]) -> bool:
    """A probe error or any application service down. Latency alone does not start an incident."""
    summary = sample.get("summary") or {}
    return float(summary.get("error_rate_pct") or 0.0) > 0 or bool(summary.get("services_down"))


def describe(sample: dict[str, Any]) -> str:
    summary = sample.get("summary") or {}
    down = summary.get("services_down") or []
    pages = sorted(n for n, m in (sample.get("metrics") or {}).items() if not m.get("success", True))
    bits = []
    if down:
        bits.append("services down: " + ", ".join(down))
    if pages:
        bits.append("pages failing: " + ", ".join(pages))
    return "; ".join(bits) or f"error rate {summary.get('error_rate_pct')}%"


@dataclass
class IncidentEvent:
    kind: str  # "start" or "end"
    sample: dict[str, Any] | None = None  # the triggering sample (start)
    samples: list[dict[str, Any]] = field(default_factory=list)  # the whole window (end)
    reason: str = ""
    experiment: bool = False  # an experiment was in progress during the incident


class IncidentDetector:
    """Pure state machine over samples, so the start and end rules are unit-testable."""

    def __init__(
        self,
        clear_after: int = CLEAR_AFTER_SAMPLES,
        context: int = CONTEXT_SAMPLES,
        max_incident_s: float = MAX_INCIDENT_S,
    ) -> None:
        self.clear_after = clear_after
        self.max_incident_s = max_incident_s
        self._ring: deque[dict[str, Any]] = deque(maxlen=context)
        self._active: list[dict[str, Any]] | None = None
        self._clean = 0
        self._experiment = False
        self._started: datetime | None = None
        self._last_ts: datetime | None = None

    @property
    def active(self) -> bool:
        return self._active is not None

    def skip_before(self, stamp: datetime | None) -> None:
        self._last_ts = stamp

    def feed(self, sample: dict[str, Any], experiment_active: bool = False) -> list[IncidentEvent]:
        if sample.get("simulated"):
            return []
        stamp = parse_ts(sample.get("timestamp"))
        if stamp is not None:
            if self._last_ts is not None and stamp <= self._last_ts:
                return []
            self._last_ts = stamp
        failing = is_failing(sample)
        events: list[IncidentEvent] = []

        if self._active is None:
            if failing:
                self._active = [*self._ring, sample]
                self._clean, self._started, self._experiment = 0, stamp, experiment_active
                events.append(IncidentEvent("start", sample=sample, experiment=experiment_active))
            else:
                self._ring.append(sample)
            return events

        self._active.append(sample)
        self._experiment = self._experiment or experiment_active
        self._clean = 0 if failing else self._clean + 1
        too_long = bool(
            stamp and self._started and (stamp - self._started).total_seconds() > self.max_incident_s
        )
        if self._clean >= self.clear_after or too_long:
            events.append(
                self._close("recovered" if self._clean >= self.clear_after else "long-running incident")
            )
        return events

    def flush(self) -> list[IncidentEvent]:
        """End of data (replay mode): close an open incident."""
        return [self._close("end of telemetry")] if self._active is not None else []

    def _close(self, reason: str) -> IncidentEvent:
        event = IncidentEvent(
            "end", samples=list(self._active or []), reason=reason, experiment=self._experiment
        )
        self._active, self._clean, self._experiment, self._started = None, 0, False, None
        self._ring.clear()
        return event


def _say(message: str) -> None:
    """print, flushed, so output shows up at once even when piped or redirected."""
    print(message, flush=True)


def watch(
    reporter: Reporter,
    poll_s: float = 2.0,
    replay: bool = False,
    echo: Callable[[str], None] = _say,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> list[str]:
    """Poll telemetry forever (or once, in replay mode). Returns the ids of the reports written."""
    detector = IncidentDetector()
    journal = reporter.results_dir / "active_experiment.json"
    written: list[str] = []
    announced_wait = False

    if not replay:
        with contextlib.suppress(TelemetryUnavailable):
            detector.skip_before(parse_ts(reporter.telemetry.latest().get("timestamp")))
        echo(
            "Agent 2 is watching telemetry. A report appears here, and in the dashboard, "
            "when a failure happens."
        )
        echo("Press Ctrl+C to stop.\n")

    def handle(events: list[IncidentEvent]) -> None:
        for event in events:
            if event.kind == "start" and event.sample is not None:
                stamp = parse_ts(event.sample.get("timestamp"))
                clock = stamp.astimezone().strftime("%H:%M:%S") if stamp else "?"
                echo(f"\n[{clock}] FAILURE DETECTED: {describe(event.sample)}")
                echo("          Agent 2 is collecting evidence; the report is written once it recovers.")
            elif event.kind == "end":
                if event.experiment:
                    echo(
                        f"Incident over ({event.reason}). It was part of an experiment, so the report comes "
                        "from the experiment run."
                    )
                    continue
                report = reporter.report_incident(event.samples)
                written.append(report.id)
                echo(to_console(report, str(reporter.reports_dir / f"{report.id}.md")))

    while not should_stop():
        try:
            samples = reporter.telemetry.recent(limit=60)
            announced_wait = False
        except TelemetryUnavailable as exc:
            if not announced_wait:
                echo(f"waiting for telemetry ({exc})")
                announced_wait = True
            samples = []
        for sample in samples:
            handle(detector.feed(sample, experiment_active=journal.exists()))
        if replay:
            handle(detector.flush())
            break
        sleep(poll_s)
    return written

"""Build the evidence pack in code - never by the LLM.

The LLM (when wired in) reads only this pack, so the selection and trimming of evidence is a
deterministic, auditable step. The pack is one ordered timeline of items, each tagged with its
source and an id (E1, E2, ...), drawn from exactly the sources the Agent 2 spec lists:

  1. the ExperimentResult (verdict, timings, phases, abort reason)
  2. the telemetry window, 60 s before injection to 30 s after recovery (first degradation, peaks)
  3. Docker lifecycle events in that window (failure time, independent of the probes)
  4. the captured container logs (the actual error: refused / timeout / ...)
  5. the runner timeline (injection, watchdog decisions, abort, rollback, recovery)
  6. the dependency graph edges and the baseline (service -> page, and what normal looks like)

Items carry structured tags in `data` so the report builder can cite the right ids for each claim.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from safety.telemetry import TelemetryReader

from .logs import read_captured_logs


def _parse(ts: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _hhmmss(dt: datetime | None) -> str:
    return dt.strftime("%H:%M:%S") if dt else "?"


@dataclass
class EvidenceItem:
    id: str
    source: str
    at: datetime | None
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def ref(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source,
                "at": self.at.isoformat() if self.at else None, "summary": self.summary}


@dataclass
class EvidencePack:
    experiment_id: str
    result: dict[str, Any]
    items: list[EvidenceItem]
    baseline: dict[str, Any] | None
    graph: dict[str, Any] | None
    window: list[dict[str, Any]]

    # ---- lookups the report builder uses to cite evidence ----
    def ids_where(self, **tags: Any) -> list[str]:
        out = []
        for item in self.items:
            if all(item.data.get(k) == v for k, v in tags.items()):
                out.append(item.id)
        return out

    def ids_for_source(self, source: str) -> list[str]:
        return [i.id for i in self.items if i.source == source]

    def first_degraded(self) -> EvidenceItem | None:
        for item in self.items:
            if item.data.get("kind") == "first_degraded":
                return item
        return None


def _analysis(graph: dict[str, Any] | None, service: str | None) -> dict[str, Any]:
    return ((graph or {}).get("analysis") or {}).get(service or "", {}) or {}


def component_degradation(samples: list[dict[str, Any]]) -> list[tuple[str, str, datetime]]:
    """First time each component (service or page) shows degraded, earliest first."""
    first: dict[tuple[str, str], datetime] = {}
    for sample in samples:
        at = _parse(sample.get("timestamp"))
        if at is None:
            continue
        summary = sample.get("summary") or {}
        for service in summary.get("services_down") or []:
            first.setdefault(("service", service), at)
        for page, metric in (sample.get("metrics") or {}).items():
            if not metric.get("success", True):
                first.setdefault(("page", page), at)
    return sorted(((kind, name, at) for (kind, name), at in first.items()), key=lambda t: t[2])


def build_pack(results_dir: Path | str, experiment_id: str) -> EvidencePack:
    """Assemble the full evidence pack for one experiment from everything on disk."""
    results_dir = Path(results_dir)
    result = json.loads((results_dir / f"{experiment_id}.json").read_text(encoding="utf-8"))

    reader = TelemetryReader(results_dir)
    baseline = reader.baseline()
    graph = _load_json(results_dir / "graph.json")

    injected = _parse(result.get("injected_at"))
    recovered = _parse(result.get("recovered_at")) or _parse(result.get("ended_at"))
    start = (injected - timedelta(seconds=60)) if injected else None
    end = (recovered + timedelta(seconds=30)) if recovered else None
    window = reader.between(start, end) if (start and end) else []

    raw: list[EvidenceItem] = []

    # (6) context: graph edge + baseline (no timestamp -> sorted first)
    service = result.get("service")
    analysis = _analysis(graph, service)
    if analysis:
        callers = analysis.get("direct_dependents") or analysis.get("dependents") or []
        raw.append(EvidenceItem(
            "", "graph", None,
            f"{service}: criticality {analysis.get('score')}, called by {callers or 'nothing'}",
            {"component": service, "callers": callers},
        ))
    if baseline:
        overall = (baseline.get("overall") or {}).get("sample_p95_ms") or {}
        raw.append(EvidenceItem(
            "", "baseline", None,
            f"baseline p95 mean {overall.get('mean')}ms (error rate "
            f"{(baseline.get('overall') or {}).get('error_rate_pct')}%)",
            {"kind": "baseline"},
        ))

    # (1) the result summary
    raw.append(EvidenceItem(
        "", "result", injected,
        f"verdict {result.get('verdict')}; "
        f"detection {result.get('detection_seconds')}s, recovery {result.get('recovery_seconds')}s"
        + (f"; abort: {result.get('abort_reason')}" if result.get("abort_reason") else ""),
        {"kind": "result"},
    ))

    # (5) the runner timeline
    for event in result.get("timeline") or []:
        raw.append(EvidenceItem(
            "", "timeline", _parse(event.get("t")),
            f"[{event.get('source')}] {event.get('event')}",
            {"kind": "timeline", "timeline_source": event.get("source")},
        ))

    # (2) telemetry window: first degradation and peaks
    degradation = component_degradation(window)
    if degradation:
        kind, name, at = degradation[0]
        raw.append(EvidenceItem(
            "", "telemetry", at,
            f"first degradation: {kind} {name} degraded",
            {"kind": "first_degraded", "component": name, "component_kind": kind},
        ))
    for ckind, name, at in degradation:
        raw.append(EvidenceItem(
            "", "telemetry", at, f"{ckind} {name} first degraded",
            {"kind": "degraded", "component": name, "component_kind": ckind},
        ))
    _add_peaks(window, raw)

    # (3) Docker events in the window
    for event in _docker_events(window):
        raw.append(EvidenceItem(
            "", "docker_event", _parse(event.get("time")),
            f"docker {event.get('action')} {event.get('container')}"
            + (f" exit={event.get('exit_code')}" if event.get("exit_code") not in (None, 0) else ""),
            {"kind": "docker_event", "component": event.get("container"),
             "action": event.get("action")},
        ))

    # (4) captured container logs
    for container, info in read_captured_logs(results_dir, experiment_id).items():
        for err in info.get("errors") or []:
            raw.append(EvidenceItem(
                "", "logs", None,
                f"{container}: {err['kind']} x{err['count']} - {err['first_line']}",
                {"kind": "log_error", "component": container, "error_kind": err["kind"]},
            ))
        if info.get("note"):
            raw.append(EvidenceItem("", "logs", None, f"{container}: {info['note']}",
                                    {"kind": "log_note", "component": container}))

    items = _order_and_number(raw)
    return EvidencePack(experiment_id, result, items, baseline, graph, window)


def _add_peaks(window: list[dict[str, Any]], raw: list[EvidenceItem]) -> None:
    real = [s for s in window if not s.get("simulated")]
    if not real:
        return
    peak_err = max(real, key=lambda s: float((s.get("summary") or {}).get("error_rate_pct") or 0.0))
    peak_p95 = max(real, key=lambda s: float((s.get("summary") or {}).get("p95_latency_ms") or 0.0))
    err = float((peak_err.get("summary") or {}).get("error_rate_pct") or 0.0)
    p95 = float((peak_p95.get("summary") or {}).get("p95_latency_ms") or 0.0)
    if err > 0:
        raw.append(EvidenceItem("", "telemetry", _parse(peak_err.get("timestamp")),
                                f"peak error rate {err}%", {"kind": "peak_error"}))
    raw.append(EvidenceItem("", "telemetry", _parse(peak_p95.get("timestamp")),
                            f"peak p95 latency {p95}ms", {"kind": "peak_p95"}))


def _docker_events(window: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for sample in window:
        for event in sample.get("recent_events") or []:
            key = (event.get("time"), event.get("action"), event.get("container"))
            if key in seen:
                continue
            seen.add(key)
            out.append(event)
    return sorted(out, key=lambda e: e.get("time") or "")


def _order_and_number(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """Sort into one timeline (untimed context first) and assign E1, E2, ... ids."""
    def key(item: EvidenceItem) -> tuple[int, str]:
        return (0, "") if item.at is None else (1, item.at.isoformat())

    ordered = sorted(items, key=key)
    for index, item in enumerate(ordered, start=1):
        item.id = f"E{index}"
    return ordered


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


__all__ = ["EvidenceItem", "EvidencePack", "build_pack", "component_degradation"]

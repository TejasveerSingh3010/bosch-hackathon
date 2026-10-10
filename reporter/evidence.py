"""Build the evidence pack: one ordered, id-tagged timeline plus the facts derived from it.

All of this is plain code. The narrator (stub now, LLM later) only ever sees this pack, so every
claim it makes can be traced back to an id (E1, E2, ...) that points at a real event: a probe that
started failing, a service that went down, a Docker event, a log finding, or a runner step.
"""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from schemas import ExperimentResult

from .models import EvidencePack, LogFinding, TimelineItem

#: Pages each service takes down. Mirrors monitor/boutique_core.SERVICE_IMPACT (kept here so the
#: reporter does not import the monitor, which pulls in the whole probing stack).
PAGE_IMPACT: dict[str, list[str]] = {
    "frontend": ["Home", "Product", "Cart", "Checkout"],
    "productcatalogservice": ["Home", "Product", "Checkout"],
    "currencyservice": ["Home", "Product", "Cart", "Checkout"],
    "cartservice": ["Home", "Product", "Cart", "Checkout"],
    "redis-cart": ["Home", "Product", "Cart", "Checkout"],
    "shippingservice": ["Cart", "Checkout"],
    "checkoutservice": ["Checkout"],
    "paymentservice": ["Checkout"],
    "emailservice": [],
    "adservice": [],
    "recommendationservice": [],
}
INFRASTRUCTURE = {"loadgenerator", "dashboard", "monitor"}

PAD_BEFORE_S = 60
PAD_AFTER_S = 30


# --------------------------------------------------------------------------- small helpers
def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def service_from_container(name: str, project: str | None = None) -> str:
    """`online-boutique-adservice-1` -> `adservice`."""
    base = re.sub(r"-\d+$", "", name)
    if project and base.startswith(project + "-"):
        base = base[len(project) + 1 :]
    return base


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------- dependency graph
def load_graph(repo_root: Path, results_dir: Path) -> dict[str, Any]:
    """results/graph.json if it exists, otherwise derive it from the Compose file. {} on failure."""
    for path in (Path(results_dir) / "graph.json", Path(repo_root) / "results" / "graph.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    try:
        from mapper.compose import parse_compose
        from mapper.graph import DependencyGraph

        return DependencyGraph(parse_compose(Path(repo_root) / "app" / "docker-compose.yml")).to_json()
    except Exception:  # noqa: BLE001 - a missing graph must never stop a report
        return {}


def callers_of(graph: dict[str, Any], service: str) -> list[str]:
    """Services that call `service`, nearest first (direct callers, then their callers, ...)."""
    callers: dict[str, list[str]] = {}
    for edge in graph.get("edges") or []:
        callers.setdefault(edge["to"], []).append(edge["from"])
    order: list[str] = []
    queue = deque([service])
    while queue:
        current = queue.popleft()
        for caller in sorted(callers.get(current, [])):
            if caller not in order and caller != service and caller not in INFRASTRUCTURE:
                order.append(caller)
                queue.append(caller)
    return order


def graph_slice(graph: dict[str, Any], service: str | None) -> dict[str, Any]:
    if not graph or not service:
        return {}
    info = (graph.get("analysis") or {}).get(service) or {}
    return {
        "service": service,
        "callers": callers_of(graph, service),
        "direct_callers": info.get("direct_dependents", []),
        "dependencies": info.get("dependencies", []),
        "criticality_score": info.get("score"),
        "criticality_reason": info.get("rationale"),
        "on_checkout_path": info.get("on_checkout_path"),
        "single_point_of_failure": info.get("single_point_of_failure"),
        "expected_pages": PAGE_IMPACT.get(service),
    }


# --------------------------------------------------------------------------- telemetry scan
class _Scan:
    """Walks telemetry samples once and records every state change as a candidate event."""

    def __init__(self, project: str | None, start: datetime | None, end: datetime | None) -> None:
        self.project = project
        self.start, self.end = start, end
        self.events: list[dict[str, Any]] = []
        self.first_page_failure: dict[str, str] = {}
        self.page_recovered: dict[str, str] = {}
        self.first_service_down: dict[str, str] = {}
        self.pages_seen: set[str] = set()
        self.failed_pages: set[str] = set()
        self.services_down_ever: set[str] = set()
        self.error_types: dict[str, int] = {}
        self.peak_error_rate = 0.0
        self.peak_p95 = 0.0
        self.samples = 0
        self._page_failing: dict[str, bool] = {}
        self._service_down: dict[str, bool] = {}
        self._seen_docker: set[tuple[Any, Any, Any]] = set()
        self._last_restarts: int | None = None
        self.restart_delta = 0
        self.container_for_service: dict[str, str] = {}

    def _in_window(self, t: datetime | None) -> bool:
        if t is None:
            return True
        if self.start and t < self.start:
            return False
        return not (self.end and t > self.end)

    def _add(self, t: datetime | None, source: str, event: str, key: str) -> None:
        self.events.append({"t": t, "source": source, "event": event, "key": key})

    def feed(self, sample: dict[str, Any]) -> None:
        if sample.get("simulated"):
            return
        t = parse_ts(sample.get("timestamp"))
        self.samples += 1
        summary = sample.get("summary") or {}

        for page, metric in (sample.get("metrics") or {}).items():
            self.pages_seen.add(page)
            failing = not metric.get("success", True)
            was = self._page_failing.get(page, False)
            if failing:
                self.failed_pages.add(page)
            if failing and not was:
                etype = metric.get("error_type") or "error"
                latency = metric.get("latency")
                detail = f"{etype}, HTTP {metric.get('status')}" + (
                    f", {float(latency):.0f} ms" if isinstance(latency, int | float) else ""
                )
                self._add(
                    t, "telemetry", f"{page} page probe started failing ({detail})", f"page_fail:{page}"
                )
                if t:
                    self.first_page_failure.setdefault(page, t.isoformat())
            elif was and not failing:
                self._add(t, "telemetry", f"{page} page probe is passing again", f"page_ok:{page}")
                if t:
                    self.page_recovered.setdefault(page, t.isoformat())
            self._page_failing[page] = failing

        for row in sample.get("services") or []:
            name = row.get("service")
            if not name:
                continue
            if row.get("container"):
                self.container_for_service[name] = row["container"]
            down = bool(row.get("down"))
            was = self._service_down.get(name, False)
            if down and not was:
                reason = row.get("reason") or "unhealthy"
                self._add(t, "telemetry", f"{name} went down ({reason})", f"service_down:{name}")
                self.services_down_ever.add(name)
                if t:
                    self.first_service_down.setdefault(name, t.isoformat())
            elif was and not down:
                self._add(t, "telemetry", f"{name} is healthy again", f"service_up:{name}")
            self._service_down[name] = down

        for pod in sample.get("workloads") or []:
            app, name = pod.get("app"), pod.get("name")
            if app and name:
                self.container_for_service.setdefault(app, name)

        for event in sample.get("recent_events") or []:
            ident = (event.get("time"), event.get("action"), event.get("container"))
            if ident in self._seen_docker:
                continue
            self._seen_docker.add(ident)
            et = parse_ts(event.get("time"))
            if not self._in_window(et):
                continue
            service = service_from_container(event.get("container") or "?", self.project)
            extra = []
            if event.get("exit_code") is not None:
                extra.append(f"exit {event['exit_code']}")
            if event.get("signal"):
                extra.append(f"signal {event['signal']}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            self._add(
                et,
                "docker",
                f"docker: {event.get('action')} on {service}{suffix}",
                f"docker:{event.get('action')}:{service}",
            )

        restarts = summary.get("restart_count")
        if isinstance(restarts, int):
            if self._last_restarts is not None and restarts > self._last_restarts:
                self._add(
                    t,
                    "telemetry",
                    f"restart count rose from {self._last_restarts} to {restarts}",
                    "restart_count",
                )
                self.restart_delta += restarts - self._last_restarts
            self._last_restarts = restarts

        for kind, count in (summary.get("error_types") or {}).items():
            self.error_types[kind] = self.error_types.get(kind, 0) + int(count)
        error_rate = float(summary.get("error_rate_pct") or 0.0)
        if error_rate > self.peak_error_rate:
            self.peak_error_rate = error_rate
        p95 = float(summary.get("p95_latency_ms") or 0.0)
        if p95 > self.peak_p95:
            self.peak_p95 = p95


# --------------------------------------------------------------------------- assembly
def _experiment_facts(result: ExperimentResult) -> dict[str, Any]:
    exp = result.experiment
    return {
        "id": exp.id,
        "target": exp.target,
        "action": exp.action,
        "params": exp.params,
        "duration_s": exp.duration_s,
        "reason": exp.reason,
        "verdict": result.verdict,
        "abort_reason": result.abort_reason,
        "container": result.container,
        "service": result.service,
        "injected_at": _iso(result.injected_at),
        "detected_at": _iso(result.detected_at),
        "rolled_back_at": _iso(result.rolled_back_at),
        "recovered_at": _iso(result.recovered_at),
        "detection_seconds": result.detection_seconds,
        "impact_seconds": result.impact_seconds,
        "recovery_seconds": result.recovery_seconds,
        "self_healed": result.self_healed,
        "restart_count_delta": result.restart_count_delta,
        "thresholds_used": result.thresholds_used,
        "baseline": (result.baseline or {}).get("overall"),
        "observations": result.observations,
        "errors": result.errors,
    }


def _runner_events(result: ExperimentResult) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    exp = result.experiment

    def add(t: datetime | None, event: str, key: str) -> None:
        if t is not None:
            out.append({"t": t, "source": "runner", "event": event, "key": key})

    add(
        result.injected_at,
        f"runner injected {exp.action} on {result.container or exp.target}",
        "runner:injected",
    )
    add(result.detected_at, "watchdog saw the first degraded sample", "runner:detected")
    if result.verdict in ("aborted", "stopped"):
        add(
            result.rolled_back_at or result.ended_at,
            f"experiment {result.verdict}: {result.abort_reason or 'no reason recorded'}",
            "runner:abort",
        )
    rollback_note = next((o for o in result.observations if o.startswith("rollback:")), "rollback done")
    add(
        result.rolled_back_at,
        f"runner rolled back ({rollback_note.removeprefix('rollback:').strip()})",
        "runner:rollback",
    )
    add(result.recovered_at, "application healthy again (two clean samples in a row)", "runner:recovered")
    return out


def _assemble(
    *,
    kind: str,
    subject: str,
    samples: list[dict[str, Any]],
    start: datetime | None,
    end: datetime | None,
    project: str | None,
    graph: dict[str, Any],
    focus_service: str | None,
    log_findings: list[LogFinding],
    result: ExperimentResult | None,
    notes: list[str],
) -> EvidencePack:
    scan = _Scan(project, start, end)
    for sample in samples:
        scan.feed(sample)

    events = list(scan.events)
    if result is not None:
        events += _runner_events(result)
    for finding in log_findings:
        events.append(
            {
                "t": finding.first_at,
                "source": "logs",
                "key": f"log:{finding.container}:{finding.kind}",
                "event": f"{finding.container} logged {finding.kind} x{finding.count}: {finding.first_line}",
            }
        )

    far_future = datetime.max.replace(tzinfo=start.tzinfo if start else None)
    ordered = sorted(
        enumerate(events), key=lambda pair: (pair[1]["t"] is None, pair[1]["t"] or far_future, pair[0])
    )
    timeline: list[TimelineItem] = []
    refs: dict[str, str] = {}
    for number, (_, event) in enumerate(ordered, start=1):
        item_id = f"E{number}"
        key = event["key"]
        refs.setdefault(key, item_id)
        timeline.append(
            TimelineItem(id=item_id, t=event["t"], source=event["source"], event=event["event"], key=key)
        )

    if focus_service is None and scan.first_service_down:
        # An incident with no experiment: the earliest service to go down is the best focus we have.
        focus_service = min(scan.first_service_down, key=lambda name: scan.first_service_down[name])

    pages_never_failed = sorted(scan.pages_seen - scan.failed_pages)
    facts = {
        "samples": scan.samples,
        "first_page_failure": scan.first_page_failure,
        "page_recovered": scan.page_recovered,
        "first_service_down": scan.first_service_down,
        "failed_pages": sorted(scan.failed_pages),
        "pages_never_failed": pages_never_failed,
        "services_down_ever": sorted(scan.services_down_ever),
        "error_types": dict(sorted(scan.error_types.items())),
        "peak_error_rate_pct": round(scan.peak_error_rate, 2),
        "peak_p95_ms": round(scan.peak_p95, 1),
        "restart_delta": scan.restart_delta,
        "container_for_service": scan.container_for_service,
    }
    if scan.samples == 0:
        notes = [
            *notes,
            "no telemetry samples fell in the evidence window; timings come from the runner record only",
        ]
    return EvidencePack(
        kind=kind,  # type: ignore[arg-type]
        subject=subject,
        window_start=start,
        window_end=end,
        experiment=_experiment_facts(result) if result else None,
        timeline=timeline,
        refs=refs,
        log_findings=log_findings,
        graph=graph_slice(graph, focus_service),
        facts=facts,
        notes=notes,
    )


def experiment_window(result: ExperimentResult) -> tuple[datetime, datetime]:
    anchor = result.injected_at or result.started_at or datetime.now().astimezone()
    start = anchor - timedelta(seconds=PAD_BEFORE_S)
    last = result.recovered_at or result.ended_at or result.rolled_back_at or anchor
    return start, last + timedelta(seconds=PAD_AFTER_S)


def build_experiment_evidence(
    result: ExperimentResult,
    samples: list[dict[str, Any]],
    graph: dict[str, Any],
    project: str | None,
    log_findings: list[LogFinding] | None = None,
    notes: list[str] | None = None,
) -> EvidencePack:
    start, end = experiment_window(result)
    focus = result.service or service_from_container(result.container or result.experiment.target, project)
    return _assemble(
        kind="experiment",
        subject=result.experiment.id,
        samples=samples,
        start=start,
        end=end,
        project=project,
        graph=graph,
        focus_service=focus,
        log_findings=log_findings or [],
        result=result,
        notes=list(notes or []),
    )


def build_incident_evidence(
    incident_id: str,
    samples: list[dict[str, Any]],
    graph: dict[str, Any],
    project: str | None,
    log_findings: list[LogFinding] | None = None,
    notes: list[str] | None = None,
    focus_service: str | None = None,
) -> EvidencePack:
    stamps = [t for t in (parse_ts(s.get("timestamp")) for s in samples) if t]
    start, end = (min(stamps), max(stamps)) if stamps else (None, None)
    return _assemble(
        kind="incident",
        subject=incident_id,
        samples=samples,
        start=start,
        end=end,
        project=project,
        graph=graph,
        focus_service=focus_service,
        log_findings=log_findings or [],
        result=None,
        notes=list(notes or []),
    )


__all__ = [
    "PAGE_IMPACT",
    "build_experiment_evidence",
    "build_incident_evidence",
    "callers_of",
    "experiment_window",
    "graph_slice",
    "load_graph",
    "parse_ts",
    "service_from_container",
]

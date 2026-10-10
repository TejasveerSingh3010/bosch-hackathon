"""Agent 2 end to end: gather evidence, run the narrator, validate, write the report files.

    evidence (code) -> narrator (stub now, LLM later) -> citation check (code) -> reports/<id>.md/.json

Outputs, all under reports/ by default (override with BOUTIQUE_REPORTS_DIR):

    <id>.md / <id>.json    one report per experiment or incident
    latest.md / latest.json the newest report, which the dashboard shows
    campaign.md            one table row per report
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agents.llm import LLMClient, NotConfigured, StubClient, get_client, log_call
from safety.telemetry import TelemetryReader, TelemetryUnavailable, summarise_phase
from schemas import ExperimentResult

from . import analysis as _stub_rules  # noqa: F401 - importing registers the stub handler
from . import prompts
from .evidence import (
    build_experiment_evidence,
    build_incident_evidence,
    experiment_window,
    load_graph,
    parse_ts,
)
from .logs import Runner, capture_logs
from .models import Analysis, Claim, EvidencePack, LogFinding, Report
from .render import to_console, to_markdown

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PAD_S = 5


def default_reports_dir() -> Path:
    return Path(os.getenv("BOUTIQUE_REPORTS_DIR") or REPO_ROOT / "reports")


class Reporter:
    def __init__(
        self,
        results_dir: Path,
        reports_dir: Path | None = None,
        client: LLMClient | None = None,
        project: str | None = None,
        repo_root: Path = REPO_ROOT,
        log_runner: Runner | None = None,
        capture_container_logs: bool | None = None,
    ) -> None:
        self.results_dir = Path(results_dir)
        self.reports_dir = Path(reports_dir) if reports_dir else default_reports_dir()
        self.client = client or get_client()
        self.project = project or os.getenv("BOUTIQUE_COMPOSE_PROJECT") or None
        self.repo_root = Path(repo_root)
        self.log_runner = log_runner
        if capture_container_logs is None:
            capture_container_logs = os.getenv("BOUTIQUE_REPORT_LOGS", "1") != "0"
        self.capture_container_logs = capture_container_logs
        self.telemetry = TelemetryReader(self.results_dir)

    # ------------------------------------------------------------------ public
    def report_experiment(
        self,
        result: ExperimentResult,
        log_findings: list[LogFinding] | None = None,
        notes: list[str] | None = None,
    ) -> Report:
        start, end = experiment_window(result)
        samples, notes = self._samples(start, end, list(notes or []))
        graph = load_graph(self.repo_root, self.results_dir)

        def build(findings: list[LogFinding], more: list[str]) -> EvidencePack:
            return build_experiment_evidence(result, samples, graph, self.project, findings, [*notes, *more])

        evidence = build([], [])
        if log_findings is None:
            log_findings, log_notes = self._logs(
                evidence, samples, start, end, self.results_dir / f"{result.experiment.id}.logs"
            )
            evidence = build(log_findings, log_notes)
        else:
            evidence = build(log_findings, [])

        exp = result.experiment
        label = result.service or exp.target
        timings = {
            "detection_seconds": result.detection_seconds,
            "impact_seconds": result.impact_seconds,
            "recovery_seconds": result.recovery_seconds,
        }
        phases = {
            "before": result.before.model_dump(),
            "during": result.during.model_dump(),
            "after": result.after.model_dump(),
        }
        return self._finish(
            evidence,
            id_=exp.id,
            title=f"Failure report: {exp.action} on {label} ({exp.id})",
            verdict=result.verdict,
            target=label,
            action=exp.action,
            timings=timings,
            phases=phases,
        )

    def report_experiment_id(self, experiment_id: str) -> Report:
        path = Path(experiment_id)
        if not path.suffix:
            path = self.results_dir / f"{experiment_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"no experiment result at {path}")
        return self.report_experiment(ExperimentResult.model_validate_json(path.read_text(encoding="utf-8")))

    def report_incident(
        self,
        samples: list[dict[str, Any]],
        incident_id: str | None = None,
        log_findings: list[LogFinding] | None = None,
        notes: list[str] | None = None,
    ) -> Report:
        stamps = [t for t in (parse_ts(s.get("timestamp")) for s in samples) if t]
        if not stamps:
            raise ValueError("an incident report needs at least one telemetry sample")
        start, end = min(stamps), max(stamps)
        incident_id = incident_id or f"incident-{start.astimezone():%Y%m%d-%H%M%S}"
        graph = load_graph(self.repo_root, self.results_dir)

        def build(findings: list[LogFinding], more: list[str]) -> EvidencePack:
            return build_incident_evidence(
                incident_id, samples, graph, self.project, findings, [*(notes or []), *more]
            )

        evidence = build([], [])
        if log_findings is None:
            log_findings, log_notes = self._logs(
                evidence, samples, start, end, self.results_dir / f"{incident_id}.logs"
            )
            evidence = build(log_findings, log_notes)
        else:
            evidence = build(log_findings, [])

        focus = evidence.graph.get("service")
        phases = {"during": summarise_phase(samples)}
        return self._finish(
            evidence,
            id_=incident_id,
            title=f"Incident report: {focus or 'unknown component'} ({incident_id})",
            verdict=None,
            target=focus,
            action=None,
            timings={},
            phases=phases,
        )

    # ------------------------------------------------------------------ internals
    def _samples(
        self, start: datetime, end: datetime, notes: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        try:
            return self.telemetry.between(start, end), notes
        except TelemetryUnavailable as exc:
            return [], [*notes, f"telemetry unavailable: {exc}"]

    def _logs(
        self,
        evidence: EvidencePack,
        samples: list[dict[str, Any]],
        start: datetime,
        end: datetime,
        out_dir: Path,
    ) -> tuple[list[LogFinding], list[str]]:
        if not self.capture_container_logs:
            return [], ["container logs were not captured (disabled)"]
        service = evidence.graph.get("service")
        if not service:
            return [], ["container logs were not captured: no failing service to look at"]
        wanted = [service, *evidence.graph.get("callers", [])]
        names = self._container_names(wanted, samples)
        return capture_logs(
            names,
            start - timedelta(seconds=LOG_PAD_S),
            end + timedelta(seconds=LOG_PAD_S),
            out_dir=out_dir,
            runner=self.log_runner,
        )

    def _container_names(self, services: list[str], samples: list[dict[str, Any]]) -> dict[str, str | None]:
        known: dict[str, str] = {}
        for sample in reversed(samples):
            for pod in sample.get("workloads") or []:
                if pod.get("app") and pod.get("name"):
                    known.setdefault(pod["app"], pod["name"])
            if known:
                break
        out: dict[str, str | None] = {}
        for service in services:
            name = known.get(service) or (f"{self.project}-{service}-1" if self.project else service)
            out[name] = service
        return out

    def _narrate(self, evidence: EvidencePack) -> tuple[Analysis, str]:
        payload = evidence.model_dump(mode="json")
        user = prompts.USER_TEMPLATE.format(
            kind=evidence.kind,
            subject=evidence.subject,
            evidence_json=json.dumps(payload, indent=2, default=str),
            schema=prompts.OUTPUT_SCHEMA,
        )
        client: LLMClient = self.client
        try:
            # TODO(llm): with the real client this call sends prompts.SYSTEM + `user` to the model.
            analysis = client.complete_json(
                task=prompts.TASK, system=prompts.SYSTEM, user=user, schema=Analysis, payload=payload
            )
        except NotConfigured:
            # The real client is not wired in or has no key: fall back to the rules,
            # so a report is still written.
            client = StubClient()
            analysis = client.complete_json(
                task=prompts.TASK, system=prompts.SYSTEM, user=user, schema=Analysis, payload=payload
            )
        log_call(
            self.results_dir,
            agent="reporter",
            task=prompts.TASK,
            client=client.name,
            system=prompts.SYSTEM,
            user=user,
            evidence_ids=[i.id for i in evidence.timeline],
        )
        return analysis, client.name

    def _finish(
        self,
        evidence: EvidencePack,
        *,
        id_: str,
        title: str,
        verdict: str | None,
        target: str | None,
        action: str | None,
        timings: dict[str, float | None],
        phases: dict[str, Any],
    ) -> Report:
        analysis, narrator = self._narrate(evidence)
        analysis = check_citations(analysis, {i.id for i in evidence.timeline})
        report = Report(
            id=id_,
            kind=evidence.kind,
            generated_at=datetime.now().astimezone(),
            narrator=narrator,
            title=title,
            verdict=verdict,
            target=target,
            action=action,
            timings=timings,
            phases=phases,
            analysis=analysis,
            evidence=evidence,
        )
        self.write(report)
        return report

    # ------------------------------------------------------------------ output
    def write(self, report: Report) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        markdown = to_markdown(report)
        payload = report.model_dump_json(indent=2)
        path = self.reports_dir / f"{report.id}.md"
        path.write_text(markdown, encoding="utf-8")
        (self.reports_dir / f"{report.id}.json").write_text(payload, encoding="utf-8")
        (self.reports_dir / "latest.md").write_text(markdown, encoding="utf-8")
        (self.reports_dir / "latest.json").write_text(payload, encoding="utf-8")
        self.refresh_campaign()
        return path

    def refresh_campaign(self) -> Path:
        rows: list[tuple[str, str]] = []
        for path in self.reports_dir.glob("*.json"):
            if path.name == "latest.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                analysis, evidence = data["analysis"], data["evidence"]
                facts = evidence.get("facts") or {}
                timings = data.get("timings") or {}
                origin = (analysis.get("origin") or {}).get("component", "-")
                row = (
                    f"| [{data['id']}]({path.stem}.md) | {data['kind']} | {data.get('verdict') or '-'} | "
                    f"{analysis['severity']} | {origin} | "
                    f"{', '.join(facts.get('failed_pages') or []) or 'none'} | "
                    f"{_secs(timings.get('detection_seconds'))} | {_secs(timings.get('recovery_seconds'))} |"
                )
                rows.append((data["generated_at"], row))
            except (OSError, KeyError, ValueError, TypeError):
                continue
        rows.sort(reverse=True)
        lines = [
            "# Failure reports",
            "",
            "| Report | Kind | Verdict | Severity | Failure origin | Pages failed | Detection | Recovery |",
            "|---|---|---|---|---|---|---|---|",
            *[row for _, row in rows],
            "",
        ]
        path = self.reports_dir / "campaign.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def _secs(value: float | None) -> str:
    return "-" if value is None else f"{value:g} s"


def check_citations(analysis: Analysis, valid_ids: set[str]) -> Analysis:
    """Drop citations that point at evidence that does not exist, and say so in the report."""
    dropped = 0

    def clean(ids: list[str]) -> list[str]:
        nonlocal dropped
        kept = [i for i in ids if i in valid_ids]
        dropped += len(ids) - len(kept)
        return kept

    if analysis.origin:
        analysis.origin.evidence = clean(analysis.origin.evidence)
    for hop in analysis.propagation:
        hop.evidence = clean(hop.evidence)
    for claim in [*analysis.what_worked, *analysis.anomalies]:
        claim.evidence = clean(claim.evidence)
    if dropped:
        analysis.anomalies.append(
            Claim(
                text=f"{dropped} citation(s) pointed at evidence ids that do not exist and were removed.",
                basis="unknown",
            )
        )
    return analysis


# --------------------------------------------------------------------------- automatic triggers
def report_after_experiment(
    result: ExperimentResult,
    results_dir: Path,
    project: str | None = None,
    reports_dir: Path | None = None,
    echo: Callable[[str], None] = print,
    reporter: Reporter | None = None,
) -> Report | None:
    """Called by the orchestrator CLI right after an experiment. Never raises: the experiment is
    already over and rolled back, so a reporter problem must not turn it into an error."""
    try:
        echo("\nAgent 2 is writing the failure report...")
        reporter = reporter or Reporter(results_dir, reports_dir, project=project)
        report = reporter.report_experiment(result)
        echo(to_console(report, str(reporter.reports_dir / f"{report.id}.md")))
        return report
    except Exception as exc:  # noqa: BLE001
        echo(f"Agent 2 could not write the report: {type(exc).__name__}: {exc}")
        return None


__all__ = ["Reporter", "check_citations", "default_reports_dir", "report_after_experiment"]

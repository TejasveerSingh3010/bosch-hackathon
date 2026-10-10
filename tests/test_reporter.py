"""Agent 2 (the reporter): evidence, stub narrator, log capture, incident watcher, automatic triggers.

No Docker, no network and no API key: telemetry comes from reporter.demo's synthetic scenarios (the
monitor's real sample format) and container logs come from an injected fake runner.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agents.llm import NotConfigured, RealClient, StubClient, get_client
from reporter import demo as demo_module
from reporter.__main__ import main as reporter_main
from reporter.demo import SCENARIOS, build_scenario, run_demo
from reporter.logs import capture_logs, classify, findings_from_text
from reporter.models import Analysis, Claim, Report
from reporter.service import Reporter, check_citations, report_after_experiment
from reporter.watch import IncidentDetector, watch
from tests.conftest import make_sample

PROJECT = "online-boutique"


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "results", tmp_path / "reports"


def cited_ids(report: Report) -> set[str]:
    a = report.analysis
    ids: set[str] = set(a.origin.evidence) if a.origin else set()
    for hop in a.propagation:
        ids |= set(hop.evidence)
    for claim in [*a.what_worked, *a.anomalies]:
        ids |= set(claim.evidence)
    return ids


# --------------------------------------------------------------------------- the report itself
def test_pause_report_pinpoints_the_frozen_service(dirs):
    results, reports = dirs
    report, path = run_demo("pause", results, reports)
    a = report.analysis

    assert a.origin is not None
    assert a.origin.component == "productcatalogservice"
    assert a.origin.basis == "observed"  # a Docker event and a health check agree
    assert a.severity == "critical"  # Checkout failed
    assert report.evidence.facts["failed_pages"] == ["Checkout", "Home", "Product"]
    assert report.evidence.facts["pages_never_failed"] == ["Cart"]
    assert [h.kind for h in a.propagation] == ["origin", "page", "page", "page"]
    assert all(h.seconds_after_origin >= 0 for h in a.propagation)
    assert "frontend logged deadline_exceeded x14" in a.failure_point  # the callers' logs are named
    assert path.exists() and path.with_suffix(".json").exists()


def test_kill_report_credits_docker_self_healing(dirs):
    results, reports = dirs
    report, _ = run_demo("kill", results, reports)
    texts = " ".join(c.text for c in report.analysis.what_worked)

    assert report.analysis.origin.component == "redis-cart"
    assert "restart policy" in texts
    assert "cartservice logged connection_refused x9" in report.analysis.failure_point


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_claim_cites_evidence_that_exists(dirs, scenario):
    results, reports = dirs
    report, _ = run_demo(scenario, results, reports)
    ids = [i.id for i in report.evidence.timeline]

    assert cited_ids(report) <= set(ids)
    assert cited_ids(report), "claims should cite something"
    times = [i.t for i in report.evidence.timeline if i.t]
    assert times == sorted(times), "timeline ids follow time order"


def test_a_tolerated_failure_is_reported_as_such(dirs, monkeypatch):
    results, reports = dirs
    quiet = {**SCENARIOS["pause"], "failed_pages": [], "down_for_s": 0, "logs": []}
    monkeypatch.setitem(demo_module.SCENARIOS, "quiet", quiet)

    report, _ = run_demo("quiet", results, reports)

    assert report.analysis.severity == "none"
    assert "tolerated" in report.analysis.headline
    assert any("tolerated the failure" in c.text for c in report.analysis.what_worked)


def test_missing_evidence_is_stated_not_hidden(dirs):
    results, reports = dirs
    result, _ = build_scenario("pause", results)
    (results / "telemetry.json").unlink()
    (results / "telemetry_log.jsonl").unlink()

    report = Reporter(results, reports, project=PROJECT, capture_container_logs=False).report_experiment(
        result
    )

    notes = " ".join(c.text for c in report.analysis.anomalies)
    assert "telemetry unavailable" in notes
    assert report.analysis.origin.basis == "inferred"  # only the runner's word, no independent signal


def test_reports_are_written_where_the_dashboard_looks(dirs):
    results, reports = dirs
    run_demo("pause", results, reports)
    run_demo("kill", results, reports)

    latest = json.loads((reports / "latest.json").read_text(encoding="utf-8"))
    campaign = (reports / "campaign.md").read_text(encoding="utf-8")
    assert latest["id"] == "demo-kill-redis-cart"
    assert "demo-pause-productcatalogservice" in campaign and "demo-kill-redis-cart" in campaign
    assert Report.model_validate(latest).analysis.origin.component == "redis-cart"
    assert (results / "agent_log.jsonl").exists()  # every narrator call is audited


def test_citations_that_do_not_exist_are_dropped_and_flagged():
    analysis = Analysis(
        headline="h",
        severity="minor",
        summary="s",
        failure_point="f",
        what_worked=[Claim(text="x", evidence=["E1", "E99"])],
    )
    cleaned = check_citations(analysis, {"E1"})

    assert cleaned.what_worked[0].evidence == ["E1"]
    assert "do not exist" in cleaned.anomalies[-1].text


# --------------------------------------------------------------------------- the LLM seam
def test_stub_is_the_default_and_unknown_tasks_are_refused():
    assert get_client().name == "stub"
    with pytest.raises(NotConfigured):
        StubClient().complete_json(task="nope", system="", user="", schema=Analysis, payload={})


def test_real_client_needs_a_key_and_is_not_wired(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(NotConfigured):
        RealClient()
    monkeypatch.setenv("LLM_API_KEY", "x")
    with pytest.raises(NotConfigured, match="not wired"):
        RealClient().complete_json(task="t", system="", user="", schema=Analysis, payload={})


def test_a_broken_real_client_falls_back_to_the_rules(dirs):
    results, reports = dirs

    class Broken:
        name = "llm"

        def complete_json(self, **_: object) -> Analysis:
            raise NotConfigured("not wired")

    result, findings = build_scenario("pause", results)
    report = Reporter(results, reports, client=Broken(), project=PROJECT).report_experiment(
        result, log_findings=findings
    )
    assert report.narrator == "stub" and report.analysis.origin.component == "productcatalogservice"


# --------------------------------------------------------------------------- container logs
LOG_TEXT = "\n".join(
    [
        "2026-10-10T08:01:00.100000000Z starting server on :8080",
        "2026-10-10T08:01:05.250000000Z rpc error: code = DeadlineExceeded desc = context deadline exceeded",
        "2026-10-10T08:01:07.500000000Z dial tcp 10.0.0.4:3550: connect: connection refused",
        "2026-10-10T08:01:09.000000000Z rpc error: DeadlineExceeded context deadline exceeded again",
        "",
    ]
)


def test_log_lines_are_classified_and_deduplicated():
    assert classify("context deadline exceeded") == "deadline_exceeded"
    assert classify("starting server") is None

    findings = findings_from_text("c", "frontend", LOG_TEXT)
    by_kind = {f.kind: f for f in findings}

    assert by_kind["deadline_exceeded"].count == 2
    assert by_kind["deadline_exceeded"].last_line.endswith("again")
    assert by_kind["connection_refused"].count == 1
    assert by_kind["deadline_exceeded"].first_at == datetime.fromisoformat("2026-10-10T08:01:05.250000+00:00")


def test_capture_logs_saves_a_trimmed_copy_and_survives_docker_problems(tmp_path):
    start = datetime.fromisoformat("2026-10-10T08:00:00+00:00")
    end = start + timedelta(minutes=2)
    commands: list[list[str]] = []

    def ok(command: list[str]) -> tuple[str, int]:
        commands.append(command)
        return LOG_TEXT, 0

    findings, notes = capture_logs(
        {"online-boutique-frontend-1": "frontend"}, start, end, tmp_path / "logs", ok
    )
    assert {f.kind for f in findings} == {"deadline_exceeded", "connection_refused"}
    assert (tmp_path / "logs" / "online-boutique-frontend-1.log").exists()
    assert commands[0][:2] == ["docker", "logs"] and "2026-10-10T08:00:00Z" in commands[0]

    def missing(_: list[str]) -> tuple[str, int]:
        raise FileNotFoundError

    def slow(_: list[str]) -> tuple[str, int]:
        raise subprocess.TimeoutExpired("docker", 15)

    def fails(_: list[str]) -> tuple[str, int]:
        return "No such container: x", 1

    assert "docker CLI was not found" in capture_logs({"a": "a"}, start, end, None, missing)[1][0]
    assert "timed out" in capture_logs({"a": "a"}, start, end, None, slow)[1][0]
    assert "No such container" in capture_logs({"a": "a"}, start, end, None, fails)[1][0]


def test_reporter_asks_for_the_target_and_its_callers_logs(dirs):
    results, reports = dirs
    result, _ = build_scenario("pause", results)
    asked: list[str] = []

    def runner(command: list[str]) -> tuple[str, int]:
        asked.append(command[-1])
        return "", 0

    Reporter(results, reports, project=PROJECT, log_runner=runner).report_experiment(result)

    assert "online-boutique-productcatalogservice-1" in asked  # the failed service
    assert "online-boutique-frontend-1" in asked  # a caller, from the dependency graph


# --------------------------------------------------------------------------- automatic triggers
def run_of(failing: int, healthy_after: int = 4, start: datetime | None = None) -> list[dict]:
    start = start or datetime.now().astimezone() - timedelta(minutes=5)
    samples = [make_sample(at=start + timedelta(seconds=2 * i)) for i in range(3)]
    samples += [
        make_sample(error_rate_pct=33.0, failed_endpoints=["Home"], at=start + timedelta(seconds=2 * (3 + i)))
        for i in range(failing)
    ]
    samples += [
        make_sample(at=start + timedelta(seconds=2 * (3 + failing + i))) for i in range(healthy_after)
    ]
    return samples


def test_incident_starts_on_the_first_failure_and_ends_after_three_clean_samples():
    detector = IncidentDetector()
    events = [e for s in run_of(failing=2) for e in detector.feed(s)]

    assert [e.kind for e in events] == ["start", "end"]
    assert events[0].sample["summary"]["error_rate_pct"] == 33.0
    assert events[1].reason == "recovered"
    assert len(events[1].samples) == 3 + 2 + 3  # context + failing + the clean samples that closed it


def test_incident_detector_ignores_simulated_and_repeated_samples():
    detector = IncidentDetector()
    simulated = make_sample(error_rate_pct=50.0, failed_endpoints=["Home"], simulated=True)
    assert detector.feed(simulated) == []

    real = make_sample(error_rate_pct=50.0, failed_endpoints=["Home"])
    assert [e.kind for e in detector.feed(real)] == ["start"]
    assert detector.feed(real) == []  # same timestamp again: not a new sample


def test_incident_during_an_experiment_is_flagged():
    detector = IncidentDetector()
    events = [e for s in run_of(failing=2) for e in detector.feed(s, experiment_active=True)]
    assert events[-1].experiment is True


def test_watch_replay_writes_one_report_for_an_unplanned_failure(dirs, write_telemetry, results_dir):
    results, reports = dirs
    write_telemetry(run_of(failing=3))
    echoed: list[str] = []

    written = watch(
        Reporter(results_dir, reports, project=PROJECT, capture_container_logs=False),
        replay=True,
        echo=echoed.append,
    )

    assert len(written) == 1 and written[0].startswith("incident-")
    assert any("FAILURE DETECTED" in line for line in echoed)
    report = Report.model_validate_json((reports / "latest.json").read_text(encoding="utf-8"))
    assert report.kind == "incident" and report.analysis.origin.component == "Home page"
    assert report.analysis.origin.basis == "inferred"  # no service was seen going down, only a symptom


def test_watch_leaves_experiment_failures_to_the_experiment_report(dirs, write_telemetry, results_dir):
    _, reports = dirs
    write_telemetry(run_of(failing=3))
    (results_dir / "active_experiment.json").write_text("{}", encoding="utf-8")

    written = watch(
        Reporter(results_dir, reports, project=PROJECT, capture_container_logs=False),
        replay=True,
        echo=lambda _: None,
    )

    assert written == [] and not (reports / "latest.json").exists()


def test_the_hook_reports_after_an_experiment_and_never_raises(dirs):
    results, reports = dirs
    result, findings = build_scenario("kill", results)
    reporter = Reporter(results, reports, project=PROJECT, capture_container_logs=False)
    out: list[str] = []

    report = report_after_experiment(result, results, PROJECT, reports, echo=out.append, reporter=reporter)
    assert report is not None and "AGENT 2 REPORT" in "\n".join(out)

    class Exploding:
        def report_experiment(self, _: object) -> None:
            raise RuntimeError("boom")

    out.clear()
    assert (
        report_after_experiment(result, results, PROJECT, reports, echo=out.append, reporter=Exploding())
        is None
    )
    assert "could not write the report" in out[-1]
    assert findings


# --------------------------------------------------------------------------- command line
def test_cli_demo_and_latest(dirs, capsys):
    results, reports = dirs
    base = ["--results-dir", str(results), "--reports-dir", str(reports)]

    assert reporter_main([*base, "latest"]) == 1  # nothing yet
    assert reporter_main([*base, "demo"]) == 0
    assert "WHERE IT FAILED" in capsys.readouterr().out
    assert reporter_main([*base, "latest"]) == 0
    assert "productcatalogservice" in capsys.readouterr().out
    assert (
        results / "demo" / "demo-pause-productcatalogservice.json"
    ).exists()  # kept apart from real results
# ------------------------------------------------------------------ the orchestrator hook, end to end

# -------------------------------------------------- the orchestrator hook, end to end
def run_cli(monkeypatch, tmp_path, results_dir, reports, *extra: str) -> int:
    """Run `python -m orchestrator run` against the fake Docker; reports go to tmp."""
    import orchestrator.__main__ as orchestrator_cli
    from tests.fake_docker import FakeDocker, allowed_container_names

    target = tmp_path / "target.yaml"
    names = "\n".join(f"  - {n}" for n in allowed_container_names(PROJECT))
    target.write_text(
        f"app_name: online-boutique\ncompose_project: {PROJECT}\nallowed_containers:\n{names}\n"
        "thresholds:\n  error_rate: 0.2\n  p95_ms: 2000\nmax_experiment_seconds: 120\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator_cli, "DockerCli", FakeDocker)
    monkeypatch.setenv("BOUTIQUE_REPORTS_DIR", str(reports))
    monkeypatch.setenv("BOUTIQUE_REPORT_LOGS", "0")
    return orchestrator_cli.main(
        [
            "--target-file",
            str(target),
            "--results-dir",
            str(results_dir),
            "run",
            "--target",
            "redis-cart",
            "--action",
            "kill",
            "--duration",
            "2",
            "--poll",
            "0.2",
            *extra,
        ]
    )


def test_an_experiment_run_triggers_the_report_automatically(
    monkeypatch, tmp_path, results_dir, write_telemetry, capsys
):
    write_telemetry()
    reports = tmp_path / "reports"

    code = run_cli(monkeypatch, tmp_path, results_dir, reports, "--id", "hook-test")

    out = capsys.readouterr().out
    assert code == 0
    assert "AGENT 2 REPORT" in out and "hook-test" in out
    assert (reports / "hook-test.md").exists() and (reports / "latest.json").exists()
    report = Report.model_validate_json((reports / "hook-test.json").read_text(encoding="utf-8"))
    assert report.target == "redis-cart" and report.action == "kill" and report.verdict == "completed"
    assert report.analysis.origin.component == "redis-cart"


def test_no_report_flag_skips_agent_2(monkeypatch, tmp_path, results_dir, write_telemetry, capsys):
    write_telemetry()
    reports = tmp_path / "reports"

    assert run_cli(monkeypatch, tmp_path, results_dir, reports, "--id", "quiet-run", "--no-report") == 0

    assert "AGENT 2 REPORT" not in capsys.readouterr().out
    assert (results_dir / "quiet-run.json").exists() and not reports.exists()

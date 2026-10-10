"""Log capture: it finds the real error, deduplicates it with a count, caps the output and never
breaks the experiment. Also checks the runner records a structured timeline and captures logs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from monitor.log_capture import LogCapture, classify_log_line
from orchestrator.runner import ExperimentRunner
from schemas import Experiment
from tests.conftest import PROJECT, make_sample

TARGET_SERVICE = "redis-cart"
TARGET = f"{PROJECT}-{TARGET_SERVICE}-1"


def test_classify_log_line_picks_the_right_kind():
    assert classify_log_line("dial tcp: connection refused") == "connection_refused"
    assert classify_log_line("rpc error: context deadline exceeded") == "timeout"
    assert classify_log_line("just a normal startup line") is None


def test_capture_dedups_errors_with_a_count_and_keeps_a_tail(docker, results_dir: Path):
    docker.logs_by_container[TARGET] = "\n".join(
        ["starting up", "connection refused", "connection refused", "connection refused", "bye"]
    )
    cap = LogCapture(docker, results_dir, "exp1")
    cap.capture([TARGET], phase="injection")
    summary = cap.finalize()

    errors = summary[TARGET]["errors"]
    refused = next(e for e in errors if e["kind"] == "connection_refused")
    assert refused["count"] == 3  # deduped to one kind with a count
    assert summary[TARGET]["tail"][-1] == "bye"
    assert (results_dir / "exp1.logs" / f"{TARGET}.injection.log").exists()
    assert (results_dir / "exp1.logs" / "summary.json").exists()


def test_capture_dedups_across_phases(docker, results_dir: Path):
    docker.logs_by_container[TARGET] = "connection refused"
    cap = LogCapture(docker, results_dir, "exp2")
    cap.capture([TARGET], phase="injection")
    cap.capture([TARGET], phase="rollback")
    summary = cap.finalize()
    refused = next(e for e in summary[TARGET]["errors"] if e["kind"] == "connection_refused")
    assert refused["count"] == 2  # one per phase, same kind
    assert summary[TARGET]["phases"] == ["injection", "rollback"]


def test_capture_caps_at_about_200_lines(docker, results_dir: Path):
    docker.logs_by_container[TARGET] = "\n".join(f"line {i}" for i in range(1000))
    cap = LogCapture(docker, results_dir, "exp3")
    cap.capture([TARGET], phase="injection")
    saved = (results_dir / "exp3.logs" / f"{TARGET}.injection.log").read_text(encoding="utf-8")
    assert len(saved.splitlines()) <= 200


def test_capture_records_a_note_when_logs_are_unreadable(docker, results_dir: Path):
    docker.fail_on = {"logs"}
    cap = LogCapture(docker, results_dir, "exp4")
    cap.capture([TARGET], phase="injection")
    summary = cap.finalize()
    assert "could not read logs" in summary[TARGET]["note"]


def test_the_runner_records_a_timeline_and_captures_logs(docker, config, results_dir: Path):
    """End to end: a tolerated kill still produces a timeline and a logs directory."""
    docker.logs_by_container[TARGET] = "serving\nconnection refused\nserving again"
    now = datetime.now().astimezone()
    samples = [make_sample(at=now - timedelta(seconds=2 * (4 - i))) for i in range(5)]
    (results_dir / "telemetry.json").write_text(json.dumps(samples), encoding="utf-8")
    (results_dir / "telemetry_log.jsonl").write_text(
        "\n".join(json.dumps(s) for s in samples) + "\n", encoding="utf-8"
    )

    class Clock:
        def __init__(self):
            self.now = 1_000.0

        def time(self):
            return self.now

        def sleep(self, s):
            self.now += max(s, 0.01)

    clock = Clock()
    runner = ExperimentRunner(docker, config, poll_s=2.0, clock=clock.time, sleep=clock.sleep)
    result = runner.run(Experiment(id="tl-1", target=TARGET_SERVICE, action="kill", duration_s=10))

    sources = {e.source for e in result.timeline}
    events = " ".join(e.event for e in result.timeline)
    assert "injector" in sources and "injected" in events
    assert "rolled back" in events
    assert any(e.source == "logs" for e in result.timeline)
    assert (results_dir / "tl-1.logs" / "summary.json").exists()

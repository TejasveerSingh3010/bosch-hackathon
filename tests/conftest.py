"""Shared fixtures: a fake Docker, a fake monitor and a temporary results directory.

`write_telemetry` writes samples in exactly the shape monitor/headless_monitor.py produces, so the
tests exercise the same parsing path as production rather than a simplified stand-in.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety.limits import TargetConfig  # noqa: E402
from safety.telemetry import TelemetryReader  # noqa: E402
from tests.fake_docker import FakeDocker, allowed_container_names  # noqa: E402

PROJECT = "online-boutique"


def make_sample(
    error_rate_pct: float = 0.0,
    p95_ms: float = 40.0,
    ready: int = 12,
    total: int = 12,
    not_ready: list[str] | None = None,
    failed_endpoints: list[str] | None = None,
    error_types: dict[str, int] | None = None,
    simulated: bool = False,
    at: datetime | None = None,
    restart_count: int = 0,
) -> dict[str, Any]:
    """One telemetry sample in the monitor's real format."""
    stamp = at or datetime.now().astimezone()
    failed = set(failed_endpoints or [])
    metrics = {
        name: {
            "status": 503 if name in failed else 200,
            "latency": p95_ms,
            "success": name not in failed,
            "error": "simulated" if name in failed else "",
            "error_type": "http_5xx" if name in failed else "none",
        }
        for name in ("Home", "Cart", "Product")
    }
    return {
        "timestamp": stamp.isoformat(),
        "simulated": simulated,
        "backend": "docker",
        "metrics": metrics,
        "summary": {
            "request_count": len(metrics),
            "error_rate_pct": error_rate_pct,
            "mean_latency_ms": p95_ms * 0.7,
            "p95_latency_ms": p95_ms,
            "slowest_endpoint": "Home",
            "error_types": error_types or ({"http_5xx": len(failed)} if failed else {}),
            "ready_pods": ready,
            "total_pods": total,
            "restart_count": restart_count,
            "not_ready": not_ready or [],
        },
        "workloads": [],
        "recent_events": [],
        "resources_age_s": 1.0,
    }


@pytest.fixture
def results_dir(tmp_path: Path) -> Path:
    path = tmp_path / "results"
    path.mkdir()
    return path


@pytest.fixture
def write_telemetry(results_dir: Path) -> Callable[..., None]:
    """Write a run of samples ending `now`, as both telemetry.json and the JSONL history."""

    def _write(samples: list[dict[str, Any]] | None = None, count: int = 5, **kwargs: Any) -> None:
        if samples is None:
            now = datetime.now().astimezone()
            samples = [
                make_sample(at=now - timedelta(seconds=2 * (count - index - 1)), **kwargs)
                for index in range(count)
            ]
        (results_dir / "telemetry.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
        (results_dir / "telemetry_log.jsonl").write_text(
            "\n".join(json.dumps(s, separators=(",", ":")) for s in samples) + "\n", encoding="utf-8"
        )

    return _write


@pytest.fixture
def telemetry(results_dir: Path) -> TelemetryReader:
    return TelemetryReader(results_dir)


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def config(results_dir: Path) -> TargetConfig:
    return TargetConfig.from_dict(
        {
            "app_name": "online-boutique",
            "compose_project": PROJECT,
            "frontend_url": "http://localhost:8080",
            "allowed_containers": allowed_container_names(PROJECT),
            "thresholds": {"error_rate": 0.20, "p95_ms": 2000},
            "max_experiment_seconds": 120,
        },
        results_dir=results_dir,
    )


@pytest.fixture(autouse=True)
def no_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never trace to a real Langfuse, whatever the shell or .env holds.

    Set to "" rather than deleted: code that calls load_dotenv() (e.g. the reporter's get_client)
    would otherwise reload the real keys from .env, since load_dotenv fills only *absent* vars.
    """
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL", "LANGFUSE_HOST"):
        monkeypatch.setenv(key, "")


@pytest.fixture(autouse=True)
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never call a real model: an empty key (which .env loading does not override) selects the stub."""
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.delenv("LLM_MODEL", raising=False)

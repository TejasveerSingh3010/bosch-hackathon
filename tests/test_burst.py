"""The dashboard traffic burst can be held for a chosen duration, and still has a hard stop."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import monitor.boutique_core as core


def _ok_response():
    response = MagicMock()
    response.status_code = 200
    return response


def test_a_duration_burst_keeps_sending_until_the_time_is_up(monkeypatch):
    def fake_get(url, timeout=5):
        time.sleep(0.05)
        return _ok_response()

    monkeypatch.setattr(core.requests, "get", fake_get)
    started = time.perf_counter()
    result = core.bounded_traffic_burst("http://boutique.test", workers=2, duration_s=1)
    elapsed = time.perf_counter() - started

    assert result["total"] > 2
    assert result["errors"] == 0
    assert result["duration_s"] == 1
    assert 0.9 <= elapsed <= 2.5


def test_a_duration_burst_stops_at_the_request_cap(monkeypatch):
    monkeypatch.setattr(core.requests, "get", lambda url, timeout=5: _ok_response())
    result = core.bounded_traffic_burst(
        "http://boutique.test", total_requests=4, workers=2, duration_s=30,
    )
    assert result["total"] == 4
    assert result["elapsed_s"] < 5


def test_duration_and_workers_cannot_exceed_the_configured_caps(monkeypatch):
    monkeypatch.setattr(core, "BURST_MAX_SECONDS", 2)
    monkeypatch.setattr(core, "BURST_MAX_WORKERS", 1)
    monkeypatch.setattr(core.requests, "get", lambda url, timeout=5: _ok_response())
    result = core.bounded_traffic_burst(
        "http://boutique.test", total_requests=3, workers=50, duration_s=999,
    )
    assert result["duration_s"] == 2
    assert result["total"] == 3

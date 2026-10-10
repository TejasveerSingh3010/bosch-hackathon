"""Every service failure must reach the dashboard, even the ones no page probe can see.

Covers: per-service status and the breach rule, paused containers, the direct gRPC / Redis
health checks, and the checkout probe retrying instead of switching itself off.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import monitor.boutique_core as core

PROJECT = "online-boutique"
APP = ["frontend", "cartservice", "redis-cart", "productcatalogservice", "currencyservice",
       "shippingservice", "checkoutservice", "paymentservice", "emailservice", "adservice",
       "recommendationservice", "loadgenerator"]


def _pod(app, ready=True, phase="running"):
    return {"name": f"{PROJECT}-{app}-1", "app": app, "phase": phase,
            "ready": "Ready" if ready else "Not ready", "restarts": 0}


def _snap(down=(), phase="exited (137)"):
    pods = [_pod(a, ready=a not in down, phase=phase if a in down else "running") for a in APP]
    return {"backend": "docker", "available": True, "pods": pods, "events": [], "error": None,
            "total_pods": len(pods), "ready_pods": sum(p["ready"] == "Ready" for p in pods),
            "restarts": 0, "cpu_memory": ""}


def _ok_probe_rows():
    return [{"endpoint": n, "url": "", "status": 200, "latency_ms": 20.0, "success": True,
             "error": "", "error_type": "none"} for n in ("Home", "Cart", "Product")]


# ----------------------------------------------------------- service status + breach
@pytest.mark.parametrize("service", ["shippingservice", "emailservice", "adservice",
                                     "recommendationservice", "checkoutservice", "paymentservice"])
def test_a_stopped_service_is_a_breach_even_when_every_page_probe_passes(service):
    sample = core.build_sample(_ok_probe_rows(), _snap(down=[service]))

    assert sample["summary"]["error_rate_pct"] == 0
    assert sample["summary"]["services_down"] == [service]
    assert core.is_breach(sample)
    row = next(r for r in sample["services"] if r["service"] == service)
    assert row["down"] and "container exited (137)" in row["reason"]
    assert sample["services"][0]["service"] == service  # down services are listed first


def test_a_healthy_system_is_not_a_breach():
    sample = core.build_sample(_ok_probe_rows(), _snap())
    assert sample["summary"]["services_down"] == []
    assert not core.is_breach(sample)


def test_loadgenerator_is_listed_but_never_counts_as_an_outage():
    sample = core.build_sample(_ok_probe_rows(), _snap(down=["loadgenerator"]))
    assert sample["summary"]["services_down"] == []
    assert not core.is_breach(sample)
    row = next(r for r in sample["services"] if r["service"] == "loadgenerator")
    assert row["infrastructure"] and not row["down"]


def test_a_failed_health_check_marks_a_running_container_down():
    probes = {"emailservice": {"ok": False, "detail": "timed out (hung or paused)", "kind": "grpc",
                               "port": 8081, "latency_ms": 1000.0}}
    sample = core.build_sample(_ok_probe_rows(), _snap(), service_probes=probes)
    row = next(r for r in sample["services"] if r["service"] == "emailservice")
    assert row["container_ready"] is True and row["health_ok"] is False
    assert sample["summary"]["services_down"] == ["emailservice"]
    assert "health check: timed out" in row["reason"]


def test_health_checks_still_report_when_docker_is_unavailable():
    snap = core._empty_snapshot("docker")
    probes = {"shippingservice": {"ok": False, "detail": "connection refused (process down)",
                                  "kind": "grpc", "port": 50052, "latency_ms": 1.0}}
    sample = core.build_sample(_ok_probe_rows(), snap, service_probes=probes)
    assert sample["summary"]["services_down"] == ["shippingservice"]


def test_the_impact_map_says_which_services_no_page_probe_can_see():
    sample = core.build_sample(_ok_probe_rows(), _snap())
    invisible = sorted(r["service"] for r in sample["services"]
                       if not r["visible_to_page_probes"] and not r["infrastructure"])
    assert invisible == ["adservice", "checkoutservice", "emailservice", "paymentservice",
                         "recommendationservice"]
    shipping = next(r for r in sample["services"] if r["service"] == "shippingservice")
    assert shipping["pages"] == ["Cart", "Checkout"]


def test_the_safety_layer_fields_are_unchanged():
    """not_ready keeps its meaning (container names), so the watchdog and pre-flight behave as before."""
    sample = core.build_sample(_ok_probe_rows(), _snap(down=["emailservice"]))
    assert sample["summary"]["not_ready"] == [f"{PROJECT}-emailservice-1"]


# ----------------------------------------------------------- paused containers
def test_a_paused_container_is_not_ready(monkeypatch):
    def container(app, paused):
        return {"Name": f"/{PROJECT}-{app}-1", "RestartCount": 0,
                "Config": {"Labels": {core.COMPOSE_PROJECT: PROJECT, core.COMPOSE_SERVICE: app}},
                "State": {"Status": "paused" if paused else "running", "Running": True, "Paused": paused,
                          "Restarting": False, "StartedAt": "2026-10-10T06:49:01.1Z"}}

    inspected = [container("frontend", False), container("emailservice", True)]

    def fake_run(cmd, timeout=8):
        if cmd[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="a\nb\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(inspected), stderr="")

    monkeypatch.setattr(core, "_run", fake_run)
    snap = core.get_docker_snapshot()
    pods = {p["app"]: p for p in snap["pods"]}
    assert pods["emailservice"]["ready"] == "Not ready" and pods["emailservice"]["paused"]
    assert pods["frontend"]["ready"] == "Ready"
    assert core.build_sample(_ok_probe_rows(), snap)["summary"]["services_down"] == ["emailservice"]


# ----------------------------------------------------------- direct health checks
grpc = pytest.importorskip("grpc")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def grpc_health_server():
    """A real gRPC server answering grpc.health.v1.Health/Check with raw bytes we choose."""
    servers = []

    def start(reply=b"\x08\x01", delay=0.0, implement=True):
        from concurrent import futures

        def check(request, context):
            time.sleep(delay)
            return reply

        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        if implement:
            handler = grpc.method_handlers_generic_handler("grpc.health.v1.Health", {
                "Check": grpc.unary_unary_rpc_method_handler(
                    check, request_deserializer=lambda b: b, response_serializer=lambda b: b)})
            server.add_generic_rpc_handlers((handler,))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        servers.append(server)
        return port

    yield start
    for server in servers:
        server.stop(0)


def _check(monkeypatch, port, timeout=0.5, kind="grpc"):
    monkeypatch.setitem(core.SERVICE_PROBES, "shippingservice", (kind, port))
    return core.check_service("shippingservice", host="127.0.0.1", timeout=timeout)


def test_grpc_health_serving_is_up(monkeypatch, grpc_health_server):
    result = _check(monkeypatch, grpc_health_server(b"\x08\x01"))
    assert result["ok"] and result["detail"] == "serving"


def test_grpc_health_not_serving_is_down(monkeypatch, grpc_health_server):
    result = _check(monkeypatch, grpc_health_server(b"\x08\x02"))
    assert not result["ok"] and result["detail"] == "not serving"


def test_a_hung_service_times_out_and_is_down(monkeypatch, grpc_health_server):
    result = _check(monkeypatch, grpc_health_server(delay=2.0), timeout=0.3)
    assert not result["ok"] and "deadline exceeded" in result["detail"]


def test_a_service_without_the_health_api_still_counts_as_responding(monkeypatch, grpc_health_server):
    result = _check(monkeypatch, grpc_health_server(implement=False))
    assert result["ok"] and "no health API" in result["detail"]


def test_a_stopped_service_is_down(monkeypatch):
    result = _check(monkeypatch, _free_port())
    assert not result["ok"]


def test_redis_ping(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        with conn:
            if conn.recv(64).startswith(b"PING"):
                conn.sendall(b"+PONG\r\n")

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setitem(core.SERVICE_PROBES, "redis-cart", ("redis", port))
    try:
        result = core.check_service("redis-cart", host="127.0.0.1", timeout=1.0)
    finally:
        listener.close()
    assert result["ok"] and result["detail"] == "PONG"


def test_the_sampler_drops_stale_results():
    sampler = core.ServiceProbeSampler(interval=1.0)
    sampler._data, sampler._updated = {"x": {"ok": True}}, time.time() - 60
    assert sampler.latest() == {}
    sampler._updated = time.time()
    assert sampler.latest() == {"x": {"ok": True}}


# ----------------------------------------------------------- checkout probe
def _response(status, text=""):
    r = MagicMock()
    r.status_code, r.text = status, text
    return r


class _FakeSession:
    """Scripted frontend: add-to-cart answers 302, place-order answers whatever `orders` says next."""

    def __init__(self, orders, posted):
        self.orders, self.posted = orders, posted

    def get(self, url, timeout=None):
        return _response(200)

    def post(self, url, data=None, timeout=None, allow_redirects=True):
        if url.endswith("/cart"):
            return _response(302)
        self.posted.append(data)
        return self.orders.pop(0)


def _journey(monkeypatch, orders, every=1, retry_every=4):
    posted = []
    monkeypatch.setattr(core.requests, "Session", lambda: _FakeSession(orders, posted))
    return core.CheckoutJourney("http://boutique.test", every=every, retry_every=retry_every), posted


def test_the_checkout_probe_keeps_retrying_instead_of_switching_off(monkeypatch):
    bad = "<html><pre>Key: 'PlaceOrderPayload.CcNumber' failed on the 'credit_card' tag</pre></html>"
    orders = [_response(422, bad) for _ in range(5)] + [_response(200)]
    journey, posted = _journey(monkeypatch, orders)

    for _ in range(3):
        assert journey.tick() is None  # untrusted until it has worked once: no false alarm
    assert journey.enabled and journey.status == "unverified"
    assert "credit_card" in journey.last_error and "HTTP 422" in journey.last_error
    assert len(posted) == 3

    journey.tick()  # tick 4 is a retry (4 % retry_every == 0)
    assert len(posted) == 4
    for _ in range(3):  # ticks 5-7 back off
        journey.tick()
    assert len(posted) == 4
    journey.tick()  # tick 8: retry, still 422
    journey.tick(), journey.tick(), journey.tick()
    row = journey.tick()  # tick 12: retry succeeds
    assert row is not None and row["success"]
    assert journey.status == "verified" and journey.last_error is None


def test_once_verified_a_checkout_failure_is_reported(monkeypatch):
    journey, _ = _journey(monkeypatch, [_response(200), _response(500, "<pre>payment down</pre>")])
    assert journey.tick()["success"]
    row = journey.tick()
    assert not row["success"] and row["error_type"] == "http_5xx" and "payment down" in row["error"]


def test_the_card_number_is_digits_only_and_passes_luhn(monkeypatch):
    journey, posted = _journey(monkeypatch, [_response(200)])
    journey.tick()
    number = posted[0]["credit_card_number"]
    assert number.isdigit()
    digits = [int(d) for d in reversed(number)]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    assert total % 10 == 0


def test_journey_every_zero_switches_it_off():
    journey = core.CheckoutJourney("http://boutique.test", every=0)
    assert journey.tick() is None and journey.status == "off"


# ----------------------------------------------------------- background sampling
def test_page_probes_run_at_the_same_time(monkeypatch):
    def slow_get(url, timeout=None):
        time.sleep(0.4)
        return _response(200)

    monkeypatch.setattr(core.requests, "get", slow_get)
    started = time.perf_counter()
    rows = core.probe_endpoints("http://boutique.test")
    assert time.perf_counter() - started < 0.9  # three pages, not 3 x 0.4 s
    assert [r["endpoint"] for r in rows] == list(core.ENDPOINT_PATHS)


def test_a_hanging_checkout_never_delays_a_sample(monkeypatch):
    monkeypatch.setattr(core, "probe_endpoints", lambda _base, simulate_failure=False: _ok_probe_rows())  # noqa: ARG005
    monkeypatch.setattr(core, "get_snapshot", lambda *_a, **_k: _snap())
    monkeypatch.setattr(core, "SERVICE_PROBES_ON", False)

    def hung_checkout(self):
        time.sleep(5)
        return {}

    monkeypatch.setattr(core.CheckoutJourney, "_run_once", hung_checkout)
    live = core.LiveSampler("http://boutique.test", backend="kubernetes", interval=0.2).start()
    try:
        assert live.wait_for_first(timeout=2)
        time.sleep(0.7)
        bundle = live.latest()
        assert bundle["age_s"] < 0.6 and len(live.history()) >= 3
    finally:
        live.stop()


def test_the_shared_sampler_is_reused_and_replaced_when_settings_change(monkeypatch):
    monkeypatch.setattr(core, "SERVICE_PROBES_ON", False)
    monkeypatch.setattr(core, "JOURNEY_EVERY", 0)
    stopped = []
    monkeypatch.setattr(core.LiveSampler, "start", lambda self: self)
    monkeypatch.setattr(core.LiveSampler, "stop", lambda self: stopped.append(self.key))
    monkeypatch.setattr(core, "_SHARED", None)
    a = core.shared_live_sampler("http://a", "kubernetes")
    assert core.shared_live_sampler("http://a", "kubernetes") is a
    b = core.shared_live_sampler("http://b", "kubernetes")
    assert b is not a and stopped == [("http://a", "kubernetes", "default", "")]

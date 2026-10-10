"""Rules for the dashboard's per-service latency graphs."""
from __future__ import annotations

import monitor.service_graphs as graphs

KNOWN = ["frontend", "cartservice", "redis-cart", "shippingservice", "emailservice"]


def _step(state, attacked=(), selection=(), now=0.0):
    return graphs.update(state, set(attacked), set(selection), now)


def test_clicking_a_row_opens_a_graph_that_stays_open():
    state = _step(graphs.new_state(), selection={"emailservice"})
    assert set(state["open"]) == {"emailservice"}
    _step(state, now=1000)  # row deselected, long after: a manual graph stays
    assert "emailservice" in state["open"]


def test_an_attacked_service_opens_its_graph_and_closes_10_s_after_recovery():
    state = _step(graphs.new_state(), attacked={"shippingservice"}, now=0)
    assert state["open"]["shippingservice"]["source"] == "auto"
    _step(state, attacked={"shippingservice"}, now=30)  # still down: stays open
    _step(state, now=32)  # recovered at ~30
    _step(state, now=39.9)
    assert "shippingservice" in state["open"]
    _step(state, now=40.1)
    assert "shippingservice" not in state["open"]


def test_a_second_attack_before_auto_close_restarts_the_countdown():
    state = _step(graphs.new_state(), attacked={"redis-cart"}, now=0)
    _step(state, now=5)
    _step(state, attacked={"redis-cart"}, now=8)
    _step(state, now=15)
    assert "redis-cart" in state["open"]
    _step(state, now=18.5)
    assert "redis-cart" not in state["open"]


def test_a_graph_opened_by_click_does_not_auto_close_after_an_attack():
    state = _step(graphs.new_state(), selection={"cartservice"}, now=0)
    _step(state, attacked={"cartservice"}, selection={"cartservice"}, now=5)
    _step(state, selection={"cartservice"}, now=100)
    assert "cartservice" in state["open"]


def test_closing_during_an_attack_keeps_it_closed_until_recovery_then_it_can_reopen():
    state = _step(graphs.new_state(), attacked={"emailservice"}, now=0)
    graphs.close(state, "emailservice", {"emailservice"})
    _step(state, attacked={"emailservice"}, now=2)
    assert "emailservice" not in state["open"]
    _step(state, now=4)  # recovered: dismissal cleared
    _step(state, attacked={"emailservice"}, now=6)  # attacked again: opens again
    assert "emailservice" in state["open"]


def test_a_row_that_stays_selected_does_not_reopen_a_closed_graph():
    state = _step(graphs.new_state(), selection={"frontend"})
    graphs.close(state, "frontend", set())
    _step(state, selection={"frontend"}, now=2)
    assert "frontend" not in state["open"]
    _step(state, selection=set(), now=4)
    _step(state, selection={"frontend"}, now=6)  # clicked again
    assert "frontend" in state["open"]


def test_attacked_graphs_are_listed_first():
    state = _step(graphs.new_state(), selection={"cartservice", "frontend"})
    _step(state, attacked={"emailservice"}, selection={"cartservice", "frontend"})
    assert graphs.ordered(state, {"emailservice"}, KNOWN) == ["emailservice", "frontend", "cartservice"]


def test_the_experiment_target_counts_as_attacked_even_before_it_looks_down():
    sample = {"summary": {"services_down": []},
              "services": [{"service": "redis-cart", "container": "online-boutique-redis-cart-1"}]}
    experiment = {"container": "online-boutique-redis-cart-1", "action": "pause"}
    assert graphs.attacked_services(sample, experiment, KNOWN) == {"redis-cart"}


def test_the_experiment_target_is_found_by_name_without_container_data():
    sample = {"summary": {"services_down": ["emailservice"]}, "services": []}
    experiment = {"container": "online-boutique-redis-cart-1"}
    assert graphs.attacked_services(sample, experiment, KNOWN) == {"emailservice", "redis-cart"}


def test_latency_rows_keep_down_points():
    history = [
        {"timestamp": "2026-10-10T22:00:00+05:30",
         "services": [{"service": "emailservice", "health_latency_ms": 3.2, "down": False, "reason": ""}]},
        {"timestamp": "2026-10-10T22:00:02+05:30",
         "services": [{"service": "emailservice", "health_latency_ms": 1000.4, "down": True,
                       "reason": "health check: timed out (hung or paused)"}]},
        {"timestamp": "2026-10-10T22:00:04+05:30", "services": []},
    ]
    rows = graphs.latency_rows(history, "emailservice")
    assert [r["status"] for r in rows] == ["Up", "Down"]
    assert rows[1]["latency_ms"] == 1000.4 and "timed out" in rows[1]["detail"]

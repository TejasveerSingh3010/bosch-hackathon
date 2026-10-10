"""The watchdog is the automatic stop the brief asks for, so each abort rule is tested alone."""
from __future__ import annotations

from safety.limits import Thresholds
from safety.watchdog import Watchdog
from tests.conftest import PROJECT, make_sample

LIMITS = Thresholds(error_rate_pct=20.0, p95_ms=2000.0, max_collateral_unready=1)
TARGET = f"{PROJECT}-redis-cart-1"


def watchdog(**kwargs) -> Watchdog:
    kwargs.setdefault("breach_streak", 2)
    return Watchdog(LIMITS, target_container=TARGET, **kwargs)


def test_a_healthy_sample_does_not_abort():
    assert watchdog().check(make_sample()).abort is False


def test_one_breach_is_not_enough_but_two_in_a_row_are():
    """A single bad probe can be noise; two consecutive ones are a signal."""
    dog = watchdog()
    first = dog.check(make_sample(error_rate_pct=100.0, failed_endpoints=["Home", "Cart", "Product"]))
    assert first.abort is False and "1/2" in first.reason
    second = dog.check(make_sample(error_rate_pct=100.0, failed_endpoints=["Home", "Cart", "Product"]))
    assert second.abort is True and "error rate" in second.reason


def test_a_recovery_between_breaches_resets_the_streak():
    dog = watchdog()
    dog.check(make_sample(error_rate_pct=100.0, failed_endpoints=["Home"]))
    dog.check(make_sample())  # recovered
    assert dog.check(make_sample(error_rate_pct=100.0, failed_endpoints=["Home"])).abort is False


def test_latency_breach_aborts():
    dog = watchdog()
    dog.check(make_sample(p95_ms=5000))
    decision = dog.check(make_sample(p95_ms=5000))
    assert decision.abort and "p95 latency" in decision.reason


def test_blast_radius_abort_ignores_the_target_itself():
    """The target being unready is the experiment working, not collateral damage."""
    dog = watchdog()
    for _ in range(3):
        decision = dog.check(make_sample(not_ready=[TARGET], ready=11))
    assert decision.abort is False


def test_too_many_other_containers_unready_aborts():
    dog = watchdog()
    collateral = [TARGET, f"{PROJECT}-cartservice-1", f"{PROJECT}-checkoutservice-1"]
    dog.check(make_sample(not_ready=collateral, ready=9))
    decision = dog.check(make_sample(not_ready=collateral, ready=9))
    assert decision.abort and "blast radius" in decision.reason


def test_stale_telemetry_aborts_immediately_without_waiting_for_a_streak():
    """Losing monitoring is itself the emergency: stop at once rather than run blind."""
    decision = watchdog().check(make_sample(), age_s=120.0)
    assert decision.abort and "stale" in decision.reason


def test_a_simulated_sample_can_never_abort_a_real_experiment():
    """The dashboard's alerting self-test marks samples simulated; it must not stop a run."""
    dog = watchdog()
    for _ in range(5):
        decision = dog.check(
            make_sample(error_rate_pct=100.0, failed_endpoints=["Home", "Cart"], simulated=True)
        )
    assert decision.abort is False and decision.ignored is True
    assert dog.samples_seen == 0


def test_detection_time_is_recorded_from_the_first_degraded_sample():
    dog = watchdog()
    dog.check(make_sample())
    assert dog.first_degraded_at is None
    dog.check(make_sample(error_rate_pct=33.3, failed_endpoints=["Cart"]))
    assert dog.first_degraded_at is not None


def test_a_degraded_but_within_budget_sample_is_reported_without_aborting():
    """15% errors is degradation worth measuring, but under the 20% abort limit."""
    decision = watchdog().check(make_sample(error_rate_pct=15.0, failed_endpoints=["Cart"]))
    assert decision.abort is False and decision.degraded is True

"""The dashboard's resilience score: each factor's rule, and how the weighted total treats missing data."""
from __future__ import annotations

import json

import pytest

import monitor.resilience as res

T0 = "2026-10-10T22:00:00+05:30"


def _row(seconds, error=0.0, p95=50.0, not_ready=False, simulated=False):
    return {"ts": res._time(T0) + seconds, "error_rate_pct": error, "p95_ms": p95,
            "not_ready": not_ready, "simulated": simulated}


def _result(id="kill-cart", service="cartservice", action="kill", injected=T0, error_mean=0.0, p95_max=100.0,
            recovered=True, recovery_s=2.0, verdict="completed", rolled_back="2026-10-10T22:00:30+05:30"):
    return {
        "experiment": {"id": id, "target": service, "action": action},
        "verdict": verdict, "service": service, "injected_at": injected,
        "rolled_back_at": rolled_back, "recovered_at": rolled_back if recovered else None,
        "recovery_seconds": recovery_s if recovered else None, "detection_seconds": None,
        "during": {"samples": 5, "error_rate_pct_mean": error_mean, "p95_ms_max": p95_max},
        "thresholds_used": {"p95_ms": 2000.0},
    }


def _loaded(*results):
    return [{**r, "_id": r["experiment"]["id"]} for r in results]


def test_linear_clamps_and_runs_either_way():
    assert res.linear(99.9, 95, 99.9) == 100 and res.linear(90, 95, 99.9) == 0
    assert res.linear(65, 120, 10) == 50.0  # lower is better
    assert res.linear(500, 3000, 1000) == 100


def test_availability_ignores_simulated_samples():
    history = [_row(i) for i in range(98)] + [_row(98, error=100.0, simulated=True)]
    assert res.availability(history)["score"] == 100
    history += [_row(99, error=100.0), _row(100, error=100.0)]  # two real outage samples in 100
    assert res.availability(history)["score"] == pytest.approx(res.linear(98.0, 95, 99.9))


def test_fault_tolerance_names_the_worst_fault_and_counts_redundancy():
    exps = _loaded(_result(error_mean=100.0), _result(id="t", service="currencyservice", action="throttle"))
    single = [{"app": "cartservice"}, {"app": "frontend"}, {"app": "loadgenerator"}]
    f = res.fault_tolerance(exps, single)
    assert f["score"] == 40.0  # 50% kept * 0.8 + 0% redundancy * 0.2
    assert "worst: kill cartservice" in f["evidence"] and "0/2 services" in f["evidence"]
    assert res.fault_tolerance(exps, single + [{"app": "cartservice"}])["score"] == 50.0


def test_recovery_is_scaled_by_the_share_that_recovered():
    assert res.recovery(_loaded(_result(recovery_s=5.0)))["score"] == 100
    both = _loaded(_result(recovery_s=5.0), _result(id="x", service="adservice", recovered=False))
    assert res.recovery(both)["score"] == 50.0


def test_failure_handling_penalises_requests_that_hang_to_the_probe_timeout():
    f = res.failure_handling(_loaded(_result(p95_max=3017.0), _result(id="ok", p95_max=400.0)), 1000, 3000)
    assert f["score"] == 50.0
    assert "hung to the 3000 ms probe timeout in kill cartservice" in f["evidence"]


def test_detection_is_the_first_degraded_sample_inside_the_fault_window():
    exp = _result()
    history = [_row(-5, error=100.0), _row(2), _row(4, p95=2500.0), _row(6, error=50.0),
               _row(40, error=100.0)]
    assert res.detection_seconds(exp, history, 2000) == 4.0
    assert res.detection_seconds(exp, [_row(2), _row(40, error=100.0)], 2000) is None  # never showed
    assert res.detection_seconds({**exp, "detection_seconds": 1.5}, [], 2000) == 1.5


def test_observability_coverage_counts_configured_checks_not_ones_that_already_answered():
    services = [{"service": "a", "health_check": None}, {"service": "b", "health_check": None},
                {"service": "loadgenerator", "infrastructure": True}]
    f = res.observability([], [], services, {"a"}, 2000)  # no detection data: coverage alone
    assert f["score"] == 50.0 and "1/2 services" in f["evidence"]


def test_missing_factors_share_their_weight_and_no_data_gives_no_score():
    empty = res.compute([], [], [], [], set(), 1000, 3000, 2000)
    assert empty["score"] is None and len(empty["missing"]) == 5

    only_availability = res.compute([_row(0), _row(2)], [], [], [], set(), 1000, 3000, 2000)
    assert only_availability["score"] == 100 and only_availability["band"] == "Resilient"
    assert only_availability["factors"]["availability"]["effective_weight"] == 1.0
    assert "fault_tolerance" in only_availability["missing"]


def test_total_is_the_weighted_sum_of_the_factor_scores():
    history = [_row(i * 2) for i in range(10)]
    exps = _loaded(_result(error_mean=100.0, p95_max=3000.0))
    out = res.compute(history, exps, [{"service": "a"}], [{"app": "a"}], {"a"}, 1000, 3000, 2000)
    expected = sum(f["score"] * res.WEIGHTS[k] for k, f in out["factors"].items())
    assert out["score"] == pytest.approx(expected, abs=0.1)
    assert sum(res.WEIGHTS.values()) == pytest.approx(1.0)


def test_loading_keeps_the_newest_counted_result_per_service_and_action(tmp_path):
    def write(name, **kw):
        (tmp_path / f"{name}.json").write_text(json.dumps(_result(id=name, **kw)), encoding="utf-8")

    write("old", error_mean=100.0, injected="2026-10-10T20:00:00+05:30")
    write("new", error_mean=0.0, injected="2026-10-10T21:00:00+05:30")
    write("dry", service="adservice", verdict="dry_run")
    (tmp_path / "telemetry.json").write_text("[]", encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert [e["_id"] for e in res.load_experiments(tmp_path)] == ["new"]


def test_history_reads_the_rotated_log_first_and_skips_torn_lines(tmp_path):
    log = tmp_path / "telemetry_log.jsonl"
    line = {"timestamp": T0, "summary": {"error_rate_pct": 0, "p95_latency_ms": 10, "not_ready": []}}
    (tmp_path / "telemetry_log.jsonl.1").write_text(json.dumps(line) + "\n", encoding="utf-8")
    later = {**line, "timestamp": "2026-10-10T22:00:02+05:30", "simulated": True}
    log.write_text(json.dumps(later) + "\n{\"timest", encoding="utf-8")
    rows = res.load_history(log)
    assert [r["simulated"] for r in rows] == [False, True]

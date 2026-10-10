"""Compute the steady-state baseline from monitor history and write results/baseline.json.

    python monitor/baseline.py                 # last 10 minutes of history
    python monitor/baseline.py --minutes 5
    python monitor/baseline.py --out results/baseline.json

Reads results/telemetry_log.jsonl (preferred, long history) or results/telemetry.json (last ~2 min).
Simulated samples are ignored. Pure standard library, so it runs anywhere Python does.

Capture the baseline while the app is healthy and the load generator is running, and before
injecting any failure. The experiment runner compares live telemetry against this file.
"""
import argparse
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results"


def pct(sorted_vals, q):
    """Nearest-rank percentile of an ascending list."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(q / 100 * len(sorted_vals) + 0.999999) - 1))
    return sorted_vals[k]


def latency_stats(values):
    v = sorted(values)
    if not v:
        return None
    mean = statistics.fmean(v)
    std = statistics.pstdev(v) if len(v) > 1 else 0.0
    return {"n": len(v), "mean_ms": round(mean, 2), "std_ms": round(std, 2), "p50_ms": round(pct(v, 50), 2),
            "p95_ms": round(pct(v, 95), 2), "p99_ms": round(pct(v, 99), 2), "max_ms": round(v[-1], 2),
            # statistical-process-control upper limit: latency above this is unusual for this app
            "ucl_ms": round(mean + 3 * std, 2)}


def load_samples(results_dir):
    src = []
    log = results_dir / "telemetry_log.jsonl"
    for path in (log.with_name(log.name + ".1"), log):
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    src.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # a partially written last line
    if src:
        return src, "telemetry_log.jsonl"
    snap = results_dir / "telemetry.json"
    if snap.exists():
        return json.loads(snap.read_text(encoding="utf-8")), "telemetry.json"
    return [], None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-dir", type=Path, default=RESULTS)
    ap.add_argument("--minutes", type=float, default=10.0, help="use only the most recent N minutes")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    samples, source = load_samples(args.results_dir)
    samples = [s for s in samples if not s.get("simulated")]
    if not samples:
        raise SystemExit(f"No usable samples in {args.results_dir}. Is the monitor running?")
    times = [datetime.fromisoformat(s["timestamp"]) for s in samples]
    cutoff = max(times) - timedelta(minutes=args.minutes)
    samples = [s for s, t in zip(samples, times) if t >= cutoff]
    times = [t for t in times if t >= cutoff]
    window_s = (times[-1] - times[0]).total_seconds()

    per_ep = {}
    for s in samples:
        for ep, m in s["metrics"].items():
            if m.get("age_s") not in (None,) and m["age_s"] >= 1:
                continue  # carried-over journey result, already counted when it was fresh
            per_ep.setdefault(ep, []).append(m)
    endpoints = {}
    for ep, ms in per_ep.items():
        stats = latency_stats([m["latency"] for m in ms if m["success"]])  # latency of successful requests
        endpoints[ep] = {**(stats or {"n": 0}), "requests": len(ms),
                         "error_rate_pct": round(100 * sum(not m["success"] for m in ms) / len(ms), 2)}

    sample_p95 = sorted(s["summary"]["p95_latency_ms"] for s in samples)
    sample_err = [s["summary"]["error_rate_pct"] for s in samples]
    all_ready = sum(1 for s in samples if s["summary"]["ready_pods"] == s["summary"]["total_pods"]
                    and s["summary"]["ready_pods"] is not None)

    containers = {}
    for s in samples:
        for w in s.get("workloads", []):
            if w.get("cpu_pct") is not None:
                c = containers.setdefault(w["name"], {"cpu": [], "mem": []})
                c["cpu"].append(w["cpu_pct"])
                if w.get("mem_mb") is not None:
                    c["mem"].append(w["mem_mb"])
    resources = {n: {"cpu_pct_avg": round(statistics.fmean(c["cpu"]), 2), "cpu_pct_max": max(c["cpu"]),
                     "mem_mb_avg": round(statistics.fmean(c["mem"]), 1) if c["mem"] else None,
                     "mem_mb_max": max(c["mem"]) if c["mem"] else None}
                 for n, c in sorted(containers.items())}

    restarts = [s["summary"]["restart_count"] for s in samples if s["summary"]["restart_count"] is not None]
    warnings = []
    if window_s < 120:
        warnings.append(f"window is only {window_s:.0f}s; capture at least 2-5 minutes for a stable baseline")
    if max(sample_err) > 0:
        warnings.append("some probes failed during the window; the baseline is not clean")
    if all_ready != len(samples):
        warnings.append("not every container was ready for the whole window")
    if restarts and restarts[-1] != restarts[0]:
        warnings.append("containers restarted during the window")
    if not resources:
        warnings.append("no CPU/memory data in this history (monitor was started before resource sampling existed)")
    if "Checkout" not in endpoints:
        warnings.append("no checkout probe data; checkout-path services (payment, shipping, email) are not covered")

    baseline = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "source": source,
        "window": {"start": times[0].isoformat(), "end": times[-1].isoformat(),
                   "seconds": round(window_s), "samples": len(samples)},
        "overall": {
            "error_rate_pct": round(statistics.fmean(sample_err), 3),
            # summary.p95_latency_ms is what live samples report, so aborts should compare against this
            "sample_p95_ms": {"mean": round(statistics.fmean(sample_p95), 2), "p95": round(pct(sample_p95, 95), 2),
                              "p99": round(pct(sample_p95, 99), 2), "max": round(sample_p95[-1], 2)},
        },
        "endpoints": endpoints,
        "workloads": {
            "containers": len({w["name"] for s in samples for w in s.get("workloads", [])}),
            "all_ready_ratio": round(all_ready / len(samples), 3),
            "restarts_in_window": (restarts[-1] - restarts[0]) if restarts else None,
            "resources": resources,
        },
        "quality_warnings": warnings,
    }
    out = args.out or (args.results_dir / "baseline.json")
    out.write_text(json.dumps(baseline, indent=2), encoding="utf-8")

    print(f"baseline from {len(samples)} samples / {window_s:.0f}s ({source}) -> {out}")
    print(f"  error rate {baseline['overall']['error_rate_pct']}% | sample p95 mean {baseline['overall']['sample_p95_ms']['mean']} ms, "
          f"max {baseline['overall']['sample_p95_ms']['max']} ms")
    for ep, e in endpoints.items():
        if e.get("n"):
            print(f"  {ep:<9} mean {e['mean_ms']} ms  p95 {e['p95_ms']} ms  ucl {e['ucl_ms']} ms  errors {e['error_rate_pct']}%")
    for w in warnings:
        print(f"  warning: {w}")


if __name__ == "__main__":
    main()

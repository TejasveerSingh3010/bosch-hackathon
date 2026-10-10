"""Headless monitor: probes Online Boutique and keeps telemetry.json / SLA_BREACH.flag fresh
without needing a browser tab open. This is the file your monitoring agent should rely on.

    python headless_monitor.py                       # docker backend, every 2 s
    python headless_monitor.py --backend kubernetes --namespace default
    python headless_monitor.py --once                # single sample (smoke test)

Outputs (in <repo>/results by default):
    telemetry.json        latest 60 samples (about 2 minutes)
    telemetry_log.jsonl   append-only history, one compact sample per line (rotates at 60 MB)
    SLA_BREACH.flag       exists while the latest sample shows a breach

Run only ONE writer at a time: if this is running, start the dashboard with
BOUTIQUE_DASHBOARD_WRITES=0 (or untick the sidebar box) so the two don't interleave samples.
"""
import argparse
import time
from datetime import datetime

import boutique_core as core


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=core.DEFAULT_BASE_URL)
    ap.add_argument("--backend", choices=["docker", "kubernetes"], default=core.DEFAULT_BACKEND)
    ap.add_argument("--namespace", default=core.DEFAULT_NAMESPACE)
    ap.add_argument("--project", default=core.DEFAULT_COMPOSE_PROJECT, help="Docker Compose project (blank = auto-detect)")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--sla-ms", type=float, default=core.LATENCY_SLA_MS)
    ap.add_argument("--journey-every", type=int, default=core.JOURNEY_EVERY,
                    help="run the synthetic checkout probe every N samples (0 = off)")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    url = args.url.rstrip("/")

    announced = {"journey": None}

    def report(bundle):
        sample, snap, err = bundle["sample"], bundle["snap"], bundle["write_error"]
        journey = live.journey
        note = None
        if journey.status == "off":
            note = f"checkout probe off - {journey.disabled_reason}"
        elif journey.status == "unverified" and journey.last_error:
            note = (f"checkout probe not working yet (retrying every "
                    f"{core.JOURNEY_RETRY_EVERY} samples) - {journey.last_error}")
        elif journey.status == "verified":
            note = "checkout probe verified"
        if note and note != announced["journey"] and (announced["journey"] or journey.status != "verified"):
            print(f"note: {note}")
        if note:
            announced["journey"] = note

        s = sample["summary"]
        ready = f"{s['ready_pods']}/{s['total_pods']}" if snap["available"] else "n/a"
        state = "BREACH" if core.is_breach(sample, args.sla_ms) else "ok"
        line = (f"{datetime.now():%H:%M:%S} {state:<6} err={s['error_rate_pct']}% "
                f"p95={s['p95_latency_ms']}ms ready={ready}")
        if "Checkout" in sample["metrics"]:
            c = sample["metrics"]["Checkout"]
            line += f" checkout={'ok' if c['success'] else 'FAIL'}({c['latency']:.0f}ms)"
        if "max_cpu_pct" in s:
            line += f" cpu_max={s['max_cpu_pct']}%"
        if s["error_types"]:
            line += f" errors={s['error_types']}"
        if s.get("services_down"):
            line += f" DOWN={','.join(s['services_down'])}"
        if s["not_ready"]:
            line += f" not_ready={','.join(s['not_ready'])}"
        if snap["error"]:
            line += f" [{args.backend}: {snap['error'][:80]}]"
        if err:
            line += f" [WRITE FAILED: {err}]"
        print(line, flush=True)

    # Sampling, the checkout probe and the service health checks each run on their own thread,
    # so a hung dependency can never stall telemetry (the safety watchdog aborts on stale data).
    live = core.LiveSampler(url, args.backend, args.namespace, args.project, interval=args.interval,
                            journey_every=args.journey_every, write=True, sla_ms=args.sla_ms,
                            on_sample=report)
    print(f"monitoring {url} | backend={args.backend} | writing {core.TELEMETRY_PATH.resolve()}")
    try:
        if args.once:
            live.start_helpers()
            if core.SERVICE_PROBES_ON:
                time.sleep(core.SERVICE_PROBE_TIMEOUT_S + 0.5)  # let the first health checks finish
            live.collect_once(sync_journey=True)
            return
        live.start()
        while True:
            time.sleep(1)
            if live.last_error:
                print(f"sampler error: {live.last_error}", flush=True)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        live.stop()

if __name__ == "__main__":
    main()

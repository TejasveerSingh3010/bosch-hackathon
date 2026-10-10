# Fix recommendations

*Source: deterministic · 2 recommendation(s). Recommendations only - nothing here has been applied.*

## 1. A pause on cartservice caused Cart, Home, Product to fail

- **Root cause:** single_point_of_failure
- **Fix (upstream_code):** Give callers a short client timeout and a retry/circuit-breaker so a hung dependency fails fast instead of tying up connections (timeouts live in the caller's code, so this needs an upstream change; a Compose `healthcheck` with `depends_on: condition: service_healthy` mitigates it meanwhile).
- **Expected effect:** removes or reduces the failure a pause on cartservice caused (watch p95_latency_ms, error_rate_pct)
- **Effort:** M · **Risk:** medium · **Confidence:** 0.75
- **Verify:** pause on cartservice - re-run this attack; the fix works if the previously-failed pages stay up, the peak error rate stays under the abort threshold, and recovery is no slower than before
- **Covers reports:** pause-cartservice
- **Evidence:** pause-cartservice, E8, E13, E16, E5

## 2. A stop on redis-cart caused a aborted verdict

- **Root cause:** single_point_of_failure
- **Fix (compose):** Evidence is thin. Before changing anything, re-run a stop on redis-cart with logging on to confirm the failure mode. Candidate fix: Run a second replica of this service (compose `deploy.replicas: 2` behind the frontend's client-side load balancing), or add a graceful fallback so its outage does not fail the page.
- **Expected effect:** removes or reduces the failure a stop on redis-cart caused (watch error_rate_pct on the affected pages)
- **Effort:** L · **Risk:** medium · **Confidence:** 0.35
- **Verify:** stop on redis-cart - re-run this attack; the fix works if the previously-failed pages stay up, the peak error rate stays under the abort threshold, and recovery is no slower than before
- **Covers reports:** stop-redis-cart
- **Evidence:** stop-redis-cart, E2

## What held up (do not touch)

- these pages kept working throughout: Cart, Checkout, Home, Product [cpu-throttle-cartservice, E3]
- the application tolerated the fault: no page probe failed while it was in place [cpu-throttle-cartservice, E3]
- these pages kept working throughout: Checkout [pause-cartservice, E5]

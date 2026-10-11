# Failure report: pause on currencyservice (pause-currencyservice-123230)

**Severity: MINOR** | experiment | generated 2026-10-11 12:33:08 | narrator: llm:openai/gpt-oss-20b

> Currencyservice pause caused transient page failures but recovered quickly.

## Where the failure was

Injected pause on currencyservice container at 2026-10-11T12:32:37.525773+05:30.

## Summary

A deliberate pause of the currencyservice container triggered timeouts for Home, Cart, and Product pages. The experiment aborted after exceeding error thresholds, unpaused the service, and pages returned to normal within 10 seconds.

## Failure origin

- **Component:** `currencyservice`
- **First degraded:** 12:32:37 (observed)
- **Evidence:** E1, E2, E7
- **Note:** injected target

## How it spread

| Step | Component | Time | +s | What happened | Evidence |
|---|---|---|---|---|---|
| 1 | currencyservice (origin) | 12:32:37 | - |  | E1, E2, E7 |
| 2 | Home (page) | 12:32:41 | +4.8 |  | E4 |
| 3 | Cart (page) | 12:32:41 | +4.8 |  | E5 |
| 4 | Product (page) | 12:32:41 | +4.8 |  | E6 |
| 5 | Home (page) | 12:32:47 | +10.0 |  | E11 |
| 6 | Cart (page) | 12:32:47 | +10.0 |  | E12 |
| 7 | Product (page) | 12:32:47 | +10.0 |  | E13 |
| 8 | currencyservice (service) | 12:32:49 | +13.0 |  | E14 |

## Impact and timings

| Phase | Samples | Error rate mean / peak | p95 mean / peak (ms) | Failed pages |
|---|---|---|---|---|
| before | 30 | 0.0% / 0.0% | 166.65 / 223.32 | none |
| during | 2 | 75.0% / 75.0% | 3018.36 / 3020.88 | Cart, Home, Product |
| after | 3 | 0.0% / 0.0% | 1928.27 / 2387.98 | none |

- Detection: not measured
- Failure in place: 8.9 s
- Recovery: 3.5 s after rollback

## What worked

- The experiment aborted itself after exceeding the error‑rate threshold. _[observed; evidence E9]_
- The container was unpaused and the service returned to healthy status. _[observed; evidence E10, E14]_
- All affected pages recovered within 10 seconds after the pause. _[observed; evidence E11, E12, E13]_

## Anomalies and unknowns

- Detection time was not recorded, so the lag between failure onset and detection is unknown. _[unknown; evidence -]_

## Container log findings

| Container | Kind | Count | First seen | First line |
|---|---|---|---|---|
| online-boutique-frontend-1 | error | 13 | 12:32:41 | {"error":"could not retrieve currencies: rpc error: code = Canceled desc = context canceled","http.req.id":"e86dc361-7864-4c62-817b-0ddbc9e8e9df","http.req.method":"GET","http.req.path":"/product/0PUK6V6EV0","message"... |

## Evidence timeline

| ID | Time | Source | Event |
|---|---|---|---|
| E1 | 12:32:37 | docker | docker: pause on currencyservice |
| E2 | 12:32:37 | runner | runner injected pause on online-boutique-currencyservice-1 |
| E3 | 12:32:41 | logs | online-boutique-frontend-1 logged error x13: {"error":"could not retrieve currencies: rpc error: code = Canceled desc = context canceled","http.req.id":"e86dc361-7864-4c62-817b-0ddbc9e8e9df","http.req.method":"GET","http.req.path":"/product/0PUK6V6EV0","message"... |
| E4 | 12:32:41 | telemetry | Home page probe started failing (timeout, HTTP 0, 3016 ms) |
| E5 | 12:32:41 | telemetry | Cart page probe started failing (timeout, HTTP 0, 3015 ms) |
| E6 | 12:32:41 | telemetry | Product page probe started failing (timeout, HTTP 0, 3014 ms) |
| E7 | 12:32:41 | telemetry | currencyservice went down (container paused; health check: gRPC deadline exceeded) |
| E8 | 12:32:46 | docker | docker: unpause on currencyservice |
| E9 | 12:32:46 | runner | experiment aborted: error rate 75.0% exceeded the limit 20.0% for 2 consecutive samples |
| E10 | 12:32:46 | runner | runner rolled back (unpaused online-boutique-currencyservice-1) |
| E11 | 12:32:47 | telemetry | Home page probe is passing again |
| E12 | 12:32:47 | telemetry | Cart page probe is passing again |
| E13 | 12:32:47 | telemetry | Product page probe is passing again |
| E14 | 12:32:49 | telemetry | currencyservice is healthy again |
| E15 | 12:32:49 | runner | application healthy again (two clean samples in a row) |

---

_Evidence window 12:31:37 to 12:33:19; 41 telemetry samples. Narrator: llm:openai/gpt-oss-20b._

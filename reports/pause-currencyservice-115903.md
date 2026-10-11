# Failure report: pause on currencyservice (pause-currencyservice-115903)

**Severity: MAJOR** | experiment | generated 2026-10-11 11:59:38 | narrator: llm:openai/gpt-oss-20b

> Pause of currencyservice caused transient timeouts; pages recovered after rollback

## Where the failure was

The pause of the currencyservice container at 2026-10-11T11:59:11.623153+05:30 caused its health checks to fail (E7) and produced timeout errors in the frontend and product pages.

## Summary

The injected pause on the currencyservice container led to health‑check failures and timeout errors in all pages that depend on currency data. The service was unpaused and restored, and dependent pages recovered within a few seconds. The experiment was aborted automatically due to high error rate.

## Failure origin

- **Component:** `currencyservice`
- **First degraded:** 11:59:15 (observed)
- **Evidence:** E7
- **Note:** 

## How it spread

| Step | Component | Time | +s | What happened | Evidence |
|---|---|---|---|---|---|
| 1 | currencyservice (origin) | 11:59:15 | - |  | E7 |
| 2 | currencyservice (service) | 11:59:15 | +0.0 |  | E7 |
| 3 | Home (page) | 11:59:15 | +0.0 |  | E4 |
| 4 | Cart (page) | 11:59:15 | +0.0 |  | E5 |
| 5 | Product (page) | 11:59:15 | +0.0 |  | E6 |

## Impact and timings

| Phase | Samples | Error rate mean / peak | p95 mean / peak (ms) | Failed pages |
|---|---|---|---|---|
| before | 30 | 0.0% / 0.0% | 156.22 / 173.68 | none |
| during | 1 | 75.0% / 75.0% | 3009.1 / 3009.1 | Cart, Home, Product |
| after | 3 | 8.33% / 25.0% | 1115.71 / 3011.88 | Product |

- Detection: not measured
- Failure in place: 6.8 s
- Recovery: 3.1 s after rollback

## What worked

- Pages Home, Cart, Product recovered within 3.6 seconds after unpause _[observed; evidence E11, E12, E14]_
- Currencyservice became healthy again after unpause _[observed; evidence E13]_
- Experiment was aborted due to exceeding error rate threshold _[observed; evidence E9]_

## Anomalies and unknowns

- none

## Container log findings

| Container | Kind | Count | First seen | First line |
|---|---|---|---|---|
| online-boutique-frontend-1 | error | 13 | 11:59:14 | {"error":"could not retrieve currencies: rpc error: code = Canceled desc = context canceled","http.req.id":"732e4716-5ee7-46f2-a621-6418236a6010","http.req.method":"GET","http.req.path":"/product/0PUK6V6EV0","message"... |

## Evidence timeline

| ID | Time | Source | Event |
|---|---|---|---|
| E1 | 11:59:11 | docker | docker: pause on currencyservice |
| E2 | 11:59:11 | runner | runner injected pause on online-boutique-currencyservice-1 |
| E3 | 11:59:14 | logs | online-boutique-frontend-1 logged error x13: {"error":"could not retrieve currencies: rpc error: code = Canceled desc = context canceled","http.req.id":"732e4716-5ee7-46f2-a621-6418236a6010","http.req.method":"GET","http.req.path":"/product/0PUK6V6EV0","message"... |
| E4 | 11:59:15 | telemetry | Home page probe started failing (timeout, HTTP 0, 3009 ms) |
| E5 | 11:59:15 | telemetry | Cart page probe started failing (timeout, HTTP 0, 3009 ms) |
| E6 | 11:59:15 | telemetry | Product page probe started failing (timeout, HTTP 0, 3007 ms) |
| E7 | 11:59:15 | telemetry | currencyservice went down (container paused; health check: gRPC deadline exceeded) |
| E8 | 11:59:18 | docker | docker: unpause on currencyservice |
| E9 | 11:59:18 | runner | experiment aborted: error rate 75.0% exceeded the limit 20.0% for 2 consecutive samples |
| E10 | 11:59:18 | runner | runner rolled back (unpaused online-boutique-currencyservice-1) |
| E11 | 11:59:18 | telemetry | Home page probe is passing again |
| E12 | 11:59:18 | telemetry | Cart page probe is passing again |
| E13 | 11:59:18 | telemetry | currencyservice is healthy again |
| E14 | 11:59:19 | telemetry | Product page probe is passing again |
| E15 | 11:59:21 | runner | application healthy again (two clean samples in a row) |

---

_Evidence window 11:58:11 to 11:59:51; 39 telemetry samples. Narrator: llm:openai/gpt-oss-20b._

# Failure report: network_partition on shippingservice (network_partition-shippingservice-114018)

**Severity: MAJOR** | experiment | generated 2026-10-11 11:41:01 | narrator: llm:openai/gpt-oss-20b

> Network partition on shippingservice caused temporary Cart page failures

## Where the failure was

shippingservice became unreachable due to network partition, causing health check failure and downstream Cart page timeouts

## Summary

A network partition was injected on shippingservice at 11:40:27, leading to gRPC health check failures and Cart page timeouts. The experiment was aborted after 7.3s when the error rate exceeded 20%, the partition was rolled back, and shippingservice returned to healthy status 9.2s later. All other pages remained available.

## Failure origin

- **Component:** `shippingservice`
- **First degraded:** 11:40:30 (observed)
- **Evidence:** E4, E1
- **Note:** 

## How it spread

| Step | Component | Time | +s | What happened | Evidence |
|---|---|---|---|---|---|
| 1 | shippingservice (service) | 11:40:30 | +0.0 |  | E4 |
| 2 | Cart (page) | 11:40:30 | +0.0 |  | E3 |

## Impact and timings

| Phase | Samples | Error rate mean / peak | p95 mean / peak (ms) | Failed pages |
|---|---|---|---|---|
| before | 30 | 0.0% / 0.0% | 151.86 / 164.43 | none |
| during | 2 | 25.0% / 25.0% | 3016.79 / 3016.97 | Cart |
| after | 4 | 6.25% / 25.0% | 2525.25 / 3018.39 | Cart |

- Detection: not measured
- Failure in place: 7.3 s
- Recovery: 9.2 s after rollback

## What worked

- Shippingservice became healthy again after rollback _[observed; evidence E6, E7]_
- Cart page probe passed again after shippingservice recovery _[observed; evidence E8]_
- Experiment was aborted once error rate exceeded threshold, preventing further degradation _[observed; evidence E5]_

## Anomalies and unknowns

- Detection of failure was delayed; error rate exceeded threshold after 7.3s _[inferred; evidence E5]_
- Recovery took 9.2s, longer than ideal _[inferred; evidence E6, E7]_
- No automatic self-healing; rollback required _[inferred; evidence E6]_

## Container log findings

| Container | Kind | Count | First seen | First line |
|---|---|---|---|---|
| online-boutique-frontend-1 | error | 6 | 11:40:30 | {"error":"failed to get shipping quote: rpc error: code = Canceled desc = context canceled","http.req.id":"22af1538-5cb7-4a53-b792-565c5843dd8a","http.req.method":"GET","http.req.path":"/cart","message":"request error... |

## Evidence timeline

| ID | Time | Source | Event |
|---|---|---|---|
| E1 | 11:40:27 | runner | runner injected network_partition on online-boutique-shippingservice-1 |
| E2 | 11:40:30 | logs | online-boutique-frontend-1 logged error x6: {"error":"failed to get shipping quote: rpc error: code = Canceled desc = context canceled","http.req.id":"22af1538-5cb7-4a53-b792-565c5843dd8a","http.req.method":"GET","http.req.path":"/cart","message":"request error... |
| E3 | 11:40:30 | telemetry | Cart page probe started failing (timeout, HTTP 0, 3017 ms) |
| E4 | 11:40:30 | telemetry | shippingservice went down (health check: gRPC deadline exceeded) |
| E5 | 11:40:34 | runner | experiment aborted: error rate 25.0% exceeded the limit 20.0% for 2 consecutive samples |
| E6 | 11:40:34 | runner | runner rolled back (reconnected online-boutique-shippingservice-1 to online-boutique_default (aliases ['online-boutique-shippingservice-1', 'shippingservice'])) |
| E7 | 11:40:38 | telemetry | shippingservice is healthy again |
| E8 | 11:40:41 | telemetry | Cart page probe is passing again |
| E9 | 11:40:43 | runner | application healthy again (two clean samples in a row) |

---

_Evidence window 11:39:27 to 11:41:13; 41 telemetry samples. Narrator: llm:openai/gpt-oss-20b._

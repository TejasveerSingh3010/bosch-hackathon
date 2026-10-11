# Failure report: kill on adservice (kill-adservice-115329)

**Severity: MAJOR** | experiment | generated 2026-10-11 11:54:29 | narrator: llm:openai/gpt-oss-20b

> Adservice kill experiment: container termination and graceful frontend degradation

## Where the failure was

Adservice container was killed (signal 9).

## Summary

An intentional kill of the adservice container caused connection refusals and timeouts in the frontend during ad requests, but no pages failed. Docker automatically restarted the container after 27 s and the runner recovered the application health 0.9 s later. No probes detected the failure and no self‑healing was triggered.

## Failure origin

- **Component:** `adservice`
- **First degraded:** 11:53:41 (observed)
- **Evidence:** E5
- **Note:** 

## Impact and timings

| Phase | Samples | Error rate mean / peak | p95 mean / peak (ms) | Failed pages |
|---|---|---|---|---|
| before | 30 | 0.0% / 0.0% | 151.65 / 160.91 | none |
| during | 14 | 0.0% / 0.0% | 157.06 / 160.71 | none |
| after | 1 | 0.0% / 0.0% | 155.33 / 155.33 | none |

- Detection: not measured
- Failure in place: 27.6 s
- Recovery: 0.9 s after rollback

## What worked

- Frontend handled adservice downtime gracefully; no page failures were observed. _[observed; evidence E3, E6]_
- Docker automatically restarted the adservice container after it was killed. _[observed; evidence E7]_
- The runner rolled back and the application became healthy again after 0.9 seconds. _[observed; evidence E8, E10]_

## Anomalies and unknowns

- No probe failure was observed while adservice was down. _[observed; evidence -]_
- Detection of the adservice failure was delayed/missing. _[observed; evidence -]_
- Recovery took approximately 27 seconds from the time of failure. _[inferred; evidence E7, E10]_
- The adservice did not self‑heal automatically. _[observed; evidence -]_

## Container log findings

| Container | Kind | Count | First seen | First line |
|---|---|---|---|---|
| online-boutique-adservice-1 | unavailable | 2 | 11:54:09 | {"instant":{"epochSecond":1791699848,"nanoOfSecond":883305099},"thread":"Thread-0","level":"INFO","loggerName":"hipstershop.AdService","message":"Stats enabled, but temporarily unavailable","endOfBatch":false,"loggerF... |
| online-boutique-frontend-1 | connection_refused | 94 | 11:53:40 | {"error":"failed to get ads: rpc error: code = Unavailable desc = connection error: desc = \"transport: Error while dialing: dial tcp 172.18.0.2:9555: connect: connection refused\"","http.req.id":"25ddcabe-189b-4b85-a... |
| online-boutique-frontend-1 | timeout | 20 | 11:54:02 | {"error":"failed to get ads: rpc error: code = Unavailable desc = connection error: desc = \"transport: Error while dialing: dial tcp 172.18.0.2:9555: i/o timeout\"","http.req.id":"9913336f-1c48-4c04-b85b-f1098df5232f... |

## Evidence timeline

| ID | Time | Source | Event |
|---|---|---|---|
| E1 | 11:53:40 | docker | docker: kill on adservice (signal 9) |
| E2 | 11:53:40 | docker | docker: die on adservice (exit 137) |
| E3 | 11:53:40 | logs | online-boutique-frontend-1 logged connection_refused x94: {"error":"failed to get ads: rpc error: code = Unavailable desc = connection error: desc = \"transport: Error while dialing: dial tcp 172.18.0.2:9555: connect: connection refused\"","http.req.id":"25ddcabe-189b-4b85-a... |
| E4 | 11:53:40 | runner | runner injected kill on online-boutique-adservice-1 |
| E5 | 11:53:41 | telemetry | adservice went down (container exited (137)) |
| E6 | 11:54:02 | logs | online-boutique-frontend-1 logged timeout x20: {"error":"failed to get ads: rpc error: code = Unavailable desc = connection error: desc = \"transport: Error while dialing: dial tcp 172.18.0.2:9555: i/o timeout\"","http.req.id":"9913336f-1c48-4c04-b85b-f1098df5232f... |
| E7 | 11:54:07 | docker | docker: start on adservice |
| E8 | 11:54:08 | runner | runner rolled back (running again) |
| E9 | 11:54:09 | logs | online-boutique-adservice-1 logged unavailable x2: {"instant":{"epochSecond":1791699848,"nanoOfSecond":883305099},"thread":"Thread-0","level":"INFO","loggerName":"hipstershop.AdService","message":"Stats enabled, but temporarily unavailable","endOfBatch":false,"loggerF... |
| E10 | 11:54:09 | runner | application healthy again (two clean samples in a row) |
| E11 | 11:54:11 | telemetry | adservice is healthy again |

---

_Evidence window 11:52:40 to 11:54:39; 50 telemetry samples. Narrator: llm:openai/gpt-oss-20b._

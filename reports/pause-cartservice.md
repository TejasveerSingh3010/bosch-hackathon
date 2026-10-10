# Failure report: pause-cartservice

*Source: deterministic · verdict: **aborted** · target: cartservice*

pause on cartservice: verdict aborted; first degradation: cartservice [E8]; pages failed: Cart, Home, Product

## Failure origin
- **cartservice** degraded first at 2026-10-11T02:04:11.370135+05:30 [E8]
- the injected target degraded first

## Propagation path
- `target` **cartservice** degraded at 2026-10-11T02:04:11.370135+05:30 [E9]
- `page` **Home** degraded at 2026-10-11T02:04:11.370135+05:30 [E10]
- `page` **Cart** degraded at 2026-10-11T02:04:11.370135+05:30 [E11]
- `page` **Product** degraded at 2026-10-11T02:04:11.370135+05:30 [E12]

## Impact
- failed endpoints: Cart, Home, Product
- error types: {'timeout': 6}
- peak error rate: 75.0% · peak p95: 3043.28ms (baseline p95 156.63ms) [E13, E16, E5]

## Timings
- detection: Nones · recovery: 3.8s · self-healed: False [E5]

## What worked
- these pages kept working throughout: Checkout (observed) [E5]

## Anomalies
- none

## Unknowns
- none

## Evidence
- **E1** (graph) cartservice: criticality 61.7, called by ['checkoutservice', 'frontend']
- **E2** (baseline) baseline p95 mean 156.63ms (error rate 0.0%)
- **E3** (logs) online-boutique-frontend-1: error x13 - {"error":"could not retrieve cart: rpc error: code = Canceled desc = context canceled","http.req.id":"428cbd59-4882-4fbb-b911-b8bf0e7b69b6","http.req.method":"GET","http.req.path":"/product/0PUK6V6EV0","message":"request error","session":"4df1d62e-c2ff-4f51-92af-4fadb48f5af0","severity":"error","tim
- **E4** (docker_event @ 02:04:08) docker pause online-boutique-cartservice-1
- **E5** (result @ 02:04:08) verdict aborted; detection Nones, recovery 3.8s; abort: error rate 75.0% exceeded the limit 20.0% for 2 consecutive samples
- **E6** (timeline @ 02:04:08) [injector] injected pause: paused online-boutique-cartservice-1
- **E7** (timeline @ 02:04:08) [logs] captured logs for 3 container(s) at injection
- **E8** (telemetry @ 02:04:11) first degradation: service cartservice degraded
- **E9** (telemetry @ 02:04:11) service cartservice first degraded
- **E10** (telemetry @ 02:04:11) page Home first degraded
- **E11** (telemetry @ 02:04:11) page Cart first degraded
- **E12** (telemetry @ 02:04:11) page Product first degraded
- **E13** (telemetry @ 02:04:11) peak error rate 75.0%
- **E14** (timeline @ 02:04:12) [watchdog] first degraded sample observed
- **E15** (timeline @ 02:04:14) [watchdog] abort: error rate 75.0% exceeded the limit 20.0% for 2 consecutive samples
- **E16** (telemetry @ 02:04:15) peak p95 latency 3043.28ms
- **E17** (docker_event @ 02:04:16) docker unpause online-boutique-cartservice-1
- **E18** (timeline @ 02:04:16) [runner] rolled back: unpaused online-boutique-cartservice-1
- **E19** (timeline @ 02:04:16) [logs] captured logs for 3 container(s) at rollback
- **E20** (timeline @ 02:04:20) [runner] application healthy again (recovery confirmed)

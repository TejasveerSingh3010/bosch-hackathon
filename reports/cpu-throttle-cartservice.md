# Failure report: cpu-throttle-cartservice

*Source: deterministic · verdict: **completed** · target: cartservice*

cpu_throttle on cartservice: verdict completed; no degradation observed

## Failure origin
- no degradation was observed in the evidence window

## Propagation path
- nothing propagated: no component degraded in the window

## Impact
- failed endpoints: none
- error types: none
- peak error rate: 0.0% · peak p95: 230.75ms (baseline p95 156.63ms) [E6, E3]

## Timings
- detection: Nones · recovery: 0.4s · self-healed: False [E3]

## What worked
- these pages kept working throughout: Cart, Checkout, Home, Product (observed) [E3]
- the application tolerated the fault: no page probe failed while it was in place (observed) [E3]

## Anomalies
- none

## Unknowns
- no container logs were captured, so the underlying error text is unknown

## Evidence
- **E1** (graph) cartservice: criticality 61.7, called by ['checkoutservice', 'frontend']
- **E2** (baseline) baseline p95 mean 156.63ms (error rate 0.0%)
- **E3** (result @ 01:50:29) verdict completed; detection Nones, recovery 0.4s
- **E4** (timeline @ 01:50:29) [injector] injected cpu_throttle: throttled online-boutique-cartservice-1 to 0.1 CPU(s)
- **E5** (timeline @ 01:50:30) [logs] captured logs for 3 container(s) at injection
- **E6** (telemetry @ 01:50:30) peak p95 latency 230.75ms
- **E7** (timeline @ 01:51:00) [runner] rolled back: online-boutique-cartservice-1: restored unlimited CPU
- **E8** (timeline @ 01:51:01) [logs] captured logs for 3 container(s) at rollback
- **E9** (timeline @ 01:51:03) [runner] application healthy again (recovery confirmed)

# Failure report: stop-redis-cart

*Source: deterministic · verdict: **aborted** · target: redis-cart*

stop on redis-cart: verdict aborted; no degradation observed

## Failure origin
- no degradation was observed in the evidence window

## Propagation path
- nothing propagated: no component degraded in the window

## Impact
- failed endpoints: none
- error types: none
- peak error rate: 0.0% · peak p95: 0.0ms (baseline p95 Nonems) [E2]

## Timings
- detection: Nones · recovery: 0.0s · self-healed: False [E2]

## What worked
- (nothing recorded)

## Anomalies
- none

## Unknowns
- no telemetry samples were found for the evidence window
- no container logs were captured, so the underlying error text is unknown

## Evidence
- **E1** (graph) redis-cart: criticality 85.0, called by ['cartservice', 'frontend', 'checkoutservice']
- **E2** (result) verdict aborted; detection Nones, recovery 0.0s; abort: telemetry went stale (16s old, limit 15s) - monitoring lost, stopping to be safe
- **E3** (timeline @ 20:31:27) [injector] injected stop: stopped online-boutique-redis-cart-1
- **E4** (timeline @ 20:31:27) [logs] captured logs for 1 container(s) at injection
- **E5** (timeline @ 20:31:43) [watchdog] abort: telemetry went stale (16s old, limit 15s) - monitoring lost, stopping to be safe
- **E6** (timeline @ 20:31:44) [runner] rolled back: running again
- **E7** (timeline @ 20:31:44) [logs] captured logs for 1 container(s) at rollback
- **E8** (timeline @ 20:31:46) [runner] application healthy again (recovery confirmed)

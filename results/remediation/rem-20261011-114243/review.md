# Code recommendation rem-20261011-114243

Author: llm:openai/gpt-oss-120b - created 2026-10-11 11:42
Answers declined config proposal rem-20261011-114106; applies to upstream commit 5b608cb1df.

## Diagnosed problem

frontend makes a shipping quote gRPC call without a per‑call deadline; when shippingservice is partitioned the call hangs, causing Cart page timeouts and high error rates.

## Evidence

- `network_partition-shippingservice-114018` `during.error_rate_pct_max` = 25.0 - Cart endpoint error rate spikes to 25 % during a shippingservice network partition because the GetQuote call hangs.

## Proposed change

1. `frontend/rpc.go`: Add a 2 s per‑call deadline to the shipping‑quote RPC so the frontend fails fast when shippingservice is unreachable, reducing latency and error‑rate spikes.

```diff
diff --git a/src/frontend/rpc.go b/src/frontend/rpc.go
--- a/src/frontend/rpc.go
+++ b/src/frontend/rpc.go
@@ -85,6 +85,8 @@
 }
 
 func (fe *frontendServer) getShippingQuote(ctx context.Context, items []*pb.CartItem, currency string) (*pb.Money, error) {
+	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
+	defer cancel()
 	quote, err := pb.NewShippingServiceClient(fe.shippingSvcConn).GetQuote(ctx,
 		&pb.GetQuoteRequest{
 			Address: nil,
```

Build check: **passed** - gofmt and go build succeeded in golang:1.27.0-alpine

## Review pass: these issues were found by the review pass (not rewritten)

- review pass failed: rate limited by the Groq API - try again shortly

## Expected benefit

Fast failure of the GetQuote RPC limits the duration of Cart page hangs, lowering the observed error‑rate max and p95 latency during shippingservice outages.

## Side effects

- When shippingservice is healthy the added 2 s timeout adds negligible overhead.
- If shippingservice is slow but eventually returns, the request may now be cancelled early, causing the Cart page to return an error instead of a delayed response.

## How it could be measured

- median `during_error_rate_max` falls by at least 50%
- by replaying `network_partition` on `shippingservice` for 30s, 3 runs with the current image and 3 with an image built from the patch

## Adopting it

In a checkout of the upstream source at 5b608cb1df: `git apply patch.diff`, then rebuild the service image. Reverting is `git apply -R patch.diff`.

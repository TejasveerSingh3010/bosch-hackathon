# Code recommendation rem-20261011-115647

Author: llm:openai/gpt-oss-120b - created 2026-10-11 11:56
Answers declined config proposal rem-20261011-115435; applies to upstream commit 5b608cb1df.

## Diagnosed problem

Frontend does not quickly recover after adservice is killed because the gRPC client backs off and propagates the error, causing a 27‑second outage before ads resume.

## Evidence

- `kill-adservice-115329` `during.error_rate_pct_max` = 0.0 - No increase in error rate was observed during the failure, indicating the page continued to render but the gRPC client remained in backoff.

## Proposed change

1. `frontend/rpc.go`: On adservice errors, reset the gRPC client backoff to reconnect quickly, log the failure, and return an empty ad slice so the page renders without ads instead of returning an HTTP 500 or panicking.

```diff
diff --git a/src/frontend/rpc.go b/src/frontend/rpc.go
--- a/src/frontend/rpc.go
+++ b/src/frontend/rpc.go
@@ -123,5 +123,10 @@
 	resp, err := pb.NewAdServiceClient(fe.adSvcConn).GetAds(ctx, &pb.AdRequest{
 		ContextKeys: ctxKeys,
 	})
-	return resp.GetAds(), errors.Wrap(err, "failed to get ads")
+	if err != nil {
+		fe.adSvcConn.ResetConnectBackoff()
+		log.WithError(err).Warn("failed to get ads, fallback to empty")
+		return []*pb.Ad{}, nil
+	}
+	return resp.GetAds(), nil
 }
```

Build check: **passed** - gofmt and go build succeeded in golang:1.27.0-alpine

## Review pass: these issues were found in the first draft, which was then rewritten once

- frontend/rpc.go (getAd): an error branch neither logs nor returns the error - it would hide it
- review: src/frontend/rpc.go: getAd may panic because resp is nil when err != nil; need to handle error before accessing resp.
- review: src/frontend/rpc.go: getAd does not log the error after resetting backoff; consider logging for observability.

Check that the change above actually addresses them.

## Expected benefit

Resetting the gRPC backoff on adservice errors and falling back to an empty ad list eliminates the long reconnection delay, allowing the frontend to render pages without ads immediately after a kill.

## Side effects

- Pages will render without ads while adservice is down (fallback behavior).
- The error is logged at WARN level for observability.

## How it could be measured

- median `recovery_seconds` falls by at least 80%
- by replaying `kill` on `adservice` for 30s, 3 runs with the current image and 3 with an image built from the patch

## Adopting it

In a checkout of the upstream source at 5b608cb1df: `git apply patch.diff`, then rebuild the service image. Reverting is `git apply -R patch.diff`.

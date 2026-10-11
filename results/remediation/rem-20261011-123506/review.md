# Code recommendation rem-20261011-123506

Author: llm:openai/gpt-oss-120b - created 2026-10-11 12:35
Answers declined config proposal rem-20261011-123315; applies to upstream commit 5b608cb1df.

## Diagnosed problem

Frontend pages fail with high error rate when currencyservice is paused because calls to currencyservice have no per-call deadlines and errors are treated as fatal, causing HTTP 500 responses.

## Evidence

- `pause-currencyservice-123230` `during.error_rate_pct_mean` = 75.0 - Error rate spikes to 75% while currencyservice is paused, confirming fatal dependency.

## Proposed change

1. `frontend/rpc.go`: Adds a 100ms deadline to the GetSupportedCurrencies RPC and treats errors as non‑fatal, preventing page failures when currencyservice hangs.
2. `frontend/rpc.go`: Adds a deadline to the Convert RPC and falls back to the original amount on error, making currency conversion optional and avoiding fatal page errors.

```diff
diff --git a/src/frontend/rpc.go b/src/frontend/rpc.go
--- a/src/frontend/rpc.go
+++ b/src/frontend/rpc.go
@@ -28,10 +28,15 @@
 )
 
 func (fe *frontendServer) getCurrencies(ctx context.Context) ([]string, error) {
+	// Apply a short deadline to avoid hanging on currencyservice pauses.
+	ctx, cancel := context.WithTimeout(ctx, 100*time.Millisecond)
+	defer cancel()
 	currs, err := pb.NewCurrencyServiceClient(fe.currencySvcConn).
 		GetSupportedCurrencies(ctx, &pb.Empty{})
 	if err != nil {
-		return nil, err
+		// Log warning and continue with empty currency list.
+		log.WithError(err).Warn("getCurrencies failed, proceeding with empty list")
+		return []string{}, nil
 	}
 	var out []string
 	for _, c := range currs.CurrencyCodes {
@@ -78,10 +83,19 @@
 	if avoidNoopCurrencyConversionRPC && money.GetCurrencyCode() == currency {
 		return money, nil
 	}
-	return pb.NewCurrencyServiceClient(fe.currencySvcConn).
+	// Apply a short deadline to avoid hanging on currencyservice pauses.
+	ctx, cancel := context.WithTimeout(ctx, 100*time.Millisecond)
+	defer cancel()
+	converted, err := pb.NewCurrencyServiceClient(fe.currencySvcConn).
 		Convert(ctx, &pb.CurrencyConversionRequest{
 			From:   money,
 			ToCode: currency})
+	if err != nil {
+		// Log warning and fallback to the original amount.
+		log.WithError(err).Warn("convertCurrency failed, using original amount")
+		return money, nil
+	}
+	return converted, nil
 }
 
 func (fe *frontendServer) getShippingQuote(ctx context.Context, items []*pb.CartItem, currency string) (*pb.Money, error) {
```

Build check: **passed** - gofmt and go build succeeded in golang:1.27.0-alpine

## Review pass: these issues were found in the first draft, which was then rewritten once

- review: src/frontend/rpc.go:getCurrencies - redeclares the ctx parameter with ':=' (ctx, cancel := ...) which is a compile‑time error; should use a new variable name for the deadline context or assign to existing ctx with 'ctx, cancel = context.WithTimeout(ctx, ...)' after declaring cancel.
- review: src/frontend/rpc.go:getCurrencies - references an undefined identifier 'log' (and checks it against nil); the frontend server does not expose a variable named log, causing a compile error. Use the existing logger (e.g., fe.logger) or import a logger package.
- review: src/frontend/rpc.go:convertCurrency - redeclares the ctx parameter with ':=' leading to a compile error, same issue as above.
- review: src/frontend/rpc.go:convertCurrency - references undefined 'log' variable; replace with the proper logger (e.g., fe.logger) or remove the nil check.
- review: src/frontend/rpc.go:convertCurrency - after adding the deadline, the code returns 'converted, nil' inside the if‑block but the original function signature expects (*pb.Money, error); the added return matches, but ensure the function still returns the expected types (no extra nil error needed).

Check that the change above actually addresses them.

## Expected benefit

Pages render even when currencyservice is paused; error rate drops dramatically, user‑visible data (prices) may be missing or fallback to original currency but the site stays up.

## Side effects

- Home/Product/Cart pages may show no currency options if the service is down.
- Price conversion may fall back to the original amount instead of the target currency.
- Warnings are logged for each failed currencyservice call.

## How it could be measured

- median `during_error_rate_mean` falls by at least 80%
- by replaying `pause` on `currencyservice` for 30s, 3 runs with the current image and 3 with an image built from the patch

## Adopting it

In a checkout of the upstream source at 5b608cb1df: `git apply patch.diff`, then rebuild the service image. Reverting is `git apply -R patch.diff`.

# Code recommendation rem-20261011-120256

Author: llm:openai/gpt-oss-120b - created 2026-10-11 12:02
Answers declined config proposal rem-20261011-115944; applies to upstream commit 5b608cb1df.

## Diagnosed problem

Frontend hangs and returns HTTP 500 when currencyservice is paused because gRPC calls have no per-call deadline and currency data is treated as fatal.

## Evidence

- `pause-currencyservice-115903` `during.error_rate_pct_max` = 75.0 - High error rate caused by frontend hanging on currencyservice pause.

## Proposed change

1. `frontend/rpc.go`: Add a per-call deadline to prevent indefinite hangs when currencyservice is paused.
2. `frontend/rpc.go`: Add a per-call deadline to the currency conversion RPC to avoid hanging calls.
3. `frontend/handlers.go`: Treat missing currency data as optional on Home page, logging the error and rendering without it.
4. `frontend/handlers.go`: Make currency list optional on Product page, avoiding fatal errors when currencyservice is down.
5. `frontend/handlers.go`: Fall back to original price if currency conversion fails, preventing page crashes.

```diff
diff --git a/src/frontend/handlers.go b/src/frontend/handlers.go
--- a/src/frontend/handlers.go
+++ b/src/frontend/handlers.go
@@ -61,8 +61,8 @@
 	log.WithField("currency", currentCurrency(r)).Info("home")
 	currencies, err := fe.getCurrencies(r.Context())
 	if err != nil {
-		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve currencies"), http.StatusInternalServerError)
-		return
+		log.WithField("error", err).Warn("could not retrieve currencies, proceeding without")
+		currencies = []string{}
 	}
 	products, err := fe.getProducts(r.Context())
 	if err != nil {
@@ -158,8 +158,8 @@
 	}
 	currencies, err := fe.getCurrencies(r.Context())
 	if err != nil {
-		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve currencies"), http.StatusInternalServerError)
-		return
+		log.WithField("error", err).Warn("could not retrieve currencies, proceeding without")
+		currencies = []string{}
 	}
 
 	cart, err := fe.getCart(r.Context(), sessionID(r))
@@ -170,8 +170,8 @@
 
 	price, err := fe.convertCurrency(r.Context(), p.GetPriceUsd(), currentCurrency(r))
 	if err != nil {
-		renderHTTPError(log, r, w, errors.Wrap(err, "failed to convert currency"), http.StatusInternalServerError)
-		return
+		log.WithField("error", err).Warn("currency conversion failed, using original price")
+		price = p.GetPriceUsd()
 	}
 
 	// ignores the error retrieving recommendations since it is not critical
diff --git a/src/frontend/rpc.go b/src/frontend/rpc.go
--- a/src/frontend/rpc.go
+++ b/src/frontend/rpc.go
@@ -28,6 +28,8 @@
 )
 
 func (fe *frontendServer) getCurrencies(ctx context.Context) ([]string, error) {
+	ctx, cancel := context.WithTimeout(ctx, 1*time.Second)
+	defer cancel()
 	currs, err := pb.NewCurrencyServiceClient(fe.currencySvcConn).
 		GetSupportedCurrencies(ctx, &pb.Empty{})
 	if err != nil {
@@ -78,6 +80,8 @@
 	if avoidNoopCurrencyConversionRPC && money.GetCurrencyCode() == currency {
 		return money, nil
 	}
+	ctx, cancel := context.WithTimeout(ctx, 1*time.Second)
+	defer cancel()
 	return pb.NewCurrencyServiceClient(fe.currencySvcConn).
 		Convert(ctx, &pb.CurrencyConversionRequest{
 			From:   money,
```

Build check: **passed** - gofmt and go build succeeded in golang:1.27.0-alpine

## Review pass: these issues were found in the first draft, which was then rewritten once

- review: src/frontend/rpc.go:getCurrencies – the added 100 ms timeout is too aggressive and will turn normal, healthy calls into failures; use a longer timeout (e.g., 1 s) or make it configurable so the success path isn’t altered.
- review: src/frontend/rpc.go:convertCurrency – same overly‑short 100 ms deadline; increase it to a safe value or make it configurable.
- review: src/frontend/rpc.go – missing import for the "time" package after adding context.WithTimeout calls; add "time" to the import list.

Check that the change above actually addresses them.

## Expected benefit

Pages will continue to render even when currencyservice is paused, reducing error rate and latency dramatically.

## Side effects

- Pages may show no currency selector and prices in default currency when currencyservice is unavailable.

## How it could be measured

- median `during_error_rate_max` falls by at least 70%
- by replaying `pause` on `currencyservice` for 30s, 3 runs with the current image and 3 with an image built from the patch

## Adopting it

In a checkout of the upstream source at 5b608cb1df: `git apply patch.diff`, then rebuild the service image. Reverting is `git apply -R patch.diff`.

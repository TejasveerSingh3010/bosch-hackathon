# Remediation proposal rem-20261011-123315

Author: llm:openai/gpt-oss-120b - created 2026-10-11 12:33

## Declined by the agent - no configuration fix

Frontend lacks per-call deadlines for currencyservice; a paused currencyservice causes fatal errors on Home, Cart, Product pages. This weakness can only be fixed by application code changes (adding deadlines and optional handling), not by allowed Compose settings.

## Diagnosed problem

Pausing currencyservice makes frontend pages fail with high error rate (75%) because currencyservice is a fatal dependency and there are no per-call deadlines.

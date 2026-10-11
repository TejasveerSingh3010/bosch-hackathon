# Remediation proposal rem-20261011-115944

Author: llm:openai/gpt-oss-120b - created 2026-10-11 11:59

## Declined by the agent - no configuration fix

The pause of the currencyservice causes frontend requests to hang because the frontend has no per-call deadlines or fallback for currencyservice. This is a code-level issue; configuration changes (restart policy, resources, or environment) cannot mitigate a pause-induced hang.

## Diagnosed problem

Pausing currencyservice leads to 75% error rate and >3000 ms latency on Home, Product, and Cart pages. The frontend lacks per-call deadlines, so a hung dependency blocks request handling.

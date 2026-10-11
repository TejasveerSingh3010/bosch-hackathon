# Remediation proposal rem-20261011-114106

Author: llm:openai/gpt-oss-120b - created 2026-10-11 11:41

## Declined by the agent - no configuration fix

The observed weakness is that shippingservice is a fatal dependency for the cart page and the frontend has no per‑call deadline for its gRPC call. When a network partition isolates shippingservice, the cart page times out and error rate spikes. The documentation states this is not fixable by configuration (requires code changes to add deadlines). No allowed Compose setting (restart, resources, or environment) can mitigate the fault.

## Diagnosed problem

Network partition of shippingservice makes it unreachable, causing Cart page failures (error rate 25%, p95 3017 ms). Lack of per‑call deadline means the frontend does not fail fast. This weakness can only be addressed by adding deadlines in code.

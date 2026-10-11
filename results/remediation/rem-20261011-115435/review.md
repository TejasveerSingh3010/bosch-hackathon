# Remediation proposal rem-20261011-115435

Author: llm:openai/gpt-oss-120b - created 2026-10-11 11:54

## Declined by the agent - no configuration fix

The observed weakness (slow recovery after adservice is killed) is due to Docker treating a kill as a manual stop, so restart policies do not apply. The service documentation states that this failure is tolerated by the frontend and that making the system recover faster requires code changes (e.g., better health‑checking or automatic restart handling), which are not configurable via Compose settings.

## Diagnosed problem

adservice kill causes a 27‑second outage because Docker does not automatically restart a container killed via the API; the frontend tolerates the missing ads but does not detect or self‑heal, leading to delayed recovery.

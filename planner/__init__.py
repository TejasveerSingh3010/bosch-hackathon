"""Agent 1, the experiment Planner: an LLM-driven attacker on a constrained tool interface.

It chooses resilience experiments from a fixed catalogue of eight fault types (each usable on any
allowed service), and can act only through typed tools - never a shell and never Docker directly.
Every injection is re-validated by the policy check (`planner.policy`, shared with the runner) and
needs human approval; the whole session is written to an audit trail under `results/plans/`.

    catalogue -> AttackPlan -> policy check -> human approval -> runner -> audit

The pieces: `catalogue` (the eight attacks), `plan` (the structured plan a human reads), `policy`
(the independent pre-execution check), `approval`, `audit`, `interface` (the tools), and `agent`
(selection and orchestration).
"""

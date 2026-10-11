"""The agent pipeline: a LangGraph graph of three agents, traced in Langfuse.

    attacker -> reporter -> remediation -> escalate (recommendation for a human)

Each agent has its own tools (`framework.add_agent`). The attacker is a stand-in whose tools are
real but whose decision is scripted; the reporter (Agent 2) writes a real failure report from the
telemetry and logs, which the remediator then uses alongside `docs/services.yaml` to file a real
recommendation. Nothing here applies a fix: the run ends with a recommendation awaiting a human.
"""

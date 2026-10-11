"""The agent pipeline: a LangGraph graph of three agents, traced in Langfuse.

    attacker -> analyst -> remediation -> escalate (recommendation for a human)

Each agent has its own tools (`framework.add_agent`). The attacker and analyst are stand-ins whose
tools are real but whose decisions are scripted; remediation files real recommendations. Nothing
here applies a fix: the run ends with a recommendation awaiting a human decision.
"""

"""Phase 5: diagnose -> recommend a fix (or decline) -> escalate to a human.

A fix is a Compose configuration change (`propose`) or, when the agent declines because only code
can fix the weakness, a code change against the pinned upstream source (`propose-code`, see
remediation.source): exact edits, policy-checked, compiled, and rendered as a patch.

The agent (remediation.agent) only recommends: its output is a proposal that has passed the
deterministic policy checks (remediation.policy). Accepting or rejecting it is the human escalation
step (remediation.store records that decision, tied to file hashes). After accepting, the human may
measure the fix with a before/after experiment (remediation.verify); that is a tool, not a stage the
agent drives. Nothing here edits app/docker-compose.yml.
"""

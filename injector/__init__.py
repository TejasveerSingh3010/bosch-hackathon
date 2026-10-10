"""Controlled failure injection for Online Boutique containers.

Nothing in here checks whether an experiment is *allowed* - that is `safety`'s job, and the
orchestrator always validates a plan before calling into this package. Keeping the two apart is
deliberate: the AI planner (day 2) can propose anything it likes, and it still cannot reach these
actions without passing the safety layer first.
"""
from .actions import ACTIONS, ActionOutcome, FailureAction, build_action, describe_actions
from .docker_cli import Container, DockerCli, DockerError
from .resolve import TargetError, resolve_target

__all__ = [
    "ACTIONS",
    "ActionOutcome",
    "Container",
    "DockerCli",
    "DockerError",
    "FailureAction",
    "TargetError",
    "build_action",
    "describe_actions",
    "resolve_target",
]

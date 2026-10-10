"""Failure actions and their rollbacks.

Each action is a small class with `apply()` and `rollback()`. Rollback is written to be
**idempotent and state-based**: it inspects the container and does only what is needed to put it
back the way it was. That matters because

  * Compose sets `restart: unless-stopped`, so after a `kill` Docker may already have restarted the
    container - rolling back must not start a second one or fail because it is running; and
  * rollback runs from several places (normal end, watchdog abort, kill switch, crash recovery), so
    it may well be called twice for the same experiment.

Adding a new failure mode means adding one class and listing it in ACTIONS - nothing else changes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .docker_cli import Container, Docker, DockerCli, DockerError


@dataclass
class ActionOutcome:
    """What an apply/rollback call did, for the experiment record."""

    ok: bool
    detail: str
    at: float = field(default_factory=time.time)
    pre_state: dict[str, Any] | None = None


class FailureAction:
    """Base class. `name` is the value used in an experiment's `action` field."""

    name: str = ""
    description: str = ""
    #: True when Docker's own restart policy is expected to undo this action.
    self_healing_expected: bool = False

    def __init__(self, docker: Docker, project: str | None = None, **params: Any) -> None:
        self.docker = docker
        self.project = project
        self.params = params
        self._pre: dict[str, Any] | None = None

    # -- helpers --
    def _snapshot(self, container_name: str) -> Container:
        return self.docker.get(container_name, self.project)

    def _record_pre(self, container: Container) -> dict[str, Any]:
        self._pre = {
            "status": container.status,
            "running": container.running,
            "paused": container.paused,
            "restart_policy": container.restart_policy,
            "restart_count": container.restart_count,
            "started_at": container.started_at,
        }
        return self._pre

    def _ensure_running(self, container_name: str, attempts: int = 3, wait_s: float = 1.0) -> str:
        """Start the container unless it is already running (possibly restarted by Docker itself)."""
        for attempt in range(attempts):
            container = self._snapshot(container_name)
            if container.paused:
                self.docker.unpause(container_name)
                continue
            if container.running and not container.restarting:
                return ("already running (Docker's restart policy recovered it)"
                        if attempt == 0 else "running again")
            if container.restarting:
                time.sleep(wait_s)
                continue
            self.docker.start(container_name)
            time.sleep(wait_s)
        container = self._snapshot(container_name)
        if container.running:
            return "running again"
        raise DockerError(f"{container_name} is still {container.status} after rollback attempts")

    # -- interface --
    def apply(self, container_name: str) -> ActionOutcome:  # pragma: no cover - overridden
        raise NotImplementedError

    def rollback(self, container_name: str) -> ActionOutcome:  # pragma: no cover - overridden
        raise NotImplementedError


class KillAction(FailureAction):
    """SIGKILL the process: a sudden crash with no graceful shutdown.

    With Compose's `restart: unless-stopped` Docker restarts it on its own, so this measures
    *automatic* recovery - the most interesting case for a resilience report.
    """

    name = "kill"
    description = "SIGKILL the container (sudden crash; Docker's restart policy should recover it)"
    self_healing_expected = True

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        self.docker.kill(container_name, self.params.get("signal", "KILL"))
        return ActionOutcome(True, f"sent SIGKILL to {container_name}", pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        return ActionOutcome(True, self._ensure_running(container_name))


class StopAction(FailureAction):
    """Stop the container: a lasting outage.

    `docker stop` is honoured by `unless-stopped`, so the container stays down until rollback -
    this is the one to use for "what happens while a dependency is gone".
    """

    name = "stop"
    description = "Stop the container (lasting outage; it stays down until rollback)"

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        self.docker.stop(container_name, int(self.params.get("grace_s", 0)))
        return ActionOutcome(True, f"stopped {container_name}", pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        return ActionOutcome(True, self._ensure_running(container_name))


class PauseAction(FailureAction):
    """Freeze every process in the container (SIGSTOP via cgroups).

    The port stays open but nothing answers, so callers hang until they time out. This is the
    "slow / unresponsive dependency" failure, and it usually hurts far more than a clean crash
    because a hang ties up the caller's connections.
    """

    name = "pause"
    description = "Freeze the container (unresponsive dependency; callers hang instead of failing fast)"

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        if container.paused:
            return ActionOutcome(
                False, f"{container_name} is already paused", pre_state=pre
            )
        self.docker.pause(container_name)
        return ActionOutcome(True, f"paused {container_name}", pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        if container.paused:
            self.docker.unpause(container_name)
            return ActionOutcome(True, f"unpaused {container_name}")
        if not container.running:
            return ActionOutcome(True, self._ensure_running(container_name))
        return ActionOutcome(True, f"{container_name} was not paused; nothing to undo")


ACTIONS: dict[str, type[FailureAction]] = {
    action.name: action for action in (KillAction, StopAction, PauseAction)
}


def build_action(action_name: str, docker: Docker, project: str | None = None,
                 **params: Any) -> FailureAction:
    try:
        return ACTIONS[action_name](docker, project, **params)
    except KeyError:
        raise ValueError(f"unknown action {action_name!r}; available: {', '.join(sorted(ACTIONS))}") from None


def describe_actions() -> str:
    return "\n".join(f"  {name:<6} {cls.description}" for name, cls in sorted(ACTIONS.items()))


__all__ = ["ACTIONS", "ActionOutcome", "DockerCli", "FailureAction", "build_action", "describe_actions"]

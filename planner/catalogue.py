"""The attack catalogue: every legal (target, fault) pair the system can run.

Agent 1 ranks this whole list, so it has to be complete and it has to be legal. "Legal" means the
safety layer could accept it in principle - the target is on target.yaml's allow-list - not that it
will accept it right now (the pre-flight re-check in the ranker decides that against live
telemetry). Each entry carries the fault's default params, the failure mode it exercises, how it is
rolled back, and whether Docker's restart policy is expected to self-heal it.

Targets are named by their Compose **service** (e.g. `redis-cart`), which is what
docs/experiment.example.json uses and what `injector.resolve` maps to a container; the allow-list in
target.yaml is by container name, so this module derives the service for each allowed container.
`spike` is the one fault that is frontend-only: it holds HTTP load on the frontend regardless of any
other target, so listing it against every service would be meaningless.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from safety.limits import TargetConfig
from schemas import Experiment

#: Default duration for a catalogue entry (well under target.yaml's cap; the human can shorten it).
DEFAULT_DURATION_S = 30

#: Container-level faults, applicable to any allow-listed service. (action, default params,
#: failure mode it tests, rollback summary, whether Docker should self-heal it).
CONTAINER_FAULTS: tuple[tuple[str, dict[str, Any], str, str, bool], ...] = (
    ("kill", {}, "sudden crash", "Docker restart policy restarts it; rollback ensures it is up", True),
    ("stop", {}, "lasting outage", "rollback starts the container again", False),
    ("pause", {}, "unresponsive dependency (callers hang)", "rollback unpauses it", False),
    ("network_partition", {}, "reachable by no one (up but isolated)",
     "rollback reconnects it with its original aliases", False),
    ("network_delay", {"delay_ms": 300, "loss_pct": 5}, "slow/lossy network link",
     "rollback removes the tc netem qdisc", False),
    ("cpu_throttle", {"cpus": 0.1}, "CPU starvation", "rollback restores the original CPU quota", False),
    ("memory_limit", {"memory": "128m"}, "memory pressure",
     "rollback restores the original memory limit", False),
    ("flapping", {"mode": "pause", "down_s": 2, "interval_s": 3}, "intermittent up/down flapping",
     "rollback stops the loop and ensures it is running", True),
)

#: Frontend-only fault.
SPIKE_FAULT: tuple[str, dict[str, Any], str, str, bool] = (
    "spike", {"rps": 50, "users": 10}, "sustained traffic surge",
    "rollback stops the load generators", False,
)


def service_from_container(container_name: str, project: str | None) -> str:
    """`online-boutique-redis-cart-1` -> `redis-cart`. Falls back to the whole name if it does not fit."""
    name = container_name
    if project and name.startswith(f"{project}-"):
        name = name[len(project) + 1:]
    return re.sub(r"-\d+$", "", name)


@dataclass
class AttackSpec:
    """One catalogue entry: a fault aimed at one service, with everything needed to build a plan."""

    target: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    duration_s: int = DEFAULT_DURATION_S
    failure_mode: str = ""
    rollback: str = ""
    self_heal_expected: bool = False

    @property
    def key(self) -> str:
        return f"{self.action}:{self.target}"

    def experiment(self, experiment_id: str | None = None) -> Experiment:
        """Build the fully valid Experiment a human would approve and the runner would execute."""
        params = dict(self.params)
        if self.action == "spike":
            params.setdefault("time", self.duration_s)
        return Experiment(
            id=experiment_id or f"{self.action}-{self.target}".replace("_", "-"),
            target=self.target,
            action=self.action,
            params=params,
            duration_s=self.duration_s,
            reason=f"{self.failure_mode} on {self.target}",
        )


def build_catalogue(config: TargetConfig, duration_s: int = DEFAULT_DURATION_S) -> list[AttackSpec]:
    """Every legal (service, fault) pair under the allow-list, plus the frontend spike."""
    services: list[str] = []
    for container in config.allowed_containers:
        service = service_from_container(container, config.compose_project)
        if service not in services:
            services.append(service)

    specs: list[AttackSpec] = []
    for service in services:
        for action, params, mode, rollback, self_heal in CONTAINER_FAULTS:
            specs.append(AttackSpec(
                target=service, action=action, params=dict(params), duration_s=duration_s,
                failure_mode=mode, rollback=rollback, self_heal_expected=self_heal,
            ))
    if "frontend" in services:
        action, params, mode, rollback, self_heal = SPIKE_FAULT
        specs.append(AttackSpec(
            target="frontend", action=action, params=dict(params), duration_s=duration_s,
            failure_mode=mode, rollback=rollback, self_heal_expected=self_heal,
        ))
    return specs


__all__ = [
    "CONTAINER_FAULTS",
    "DEFAULT_DURATION_S",
    "SPIKE_FAULT",
    "AttackSpec",
    "build_catalogue",
    "service_from_container",
]

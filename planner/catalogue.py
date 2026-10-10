"""The attack catalogue: the eight fault classes Agent 1 may use, each runnable on any service.

Each class names one `injector` action, the parameters it takes (validated by the injector's own
param functions, so the planner and the runner agree on what is legal), and the evidence a human
needs to judge a plan: the hypothesis it tests, what to expect if the system is resilient, and the
criterion for calling it recovered. Nothing here touches Docker; it is data plus validation.

`spike` is deliberately absent: it holds HTTP load on the frontend, so it is not a per-service
fault. The dashboard's traffic-burst button covers that case outside the agent.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from injector.actions import flapping_params, memory_params, netem_params, throttle_params

#: Default hold for a catalogue experiment, in seconds. Well under target.yaml's cap; a plan may
#: shorten it, and the policy layer clamps anything longer.
DEFAULT_DURATION_S = 30


def _no_params(params: dict[str, Any]) -> dict[str, Any]:
    """kill/stop/pause/network_partition take no parameters; reject any that are passed."""
    extra = set(params) - {"signal"}
    if extra:
        raise ValueError(f"this attack takes no parameters; got {', '.join(sorted(extra))}")
    return {}


@dataclass(frozen=True)
class AttackClass:
    """One fault the agent can inject, with the facts a human weighs before approving it."""

    action: str  # the injector action name
    title: str
    description: str
    #: Validate+normalise the fault parameters, or raise ValueError. Returns what the plan will carry.
    validate: Callable[[dict[str, Any]], dict[str, Any]]
    hypothesis: str  # what the experiment is meant to reveal
    expected_observations: str  # what a resilient system should show
    recovery_criteria: str  # how a human confirms the system came back
    params_help: dict[str, str] = field(default_factory=dict)
    self_heal_expected: bool = False

    def normalise(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return self.validate(dict(params or {}))


CATALOGUE: dict[str, AttackClass] = {
    "kill": AttackClass(
        action="kill",
        title="Kill (SIGKILL)",
        description="Kill the container outright: a sudden crash with no graceful shutdown.",
        validate=_no_params,
        hypothesis="A sudden crash of {target} is recovered automatically by Docker's restart policy "
                   "and callers degrade gracefully meanwhile.",
        expected_observations="A brief error blip, then automatic restart; pages that do not depend on "
                              "{target} keep working.",
        recovery_criteria="{target} is running again and probe success returns to baseline.",
        self_heal_expected=True,
    ),
    "stop": AttackClass(
        action="stop",
        title="Stop (lasting outage)",
        description="Stop the container; it stays down until rollback. A lasting dependency outage.",
        validate=_no_params,
        hypothesis="While {target} is down, callers fail fast or fall back rather than hanging.",
        expected_observations="Pages that hard-depend on {target} return errors quickly; tolerated "
                              "dependencies are skipped and the page still renders.",
        recovery_criteria="{target} is started again and the error rate returns to baseline.",
    ),
    "pause": AttackClass(
        action="pause",
        title="Pause (unresponsive)",
        description="Freeze every process in the container: the port is open but nothing answers.",
        validate=_no_params,
        hypothesis="An unresponsive {target} does not tie up its callers: they time out rather than "
                   "hang until the request budget is exhausted.",
        expected_observations="Latency on dependent paths rises to a bounded deadline, not to the probe "
                              "timeout; callers without a deadline will hang (a finding).",
        recovery_criteria="{target} is unpaused and latency returns to baseline.",
    ),
    "throttle": AttackClass(
        action="throttle",
        title="CPU throttle (starvation)",
        description="Cap the container's CPU in place: degraded, not down (a noisy neighbour).",
        validate=lambda p: {"cpus": throttle_params(p)},
        hypothesis="A CPU-starved {target} slows down without taking dependent pages past their SLA.",
        expected_observations="Raised latency on paths through {target}, staying under the abort "
                              "threshold; no cascading errors.",
        recovery_criteria="The CPU limit is restored and latency returns to baseline.",
        params_help={"cpus": "CPU limit to impose, e.g. 0.1 (must be below any existing limit)"},
    ),
    "memory_limit": AttackClass(
        action="memory_limit",
        title="Memory limit (pressure)",
        description="Cap the container's memory in place: memory pressure, possibly an OOM kill.",
        validate=lambda p: {"memory_mb": memory_params(p)},
        hypothesis="Under a tight memory cap {target} either stays within it or is OOM-killed and "
                   "restarted cleanly, without corrupting dependent requests.",
        expected_observations="Either steady operation under the cap, or a restart blip like a kill; "
                              "no stuck or half-failed requests.",
        recovery_criteria="The memory limit is restored and {target} is running at baseline.",
        params_help={"memory_mb": "memory cap in MiB, e.g. 128 (must be below any existing limit)"},
    ),
    "network_partition": AttackClass(
        action="network_partition",
        title="Network partition (isolated)",
        description="Disconnect the container from its network: up, but reachable by no one.",
        validate=_no_params,
        hypothesis="When {target} is reachable by no one, callers treat it as down rather than "
                   "hanging on connections that never complete.",
        expected_observations="Connection/resolution errors on dependent paths, handled like an "
                              "outage; the isolated service itself stays up.",
        recovery_criteria="{target} is reconnected to its network and callers reach it again.",
    ),
    "network_delay": AttackClass(
        action="network_delay",
        title="Network delay / loss (slow link)",
        description="Add latency and packet loss on the container's interface: a slow, lossy link.",
        validate=lambda p: dict(zip(("delay_ms", "loss_pct"), netem_params(p), strict=True)),
        hypothesis="A slow, lossy link to {target} is absorbed by timeouts and retries without "
                   "breaching the latency SLA on dependent pages.",
        expected_observations="Bounded extra latency on paths through {target}; retries mask the loss; "
                              "no page exceeds the abort threshold.",
        recovery_criteria="The netem qdisc is removed and latency returns to baseline.",
        params_help={"delay_ms": "added latency in ms, 0-10000", "loss_pct": "packet loss %, 0-100"},
    ),
    "flapping": AttackClass(
        action="flapping",
        title="Flapping (intermittent)",
        description="Pause and unpause the container repeatedly on a timer: intermittent outages.",
        validate=lambda p: dict(zip(("down_s", "up_s", "cycles"), flapping_params(p), strict=True)),
        hypothesis="Intermittent flapping of {target} does not defeat health checks and retries, and "
                   "does not leave callers stuck between cycles.",
        expected_observations="Error/latency spikes that clear on each recovery; no monotonic "
                              "degradation across cycles.",
        recovery_criteria="Flapping stops, {target} is running, and metrics return to baseline.",
        params_help={"down_s": "seconds down per cycle, 0.5-60", "up_s": "seconds up per cycle, 0.5-60",
                     "cycles": "number of cycles, 0 = until the duration ends"},
    ),
}

#: The attack names in a stable order (used by list_experiments and the CLI).
ATTACK_NAMES: tuple[str, ...] = tuple(CATALOGUE)


def get_attack(action: str) -> AttackClass:
    try:
        return CATALOGUE[action]
    except KeyError:
        raise KeyError(f"unknown attack {action!r}; available: {', '.join(ATTACK_NAMES)}") from None


__all__ = ["ATTACK_NAMES", "CATALOGUE", "DEFAULT_DURATION_S", "AttackClass", "get_attack"]

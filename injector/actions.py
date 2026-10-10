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

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .docker_cli import Container, Docker, DockerCli, DockerError

#: Hard ceilings for a spike. Duration is capped separately by the experiment schema (600 s).
SPIKE_MAX_RPS = 20000
SPIKE_MAX_USERS = 50
SPIKE_PATHS = ("/", "/cart", "/product/0PUK6V6EV0")

#: Image used for the network_delay sidecar: it must carry `tc` (iproute2). netshoot is the usual
#: choice. Override with CHAOS_NETEM_IMAGE if the demo machine mirrors a different one.
NETEM_IMAGE = os.getenv("CHAOS_NETEM_IMAGE", "nicolaka/netshoot")
#: The interface inside the target's network namespace that netem is applied to.
NETEM_IFACE = os.getenv("CHAOS_NETEM_IFACE", "eth0")
#: Floor for memory_limit: never set a limit so low the container is OOM-killed the instant it is
#: applied. 64 MiB keeps the process alive long enough to observe real memory pressure.
MEMORY_FLOOR_BYTES = 64 * 1024 * 1024
#: Smallest CPU fraction cpu_throttle will set (0 would be rejected by docker; this is a hard choke).
CPU_MIN_FRACTION = 0.01


def parse_memory(value: Any) -> int:
    """Parse '256m', '1g', '512k', or a plain byte count into bytes. Raises ValueError otherwise."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    units = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2,
             "g": 1024**3, "gb": 1024**3}
    for suffix, factor in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            return int(float(number) * factor)
    return int(float(text))


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


class SpikeAction(FailureAction):
    """Hold extra HTTP traffic on the Online Boutique frontend.

    Params, all required:
      * ``rps``   aggregate requests per second
      * ``users`` concurrent clients sharing that rate
      * ``time``  seconds to keep sending, unless rollback stops it sooner

    Requests go to ``BOUTIQUE_URL`` (default ``http://localhost:8080``), which is the
    port-forwarded frontend on Kubernetes and the Compose frontend locally. A plan cannot
    point this at any other host. Rollback stops the clients; it does not restart a container.
    """

    name = "spike"
    description = "Sustain HTTP load on the frontend (params: rps, users, time)"

    def __init__(self, docker: Docker, project: str | None = None, **params: Any) -> None:
        super().__init__(docker, project, **params)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        if self._threads:
            return ActionOutcome(False, "spike is already running", pre_state=pre)
        try:
            rps, users, hold = spike_params(self.params)
        except ValueError as exc:
            return ActionOutcome(False, str(exc), pre_state=pre)

        self._stop.clear()
        base = str(self.params.get("base_url") or os.getenv("BOUTIQUE_URL") or "http://localhost:8080").rstrip("/")
        for index in range(users):
            thread = threading.Thread(
                target=self._user, args=(base, index, rps, users, hold),
                name=f"spike-{index}", daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        return ActionOutcome(
            True, f"spike on {base}: {rps} req/s, {users} users, {hold}s", pre_state=pre,
        )

    def rollback(self, container_name: str) -> ActionOutcome:
        if not self._threads:
            return ActionOutcome(True, f"spike on {container_name} was not running; nothing to undo")
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=6)
        self._threads.clear()
        return ActionOutcome(True, f"stopped the traffic spike on {container_name}")

    def _user(self, base: str, index: int, rps: int, users: int, hold: int) -> None:
        interval = users / rps
        deadline = time.perf_counter() + hold
        sent = 0
        while not self._stop.is_set() and time.perf_counter() < deadline:
            url = base + SPIKE_PATHS[(index + sent) % len(SPIKE_PATHS)]
            started = time.perf_counter()
            try:
                requests.get(url, timeout=5, allow_redirects=False)
            except requests.RequestException:
                pass
            sent += 1
            remaining = interval - (time.perf_counter() - started)
            if remaining > 0 and self._stop.wait(remaining):
                return


class NetworkPartitionAction(FailureAction):
    """Disconnect the target from its Compose network: a clean network split.

    The container keeps running and still reports healthy to Docker, but nothing can reach it and
    it can reach nothing - the "it's up but unreachable" failure that a crash never reproduces.
    Rollback reconnects it with the exact aliases it had, recorded in pre_state, so service-name
    resolution works again.
    """

    name = "network_partition"
    description = "Disconnect the container from its Compose network (reachable by no one)"

    def _network(self) -> str:
        return str(self.params.get("network") or f"{self.project}_default")

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(False, f"{container_name} is not running", pre_state=pre)
        network = self._network()
        conn = container.network(network)
        if conn is None:
            return ActionOutcome(
                False, f"{container_name} is not on network {network!r}", pre_state=pre
            )
        pre["network"] = network
        pre["aliases"] = list(conn.aliases)
        self.docker.network_disconnect(network, container_name)
        return ActionOutcome(True, f"disconnected {container_name} from {network}", pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        network = self._network()
        aliases = tuple((self._pre or {}).get("aliases") or ())
        container = self._snapshot(container_name)
        if container.network(network) is not None:
            return ActionOutcome(True, f"{container_name} is already on {network}; nothing to undo")
        self.docker.network_connect(network, container_name, aliases)
        return ActionOutcome(True, f"reconnected {container_name} to {network} (aliases {list(aliases)})")


class NetworkDelayAction(FailureAction):
    """Add latency and/or packet loss on the target's interface with `tc netem`, via a sidecar.

    A throwaway container sharing the target's network namespace (``--net container:<target>
    --cap-add NET_ADMIN``) installs the qdisc and exits; the qdisc lives on the target's
    interface, so it persists after the sidecar is gone. This is the "slow network" failure -
    distinct from pause, which freezes the process rather than the link.

    Params: ``delay_ms`` (default 200), ``loss_pct`` (default 0), ``iface`` (default eth0).
    """

    name = "network_delay"
    description = "Add latency/packet loss on the container's interface (tc netem sidecar)"

    def _iface(self) -> str:
        return str(self.params.get("iface") or NETEM_IFACE)

    def _image(self) -> str:
        return str(self.params.get("image") or NETEM_IMAGE)

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(False, f"{container_name} is not running", pre_state=pre)
        try:
            delay_ms = int(self.params.get("delay_ms", 200))
            loss_pct = float(self.params.get("loss_pct", 0))
        except (TypeError, ValueError) as exc:
            return ActionOutcome(False, f"bad netem params: {exc}", pre_state=pre)
        iface = self._iface()
        netem = ["delay", f"{delay_ms}ms"]
        if loss_pct > 0:
            netem += ["loss", f"{loss_pct}%"]
        command = ("tc", "qdisc", "add", "dev", iface, "root", "netem", *netem)
        self.docker.run_sidecar(self._image(), command, container_name, cap_add=("NET_ADMIN",))
        pre["iface"] = iface
        detail = f"netem on {container_name} {iface}: delay {delay_ms}ms" + (
            f", loss {loss_pct}%" if loss_pct > 0 else "")
        return ActionOutcome(True, detail, pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        iface = str((self._pre or {}).get("iface") or self._iface())
        command = ("tc", "qdisc", "del", "dev", iface, "root")
        try:
            self.docker.run_sidecar(self._image(), command, container_name, cap_add=("NET_ADMIN",))
        except DockerError as exc:
            # Deleting a qdisc that is already gone is success, not failure (idempotent rollback).
            return ActionOutcome(True, f"netem on {container_name} already cleared ({exc})")
        return ActionOutcome(True, f"removed netem qdisc on {container_name} {iface}")


class CpuThrottleAction(FailureAction):
    """Choke the target's CPU quota with `docker update --cpus`: a slow, starved service.

    Param: ``cpus`` (default 0.1 of a core). Rollback restores the original quota from pre_state
    (``--cpus 0`` when the container had no limit, which means unlimited again).
    """

    name = "cpu_throttle"
    description = "Restrict the container's CPU quota (docker update --cpus)"

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(False, f"{container_name} is not running", pre_state=pre)
        try:
            cpus = max(CPU_MIN_FRACTION, float(self.params.get("cpus", 0.1)))
        except (TypeError, ValueError) as exc:
            return ActionOutcome(False, f"bad cpus param: {exc}", pre_state=pre)
        pre["nano_cpus"] = container.nano_cpus
        self.docker.update(container_name, cpus=cpus)
        return ActionOutcome(True, f"throttled {container_name} to {cpus:g} CPU(s)", pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        nano = (self._pre or {}).get("nano_cpus")
        original = (int(nano) / 1_000_000_000) if nano else 0  # 0 = unlimited
        self.docker.update(container_name, cpus=original)
        detail = "restored original CPU limit" if nano else "restored unlimited CPU"
        return ActionOutcome(True, f"{container_name}: {detail}")


class MemoryLimitAction(FailureAction):
    """Cap the target's memory with `docker update --memory`, never below a floor.

    Param: ``memory`` (default '256m'). The floor (64 MiB) means the limit is tight enough to
    cause real pressure but not so tight the kernel OOM-kills the process the instant it is set.
    Rollback restores the original limit from pre_state.
    """

    name = "memory_limit"
    description = "Cap the container's memory (docker update --memory, with a floor)"

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(False, f"{container_name} is not running", pre_state=pre)
        try:
            requested = parse_memory(self.params.get("memory", "256m"))
        except (TypeError, ValueError) as exc:
            return ActionOutcome(False, f"bad memory param: {exc}", pre_state=pre)
        limit = max(MEMORY_FLOOR_BYTES, requested)
        pre["mem_limit"] = container.mem_limit
        self.docker.update(container_name, memory=limit)
        floored = " (raised to the 64 MiB floor)" if limit != requested else ""
        return ActionOutcome(True, f"capped {container_name} at {limit} bytes{floored}", pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        mem = (self._pre or {}).get("mem_limit")
        original = int(mem) if mem else 0  # 0 = unlimited
        self.docker.update(container_name, memory=original)
        detail = "restored original memory limit" if mem else "restored unlimited memory"
        return ActionOutcome(True, f"{container_name}: {detail}")


class FlappingAction(FailureAction):
    """Make the target flap: repeated short outages on a timer, not one clean failure.

    This is the failure that defeats naive health checks and retry logic - a service that is up,
    then down, then up again every few seconds. Params: ``mode`` ('pause' or 'kill', default
    'pause'), ``down_s`` (how long each outage lasts, default 1), ``interval_s`` (gap between
    outages, default 2). Rollback stops the loop and ensures the container is running.
    """

    name = "flapping"
    description = "Repeatedly take the container down and back up on a timer (pause or kill)"
    self_healing_expected = True

    def __init__(self, docker: Docker, project: str | None = None, **params: Any) -> None:
        super().__init__(docker, project, **params)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(False, f"{container_name} is not running", pre_state=pre)
        if self._thread is not None:
            return ActionOutcome(False, "flapping is already running", pre_state=pre)
        mode = str(self.params.get("mode", "pause"))
        if mode not in ("pause", "kill"):
            return ActionOutcome(
                False, f"flapping mode must be 'pause' or 'kill', not {mode!r}", pre_state=pre
            )
        down_s = max(0.1, float(self.params.get("down_s", 1)))
        interval_s = max(0.1, float(self.params.get("interval_s", 2)))
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._flap, args=(container_name, mode, down_s, interval_s),
            name=f"flap-{container_name}", daemon=True,
        )
        self._thread.start()
        return ActionOutcome(
            True, f"flapping {container_name} ({mode}, {down_s}s down / {interval_s}s up)", pre_state=pre
        )

    def rollback(self, container_name: str) -> ActionOutcome:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        detail = self._ensure_running(container_name)
        return ActionOutcome(True, f"stopped flapping {container_name}; {detail}")

    def _flap(self, container_name: str, mode: str, down_s: float, interval_s: float) -> None:
        while not self._stop.is_set():
            try:
                if mode == "pause":
                    self.docker.pause(container_name)
                    if self._stop.wait(down_s):
                        break
                    self.docker.unpause(container_name)
                else:  # kill: Docker's restart policy brings it back between cycles
                    self.docker.kill(container_name, "KILL")
            except DockerError:
                pass  # a transient docker error must not kill the loop; rollback still cleans up
            if self._stop.wait(interval_s):
                break


def spike_params(params: dict[str, Any]) -> tuple[int, int, int]:
    """Return (rps, users, time) or raise ValueError."""
    missing = [key for key in ("rps", "users", "time") if key not in params]
    if missing:
        raise ValueError(f"spike requires params {', '.join(missing)} (rps, users, time)")
    try:
        rps, users, hold = int(params["rps"]), int(params["users"]), int(params["time"])
    except (TypeError, ValueError) as exc:
        raise ValueError("spike params rps, users and time must be whole numbers") from exc
    if not 1 <= rps <= SPIKE_MAX_RPS:
        raise ValueError(f"spike rps must be from 1 to {SPIKE_MAX_RPS}")
    if not 1 <= users <= SPIKE_MAX_USERS:
        raise ValueError(f"spike users must be from 1 to {SPIKE_MAX_USERS}")
    if not 1 <= hold <= 600:
        raise ValueError("spike time must be from 1 to 600 seconds")
    return rps, users, hold


ACTIONS: dict[str, type[FailureAction]] = {
    action.name: action
    for action in (
        KillAction, StopAction, PauseAction, SpikeAction,
        NetworkPartitionAction, NetworkDelayAction, CpuThrottleAction, MemoryLimitAction,
        FlappingAction,
    )
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

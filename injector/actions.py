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
#: Bounds for a CPU throttle, in CPUs. Docker's own minimum is 0.01.
THROTTLE_MIN_CPUS, THROTTLE_MAX_CPUS = 0.01, 4.0


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
            "nano_cpus": container.nano_cpus,
            "mem_limit": container.mem_limit,
        }
        return self._pre

    def resume(self, pre_state: dict[str, Any] | None) -> None:
        """Hand a fresh action the pre-state a crashed run journaled, so its rollback restores that."""
        self._pre = pre_state

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


class ThrottleAction(FailureAction):
    """Cap the container's CPU in place (`docker update --cpus`): a starved dependency.

    The process keeps running, only slower, so this is the "degraded, not down" failure - the
    shape of a noisy neighbour or a CPU limit set too tight. Param ``cpus`` is the limit to impose
    and must be below any existing one (loosening a limit tests nothing). Nothing restarts, so no
    restart policy is involved; rollback puts back the limit recorded in the pre-state, which the
    journal keeps so `orchestrator recover` can restore it after a crash. Docker cannot remove a
    CPU limit in place, so "unlimited" is restored as a limit of every host CPU, which is the same.
    """

    name = "throttle"
    description = "Cap the container's CPU in place (starved dependency; slower, not down) (params: cpus)"

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        try:
            cpus = throttle_params(self.params)
        except ValueError as exc:
            return ActionOutcome(False, str(exc), pre_state=pre)
        host = self.docker.host_cpus()
        if not _unlimited(container.nano_cpus, host) and cpus >= container.cpus:
            return ActionOutcome(
                False,
                f"{container_name} is already limited to {container.cpus:g} CPUs; "
                f"a throttle must go below that",
                pre_state=pre,
            )
        self.docker.update_cpus(container_name, cpus)
        return ActionOutcome(
            True, f"limited {container_name} to {cpus:g} CPUs (was {_cpu_limit(container.nano_cpus, host)})",
            pre_state=pre,
        )

    def rollback(self, container_name: str) -> ActionOutcome:
        if not self._pre or "nano_cpus" not in self._pre:
            raise DockerError(
                f"the CPU limit {container_name} had before the throttle was not recorded; recreate it "
                f"from Compose (`docker compose up -d --no-deps <service>`) to restore the configured limit"
            )
        original, host = int(self._pre["nano_cpus"]), self.docker.host_cpus()

        def restored() -> bool:
            current = self._snapshot(container_name).nano_cpus
            both_unlimited = _unlimited(original, host) and _unlimited(current, host)
            return current == original or both_unlimited

        if restored():
            return ActionOutcome(
                True, f"{container_name} already has its original CPU limit; nothing to undo"
            )
        self.docker.update_cpus(container_name, host if _unlimited(original, host) else original / 1e9)
        if not restored():  # do not report a restore Docker did not make
            raise DockerError(
                f"{container_name} still has a CPU limit of "
                f"{_cpu_limit(self._snapshot(container_name).nano_cpus, host)} after rollback"
            )
        return ActionOutcome(
            True, f"restored the CPU limit of {container_name} to {_cpu_limit(original, host)}"
        )


class MemoryLimitAction(FailureAction):
    """Cap the container's memory in place (`docker update --memory`): memory pressure.

    The process keeps running under a tight cap; if it needs more than the cap the kernel OOM-kills
    it and the restart policy brings it back, which is itself part of what this measures. Param
    ``memory_mb`` is the cap to impose and must be below any existing limit. Rollback restores the
    limit recorded in the pre-state ("unlimited" as the host total, since Docker cannot remove a
    limit in place), re-inspects, and ensures the container is running after a possible OOM kill.
    """

    name = "memory_limit"
    description = "Cap the container's memory in place (memory pressure) (params: memory_mb)"

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        try:
            limit = memory_params(self.params) * MB
        except ValueError as exc:
            return ActionOutcome(False, str(exc), pre_state=pre)
        host = self.docker.host_memory()
        if not _mem_unlimited(container.mem_limit, host) and limit >= container.mem_limit:
            return ActionOutcome(
                False,
                f"{container_name} already has a memory limit of {_mem_label(container.mem_limit, host)}; "
                f"a memory_limit must go below that",
                pre_state=pre,
            )
        self.docker.update_memory(container_name, limit)
        return ActionOutcome(
            True,
            f"capped {container_name} at {limit // MB} MiB (was {_mem_label(container.mem_limit, host)})",
            pre_state=pre,
        )

    def rollback(self, container_name: str) -> ActionOutcome:
        if not self._pre or "mem_limit" not in self._pre:
            raise DockerError(
                f"the memory limit {container_name} had before the experiment was not recorded; "
                f"recreate it from Compose (`docker compose up -d --no-deps <service>`) to restore it"
            )
        original, host = int(self._pre["mem_limit"]), self.docker.host_memory()

        def restored() -> bool:
            current = self._snapshot(container_name).mem_limit
            return current == original or (_mem_unlimited(original, host) and _mem_unlimited(current, host))

        detail = f"{container_name} already has its original memory limit"
        if not restored():
            self.docker.update_memory(container_name, host if _mem_unlimited(original, host) else original)
            if not restored():  # do not report a restore Docker did not make
                raise DockerError(
                    f"{container_name} still has a memory limit of "
                    f"{_mem_label(self._snapshot(container_name).mem_limit, host)} after rollback"
                )
            detail = f"restored the memory limit of {container_name} to {_mem_label(original, host)}"
        return ActionOutcome(True, f"{detail}; {self._ensure_running(container_name)}")


class NetworkPartitionAction(FailureAction):
    """Disconnect the container from its Compose network: up, but reachable by no one.

    The process keeps running and Docker still reports it running, but every caller gets a name
    resolution or connection failure - the "up but isolated" split that a clean crash never
    reproduces. Rollback reconnects it with the aliases recorded in the pre-state (so the service
    name resolves again) and re-inspects that it is back on the network.
    """

    name = "network_partition"
    description = "Disconnect the container from its Compose network (up but unreachable)"

    def _network(self) -> str:
        return str(self.params.get("network") or (f"{self.project}_default" if self.project else "default"))

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        network = self._network()
        conn = container.network(network)
        if conn is None:
            on = ", ".join(c.name for c in container.networks) or "none"
            return ActionOutcome(
                False, f"{container_name} is not on network {network!r} (on: {on})", pre_state=pre
            )
        pre["network"] = network
        pre["aliases"] = list(conn.aliases)
        self.docker.network_disconnect(network, container_name)
        return ActionOutcome(True, f"disconnected {container_name} from {network}", pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        network = str((self._pre or {}).get("network") or self._network())
        aliases = tuple((self._pre or {}).get("aliases") or ())
        if self._snapshot(container_name).network(network) is not None:
            return ActionOutcome(True, f"{container_name} is already on {network}; nothing to undo")
        self.docker.network_connect(network, container_name, aliases)
        if self._snapshot(container_name).network(network) is None:
            raise DockerError(f"{container_name} is still off {network} after rollback")
        return ActionOutcome(True, f"reconnected {container_name} to {network} (aliases {list(aliases)})")


class NetworkDelayAction(FailureAction):
    """Add latency and packet loss on the container's interface with `tc netem`: a slow, lossy link.

    A throwaway sidecar sharing the target's network namespace installs the qdisc, which then lives
    on the target's interface after the sidecar exits. This is the "slow network" failure, distinct
    from pause (which freezes the process). Params ``delay_ms`` (NETEM_MAX_DELAY_MS) and ``loss_pct``
    (0-NETEM_MAX_LOSS_PCT). Rollback deletes the qdisc; deleting one already gone is still success.
    """

    name = "network_delay"
    description = "Add latency/packet loss on the container's interface (params: delay_ms, loss_pct)"

    def _iface(self) -> str:
        return str(self.params.get("iface") or "eth0")

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        try:
            delay_ms, loss_pct = netem_params(self.params)
        except ValueError as exc:
            return ActionOutcome(False, str(exc), pre_state=pre)
        iface = self._iface()
        netem = ["delay", f"{delay_ms}ms"]
        if loss_pct > 0:
            netem += ["loss", f"{loss_pct:g}%"]
        self.docker.tc(container_name, ("qdisc", "add", "dev", iface, "root", "netem", *netem))
        pre["iface"] = iface
        detail = f"netem on {container_name} {iface}: delay {delay_ms}ms" + (
            f", loss {loss_pct:g}%" if loss_pct > 0 else "")
        return ActionOutcome(True, detail, pre_state=pre)

    def rollback(self, container_name: str) -> ActionOutcome:
        iface = str((self._pre or {}).get("iface") or self._iface())
        try:
            self.docker.tc(container_name, ("qdisc", "del", "dev", iface, "root"))
        except DockerError as exc:
            # Deleting a qdisc that is already gone is success, not failure (idempotent rollback).
            return ActionOutcome(True, f"netem on {container_name} already cleared ({exc})")
        return ActionOutcome(True, f"removed the netem qdisc on {container_name} {iface}")


class FlappingAction(FailureAction):
    """Take the container down and back up repeatedly on a timer: intermittent flapping.

    This is the failure that defeats naive health checks and retries - up, then down, then up again
    every few seconds - rather than one clean outage. Params: ``down_s`` (each outage, default 2),
    ``up_s`` (gap between them, default 3), ``cycles`` (how many, default runs until rollback). It
    pauses/unpauses on a background thread; rollback stops the loop and ensures the container is up.
    """

    name = "flapping"
    description = "Pause and unpause the container repeatedly on a timer (params: down_s, up_s, cycles)"

    def __init__(self, docker: Docker, project: str | None = None, **params: Any) -> None:
        super().__init__(docker, project, **params)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def apply(self, container_name: str) -> ActionOutcome:
        container = self._snapshot(container_name)
        pre = self._record_pre(container)
        if not container.running:
            return ActionOutcome(
                False, f"{container_name} is not running (status {container.status})", pre_state=pre
            )
        if self._thread is not None:
            return ActionOutcome(False, "flapping is already running", pre_state=pre)
        try:
            down_s, up_s, cycles = flapping_params(self.params)
        except ValueError as exc:
            return ActionOutcome(False, str(exc), pre_state=pre)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._flap, args=(container_name, down_s, up_s, cycles),
            name=f"flap-{container_name}", daemon=True,
        )
        self._thread.start()
        count = "until rollback" if cycles == 0 else f"{cycles} cycle(s)"
        return ActionOutcome(
            True, f"flapping {container_name} ({down_s:g}s down / {up_s:g}s up, {count})", pre_state=pre
        )

    def rollback(self, container_name: str) -> ActionOutcome:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None
        return ActionOutcome(
            True, f"stopped flapping {container_name}; {self._ensure_running(container_name)}"
        )

    def _flap(self, container_name: str, down_s: float, up_s: float, cycles: int) -> None:
        done = 0
        while not self._stop.is_set() and (cycles == 0 or done < cycles):
            try:
                if not self._snapshot(container_name).paused:
                    self.docker.pause(container_name)
                if self._stop.wait(down_s):
                    break
                if self._snapshot(container_name).paused:
                    self.docker.unpause(container_name)
            except DockerError:
                pass  # a transient docker error must not kill the loop; rollback still cleans up
            done += 1
            if self._stop.wait(up_s):
                break


MB = 1024 * 1024
#: Bounds for memory_limit, in MiB. The floor keeps a cap from OOM-killing the process the instant
#: it is set; the ceiling keeps a "limit" from being so high it constrains nothing.
MEMORY_MIN_MB, MEMORY_MAX_MB = 16, 4096
#: Bounds for network_delay.
NETEM_MAX_DELAY_MS = 10000
NETEM_MAX_LOSS_PCT = 100.0
#: Bounds for flapping, in seconds, and the maximum number of cycles.
FLAP_MIN_S, FLAP_MAX_S = 0.5, 60.0
FLAP_MAX_CYCLES = 100


def _mem_unlimited(mem_limit: int, host_memory: int) -> bool:
    return mem_limit == 0 or mem_limit >= host_memory


def _mem_label(mem_limit: int, host_memory: int) -> str:
    return "unlimited" if _mem_unlimited(mem_limit, host_memory) else f"{mem_limit // MB} MiB"


def memory_params(params: dict[str, Any]) -> int:
    """Return the memory cap in MiB, or raise ValueError."""
    if "memory_mb" not in params:
        raise ValueError("memory_limit requires params.memory_mb (the cap in MiB, e.g. 128)")
    try:
        memory_mb = int(params["memory_mb"])
    except (TypeError, ValueError) as exc:
        raise ValueError("memory_limit params.memory_mb must be a whole number of MiB") from exc
    if not MEMORY_MIN_MB <= memory_mb <= MEMORY_MAX_MB:
        raise ValueError(f"memory_limit memory_mb must be from {MEMORY_MIN_MB} to {MEMORY_MAX_MB}")
    return memory_mb


def netem_params(params: dict[str, Any]) -> tuple[int, float]:
    """Return (delay_ms, loss_pct), or raise ValueError. At least one must be non-zero."""
    try:
        delay_ms = int(params.get("delay_ms", 0))
        loss_pct = float(params.get("loss_pct", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("network_delay params delay_ms and loss_pct must be numbers") from exc
    if not 0 <= delay_ms <= NETEM_MAX_DELAY_MS:
        raise ValueError(f"network_delay delay_ms must be from 0 to {NETEM_MAX_DELAY_MS}")
    if not 0 <= loss_pct <= NETEM_MAX_LOSS_PCT:
        raise ValueError(f"network_delay loss_pct must be from 0 to {NETEM_MAX_LOSS_PCT:g}")
    if delay_ms == 0 and loss_pct == 0:
        raise ValueError("network_delay needs a non-zero delay_ms or loss_pct")
    return delay_ms, loss_pct


def flapping_params(params: dict[str, Any]) -> tuple[float, float, int]:
    """Return (down_s, up_s, cycles); cycles 0 means run until rollback. Raises ValueError."""
    try:
        down_s = float(params.get("down_s", 2))
        up_s = float(params.get("up_s", 3))
        cycles = int(params.get("cycles", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("flapping params down_s, up_s and cycles must be numbers") from exc
    for label, value in (("down_s", down_s), ("up_s", up_s)):
        if not FLAP_MIN_S <= value <= FLAP_MAX_S:
            raise ValueError(f"flapping {label} must be from {FLAP_MIN_S:g} to {FLAP_MAX_S:g} seconds")
    if not 0 <= cycles <= FLAP_MAX_CYCLES:
        raise ValueError(f"flapping cycles must be from 0 (until rollback) to {FLAP_MAX_CYCLES}")
    return down_s, up_s, cycles


def _unlimited(nano_cpus: int, host_cpus: int) -> bool:
    return nano_cpus == 0 or nano_cpus >= host_cpus * 1_000_000_000


def _cpu_limit(nano_cpus: int, host_cpus: int) -> str:
    return "unlimited" if _unlimited(nano_cpus, host_cpus) else f"{nano_cpus / 1e9:g} CPUs"


def throttle_params(params: dict[str, Any]) -> float:
    """Return the CPU limit to impose or raise ValueError."""
    if "cpus" not in params:
        raise ValueError("throttle requires params.cpus (the CPU limit to impose, e.g. 0.1)")
    try:
        cpus = float(params["cpus"])
    except (TypeError, ValueError) as exc:
        raise ValueError("throttle params.cpus must be a number") from exc
    if not THROTTLE_MIN_CPUS <= cpus <= THROTTLE_MAX_CPUS:
        raise ValueError(f"throttle cpus must be from {THROTTLE_MIN_CPUS:g} to {THROTTLE_MAX_CPUS:g}")
    return cpus


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
        KillAction, StopAction, PauseAction, SpikeAction, ThrottleAction,
        MemoryLimitAction, NetworkPartitionAction, NetworkDelayAction, FlappingAction,
    )
}


def build_action(action_name: str, docker: Docker, project: str | None = None,
                 **params: Any) -> FailureAction:
    try:
        return ACTIONS[action_name](docker, project, **params)
    except KeyError:
        raise ValueError(f"unknown action {action_name!r}; available: {', '.join(sorted(ACTIONS))}") from None


def describe_actions() -> str:
    return "\n".join(f"  {name:<8} {cls.description}" for name, cls in sorted(ACTIONS.items()))


__all__ = ["ACTIONS", "ActionOutcome", "DockerCli", "FailureAction", "build_action", "describe_actions"]

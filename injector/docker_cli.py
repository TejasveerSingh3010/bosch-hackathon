"""Thin wrapper around the `docker` CLI.

The CLI is used rather than the `docker` Python SDK because the monitor already proves the CLI
works on this project's Windows + Docker Desktop setup, and because every call here is a fixed
argument list - no shell string is ever built, so a container name can never be injected as flags.

`DockerCli` is small on purpose: tests substitute `tests.fake_docker.FakeDocker`, which implements
the same methods, so the injector and runner can be tested without Docker.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"


class DockerError(RuntimeError):
    """A docker command failed, or the docker CLI/daemon is unreachable."""


@dataclass(frozen=True)
class NetworkConn:
    """One network a container is attached to, with the aliases other services resolve it by.

    The aliases matter for rollback: `docker network disconnect` drops them, so a
    `network_partition` must record them here and pass them back to `network connect --alias`,
    or the reconnected container would be unreachable by its service name.
    """

    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Container:
    """The parts of `docker inspect` output this project needs."""

    name: str
    service: str | None
    project: str | None
    status: str  # running | exited | paused | created | restarting | dead
    running: bool
    paused: bool
    restarting: bool
    exit_code: int | None
    restart_count: int
    restart_policy: str  # no | always | unless-stopped | on-failure
    started_at: str | None
    #: HostConfig.NanoCpus (CPU quota in billionths of a core; 0/None means unlimited) and
    #: HostConfig.Memory (byte limit; 0/None means unlimited). Recorded so cpu_throttle /
    #: memory_limit can restore the original limit on rollback.
    nano_cpus: int | None = None
    mem_limit: int | None = None
    #: Networks the container is on, for network_partition rollback.
    networks: tuple[NetworkConn, ...] = ()

    @property
    def self_healing(self) -> bool:
        """True when Docker will restart this container by itself after a crash."""
        return self.restart_policy in ("always", "unless-stopped", "on-failure")

    def network(self, name: str) -> NetworkConn | None:
        """The connection to `name`, or None if the container is not on that network."""
        for conn in self.networks:
            if conn.name == name:
                return conn
        return None

    @classmethod
    def from_inspect(cls, payload: dict[str, Any]) -> Container:
        state = payload.get("State") or {}
        config = payload.get("Config") or {}
        labels = config.get("Labels") or {}
        host_config = payload.get("HostConfig") or {}
        policy = (host_config.get("RestartPolicy") or {}).get("Name") or "no"
        status = state.get("Status", "unknown")
        nets = (payload.get("NetworkSettings") or {}).get("Networks") or {}
        networks = tuple(
            NetworkConn(name=net, aliases=tuple(data.get("Aliases") or []))
            for net, data in nets.items()
        )
        return cls(
            name=(payload.get("Name") or "").lstrip("/"),
            service=labels.get(COMPOSE_SERVICE_LABEL),
            project=labels.get(COMPOSE_PROJECT_LABEL),
            status=status,
            running=bool(state.get("Running")),
            paused=bool(state.get("Paused")),
            restarting=bool(state.get("Restarting")),
            exit_code=state.get("ExitCode") if not state.get("Running") else None,
            restart_count=int(payload.get("RestartCount", 0) or 0),
            restart_policy=policy,
            started_at=state.get("StartedAt"),
            nano_cpus=int(host_config.get("NanoCpus") or 0) or None,
            mem_limit=int(host_config.get("Memory") or 0) or None,
            networks=networks,
        )


class Docker(Protocol):
    """What the rest of the project needs from Docker.

    Both `DockerCli` and `tests.fake_docker.FakeDocker` satisfy this, which is what lets the
    injector, safety layer and runner be tested without Docker installed.
    """

    def list_containers(self, project: str | None = ...) -> list[Container]: ...
    def get(self, name: str, project: str | None = ...) -> Container: ...
    def kill(self, name: str, signal: str = ...) -> None: ...
    def stop(self, name: str, grace_s: int = ...) -> None: ...
    def start(self, name: str) -> None: ...
    def pause(self, name: str) -> None: ...
    def unpause(self, name: str) -> None: ...
    # Added for the day-2 fault actions (network_partition, network_delay, cpu_throttle,
    # memory_limit). All read-or-tweak operations; none of them can escape the safety layer,
    # which still validates every plan before the runner builds an action.
    def update(self, name: str, cpus: float | None = ..., memory: int | None = ...) -> None: ...
    def network_disconnect(self, network: str, name: str, force: bool = ...) -> None: ...
    def network_connect(self, network: str, name: str, aliases: tuple[str, ...] = ...) -> None: ...
    def logs(self, name: str, since: str | None = ..., tail: int = ...) -> str: ...
    def run_sidecar(
        self,
        image: str,
        command: tuple[str, ...],
        target: str,
        cap_add: tuple[str, ...] = ...,
        name: str | None = ...,
        remove: bool = ...,
    ) -> str: ...


class DockerCli:
    """Runs fixed `docker` argument lists and parses the JSON output."""

    def __init__(self, binary: str = "docker", timeout: float = 20.0) -> None:
        self.binary = binary
        self.timeout = timeout

    def _run(self, args: list[str], timeout: float | None = None) -> str:
        try:
            proc = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise DockerError(f"docker CLI not found on PATH ({self.binary})") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"docker {' '.join(args)} timed out after {timeout or self.timeout}s") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise DockerError(f"docker {' '.join(args)} failed: {detail}")
        return proc.stdout

    # ---- reads ----
    def list_containers(self, project: str | None = None) -> list[Container]:
        """Every container (including stopped ones), optionally filtered to one Compose project."""
        ids = self._run(["ps", "-a", "-q"]).split()
        if not ids:
            return []
        payload = json.loads(self._run(["inspect", *ids]))
        containers = [Container.from_inspect(item) for item in payload]
        if project:
            containers = [c for c in containers if c.project == project]
        return containers

    def get(self, name: str, project: str | None = None) -> Container:
        for container in self.list_containers(project):
            if container.name == name:
                return container
        raise DockerError(f"container {name!r} not found")

    # ---- writes (each one is a single, explicit action) ----
    def kill(self, name: str, signal: str = "KILL") -> None:
        self._run(["kill", "--signal", signal, name])

    def stop(self, name: str, grace_s: int = 0) -> None:
        self._run(["stop", "--time", str(int(grace_s)), name], timeout=max(self.timeout, grace_s + 20))

    def start(self, name: str) -> None:
        self._run(["start", name], timeout=max(self.timeout, 40))

    def pause(self, name: str) -> None:
        self._run(["pause", name])

    def unpause(self, name: str) -> None:
        self._run(["unpause", name])

    def update(self, name: str, cpus: float | None = None, memory: int | None = None) -> None:
        """Change resource limits on a live container (`docker update`). No restart."""
        args = ["update"]
        if cpus is not None:
            args += ["--cpus", f"{cpus:g}"]
        if memory is not None:
            # Set swap equal to memory so the limit is a true RAM cap, not RAM+swap.
            args += ["--memory", str(int(memory)), "--memory-swap", str(int(memory))]
        if len(args) == 1:
            return
        args.append(name)
        self._run(args)

    def network_disconnect(self, network: str, name: str, force: bool = False) -> None:
        args = ["network", "disconnect"]
        if force:
            args.append("--force")
        self._run([*args, network, name])

    def network_connect(self, network: str, name: str, aliases: tuple[str, ...] = ()) -> None:
        args = ["network", "connect"]
        for alias in aliases:
            args += ["--alias", alias]
        self._run([*args, network, name])

    def logs(self, name: str, since: str | None = None, tail: int = 200) -> str:
        """Container logs for the evidence window. stdout and stderr are combined, because many
        of these services log to stderr; the caller wants whatever the container actually printed.
        """
        args = ["logs"]
        if since is not None:
            args += ["--since", str(since)]
        if tail is not None:
            args += ["--tail", str(int(tail))]
        args.append(name)
        try:
            proc = subprocess.run(
                [self.binary, *args],
                capture_output=True, text=True, timeout=self.timeout,
                check=False, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as exc:
            raise DockerError(f"docker CLI not found on PATH ({self.binary})") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"docker logs {name} timed out") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise DockerError(f"docker logs {name} failed: {detail}")
        return (proc.stdout or "") + (proc.stderr or "")

    def run_sidecar(
        self,
        image: str,
        command: tuple[str, ...],
        target: str,
        cap_add: tuple[str, ...] = (),
        name: str | None = None,
        remove: bool = True,
    ) -> str:
        """Run a throwaway container that shares the target's network namespace.

        Used by network_delay to run `tc netem` against the target's interface without needing a
        shell inside the (minimal, often shell-less) target image. The sidecar sets up or tears
        down the qdisc and exits; with `remove=True` it leaves nothing behind.
        """
        args = ["run"]
        if remove:
            args.append("--rm")
        if name:
            args += ["--name", name]
        args += ["--network", f"container:{target}"]
        for cap in cap_add:
            args += ["--cap-add", cap]
        args.append(image)
        args += list(command)
        return self._run(args, timeout=max(self.timeout, 60))

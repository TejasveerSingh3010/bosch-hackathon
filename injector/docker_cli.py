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
    nano_cpus: int = 0  # CPU limit in billionths of a CPU; 0 = no limit

    @property
    def cpus(self) -> float:
        """The CPU limit as `docker update --cpus` takes it; 0.0 means unlimited."""
        return self.nano_cpus / 1e9

    @property
    def self_healing(self) -> bool:
        """True when Docker will restart this container by itself after a crash."""
        return self.restart_policy in ("always", "unless-stopped", "on-failure")

    @classmethod
    def from_inspect(cls, payload: dict[str, Any]) -> Container:
        state = payload.get("State") or {}
        config = payload.get("Config") or {}
        labels = config.get("Labels") or {}
        host_config = payload.get("HostConfig") or {}
        policy = (host_config.get("RestartPolicy") or {}).get("Name") or "no"
        status = state.get("Status", "unknown")
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
            nano_cpus=int(host_config.get("NanoCpus") or 0),
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
    def update_cpus(self, name: str, cpus: float) -> None: ...
    def host_cpus(self) -> int: ...


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

    def update_cpus(self, name: str, cpus: float) -> None:
        """Change the CPU limit of a live container in place.

        Docker treats `--cpus 0` as "leave unchanged", so a limit cannot be removed this way;
        a limit of `host_cpus()` is the equivalent of none.
        """
        self._run(["update", "--cpus", f"{cpus:g}", name])

    def host_cpus(self) -> int:
        return int(self._run(["info", "--format", "{{.NCPU}}"]).strip())

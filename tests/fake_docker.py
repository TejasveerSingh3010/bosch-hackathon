"""An in-memory stand-in for the docker CLI, so the whole system can be tested without Docker.

It implements the same methods as `injector.docker_cli.DockerCli` and models the behaviour that
actually matters for correctness:

  * `restart: unless-stopped` restarts a container after `kill` (so rollback must cope with a
    container that is already running again) but **not** after `stop`;
  * paused containers are running but frozen;
  * `RestartCount` goes up on a policy restart, which is how the runner detects self-healing;
  * every command can be made to fail, so the error paths are testable too.
"""
from __future__ import annotations

from dataclasses import dataclass

from injector.docker_cli import Container, DockerError


@dataclass
class FakeContainer:
    name: str
    service: str
    project: str = "online-boutique"
    status: str = "running"
    paused: bool = False
    restart_policy: str = "unless-stopped"
    restart_count: int = 0
    exit_code: int | None = None
    started_at: str | None = "2026-10-10T06:00:00.000000000Z"

    @property
    def running(self) -> bool:
        return self.status == "running"

    def to_container(self) -> Container:
        return Container(
            name=self.name,
            service=self.service,
            project=self.project,
            status="paused" if self.paused else self.status,
            running=self.running,
            paused=self.paused,
            restarting=self.status == "restarting",
            exit_code=None if self.running else self.exit_code,
            restart_count=self.restart_count,
            restart_policy=self.restart_policy,
            started_at=self.started_at if self.running else None,
        )


class FakeDocker:
    """Drop-in replacement for DockerCli in tests."""

    def __init__(self, containers: list[FakeContainer] | None = None,
                 self_heal_on_kill: bool = True) -> None:
        self.containers: dict[str, FakeContainer] = {c.name: c for c in (containers or default_stack())}
        self.calls: list[tuple[str, str]] = []
        self.self_heal_on_kill = self_heal_on_kill
        #: Set method names here to make them raise, e.g. docker.fail_on = {"start"}.
        self.fail_on: set[str] = set()

    # ---- internals ----
    def _get(self, name: str) -> FakeContainer:
        if name not in self.containers:
            raise DockerError(f"container {name!r} not found")
        return self.containers[name]

    def _record(self, method: str, name: str) -> None:
        self.calls.append((method, name))
        if method in self.fail_on:
            raise DockerError(f"simulated failure of docker {method} {name}")

    # ---- DockerCli interface ----
    def list_containers(self, project: str | None = None) -> list[Container]:
        items = self.containers.values()
        if project:
            items = [c for c in items if c.project == project]
        return [c.to_container() for c in items]

    def get(self, name: str, project: str | None = None) -> Container:
        return self._get(name).to_container()

    def kill(self, name: str, signal: str = "KILL") -> None:
        self._record("kill", name)
        container = self._get(name)
        if not container.running:
            raise DockerError(f"cannot kill {name}: container is not running")
        container.status = "exited"
        container.exit_code = 137
        container.paused = False
        if self.self_heal_on_kill and container.restart_policy in ("always", "unless-stopped", "on-failure"):
            # Docker's restart policy brings a crashed container back by itself.
            container.status = "running"
            container.exit_code = None
            container.restart_count += 1

    def stop(self, name: str, grace_s: int = 0) -> None:
        self._record("stop", name)
        container = self._get(name)
        container.status = "exited"
        container.exit_code = 0
        container.paused = False  # `unless-stopped` does not restart a deliberate stop

    def start(self, name: str) -> None:
        self._record("start", name)
        container = self._get(name)
        container.status = "running"
        container.exit_code = None
        container.paused = False

    def pause(self, name: str) -> None:
        self._record("pause", name)
        container = self._get(name)
        if not container.running:
            raise DockerError(f"cannot pause {name}: container is not running")
        container.paused = True

    def unpause(self, name: str) -> None:
        self._record("unpause", name)
        container = self._get(name)
        if not container.paused:
            raise DockerError(f"cannot unpause {name}: container is not paused")
        container.paused = False


def default_stack(project: str = "online-boutique") -> list[FakeContainer]:
    """The real Online Boutique service set, named the way Compose names containers."""
    services = [
        "frontend", "cartservice", "productcatalogservice", "currencyservice", "paymentservice",
        "shippingservice", "emailservice", "checkoutservice", "recommendationservice", "adservice",
        "redis-cart", "loadgenerator",
    ]
    return [FakeContainer(name=f"{project}-{s}-1", service=s, project=project) for s in services]


def allowed_container_names(project: str = "online-boutique") -> list[str]:
    """The allow-list target.yaml would hold: every app container, never the load generator."""
    return sorted(c.name for c in default_stack(project) if c.service != "loadgenerator")

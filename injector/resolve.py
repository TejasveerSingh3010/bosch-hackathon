"""Resolve an experiment's `target` to exactly one running container.

Experiments name a **service** (`redis-cart`, as in docs/experiment.example.json) while
target.yaml's `allowed_containers` lists **container** names
(`online-boutique-redis-cart-1`). This module bridges the two, and refuses to guess: if a
target matches several containers it raises rather than picking one, because injecting into
the wrong container would invalidate the experiment.
"""
from __future__ import annotations

from .docker_cli import Container, Docker


class TargetError(RuntimeError):
    """The target does not resolve to exactly one container."""


def resolve_target(containers: list[Container], target: str) -> Container:
    """Find the one container a target names, by Compose service name or container name.

    Matching order: exact container name, then Compose service name, then a unique
    case-insensitive substring of a container name (so `redis` finds `...-redis-cart-1`).
    """
    if not containers:
        raise TargetError("no containers found - is the application running?")

    for container in containers:
        if container.name == target:
            return container

    by_service = [c for c in containers if c.service == target]
    if len(by_service) == 1:
        return by_service[0]
    if len(by_service) > 1:
        names = ", ".join(sorted(c.name for c in by_service))
        raise TargetError(f"service {target!r} has several containers ({names}); name one exactly")

    lowered = target.lower()
    fuzzy = [c for c in containers if lowered in c.name.lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        names = ", ".join(sorted(c.name for c in fuzzy))
        raise TargetError(f"target {target!r} is ambiguous ({names}); name one exactly")

    known = ", ".join(sorted({c.service or c.name for c in containers}))
    raise TargetError(f"target {target!r} not found. Known services: {known}")


def resolve_many(docker: Docker, targets: list[str], project: str | None = None) -> dict[str, Container]:
    """Resolve several targets with a single `docker inspect` call."""
    containers = docker.list_containers(project)  # type: ignore[attr-defined]
    return {target: resolve_target(containers, target) for target in targets}

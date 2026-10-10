"""What the agents' tools act on. Built once per run; tests substitute fakes for each part."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from injector.docker_cli import Docker
from orchestrator.runner import ExperimentRunner
from remediation.agent import Proposer
from remediation.recommend import Build
from remediation.source import build_check
from safety.limits import TargetConfig


@dataclass
class Deps:
    config: TargetConfig
    docker: Docker
    #: Created on first use, so a run that never reaches remediation needs no LLM key.
    make_proposer: Callable[[], Proposer]
    build: Build | None = build_check
    make_runner: Callable[[Docker, TargetConfig], ExperimentRunner] = ExperimentRunner
    services: Callable[[], dict[str, dict[str, Any]]] = field(default=lambda: {})

    @cached_property
    def proposer(self) -> Proposer:
        return self.make_proposer()

    @cached_property
    def runner(self) -> ExperimentRunner:
        return self.make_runner(self.docker, self.config)

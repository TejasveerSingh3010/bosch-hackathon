"""What the agents' tools act on. Built once per run; tests substitute fakes for each part."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from injector.docker_cli import Docker
from orchestrator.runner import ExperimentRunner
from remediation.agent import Proposer
from remediation.recommend import Build
from remediation.source import build_check
from safety.limits import TargetConfig

if TYPE_CHECKING:
    from reporter.service import Reporter


@dataclass
class Deps:
    config: TargetConfig
    docker: Docker
    #: Created on first use, so a run that never reaches remediation needs no LLM key.
    make_proposer: Callable[[], Proposer]
    build: Build | None = build_check
    make_runner: Callable[[Docker, TargetConfig], ExperimentRunner] = ExperimentRunner
    services: Callable[[], dict[str, dict[str, Any]]] = field(default=lambda: {})
    #: Where Agent 2 writes its reports and the remediator reads them (None = reports/ at the repo root).
    reports_dir: Path | None = None
    #: Override to substitute a fake reporter in tests; None builds the real Agent 2 from config.
    make_reporter: Callable[[Deps], Reporter] | None = None

    @cached_property
    def proposer(self) -> Proposer:
        return self.make_proposer()

    @cached_property
    def runner(self) -> ExperimentRunner:
        return self.make_runner(self.docker, self.config)

    @cached_property
    def reporter(self) -> Reporter:
        if self.make_reporter is not None:
            return self.make_reporter(self)
        from reporter.service import Reporter

        return Reporter(self.config.results_dir, reports_dir=self.reports_dir,
                        project=self.config.compose_project)

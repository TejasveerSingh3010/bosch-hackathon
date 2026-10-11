"""The constrained tool interface: the only way Agent 1 can act on the system.

Every tool takes structured, validated arguments and returns a structured result. There is no tool
that runs an arbitrary shell command, and none that reaches Docker except through the runner, which
re-validates every plan. The eight injection tools (one per catalogue attack, each usable on any
allowed service) share one path:

    build plan -> policy check -> human approval -> runner.run -> audit

A plan that fails the policy check is never offered for approval; a plan that is not approved is
never run. The read-only tools (list/health/details/results/recovery) are safe and need no approval.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from injector.docker_cli import Docker, DockerError
from orchestrator.runner import ExperimentRunner
from safety.limits import TargetConfig
from safety.telemetry import TelemetryReader, TelemetryUnavailable, healthy, is_degraded
from schemas import ExperimentResult

from .approval import Approver, DenyAll
from .audit import Auditor
from .catalogue import ATTACK_NAMES, CATALOGUE, DEFAULT_DURATION_S, get_attack
from .plan import AttackPlan
from .policy import check_plan


def allowed_services(config: TargetConfig) -> list[str]:
    """The Compose services behind target.yaml's allowed containers (never loadgenerator)."""
    project, services = config.compose_project, []
    for container in config.allowed_containers:
        name = container
        if project and name.startswith(f"{project}-"):
            name = name[len(project) + 1:]
        service = re.sub(r"-\d+$", "", name)
        if service not in services:
            services.append(service)
    return services


@dataclass
class ToolResult:
    """What an injection tool returns: one of ran / rejected / not_approved / error."""

    status: str
    experiment_id: str
    detail: str
    verdict: str | None = None
    reasons: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "experiment_id": self.experiment_id,
                               "detail": self.detail}
        if self.verdict is not None:
            out["verdict"] = self.verdict
        if self.reasons:
            out["reasons"] = self.reasons
        return out


class ToolInterface:
    """The planner's tools. One instance per session; holds the approver and the audit trail."""

    def __init__(
        self,
        config: TargetConfig,
        docker: Docker,
        auditor: Auditor,
        approver: Approver | None = None,
        telemetry: TelemetryReader | None = None,
        make_runner: Callable[[Docker, TargetConfig], ExperimentRunner] = ExperimentRunner,
        skip_health_check: bool = False,
    ) -> None:
        self.config = config
        self.docker = docker
        self.auditor = auditor
        self.approver = approver or DenyAll()
        self.telemetry = telemetry or TelemetryReader(Path(config.results_dir))
        self.make_runner = make_runner
        self.skip_health_check = skip_health_check

    # ------------------------------------------------------------------ read-only tools
    def list_experiments(self) -> dict[str, Any]:
        """The eight attack types and the services each one may run on."""
        self.auditor.tool_request("list_experiments", {})
        return {
            "attacks": [
                {"action": name, "title": a.title, "description": a.description,
                 "params": a.params_help or "none"}
                for name, a in CATALOGUE.items()
            ],
            "targets": allowed_services(self.config),
            "note": "any attack may target any listed service; spike is intentionally excluded",
        }

    def get_experiment_details(self, action: str) -> dict[str, Any]:
        """The hypothesis, expected observations, recovery criteria and parameters of one attack."""
        self.auditor.tool_request("get_experiment_details", {"action": action})
        try:
            a = get_attack(action)
        except KeyError as exc:
            return {"error": str(exc)}
        return {
            "action": a.action, "title": a.title, "description": a.description,
            "params": a.params_help or "none", "hypothesis": a.hypothesis,
            "expected_observations": a.expected_observations, "recovery_criteria": a.recovery_criteria,
            "self_heal_expected": a.self_heal_expected,
        }

    def get_cluster_health(self) -> dict[str, Any]:
        """A snapshot of the monitored cluster: fresh, degraded, which containers are not ready."""
        self.auditor.tool_request("get_cluster_health", {})
        try:
            sample = self.telemetry.latest()
            age = self.telemetry.age_seconds(sample)
        except (TelemetryUnavailable, ValueError, KeyError) as exc:
            return {"available": False, "reason": str(exc)}
        summary = sample.get("summary") or {}
        return {
            "available": True,
            "fresh": age <= 15,
            "age_s": round(age, 1),
            "degraded": is_degraded(sample),
            "healthy": healthy(sample),
            "error_rate_pct": summary.get("error_rate_pct"),
            "p95_latency_ms": summary.get("p95_latency_ms"),
            "not_ready": list(summary.get("not_ready") or []),
            "ready": f"{summary.get('ready_pods')}/{summary.get('total_pods')}",
        }

    def get_experiment_results(self, experiment_id: str) -> dict[str, Any]:
        """The recorded result of a finished experiment (results/<id>.json), summarised."""
        self.auditor.tool_request("get_experiment_results", {"experiment_id": experiment_id})
        path = Path(self.config.results_dir) / f"{experiment_id}.json"
        if not path.exists():
            return {"found": False, "experiment_id": experiment_id}
        try:
            result = ExperimentResult.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return {"found": True, "experiment_id": experiment_id, "error": f"unreadable: {exc}"}
        return {
            "found": True, "experiment_id": experiment_id, "verdict": result.verdict,
            "abort_reason": result.abort_reason, "self_healed": result.self_healed,
            "detection_seconds": result.detection_seconds, "impact_seconds": result.impact_seconds,
            "recovery_seconds": result.recovery_seconds,
            "during_error_pct_max": result.during.error_rate_pct_max,
            "during_p95_ms_max": result.during.p95_ms_max,
            "observations": result.observations,
        }

    def check_recovery(self) -> dict[str, Any]:
        """Whether the system is clean to run another experiment: no open journal, telemetry healthy."""
        self.auditor.tool_request("check_recovery", {})
        runner = self.make_runner(self.docker, self.config)
        pending = runner.journal.read()
        try:
            sample = self.telemetry.latest()
            is_healthy = healthy(sample)
        except (TelemetryUnavailable, ValueError, KeyError):
            is_healthy = False
        clean = pending is None and is_healthy
        out: dict[str, Any] = {"clean": clean, "healthy": is_healthy, "pending_experiment": None}
        if pending is not None:
            out["pending_experiment"] = {"id": pending.experiment_id, "action": pending.action,
                                         "container": pending.container}
            out["hint"] = "run `python -m orchestrator recover` before injecting again"
        return out

    # ------------------------------------------------------------------ injection tools (8)
    def inject_kill(self, target: str, duration_s: int = DEFAULT_DURATION_S,
                    hypothesis: str = "") -> dict[str, Any]:
        """Kill (SIGKILL) `target`: a sudden crash. Measures automatic restart and recovery."""
        return self._inject("kill", target, {}, duration_s, hypothesis)

    def inject_stop(self, target: str, duration_s: int = DEFAULT_DURATION_S,
                    hypothesis: str = "") -> dict[str, Any]:
        """Stop `target` for the duration: a lasting outage. Measures fail-fast / fallback behaviour."""
        return self._inject("stop", target, {}, duration_s, hypothesis)

    def inject_pause(self, target: str, duration_s: int = DEFAULT_DURATION_S,
                     hypothesis: str = "") -> dict[str, Any]:
        """Pause `target`: it is up but answers nothing. Measures whether callers time out or hang."""
        return self._inject("pause", target, {}, duration_s, hypothesis)

    def inject_cpu_throttle(self, target: str, cpus: float, duration_s: int = DEFAULT_DURATION_S,
                            hypothesis: str = "") -> dict[str, Any]:
        """Cap `target` at `cpus` CPUs: a starved, slow dependency (degraded, not down)."""
        return self._inject("throttle", target, {"cpus": cpus}, duration_s, hypothesis)

    def inject_memory_limit(self, target: str, memory_mb: int, duration_s: int = DEFAULT_DURATION_S,
                            hypothesis: str = "") -> dict[str, Any]:
        """Cap `target` at `memory_mb` MiB: memory pressure, possibly an OOM kill and restart."""
        return self._inject("memory_limit", target, {"memory_mb": memory_mb}, duration_s, hypothesis)

    def inject_network_partition(self, target: str, duration_s: int = DEFAULT_DURATION_S,
                                 hypothesis: str = "") -> dict[str, Any]:
        """Disconnect `target` from its network: up but reachable by no one."""
        return self._inject("network_partition", target, {}, duration_s, hypothesis)

    def inject_network_delay(self, target: str, delay_ms: int = 200, loss_pct: float = 0.0,
                             duration_s: int = DEFAULT_DURATION_S, hypothesis: str = "") -> dict[str, Any]:
        """Add `delay_ms` latency and `loss_pct` packet loss on `target`'s link: a slow, lossy network."""
        return self._inject("network_delay", target, {"delay_ms": delay_ms, "loss_pct": loss_pct},
                            duration_s, hypothesis)

    def inject_flapping(self, target: str, down_s: float = 2.0, up_s: float = 3.0, cycles: int = 0,
                        duration_s: int = DEFAULT_DURATION_S, hypothesis: str = "") -> dict[str, Any]:
        """Flap `target`: pause/unpause repeatedly (`down_s` down, `up_s` up, `cycles` times)."""
        return self._inject("flapping", target, {"down_s": down_s, "up_s": up_s, "cycles": cycles},
                            duration_s, hypothesis)

    # ------------------------------------------------------------------ shared injection path
    def _inject(self, action: str, target: str, params: dict[str, Any], duration_s: int,
                hypothesis: str) -> dict[str, Any]:
        self.auditor.tool_request(f"inject_{action}", {"target": target, "params": params,
                                                       "duration_s": duration_s})
        # 1. build the plan (rejects unknown attack / bad params before anything else)
        try:
            plan = AttackPlan.build(action, target, self.config.app_name, params, duration_s, hypothesis)
        except (KeyError, ValueError) as exc:
            return ToolResult("error", f"{action}-{target}", f"could not build a plan: {exc}").as_dict()
        self.auditor.plan(plan)

        # 2. independent policy check
        containers = self.docker.list_containers(self.config.compose_project)  # type: ignore[attr-defined]
        telemetry = None if self.skip_health_check else self.telemetry
        decision = check_plan(plan, self.config, containers, telemetry, self.skip_health_check)
        self.auditor.policy(decision)
        if not decision.allowed:
            return ToolResult("rejected", plan.experiment_id, decision.summary,
                              reasons=decision.reasons).as_dict()

        # 3. human approval
        approval = self.approver.decide(plan, decision)
        self.auditor.approval(plan.experiment_id, approval)
        if not approval.approved:
            return ToolResult("not_approved", plan.experiment_id,
                              f"approval withheld ({approval.by}): {approval.reason}").as_dict()

        # 4. run through the orchestrator (validates again, journals, watches, always rolls back)
        runner = self.make_runner(self.docker, self.config)
        try:
            result = runner.run(plan.to_experiment())
        except DockerError as exc:
            self.auditor.result(plan.experiment_id, {"status": "error", "error": str(exc)})
            return ToolResult("error", plan.experiment_id, f"docker error: {exc}").as_dict()
        summary = self.get_experiment_results(plan.experiment_id)
        self.auditor.result(plan.experiment_id, {"status": "ran", **summary})
        return ToolResult("ran", plan.experiment_id,
                          f"verdict {result.verdict}"
                          + (f"; {result.abort_reason}" if result.abort_reason else ""),
                          verdict=result.verdict).as_dict()


#: The eight injection tool names, for the LLM driver and the CLI.
INJECTION_TOOLS: tuple[str, ...] = tuple(f"inject_{name if name != 'throttle' else 'cpu_throttle'}"
                                         for name in ATTACK_NAMES)

__all__ = ["INJECTION_TOOLS", "ToolInterface", "ToolResult", "allowed_services"]

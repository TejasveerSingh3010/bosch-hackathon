"""Optional, after a human accepts a recommendation: run the same fault against the original
configuration and against the fix, then judge.

    wait steady -> [before arm: N runs] -> apply fix -> wait steady -> [after arm: N runs] -> restore

Each run is a normal orchestrator experiment (so every safety control still applies), wrapped by
the cart consistency probe. The fix is always undone in a `finally`, whatever the verdict: adopting
it permanently is a human edit to app/docker-compose.yml.

The verdict is deliberately hard to earn. "effective" needs every success criterion met by the
median *and* every after-run better than every before-run (with 3 runs per arm, a split that clean
happens by chance 1 time in 20), no regression on any other metric or on resource use, and
equivalent steady-state conditions in both arms. Anything less is ineffective or inconclusive.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from orchestrator.runner import RECOVERY_TIMEOUT_S
from safety.killswitch import KillSwitch
from safety.limits import TargetConfig
from safety.telemetry import TelemetryReader, TelemetryUnavailable, healthy
from safety.validator import PlanRejected
from schemas import Experiment, ExperimentResult

from .compose import Compose, ComposeError, FixJournal
from .consistency import CartProbe
from .models import METRICS, CriterionOutcome, Proposal, RunRecord, SuccessCriterion, Verification
from .store import Store

log = logging.getLogger("remediation")

#: A worsening smaller than this is noise, not a regression (same units as the metric).
REGRESSION_FLOOR = {
    "during_error_rate_mean": 2.0,
    "during_error_rate_max": 5.0,
    "during_p95_max": 200.0,
    "recovery_seconds": 4.0,  # two probe intervals
    "failed_endpoint_count": 1.0,
    "writes_lost": 0.0,
}
REGRESSION_PCT = 20.0
#: Steady-state p95 may differ by this much between arms before the runs stop being comparable.
EQUIVALENCE_P95 = (0.5, 100.0)  # relative, absolute ms - both must be exceeded
EQUIVALENCE_ERROR_PCT = 1.0
#: Resource cost of the fix on the changed services: flagged if both limits are exceeded.
RESOURCE_LIMITS = {"mem_mb": (0.5, 50.0), "cpu_pct": (0.5, 20.0)}


class VerificationAborted(RuntimeError):
    pass


def run_metrics(result: ExperimentResult, write_lost: bool | None) -> dict[str, float | None]:
    recovery = result.recovery_seconds
    if recovery is None and result.rolled_back_at is not None:
        recovery = RECOVERY_TIMEOUT_S  # never recovered inside the window: score it as the whole window
    return {
        "during_error_rate_mean": result.during.error_rate_pct_mean,
        "during_error_rate_max": result.during.error_rate_pct_max,
        "during_p95_max": result.during.p95_ms_max,
        "recovery_seconds": recovery,
        "failed_endpoint_count": float(len(result.during.failed_endpoints)),
        "writes_lost": None if write_lost is None else float(write_lost),
    }


def _values(runs: list[RunRecord], arm: str, metric: str) -> list[float]:
    return [r.metrics[metric] for r in runs if r.arm == arm and r.metrics.get(metric) is not None]


def judge_criterion(crit: SuccessCriterion, runs: list[RunRecord]) -> CriterionOutcome:
    before, after = _values(runs, "before", crit.metric), _values(runs, "after", crit.metric)
    mb = statistics.median(before) if before else 0.0
    ma = statistics.median(after) if after else 0.0
    improvement = 0.0 if mb == 0 else round((mb - ma) / mb * 100, 1)
    separated = bool(before and after) and max(after) < min(before)
    if len(before) < 2 or len(after) < 2:
        status = "insufficient_data"
    elif mb == 0:
        status = "not_reproduced"
    elif improvement < crit.min_improvement_pct:
        status = "unmet"
    elif not separated:
        status = "not_separated"
    else:
        status = "met"
    return CriterionOutcome(
        metric=crit.metric,
        before=before,
        after=after,
        before_median=mb,
        after_median=ma,
        improvement_pct=improvement,
        required_pct=crit.min_improvement_pct,
        separated=separated,
        status=status,
    )


def find_regressions(runs: list[RunRecord]) -> list[str]:
    found = []
    for metric in METRICS:
        before, after = _values(runs, "before", metric), _values(runs, "after", metric)
        if len(before) < 2 or len(after) < 2:
            continue
        if metric == "writes_lost":
            if sum(after) > sum(before):
                found.append(
                    f"writes_lost: {sum(after):g} lost writes with the fix vs {sum(before):g} without"
                )
            continue
        mb, ma = statistics.median(before), statistics.median(after)
        worse = ma - mb
        if worse > REGRESSION_FLOOR[metric] and (mb == 0 or worse / mb * 100 > REGRESSION_PCT):
            found.append(f"{metric}: median {mb:g} -> {ma:g} (worse)")
    steady_b = [r.steady_p95_ms for r in runs if r.arm == "before" and r.steady_p95_ms is not None]
    steady_a = [r.steady_p95_ms for r in runs if r.arm == "after" and r.steady_p95_ms is not None]
    if steady_b and steady_a:
        mb, ma = statistics.median(steady_b), statistics.median(steady_a)
        if ma - mb > 50 and ma > mb * 1.3:
            found.append(f"normal-operation p95 {mb:g}ms -> {ma:g}ms: the fix slows the healthy system")
    return found


def equivalence_problems(runs: list[RunRecord]) -> list[str]:
    problems = []
    for field, label in (
        ("steady_p95_ms", "steady-state p95"),
        ("steady_error_rate_pct", "steady-state error rate"),
    ):
        b = [getattr(r, field) for r in runs if r.arm == "before" and getattr(r, field) is not None]
        a = [getattr(r, field) for r in runs if r.arm == "after" and getattr(r, field) is not None]
        if not (b and a):
            continue
        mb, ma = statistics.median(b), statistics.median(a)
        if field == "steady_p95_ms":
            rel, absolute = EQUIVALENCE_P95
            if abs(ma - mb) > absolute and abs(ma - mb) > rel * min(ma, mb):
                problems.append(
                    f"{label} differed between arms ({mb:g}ms vs {ma:g}ms) - conditions not equivalent"
                )
        elif abs(ma - mb) > EQUIVALENCE_ERROR_PCT:
            problems.append(f"{label} differed between arms ({mb:g}% vs {ma:g}%) - conditions not equivalent")
    return problems


def resource_regressions(cost: dict[str, dict[str, float | None]]) -> list[str]:
    found = []
    for key, (rel, absolute) in RESOURCE_LIMITS.items():
        b, a = cost.get("before", {}).get(key), cost.get("after", {}).get(key)
        if b is not None and a is not None and a - b > absolute and a - b > rel * b:
            found.append(f"resource cost: mean {key} of the changed services {b:g} -> {a:g}")
    return found


def decide(verification: Verification, criteria: list[SuccessCriterion]) -> Verification:
    """Fill in criteria, regressions, equivalence and the verdict from the recorded runs."""
    runs = verification.runs
    invalid = [r for r in runs if not r.valid]
    valid = [r for r in runs if r.valid]
    verification.criteria = [judge_criterion(c, valid) for c in criteria]
    verification.regressions = find_regressions(valid) + resource_regressions(verification.resource_cost)
    verification.equivalence_problems = equivalence_problems(valid)
    reasons = list(verification.reasons)
    reasons += [f"run {r.experiment_id} invalid: {r.invalid_reason}" for r in invalid]
    statuses = {c.status for c in verification.criteria}

    for arm in ("before", "after"):
        if sum(r.arm == arm for r in valid) < 2:
            reasons.append(f"fewer than 2 valid runs on the {arm} arm")
    if any("fewer than 2 valid" in r for r in reasons) or verification.equivalence_problems:
        verdict = "inconclusive"
        reasons += verification.equivalence_problems
        reasons += [f"possible regression (conditions unreliable): {r}" for r in verification.regressions]
    elif verification.regressions:
        verdict = "regressed"
        reasons += verification.regressions
    elif "unmet" in statuses:
        verdict = "ineffective"
    elif statuses == {"met"}:
        verdict = "effective"
    else:
        verdict = "inconclusive"
    for c in verification.criteria:
        reasons.append(
            f"{c.metric}: median {c.before_median:g} -> {c.after_median:g} "
            f"({c.improvement_pct:+g}%, needed {c.required_pct:g}%) - {c.status.replace('_', ' ')}"
        )
    verification.verdict = verdict
    verification.reasons = reasons
    return verification


class Verifier:
    def __init__(
        self,
        config: TargetConfig,
        run_experiment: Callable[[Experiment], ExperimentResult],
        compose: Compose,
        probe: CartProbe,
        store: Store,
        telemetry: TelemetryReader | None = None,
        loadgen_running: Callable[[], bool] = lambda: True,
        cooldown_s: float = 20.0,
        steady_samples: int = 5,
        steady_timeout_s: float = 180.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.run_experiment = run_experiment
        self.compose = compose
        self.probe = probe
        self.store = store
        self.telemetry = telemetry or TelemetryReader(config.results_dir)
        self.loadgen_running = loadgen_running
        self.cooldown_s = cooldown_s
        self.steady_samples = steady_samples
        self.steady_timeout_s = steady_timeout_s
        self.clock = clock
        self.sleep = sleep
        self.killswitch = KillSwitch(config.results_dir)
        self.journal = FixJournal(config.results_dir)

    # ---- helpers
    def _wait_steady(self) -> tuple[float, float]:
        """Wait for N distinct healthy samples in a row; return their mean p95 and error rate."""
        self.sleep(self.cooldown_s)
        deadline = self.clock() + self.steady_timeout_s
        streak: list[dict[str, Any]] = []
        while self.clock() < deadline:
            if self.killswitch.engaged():
                raise VerificationAborted(f"kill switch: {self.killswitch.reason()}")
            try:
                sample = self.telemetry.require_fresh()
            except (TelemetryUnavailable, ValueError, KeyError) as exc:
                raise VerificationAborted(f"telemetry unavailable: {exc}") from exc
            if not streak or sample["timestamp"] != streak[-1]["timestamp"]:
                streak = streak + [sample] if (healthy(sample) and not sample.get("simulated")) else []
            if len(streak) >= self.steady_samples:
                p95 = statistics.mean(float(s["summary"].get("p95_latency_ms") or 0) for s in streak)
                err = statistics.mean(float(s["summary"].get("error_rate_pct") or 0) for s in streak)
                return round(p95, 1), round(err, 2)
            self.sleep(1.0)
        raise VerificationAborted(
            f"the system did not reach a steady healthy state in {self.steady_timeout_s:.0f}s"
        )

    def _resource_cost(self, services: set[str], start: datetime, end: datetime) -> dict[str, float | None]:
        mem, cpu = [], []
        for sample in self.telemetry.between(start, end):
            for w in sample.get("workloads") or []:
                if w.get("app") in services:
                    if w.get("mem_mb") is not None:
                        mem.append(float(w["mem_mb"]))
                    if w.get("cpu_pct") is not None:
                        cpu.append(float(w["cpu_pct"]))
        return {
            "mem_mb": round(statistics.mean(mem), 1) if mem else None,
            "cpu_pct": round(statistics.mean(cpu), 1) if cpu else None,
        }

    def _one_run(self, experiment: Experiment, arm: str) -> RunRecord:
        if not self.loadgen_running():
            raise VerificationAborted("the load generator is not running - runs would not be comparable")
        steady_p95, steady_err = self._wait_steady()
        write = self.probe.write()
        log.info("%s: %s (steady p95 %.0fms)", arm, experiment.id, steady_p95)
        try:
            result = self.run_experiment(experiment)
        except PlanRejected as exc:
            return RunRecord(
                arm=arm,
                experiment_id=experiment.id,
                verdict="rejected",
                valid=False,
                invalid_reason=f"safety layer refused it: {exc}",
                steady_p95_ms=steady_p95,
                steady_error_rate_pct=steady_err,
            )
        if result.verdict == "stopped":
            raise VerificationAborted(f"experiment {experiment.id} was stopped: {result.abort_reason}")
        preserved = self.probe.preserved(write) if write.acknowledged else None
        lost = None if preserved is None else not preserved
        invalid = None
        if result.verdict == "failed" or result.errors or result.injected_at is None:
            invalid = "; ".join(result.errors) or f"verdict {result.verdict}"
        return RunRecord(
            arm=arm,
            experiment_id=experiment.id,
            verdict=result.verdict,
            valid=invalid is None,
            invalid_reason=invalid,
            metrics=run_metrics(result, lost),
            steady_p95_ms=steady_p95,
            steady_error_rate_pct=steady_err,
            write_acknowledged=write.acknowledged,
            write_preserved=preserved,
        )

    # ---- main
    def verify(self, proposal: Proposal, repeats: int | None = None) -> Verification:
        problem = self.store.approval_problem(proposal.id)
        if problem:
            raise PermissionError(f"{proposal.id} cannot be verified: {problem}")
        if proposal.policy_problems:
            raise PermissionError(
                f"{proposal.id} failed policy checks: {'; '.join(proposal.policy_problems)}"
            )
        if self.journal.read():
            raise RuntimeError("a previous fix is still applied - run `python -m remediation recover` first")

        repeats = repeats or proposal.validation_experiment.repeats
        services = sorted({c.service for c in proposal.changes})
        base = proposal.validation_experiment.to_experiment(f"{proposal.id}-x", proposal.diagnosed_problem)
        verification = Verification(
            proposal_id=proposal.id,
            verdict="inconclusive",
            repeats=repeats,
            started_at=datetime.now().astimezone(),
            experiment=base,
        )
        override = self.store.dir(proposal.id) / "override.yml"
        applied = False
        try:
            for arm in ("before", "after"):
                arm_start = datetime.now().astimezone()
                if arm == "after":
                    self.journal.write(proposal.id, services)  # first, so a crash is recoverable
                    log.info("applying fix to %s", ", ".join(services))
                    self.compose.apply(override, services)
                    applied = True
                for i in range(1, repeats + 1):
                    exp = base.model_copy(update={"id": f"{proposal.id}-{arm}-{i}"})
                    verification.runs.append(self._one_run(exp, arm))
                verification.resource_cost[arm] = self._resource_cost(
                    set(services), arm_start, datetime.now().astimezone()
                )
        except (VerificationAborted, ComposeError) as exc:
            verification.reasons.append(f"verification stopped early: {exc}")
            log.error("verification stopped early: %s", exc)
        except KeyboardInterrupt:
            verification.reasons.append("verification interrupted (Ctrl+C)")
        finally:
            if applied or self.journal.read():
                try:
                    self.compose.restore(services)
                    self.journal.clear()
                    verification.fix_restored = True
                    log.info("original configuration restored for %s", ", ".join(services))
                except ComposeError as exc:
                    verification.reasons.append(
                        f"RESTORE FAILED - run `python -m remediation recover`: {exc}"
                    )
            else:
                verification.fix_restored = True
            verification.ended_at = datetime.now().astimezone()
            decide(verification, proposal.success_criteria)
            self.store.save_verification(verification)
        return verification

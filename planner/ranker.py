"""Agent 1's engine: rank every catalogue attack from most to least harmful for the current state.

Two stages, exactly as the brief specifies:

  1. **Deterministic prefilter.** For each candidate, call `ExperimentRunner.dry_run` (the only path
     to the safety layer) and record whether pre-flight would allow or refuse it, and why. Compute
     features from the graph, the page table and earlier results.
  2. **Ranking.** In Phase 1 (no LLM) this is deterministic: harm = criticality x pages affected,
     normalised to 0-100, highest first. When a client is configured the LLM re-scores stage 2 only
     - it sees the features, never raw logs - and the deterministic result stays as the permanent
     fallback. The Experiments and pre-flight verdicts are always code-built, so what a human
     approves is exactly what runs.

Re-ranking is state-dependent: feed it fresh telemetry and the current results folder and the order
changes - a degraded app marks every candidate "pre-flight would refuse", and attacks already
survived fall behind untested ones of similar harm.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agents.llm import LLMClient
from agents.schemas import PredictedEffects, RankedAttack, Ranking
from orchestrator.runner import ExperimentRunner
from safety.limits import TargetConfig
from safety.telemetry import TelemetryReader, TelemetryUnavailable, is_degraded
from safety.validator import PlanRejected
from schemas import Experiment

from . import prompts
from .catalogue import AttackSpec, build_catalogue
from .features import AttackFeatures, compute_features

log = logging.getLogger("planner")

#: Action-severity tiebreaker for equal harm (higher = sorted first). Does not change the harm
#: score itself, which stays criticality x pages, but orders a tie sensibly (a lasting outage above
#: a self-healing crash).
ACTION_SEVERITY: dict[str, float] = {
    "stop": 1.0, "network_partition": 0.95, "pause": 0.9, "flapping": 0.85,
    "memory_limit": 0.8, "cpu_throttle": 0.75, "network_delay": 0.7, "spike": 0.65, "kill": 0.6,
}


class LLMRankingEntry(BaseModel):
    """One re-scored candidate from the LLM (validated; see planner/prompts.py)."""

    model_config = ConfigDict(extra="ignore")

    candidate_id: str
    predicted_harm: float = Field(ge=0, le=100)
    predicted_failed_endpoints: list[str] = Field(default_factory=list)
    predicted_blast_radius: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.5)
    rationale: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class LLMRanking(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entries: list[LLMRankingEntry] = Field(default_factory=list)


class Ranker:
    """Builds `results/ranking.json` for the current state of the app."""

    def __init__(
        self,
        runner: ExperimentRunner,
        config: TargetConfig,
        client: LLMClient | None = None,
    ) -> None:
        self.runner = runner
        self.config = config
        self.client = client
        self.results_dir = Path(config.results_dir)
        self.telemetry = TelemetryReader(self.results_dir)

    # ------------------------------------------------------------------ inputs
    def _graph(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.results_dir / "graph.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _prior_results(self) -> dict[str, dict[str, Any]]:
        """Earlier results, keyed by '<action>:<target>', so a candidate can see its own history."""
        prior: dict[str, dict[str, Any]] = {}
        for path in sorted(self.results_dir.glob("*.json")):
            if path.name in ("ranking.json", "graph.json", "baseline.json", "telemetry.json"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                exp = data.get("experiment") or {}
                key = f"{exp.get('action')}:{exp.get('target')}"
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if not exp:
                continue
            # keep the most recent (sorted glob is roughly chronological; prefer the last seen)
            prior[key] = {"id": exp.get("id") or data.get("experiment", {}).get("id"),
                          "verdict": data.get("verdict")}
        return prior

    def _app_state(self) -> tuple[str, bool]:
        """(label, degraded?) from the latest telemetry sample; 'unknown' if the monitor is quiet."""
        try:
            sample = self.telemetry.require_fresh()
        except TelemetryUnavailable:
            return "unknown", False
        if is_degraded(sample) or (sample.get("summary") or {}).get("services_down"):
            return "degraded", True
        return "healthy", False

    # ------------------------------------------------------------------ stage 1
    def _preflight(self, experiment: Experiment) -> tuple[str, str]:
        """Dry-run through the safety layer. Returns ('allow'|'refuse', reason)."""
        try:
            self.runner.dry_run(experiment)
            return "allow", "pre-flight accepts this plan"
        except PlanRejected as exc:
            return "refuse", str(exc)
        except Exception as exc:  # noqa: BLE001 - any dry-run failure is treated as a refusal
            return "refuse", f"pre-flight could not be evaluated: {exc}"

    # ------------------------------------------------------------------ stage 2 (deterministic)
    def _deterministic_entry(
        self, experiment: Experiment, feats: AttackFeatures, harm: float
    ) -> RankedAttack:
        bits = [f"criticality {feats.criticality:.0f} ({feats.criticality_reason})"]
        pages = feats.pages_affected
        bits.append(f"affects {len(pages)} page(s)" + (f": {', '.join(pages)}" if pages else ""))
        if feats.prior_run_id:
            bits.append(f"already tested ({feats.prior_verdict}) in {feats.prior_run_id}")
        if feats.preflight_verdict == "refuse":
            bits.append("pre-flight would refuse")
        return RankedAttack(
            rank=1,  # reassigned after the sort
            experiment=experiment,
            predicted_harm=round(harm, 1),
            predicted_effects=PredictedEffects(
                failed_endpoints=list(pages),
                blast_radius=list(feats.dependents),
                recovery_difficulty=feats.recovery_difficulty,
            ),
            confidence=0.6 if feats.criticality else 0.3,
            rationale="; ".join(bits),
            evidence_refs=list(feats.evidence_refs),
            preflight_verdict=feats.preflight_verdict,  # type: ignore[arg-type]
            preflight_reason=feats.preflight_reason,
            prior_run_id=feats.prior_run_id,
        )

    # ------------------------------------------------------------------ orchestration
    def rank(self) -> Ranking:
        catalogue = build_catalogue(self.config)
        graph = self._graph()
        prior = self._prior_results()
        app_state, degraded = self._app_state()

        prepared: list[tuple[AttackFeatures, AttackSpec, Experiment]] = []
        raw_harms: list[float] = []
        for spec in catalogue:
            experiment = spec.experiment()
            preflight = self._preflight(experiment)
            feats = compute_features(spec, graph, prior, degraded, preflight)
            raw_harms.append(feats.criticality * len(feats.pages_affected))
            prepared.append((feats, spec, experiment))

        max_raw = max(raw_harms, default=0.0) or 1.0
        entries: list[tuple[AttackFeatures, str, RankedAttack]] = []
        for (feats, spec, experiment), raw in zip(prepared, raw_harms, strict=True):
            harm = 100.0 * raw / max_raw
            entry = self._deterministic_entry(experiment, feats, harm)
            entries.append((feats, spec.action, entry))

        source = "deterministic"
        model_name: str | None = None
        notes: list[str] = []
        if graph is None:
            notes.append("results/graph.json not found; criticality is 0 for all. Run `python -m mapper`.")
        if app_state == "unknown":
            notes.append("telemetry is unavailable; pre-flight refused every attack. Start the monitor.")
        if degraded:
            notes.append("the app is currently degraded; pre-flight would refuse every attack now.")

        # Stage 2, LLM: re-score only. Deterministic result is the permanent fallback (constraint 8).
        if self.client is not None:
            try:
                entries = self._llm_rerank(app_state, entries)  # TODO(llm)
                source, model_name = "llm", self.client.model
            except Exception as exc:  # noqa: BLE001 - never let the LLM break the pipeline
                log.warning("LLM ranking failed, keeping the deterministic order: %s", exc)
                notes.append(f"LLM ranking failed; used the deterministic fallback ({exc}).")

        def sort_key(t: tuple[AttackFeatures, str, RankedAttack]) -> tuple[float, float, float, str]:
            feats, action, entry = t
            return (-entry.predicted_harm, -feats.criticality,
                    -ACTION_SEVERITY.get(action, 0.5), entry.experiment.target)

        ranked = [entry for _f, _a, entry in sorted(entries, key=sort_key)]
        for index, entry in enumerate(ranked, start=1):
            entry.rank = index

        return Ranking(source=source, model=model_name, app_state=app_state,  # type: ignore[arg-type]
                       attacks=ranked, notes=notes)

    def _llm_rerank(
        self, app_state: str, entries: list[tuple[AttackFeatures, str, RankedAttack]]
    ) -> list[tuple[AttackFeatures, str, RankedAttack]]:
        """Replace stage 2 with the model's harm scores and predicted effects. TODO(llm) call site."""
        candidates = [
            {
                "candidate_id": entry.experiment.id,
                "target": entry.experiment.target,
                "action": entry.experiment.action,
                "criticality": feats.criticality,
                "criticality_reason": feats.criticality_reason,
                "pages_affected": feats.pages_affected,
                "dependents": feats.dependents,
                "on_checkout_path": feats.on_checkout_path,
                "single_point": feats.single_point,
                "stateful": feats.stateful,
                "prior_verdict": feats.prior_verdict,
                "preflight_verdict": feats.preflight_verdict,
                "evidence_refs": feats.evidence_refs,
            }
            for feats, _action, entry in entries
        ]
        evidence_ids = sorted({ref for feats, _a, _e in entries for ref in feats.evidence_refs})
        # TODO(llm): this is the planner's single model call. The client handles validation, the
        # one retry and the audit log; we only merge the scores back onto the code-built entries.
        result = self.client.generate(  # type: ignore[union-attr]
            agent="planner",
            system=prompts.SYSTEM_PROMPT,
            user=prompts.build_user_prompt(app_state, candidates),
            schema=LLMRanking,
            evidence_ids=evidence_ids,
        )
        by_id = {e.candidate_id: e for e in result.entries}
        for _feats, _action, entry in entries:
            scored = by_id.get(entry.experiment.id)
            if scored is None:
                continue
            entry.predicted_harm = round(scored.predicted_harm, 1)
            entry.predicted_effects = PredictedEffects(
                failed_endpoints=scored.predicted_failed_endpoints,
                blast_radius=scored.predicted_blast_radius,
                recovery_difficulty=entry.predicted_effects.recovery_difficulty,
            )
            entry.confidence = scored.confidence
            entry.rationale = scored.rationale or entry.rationale
            if scored.evidence_refs:
                entry.evidence_refs = scored.evidence_refs
        return entries


def write_ranking(ranking: Ranking, results_dir: Path | str) -> Path:
    path = Path(results_dir) / "ranking.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ranking.model_dump_json(indent=2), encoding="utf-8")
    return path


__all__ = ["ACTION_SEVERITY", "LLMRanking", "LLMRankingEntry", "Ranker", "write_ranking"]

"""Deterministic checks every proposal must pass before a human is even asked to review it.

No LLM is involved here. The same rules apply to agent-written and hand-written proposals.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from safety.limits import REPO_ROOT, TargetConfig

from .codelint import lint_edits
from .models import CodeProposalDraft, ProposalDraft
from .reports import synthetic_sources
from .source import EditResult, apply_edits

#: Services a proposal may never touch, and why.
PROTECTED_SERVICES = {
    "redis-cart": "it is the datastore; its storage and recovery configuration is off limits to the agent",
    "loadgenerator": "it is the workload; changing it would make before/after runs incomparable",
}
#: Per-service facts read from the upstream source; `tunable_env` is the environment allow-list.
SERVICE_DOCS = REPO_ROOT / "docs" / "services.yaml"
RESTART_VALUES = re.compile(r"^(no|always|unless-stopped|on-failure(:[1-9][0-9]?)?)$")
MEMORY = re.compile(r"^(\d+(?:\.\d+)?)([kmg])$", re.IGNORECASE)
MAX_CHANGES = 3
#: A code recommendation is a targeted fix for a reviewer to read, not a rewrite.
MAX_EDITS, MAX_CHANGED_LINES = 6, 120
MIN_REPEATS, MAX_REPEATS = 2, 5


@cache
def service_docs(path: Path = SERVICE_DOCS) -> dict[str, dict[str, Any]]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services") or {}


def check_env(service: str, env_key: str, env_value: str, docs: dict[str, dict[str, Any]]) -> str | None:
    """None if docs/services.yaml lists this variable as tunable for this service and the value fits."""
    doc = docs.get(service) or {}
    tunable = doc.get("tunable_env") or {}
    if env_key not in tunable:
        why = (doc.get("not_tunable") or {}).get(env_key)
        if why:
            return f"{env_key} is not tunable on {service}: {why}"
        reads = ", ".join(doc.get("env_reads") or []) or "nothing documented"
        return (
            f"{env_key} is not a documented tunable for {service} (docs/services.yaml); "
            f"tunable: {', '.join(tunable) or 'none'}; the code reads: {reads}"
        )
    pattern = (tunable[env_key] or {}).get("pattern")
    if pattern and not re.fullmatch(pattern, env_value):
        return f"{env_key}={env_value!r} does not match the documented form {pattern}"
    return None


def _memory_mb(value: str) -> float | None:
    match = MEMORY.match(value.strip())
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2).lower()
    return number / 1024 if unit == "k" else number * 1024 if unit == "g" else number


def resolve_field(record: dict[str, Any], dotted: str) -> Any:
    node: Any = record
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise KeyError(dotted)
    return node


def _same_value(actual: Any, observed: str) -> bool:
    if isinstance(actual, bool) or actual is None:
        return str(actual).lower() == observed.strip().lower() or json.dumps(actual) == observed.strip()
    if isinstance(actual, (int, float)):
        try:
            claimed = float(re.sub(r"\s*(%|ms|s)$", "", observed.strip(), flags=re.IGNORECASE))
        except ValueError:
            return False
        return abs(claimed - float(actual)) <= max(1e-6, abs(float(actual)) * 0.01)
    if isinstance(actual, str):
        return actual.strip() == observed.strip().strip('"')
    try:
        return actual == json.loads(observed)
    except (ValueError, TypeError):
        return json.dumps(actual, sort_keys=True) == observed.strip()


def check_evidence(draft: ProposalDraft | CodeProposalDraft, results_dir: Path) -> list[str]:
    problems = []
    for i, item in enumerate(draft.evidence, 1):
        path = Path(results_dir) / f"{item.result_id}.json"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", item.result_id) or not path.exists():
            problems.append(f"evidence {i}: no experiment record {item.result_id!r}")
            continue
        try:
            actual = resolve_field(json.loads(path.read_text(encoding="utf-8")), item.field)
        except KeyError:
            problems.append(f"evidence {i}: {item.result_id} has no field {item.field!r}")
            continue
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"evidence {i}: cannot read {path.name}: {exc}")
            continue
        if not _same_value(actual, item.observed):
            problems.append(
                f"evidence {i}: {item.result_id}.{item.field} is {json.dumps(actual)}, "
                f"not {item.observed!r} as claimed"
            )
    return problems


def check_changes(
    draft: ProposalDraft, services: dict[str, dict[str, Any]], docs: dict[str, dict[str, Any]] | None = None
) -> list[str]:
    docs = service_docs() if docs is None else docs
    problems = []
    if not draft.changes:
        problems.append("no configuration change proposed")
    if len(draft.changes) > MAX_CHANGES:
        problems.append(f"{len(draft.changes)} changes; at most {MAX_CHANGES} so the effect is attributable")
    seen: set[tuple[str, str]] = set()
    for change in draft.changes:
        where = f"{change.service}.{change.setting}"
        if change.service in PROTECTED_SERVICES:
            problems.append(f"{where}: {change.service} is protected - {PROTECTED_SERVICES[change.service]}")
            continue
        if change.service not in services:
            problems.append(f"{where}: no such Compose service")
            continue
        key = change.setting
        value = change.value.strip()
        if key == "environment":
            env_key, sep, env_value = value.partition("=")
            key = f"environment.{env_key}"
            if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_key):
                problems.append(f"{where}: expected KEY=value, got {value!r}")
            elif "\n" in value:
                problems.append(f"{where}: value may not contain a newline")
            elif problem := check_env(change.service, env_key, env_value, docs):
                problems.append(f"{where}: {problem}")
        elif key == "restart" and not RESTART_VALUES.match(value):
            problems.append(f"{where}: {value!r} is not a Compose restart policy")
        elif key == "cpus":
            try:
                if not 0.1 <= float(value) <= 4:
                    problems.append(f"{where}: cpus must be between 0.1 and 4")
            except ValueError:
                problems.append(f"{where}: {value!r} is not a number")
        elif key in ("mem_limit", "mem_reservation"):
            mb = _memory_mb(value)
            if mb is None:
                problems.append(f"{where}: {value!r} is not a size like 256m or 1g")
            elif not 64 <= mb <= 4096:
                problems.append(f"{where}: memory must be between 64m and 4g")
        if (change.service, key) in seen:
            problems.append(f"{where}: changed twice")
        seen.add((change.service, key))
    return problems


def check_proposal(
    draft: ProposalDraft,
    services: dict[str, dict[str, Any]],
    config: TargetConfig,
    reports_dir: Path | None = None,
) -> list[str]:
    """Everything wrong with a proposal. An empty list means it may go to human review."""
    if not draft.remediable:
        return [f"agent declined: {draft.declined_reason or 'no reason given'}"]
    problems = []
    if not draft.rollback_procedure.strip():
        problems.append("rollback_procedure is empty")
    problems += check_common(draft, config, reports_dir)
    problems += check_changes(draft, services)
    return problems


def check_code_proposal(
    draft: CodeProposalDraft,
    config: TargetConfig,
    checkout: Path,
    docs: dict[str, dict[str, Any]] | None = None,
    reports_dir: Path | None = None,
) -> tuple[list[str], EditResult]:
    """Everything wrong with a code recommendation, plus the edited files for the patch.

    Each edit must quote the pinned source exactly once, stay inside the service's own source
    directory, and leave a change small enough to review. The compile check is separate (slow).
    """
    docs = service_docs() if docs is None else docs
    if not draft.remediable:
        return [f"agent declined: {draft.declined_reason or 'no reason given'}"], EditResult({}, [])
    problems = check_common(draft, config, reports_dir)
    if not draft.edits:
        problems.append("no code edit proposed")
    if len(draft.edits) > MAX_EDITS:
        problems.append(f"{len(draft.edits)} edits; at most {MAX_EDITS} so the change stays reviewable")
    code_dirs, languages = {}, {}
    for edit in draft.edits:
        if edit.service in PROTECTED_SERVICES:
            problems.append(f"{edit.service} is protected - {PROTECTED_SERVICES[edit.service]}")
        elif (docs.get(edit.service) or {}).get("code"):
            code_dirs[edit.service] = docs[edit.service]["code"]["dir"]
            languages[edit.service] = docs[edit.service].get("language", "")
    allowed = [e for e in draft.edits if e.service not in PROTECTED_SERVICES]
    result = apply_edits(checkout, code_dirs, allowed, languages)
    problems += result.problems
    problems += lint_edits(allowed, languages, draft.diagnosed_problem)
    if result.changed_lines > MAX_CHANGED_LINES:
        problems.append(f"{result.changed_lines} changed lines; at most {MAX_CHANGED_LINES}")
    return problems, result


def check_common(
    draft: ProposalDraft | CodeProposalDraft, config: TargetConfig, reports_dir: Path | None = None
) -> list[str]:
    """The checks config and code proposals share: stated reasoning, evidence, criteria, experiment,
    and real data: evidence from an experiment Agent 2 reported as synthetic cannot back a fix."""
    problems = []
    synthetic = synthetic_sources([e.result_id for e in draft.evidence], reports_dir)
    for result_id, caveat in synthetic.items():
        problems.append(f"built on synthetic data: Agent 2's report on {result_id} says {caveat!r}")
    for name, text in (
        ("diagnosed_problem", draft.diagnosed_problem),
        ("expected_benefit", draft.expected_benefit),
    ):
        if not text.strip():
            problems.append(f"{name} is empty")
    if not draft.side_effects:
        problems.append("side_effects is empty - every change has some; state them")
    if not draft.evidence:
        problems.append("no evidence cited")
    problems += check_evidence(draft, config.results_dir)

    if not draft.success_criteria:
        problems.append("no success criteria - the fix could never be judged")
    for crit in draft.success_criteria:
        if not 5 <= crit.min_improvement_pct <= 100:
            problems.append(f"criterion {crit.metric}: min_improvement_pct must be 5-100")

    exp = draft.validation_experiment
    if not MIN_REPEATS <= exp.repeats <= MAX_REPEATS:
        problems.append(f"validation_experiment.repeats must be {MIN_REPEATS}-{MAX_REPEATS}")
    if not 0 < exp.duration_s <= config.effective_max_seconds:
        problems.append(f"validation_experiment.duration_s must be 1-{config.effective_max_seconds}")
    if exp.target == "loadgenerator":
        problems.append("validation_experiment cannot target the load generator")
    try:
        exp.to_experiment("policy-check", draft.diagnosed_problem)
    except ValueError as exc:
        problems.append(f"validation_experiment is invalid: {exc}")
    return problems

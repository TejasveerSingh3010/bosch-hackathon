"""The remediation agent: experiment records in, a reviewable `ProposalDraft` out.

It only recommends. It cannot apply anything: `policy` checks the draft, and accepting it is the
human escalation step (a human may then measure it with `verify`). The model (on Groq) answers in
strict JSON-schema mode and the reply is validated into `ProposalDraft`; anything malformed fails
loudly instead of being guessed at.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import groq
import yaml
from dotenv import load_dotenv

from agents import tracing
from safety.limits import REPO_ROOT, TargetConfig
from safety.telemetry import TelemetryReader
from schemas import ExperimentResult

from .models import METRICS, CodeProposalDraft, Critique, Proposal, ProposalDraft
from .policy import (
    MAX_CHANGED_LINES,
    MAX_CHANGES,
    MAX_EDITS,
    PROTECTED_SERVICES,
    SERVICE_DOCS,
    _memory_mb,
)
from .reports import default_reports_dir, report_digest  # noqa: F401 - re-exported
from .source import SourceError, family, file_imports, find_function

MODEL = "openai/gpt-oss-120b"

#: Groq's free tier: tokens a minute, and a request larger than that is refused outright (HTTP 413).
GROQ_TPM = 8000
#: What a whole request (system prompt + context + schema) is sized to, leaving headroom under GROQ_TPM.
MAX_REQUEST_TOKENS = 7200
#: How Groq's admission check counts a request. MEASURED 2026-10-11 from its 413 replies on a code
#: request (17,345 message characters + a 4,430-character schema): the messages counted 4,890 tokens
#: (Groq's own prompt_tokens for them: 4,266), the schema another 3,444. Schema text weighs about
#: 2.6x as much as message text, so field guidance travels in the system prompt (`field_guide`) and
#: the schema goes without titles and descriptions (`wire_schema`).
MESSAGE_CHARS_PER_TOKEN = 3.4
SCHEMA_CHARS_PER_TOKEN = 1.3


def estimate_request(system: str, context: str, schema: str) -> int:
    """Groq's count of a request, per the measured ratios; deliberately on the high side."""
    messages = (len(system) + len(context)) / MESSAGE_CHARS_PER_TOKEN
    return int(messages + len(schema) / SCHEMA_CHARS_PER_TOKEN) + 1


#: gpt-oss reasoning effort. Code proposals can be given more ("high"), with a fallback to the default
#: if that answer is cut off or breaks the schema. MEASURED 2026-10-11: "high" failed both live code
#: requests (one broke the JSON schema mid-answer; the other's cause was not recorded), so each cost
#: an extra full request and a minute's wait.
#: It stays at the default until Langfuse (model_parameters per generation) shows otherwise.
REASONING_EFFORT = "medium"
CODE_REASONING_EFFORT = "medium"

SYSTEM_PROMPT = f"""\
You are the remediation stage of a chaos-engineering pipeline for Online Boutique, a microservice \
shop running under Docker Compose on one machine. Earlier stages injected failures (kill, stop, \
pause, traffic spike, CPU throttle) and recorded what happened. Your job is to read those records and \
propose at most one small configuration fix that would measurably improve resilience, or to decline.

What you can change: only these Compose settings on a service - restart (restart policy), cpus, \
mem_limit, mem_reservation, and environment (one KEY=value per change). At most {MAX_CHANGES} \
changes. Never touch {", ".join(sorted(PROTECTED_SERVICES))}: \
{"; ".join(f"{k}: {v}" for k, v in PROTECTED_SERVICES.items())}. An environment change is accepted \
only if docs/services.yaml lists the variable under that service's tunable_env (and the value \
matches its pattern); anything else is rejected before review.

A record may carry a `reporter:` section: Agent 2's failure report for it (where the failure \
started, how it spread, the errors the containers logged, and caveats). Use it for the diagnosis, \
but cite evidence only from the record's own fields. If a caveat says the data is synthetic or a \
demo, say so in diagnosed_problem.

The service documentation below was read from the upstream source and is authoritative: what each \
service reads, which calls are fatal to which page, which have deadlines, and what is or is not \
fixable by configuration. Base the diagnosis on it and on the fault timelines, which show sample by \
sample which probes failed, how, and what each container was doing. If the weakness can only be \
fixed in application code (for example a missing per-call deadline or a dependency treated as \
fatal), set remediable to false and name the code change in declined_reason.

How your proposal is used: a deterministic checker verifies every evidence item by looking up \
results/<result_id>.json at the dotted field path and comparing the value you quote, so copy \
values exactly from the records shown. Your proposal is a recommendation: a human decides whether \
to accept it. If they choose to measure it, the validation experiment is run against the original \
configuration and again with the fix, `repeats` times each under the same load, and the fix counts \
as effective only if each success criterion's median improves by \
at least min_improvement_pct and every run with the fix beats every run without it. Success metrics \
(all lower is better): {", ".join(METRICS)}. writes_lost counts cart items that were acknowledged \
before the fault and missing after it.

Choose a validation experiment that reproduces the problem you diagnosed - if the original \
configuration does not show the problem, the fix cannot be validated. Set realistic thresholds. \
List real side effects (resource cost, slower restarts, masking a crash loop, and so on) and a \
concrete rollback. If no allowed change can plausibly help - for example the weakness is in \
application code, or in the protected datastore - set remediable to false and explain why in \
declined_reason; fill the other fields with your best description and leave changes empty. \
Declining is better than a speculative fix."""


CODE_SYSTEM_PROMPT = f"""\
You are the code-remediation stage of a chaos-engineering pipeline for Online Boutique (the Google \
microservices-demo, run under Docker Compose). An earlier stage diagnosed a weakness and declined \
to fix it with configuration because only an application code change can. Your job is to recommend \
that code change, as exact edits to the source shown below, or to decline.

The source below is the pinned upstream release the running images were built from. Each edit \
replaces one snippet: `original` must be copied verbatim from the code shown - same tabs, spaces \
and line breaks - quoting whole lines; `file` is the path inside the service directory, exactly as \
in the ### heading (for example rpc.go), and `function` the function or method the snippet is in, \
exactly as named in parentheses on its location line (for example homeHandler or \
EmailService.SendOrderConfirmation); empty only for the import lines. The snippet must occur exactly \
once in that function. Quote enough lines to be unique, but no more than needed; to change the same \
code in several functions, use one edit per function. Only edit code shown. If you need a new \
import, edit the import lines shown. Python indentation must be exact. Keep it small and targeted: \
at most {MAX_EDITS} edits and {MAX_CHANGED_LINES} changed lines, in the service whose code is at \
fault; prefer the change a reviewer can check at a glance (for example a per-call deadline, or \
logging an error and rendering the page without an optional part instead of returning HTTP 500). \
Keep the existing behaviour on the success path. Before anyone sees them, edits are checked with \
the service's own toolchain - Go is gofmt-formatted and compiled, Node and Python files are syntax \
checked - and an edit that fails is rejected. Only Go catches undefined names, and C# and Java are \
not checked at all, so write code you are sure is valid. Also rejected automatically: an error \
branch you add or change that neither logs nor returns the error (a fallback must log, e.g. \
log.WithError(err).Warn(...)); a comment saying something is logged with no log call; and a \
diagnosis of missing deadlines or timeouts with no edit adding one (Go: ctx, cancel := \
context.WithTimeout(ctx, d) then defer cancel(), in the call wrapper). Fix the diagnosed cause, \
and only the fault in these records.

A `reporter:` section under a record is Agent 2's failure report for it (failure origin, spread, \
container log errors, caveats); the logged errors usually name the failing call site.

Evidence: a deterministic checker looks up every evidence item at results/<result_id>.json and the \
dotted field path, so copy values exactly from the records shown. Success criteria and the \
validation experiment say how a human could later measure the change with an image built from the \
patch: choose an experiment that reproduces the problem. Success metrics (all lower is better): \
{", ".join(METRICS)}. List real side effects (behaviour a user would now see, such as an empty \
cart badge while the cart is down; errors that are now hidden; latency budgets). Your output is a \
recommendation: a human decides whether to accept it. If no small, safe change in the code shown \
can help, set remediable to false, explain why in declined_reason and leave edits empty."""


CRITIQUE_SYSTEM_PROMPT = """\
You review a code-change recommendation another model wrote for Online Boutique (Google \
microservices-demo, Go/C#/Node/Python/Java services under Docker Compose), before a human sees it. \
You cannot approve or change anything; you only list what is wrong, so the author can fix it once.

Check, in order: (1) does the patch remove the diagnosed cause - a diagnosis of missing deadlines \
needs a deadline on the faulted call, a fatal optional dependency needs the page to render without \
it; (2) is the success path unchanged; (3) does any error branch now hide the error (no log, not \
returned); (4) would it compile - undefined names, missing imports, wrong types, unbalanced braces; \
(5) does it change behaviour beyond the fault - for example a write (add to cart, place order) that \
now silently succeeds without storing anything loses data; (6) is it the smallest change that \
works. Name the file and function in each issue, and say what to do instead. Automated checks \
already ran; their findings are listed and need not be repeated. If the patch is sound, return \
fixes_diagnosis true and no issues. Do not invent problems to have something to say."""


class Proposer(Protocol):
    model_name: str

    def propose(self, context: str) -> ProposalDraft: ...

    def propose_code(self, context: str) -> CodeProposalDraft: ...

    def critique_code(self, context: str) -> Critique: ...


class ProposalError(RuntimeError):
    pass


class CutOff(ProposalError):
    """The model did not finish a valid answer: out of output tokens, or it broke the JSON schema."""


def strict_schema(node: Any) -> Any:
    """Groq's strict mode wants every property listed in `required`; optional fields stay nullable."""
    if isinstance(node, dict):
        node = {k: strict_schema(v) for k, v in node.items()}
        if node.get("type") == "object" and "properties" in node:
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        return node
    if isinstance(node, list):
        return [strict_schema(v) for v in node]
    return node


def _bare(node: Any, in_properties: bool = False) -> Any:
    """The schema without `title` / `description` annotations (field names stay, even if so named)."""
    if isinstance(node, dict):
        return {
            k: _bare(v, k == "properties")
            for k, v in node.items()
            if in_properties or k not in ("title", "description")
        }
    if isinstance(node, list):
        return [_bare(v) for v in node]
    return node


def field_guide(schema: dict[str, Any]) -> str:
    """One line per described field (`Type.field: description`), for the system prompt."""
    lines = []
    for owner, node in [("", schema), *((f"{k}.", v) for k, v in (schema.get("$defs") or {}).items())]:
        for name, prop in (node.get("properties") or {}).items():
            if desc := prop.get("description"):
                lines.append(f"- {owner}{name}: {' '.join(desc.split())}")
    return "\n".join(lines)


def wire_schema(shape: type[Any], system: str) -> tuple[str, dict[str, Any]]:
    """(system prompt with the field guide, schema as sent): strict, and stripped of annotations,
    which Groq counts at about 2.6x the weight of the same text in the prompt."""
    full = strict_schema(shape.model_json_schema())
    guide = field_guide(full)
    return (f"{system}\n\nField guide:\n{guide}" if guide else system), _bare(full)


log = logging.getLogger(__name__)


class MinuteBudget:
    """Keeps consecutive requests under Groq's tokens-a-minute limit by waiting, instead of letting
    the API refuse them (HTTP 429) and retrying blind."""

    def __init__(
        self,
        limit: int = GROQ_TPM,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limit, self.clock, self.sleep = limit, clock, sleep
        self.spent: list[tuple[float, int]] = []  # (when, tokens) of requests in the last minute

    def used(self) -> int:
        now = self.clock()
        self.spent = [(t, n) for t, n in self.spent if now - t < 60]
        return sum(n for _, n in self.spent)

    def wait_for(self, tokens: int) -> float:
        """Block until `tokens` more fit in the current minute; returns the seconds waited."""
        waited = 0.0
        while self.spent and self.used() + tokens > self.limit:
            pause = max(60 - (self.clock() - self.spent[0][0]), 0) + 0.5
            log.info("waiting %.0fs for Groq's %s-tokens-a-minute limit", pause, f"{self.limit:,}")
            self.sleep(pause)
            waited += pause
        return waited

    def record(self, tokens: int) -> None:
        self.spent.append((self.clock(), tokens))


def second_api_key() -> str | None:
    """The optional second Groq key for the code stage, so it draws on its own tokens-a-minute budget
    instead of sharing the config stage's. Returns None when only one key is set (one shared bucket)."""
    load_dotenv(REPO_ROOT / ".env")
    for name in ("LLM_API_KEY_2", "GROQ_API_KEY_2", "LLM_CODE_API_KEY"):
        value = os.getenv(name)
        if value and value != "your-key-here":
            return value
    return None


class GroqProposer:
    def __init__(
        self, model: str = MODEL, client: groq.Groq | None = None, budget: MinuteBudget | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model_name = model
        self.budget = budget or MinuteBudget()
        self.last_trace_id: str | None = None  # Langfuse trace of the latest call, for scores
        if client is None:
            load_dotenv(REPO_ROOT / ".env")
            key = api_key or os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY")
            if not key or key == "your-key-here":
                raise ProposalError("no Groq API key - set LLM_API_KEY in .env")
            client = groq.Groq(api_key=key)
        self.client = client

    def propose(self, context: str) -> ProposalDraft:
        return self._ask(SYSTEM_PROMPT, context, ProposalDraft, "remediation_proposal")

    def propose_code(self, context: str) -> CodeProposalDraft:
        """Code is where reasoning pays most, so it gets CODE_REASONING_EFFORT; if that answer is
        cut off or breaks the schema, the same request is retried once at the default effort."""
        try:
            return self._ask(
                CODE_SYSTEM_PROMPT, context, CodeProposalDraft, "code_recommendation", CODE_REASONING_EFFORT
            )
        except CutOff as exc:
            if CODE_REASONING_EFFORT == REASONING_EFFORT:
                raise
            log.info("%s-effort answer failed (%s); retrying at %s", CODE_REASONING_EFFORT, exc,
                     REASONING_EFFORT)
            return self._ask(CODE_SYSTEM_PROMPT, context, CodeProposalDraft, "code_recommendation")

    def critique_code(self, context: str) -> Critique:
        return self._ask(CRITIQUE_SYSTEM_PROMPT, context, Critique, "code_critique")

    def _ask(
        self, system: str, context: str, shape: type[Any], name: str, effort: str = REASONING_EFFORT
    ) -> Any:
        system, schema = wire_schema(shape, system)
        size = estimate_request(system, context, json.dumps(schema))
        if size > GROQ_TPM:
            raise ProposalError(
                f"the request is about {size} tokens, over Groq's {GROQ_TPM:,} tokens a minute - "
                "ask about fewer experiment records"
            )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": context}]
        params = {"max_completion_tokens": 6000, "reasoning_effort": effort}
        self.budget.wait_for(size)
        try:
            with tracing.generation(name, self.model_name, messages, params) as traced:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": name, "schema": schema, "strict": True},
                    },
                    **params,
                )
                traced.finish(response)
            self.last_trace_id = traced.trace_id
            usage = getattr(response, "usage", None)
            self.budget.record(int(getattr(usage, "total_tokens", 0) or size))
        except groq.AuthenticationError as exc:
            raise ProposalError("Groq rejected the API key - check LLM_API_KEY in .env") from exc
        except groq.RateLimitError as exc:
            raise ProposalError("rate limited by the Groq API - try again shortly") from exc
        except groq.APIStatusError as exc:
            if "json_validate_failed" in str(exc.message):  # the model broke the schema mid-answer
                raise CutOff(f"the model's answer did not fit the schema (HTTP {exc.status_code})") from exc
            raise ProposalError(f"Groq API error {exc.status_code}: {exc.message}") from exc
        except groq.APIConnectionError as exc:
            raise ProposalError(f"could not reach the Groq API: {exc}") from exc

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise CutOff("the proposal was cut off at the token limit")
        try:
            return shape.model_validate_json(choice.message.content or "")
        except ValueError as exc:
            raise ProposalError(f"the model reply did not parse as a proposal: {exc}") from exc


def fault_groups(records: list[ExperimentResult]) -> list[list[ExperimentResult]]:
    """Records split by fault (faulted service + action), most recent fault first.

    One recommendation per fault: mixing unrelated failures in one request makes the model patch
    several things at once, and halves the room for the code each fix needs.
    """
    groups: dict[tuple[str, str], list[ExperimentResult]] = {}
    for record in records:
        key = (record.service or record.experiment.target, record.experiment.action)
        groups.setdefault(key, []).append(record)
    return list(groups.values())


def experiment_records(
    results_dir: Path, ids: list[str] | None = None, limit: int = 8
) -> list[ExperimentResult]:
    """The named experiment records, or the most recent ones that are real (not dry) runs."""
    results_dir = Path(results_dir)
    if ids:
        paths = [results_dir / f"{i}.json" for i in ids]
        missing = [p.name for p in paths if not p.exists()]
        if missing:
            raise FileNotFoundError(f"no experiment record(s): {', '.join(missing)}")
    else:
        paths = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    records = []
    for path in paths:
        try:
            record = ExperimentResult.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            continue  # telemetry.json, baseline.json, graph.json, ...
        if record.injected_at is None and not ids:
            continue
        # Verification runs are measurements of earlier proposals, not new findings.
        if not ids and ("-before-" in record.experiment.id or "-after-" in record.experiment.id):
            continue
        records.append(record)
        if not ids and len(records) >= limit:
            break
    return records


#: Record fields the agent sees, flattened to `path = value`. These are the same dotted paths the
#: policy's evidence check resolves, so anything the agent cites can be looked up exactly.
RECORD_FIELDS = (
    "experiment.target",
    "experiment.action",
    "experiment.duration_s",
    "experiment.params",
    "verdict",
    "abort_reason",
    "self_healed",
    "restart_count_delta",
    "detection_seconds",
    "impact_seconds",
    "recovery_seconds",
    "before.error_rate_pct_mean",
    "before.p95_ms_mean",
    "during.error_rate_pct_mean",
    "during.error_rate_pct_max",
    "during.p95_ms_max",
    "during.failed_endpoints",
    "during.error_types",
    "after.error_rate_pct_mean",
    "after.p95_ms_mean",
    "observations",
)
#: Keys of a service sheet worth their tokens (env_reads is long and the policy enforces tunable_env).
DOC_KEYS = (
    "language",
    "role",
    "protected",
    "tunable_env",
    "not_tunable",
    "calls",
    "deadlines",
    "fallbacks",
    "failure_behaviour",
    "fixable_by_config",
    "notes",
    "upstream_resources",
)


def record_digest(record: ExperimentResult) -> str:
    data = json.loads(record.model_dump_json())
    lines = []
    for path in RECORD_FIELDS:
        node: Any = data
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node not in (None, [], {}):
            lines.append(f"{path} = {json.dumps(node)}")
    return "\n".join(lines)


def _offset(stamp: datetime, origin: datetime) -> str:
    return f"{(stamp - origin).total_seconds():+.0f}s"


#: A container using at least this share of a configured CPU or memory limit is reported as at its cap.
SATURATED = 0.8


def resource_caps(services: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Configured limits per service in telemetry units: cpu_pct (100 = one core) and mem_mb."""
    caps: dict[str, dict[str, float]] = {}
    for name, svc in services.items():
        cap = {}
        if svc.get("cpus") is not None:
            cap["cpu_pct"] = float(svc["cpus"]) * 100
        if svc.get("mem_limit") is not None and (mb := _memory_mb(str(svc["mem_limit"]))):
            cap["mem_mb"] = mb
        if cap:
            caps[name] = cap
    return caps


def fault_timeline(
    record: ExperimentResult,
    telemetry: TelemetryReader,
    max_lines: int = 30,
    caps: dict[str, dict[str, float]] | None = None,
) -> str:
    """Sample by sample from just before injection to recovery: probes plus container health.

    Phase averages hide the order of events (did the container restart by itself? when did the
    errors stop?), and container state is what any resource or restart-policy fix has to rest on.
    A container running at a configured limit (`caps`) is flagged, since that is invisible in raw %.
    """
    caps = caps or {}
    if record.injected_at is None:
        return "(no injection)"
    origin = record.injected_at
    end = record.recovered_at or record.ended_at or record.rolled_back_at or origin
    start, stop = origin - timedelta(seconds=3), end + timedelta(seconds=3)
    samples = telemetry.between(start, stop)
    if not samples:
        return "(no telemetry history covers this experiment)"
    lines = [
        f"t from injection; rollback at {_offset(record.rolled_back_at, origin)}"
        if record.rolled_back_at
        else "t from injection; no rollback"
    ]
    events: dict[tuple, dict[str, Any]] = {}
    first: dict[str, dict[str, Any]] = {}
    quiet: list[Any] | None = None  # [state, last offset folded in, max p95] of the current healthy run

    def flush_quiet() -> None:
        if quiet and quiet[1]:
            lines.append(f"  ... same through {quiet[1]} (p95 up to {quiet[2]:.0f}ms)")

    for sample in samples[:max_lines]:
        summary = sample.get("summary") or {}
        failed = sorted(n for n, m in (sample.get("metrics") or {}).items() if not m.get("success", True))
        notes = []
        for w in sample.get("workloads") or []:
            base = first.setdefault(w.get("name"), w)
            at_cap = [
                f"{key.split('_')[0]}={w[key]:.0f}/{limit:.0f}{'%' if key == 'cpu_pct' else 'MB'} AT LIMIT"
                for key, limit in (caps.get(w.get("app")) or {}).items()
                if w.get(key) is not None and float(w[key]) >= SATURATED * limit
            ]
            if (
                at_cap
                or w.get("app") == record.service
                or w.get("phase") != "running"
                or w.get("restarts") != base.get("restarts")
                or w.get("oom_killed")
            ):
                cpu = w.get("cpu_pct")
                notes.append(
                    f"{w.get('app')} {w.get('phase')} r={w.get('restarts')}"
                    + (" OOM" if w.get("oom_killed") else "")
                    + (f" cpu={cpu:.0f}%" if cpu is not None and cpu >= 25 and not at_cap else "")
                    + "".join(f" {c}" for c in at_cap)
                )
        for ev in sample.get("recent_events") or []:
            events.setdefault((ev.get("time"), ev.get("action"), ev.get("container")), ev)
        types = ",".join(f"{k}:{v}" for k, v in sorted((summary.get("error_types") or {}).items()))
        state = (f" failed={','.join(failed)} {types}" if failed else "") + (
            " | " + "; ".join(notes) if notes else ""
        )
        stamp = _offset(datetime.fromisoformat(sample["timestamp"]), origin)
        p95 = float(summary.get("p95_latency_ms") or 0)
        if not failed and quiet and quiet[0] == state:
            # Same healthy state as the previous sample: fold it into one line instead of repeating it.
            quiet[1], quiet[2] = stamp, max(quiet[2], p95)
            continue
        flush_quiet()
        line = f"{stamp} err={float(summary.get('error_rate_pct') or 0):.0f}% p95={p95:.0f}ms{state}"
        lines.append(line)
        quiet = [state, None, p95] if not failed else None
    flush_quiet()
    window = sorted(
        (
            ev
            for ev in events.values()
            if ev.get("time") and start <= datetime.fromisoformat(ev["time"]) <= stop
        ),
        key=lambda e: e["time"],
    )
    if window:
        lines.append(
            "docker events: "
            + "; ".join(
                f"{_offset(datetime.fromisoformat(ev['time']), origin)} {ev.get('action')} "
                f"{ev.get('container', '').removeprefix('online-boutique-').removesuffix('-1')}"
                + (f" exit={ev['exit_code']}" if ev.get("exit_code") is not None else "")
                for ev in window
            )
        )
    return "\n".join(lines)


def involved_services(
    records: list[ExperimentResult], docs: dict[str, dict[str, Any]], limited: set[str] = frozenset()
) -> list[str]:
    """Each target, everything that calls it, the frontend, and every service with a resource limit
    (a limit can be the weakness) - the services a diagnosis needs."""
    targets = {r.service or r.experiment.target for r in records}
    names = {"frontend"} | targets | set(limited)
    for name, doc in docs.items():
        if any(t in json.dumps(doc.get("calls") or {}) for t in targets):
            names.add(name)
    return sorted(n for n in names if n in docs)


def docs_digest(
    records: list[ExperimentResult],
    docs_path: Path,
    limited: set[str] = frozenset(),
    compose_settings: bool = True,
) -> str:
    raw = yaml.safe_load(docs_path.read_text(encoding="utf-8")) or {}
    docs = raw.get("services") or {}
    digest = {
        name: {k: docs[name][k] for k in DOC_KEYS if docs[name].get(k)}
        for name in involved_services(records, docs, limited)
    }
    if compose_settings:
        digest["compose_settings"] = raw.get("compose_settings") or {}
    return yaml.safe_dump(digest, sort_keys=False, width=110)


def build_context(
    records: list[ExperimentResult],
    config: TargetConfig,
    services: dict[str, dict[str, Any]],
    docs_path: Path = SERVICE_DOCS,
    reports_dir: Path | None = None,
) -> str:
    """Everything the agent sees, as one user message - dense, to fit small token budgets."""
    telemetry = TelemetryReader(config.results_dir)
    caps = resource_caps(services)
    parts = ["# Experiments (cite evidence as result_id + one of these paths, value copied exactly)", ""]
    for record in records:
        parts += [
            f"## {record.experiment.id}",
            record_digest(record),
            "timeline:",
            fault_timeline(record, telemetry, caps=caps),
        ]
        if report := report_digest(record.experiment.id, reports_dir):
            parts.append(report)
        parts.append("")
    if docs_path.exists():
        parts += [
            "# Service documentation (from the upstream v0.10.7 source; authoritative)",
            docs_digest(records, docs_path, set(caps)),
        ]
    baseline = Path(config.results_dir) / "baseline.json"
    if baseline.exists():
        overall = json.loads(baseline.read_text(encoding="utf-8")).get("overall")
        parts += [f"# Steady-state baseline (overall): {json.dumps(overall)}", ""]
    current = {
        name: {
            "restart": svc.get("restart"),
            # Resource limits only where set: a resource fix has to start from what is configured.
            **{k: svc[k] for k in ("cpus", "mem_limit", "mem_reservation") if svc.get(k) is not None},
            "environment": {
                k: v
                for k, v in (svc.get("environment") or {}).items()
                if not k.endswith("_ADDR") or k == "REDIS_ADDR"
            },
        }
        for name, svc in services.items()
    }
    parts += [
        f"# Current Compose settings: {json.dumps(current, separators=(',', ':'))}",
        "",
        f"Limits: experiments run at most {config.effective_max_seconds}s; abort at error rate "
        f"{config.thresholds.error_rate_pct:g}% or p95 {config.thresholds.p95_ms:g}ms.",
        "Propose one fix for the most important weakness these records show, or decline.",
    ]
    return "\n".join(parts)


#: Upper bound on source lines shown to the code agent; `build_code_context` also caps them by size.
MAX_CODE_LINES = 360


FENCE = {"go": "go", "python": "python", "c#": "csharp", "java": "java", "node": "javascript"}
#: Faults injected from outside the service: a fix belongs in its callers, not in its own handlers.
OUTAGE_ACTIONS = frozenset({"kill", "stop", "pause"})


def code_excerpts(
    records: list[ExperimentResult],
    docs: dict[str, dict[str, Any]],
    checkout: Path,
    max_chars: int | None = None,
) -> tuple[str, list[str]]:
    """The source a code fix for these records needs, and what was left out for size.

    The failure path is each faulted service, everything that calls it, and the frontend. Shown:
    the faulted services' own request handlers (`code.serves`), every call site on the path into
    another service on it (`code.calls`), and the handler of every page that failed
    (`code.pages`). With none of that (a pure load problem, say), every call site and handler of
    the faulted services. Faulted services come first, then the frontend, within MAX_CODE_LINES
    and `max_chars` (what is left of the request budget); whatever does not fit is listed as omitted.

    What gets the budget first depends on the fault. A kill, stop or pause is done to a service
    from outside, so its own handlers are not where a fix goes; the callers are (deadlines,
    fallbacks). For those, call sites and failed pages come first and the faulted service's
    handlers only get what is left. Under load (spike, throttle) the faulted code itself may be
    the problem, so its handlers rank with the call sites.
    """
    targets = {r.service or r.experiment.target for r in records}
    path = set(involved_services(records, docs)) | targets
    pages = {p for r in records for p in r.during.failed_endpoints}
    mapped = {name: doc for name, doc in docs.items() if doc.get("code")}
    outage = all(r.experiment.action in OUTAGE_ACTIONS for r in records)

    def selection(service: str, everything: bool) -> list[tuple[int, dict[str, Any]]]:
        """(priority, entry): 0 is shown first."""
        code = mapped[service]["code"]
        calls, serves = code.get("calls") or {}, code.get("serves") or {}
        if everything:
            return [(0, v) for v in [*serves.values(), *calls.values()]]
        # Direct calls into a faulted service; failing that, calls one hop away through a service
        # on the path (the frontend reaches redis-cart only through cartservice).
        direct = [v for k, v in calls.items() if k in targets and k != service]
        hop = [v for k, v in calls.items() if k in path and k != service]
        wanted = [(0, v) for v in (direct or hop)] if service in path else []
        wanted += [(0, v) for k, v in (code.get("pages") or {}).items() if k in pages]
        own = [(1 if outage else 0, v) for v in serves.values()] if service in targets else []
        return own + wanted

    order = sorted(mapped, key=lambda n: (n not in targets, n != "frontend"))
    chosen = {n: selection(n, False) for n in order}
    if not any(chosen.values()):
        chosen = {n: selection(n, True) for n in order if n in targets}

    # Every candidate function in display order, then admitted to the budget in priority order.
    files: dict[tuple[str, str], dict[str, Any]] = {}
    candidates: list[tuple[int, int, tuple[str, str], str, int, str]] = []
    omitted: list[str] = []
    for service, wanted in chosen.items():
        language = mapped[service].get("language", "")
        for priority, entry in wanted:
            key = (service, entry["file"])
            if key not in files:
                text = (checkout / mapped[service]["code"]["dir"] / entry["file"]).read_text(encoding="utf-8")
                fence = next((v for k, v in FENCE.items() if language.lower().startswith(k)), "")
                header = [f"### {service}: {entry['file']}", f"```{fence}"]
                if imports := file_imports(text, language):
                    header += [imports, ""]
                files[key] = {"text": text, "language": language, "header": header, "names": {}}
            for name in entry["functions"]:
                known = files[key]["names"]
                if name in known:
                    known[name] = min(known[name], priority)
                    continue
                known[name] = priority
    for key, info in files.items():
        for name, priority in info["names"].items():
            try:
                line, body = find_function(info["text"], name, info["language"])
            except SourceError:
                omitted.append(f"{key[0]}/{key[1]}:{name} (not found)")
                continue
            candidates.append((priority, len(candidates), key, name, line, body))

    lines_left = MAX_CODE_LINES
    chars_left = max_chars if max_chars is not None else float("inf")
    shown: set[int] = set()
    opened: set[tuple[str, str]] = set()
    for _priority, index, key, name, _line, body in sorted(candidates):
        size, chars = body.count("\n") + 2, len(body) + len(name) + len(key[1]) + 16
        if key not in opened:  # the file's heading and imports are paid for with its first function
            header = "\n".join(files[key]["header"]) + "\n```\n"
            size, chars = size + header.count("\n") + 1, chars + len(header)
        if size > lines_left or chars > chars_left:
            omitted.append(f"{key[0]}/{key[1]}:{name}")
            continue
        lines_left, chars_left = lines_left - size, chars_left - chars
        shown.add(index)
        opened.add(key)

    parts: list[str] = []
    for key, info in files.items():
        blocks = [c for c in candidates if c[2] == key and c[1] in shown]
        if not blocks:
            continue
        comment = "#" if family(info["language"]) == "python" else "//"
        parts += info["header"]
        for _, _, _, name, line, body in blocks:
            parts += [f"{comment} {key[1]}:{line} ({name})", body, ""]
        parts += ["```", ""]
    return "\n".join(parts), omitted


def build_code_context(
    declined: Proposal,
    records: list[ExperimentResult],
    checkout: Path,
    docs_path: Path = SERVICE_DOCS,
    reports_dir: Path | None = None,
    extra: str = "",
) -> str:
    """What the code agent sees: the declined diagnosis, the records, the docs, the fix recipes
    that apply, the source - and `extra` (a previous draft and its review, when revising).

    The source excerpts get whatever the rest of the request leaves of MAX_REQUEST_TOKENS, so the
    whole request (system prompt and schema included) stays under Groq's per-minute limit.
    """
    raw = yaml.safe_load(docs_path.read_text(encoding="utf-8")) or {}
    docs = raw.get("services") or {}
    source = raw.get("source") or {}
    head = [
        f"# Declined configuration proposal {declined.id}",
        f"diagnosis: {declined.diagnosed_problem}",
        f"why config cannot fix it: {declined.declined_reason}",
        "",
        "# Experiments (cite evidence as result_id + one of these paths, value copied exactly)",
        "",
    ]
    for record in records:
        head += [f"## {record.experiment.id}", record_digest(record)]
        if report := report_digest(record.experiment.id, reports_dir):
            head.append(report)
        head.append("")
    head += [
        "# Service documentation (from the upstream source; authoritative)",
        docs_digest(records, docs_path, compose_settings=False),
    ]
    if recipes := fix_recipes(raw, records):
        head += ["# Fix recipes (patterns a reviewer accepts here; adapt, do not copy blindly)", recipes, ""]
    head += [
        f"# Source: {source.get('repo')} {source.get('tag')} (commit {str(source.get('commit'))[:10]})",
        "",
    ]
    tail = " " * (400 + len(extra))  # room for the omitted list, the closing line, a revision's extra
    system, schema = wire_schema(CodeProposalDraft, CODE_SYSTEM_PROMPT)
    used = estimate_request(system, "\n".join(head) + tail, json.dumps(schema))
    room = int((MAX_REQUEST_TOKENS - used) * MESSAGE_CHARS_PER_TOKEN)
    excerpts, omitted = code_excerpts(records, docs, checkout, max(room, 0))
    parts = [*head, excerpts]
    if omitted:
        parts.append(f"(not shown, to fit the context: {', '.join(omitted)})")
    if extra:
        parts.append(extra)
    parts.append("Recommend one small code change that fixes the declined weakness, or decline.")
    return "\n".join(parts)


def fix_recipes(raw_docs: dict[str, Any], records: list[ExperimentResult]) -> str:
    """The `fix_recipes` from docs/services.yaml that apply to these faults (by action), one line each."""
    actions = {r.experiment.action for r in records}
    lines = []
    for name, recipe in (raw_docs.get("fix_recipes") or {}).items():
        if actions & set(recipe.get("faults") or actions):
            lines.append(f"- {name}: {' '.join(str(recipe.get('recipe', '')).split())}")
    return "\n".join(lines)


#: What each fault means for a fix, so the reviewer does not demand the wrong one.
FAULT_MEANING = {
    "kill": "the dependency is down: calls to it fail at once, so a deadline does not help; "
    "the fix is to survive the error",
    "stop": "the dependency is down: calls to it fail at once, so a deadline does not help; "
    "the fix is to survive the error",
    "pause": "the dependency is frozen: calls to it hang, so the fix needs a deadline on them",
    "spike": "the system is under load: slow calls, so deadlines or less work per request",
    "throttle": "the dependency is CPU-starved: slow calls, so deadlines or less work per request",
}


def critique_context(
    draft: CodeProposalDraft,
    declined: Proposal,
    patch: str,
    checked: list[str],
    records: list[ExperimentResult] | None = None,
) -> str:
    """What the reviewer sees: small on purpose (the fault, the diagnosis and the patch, not the
    whole source)."""
    faults = {(r.service or r.experiment.target, r.experiment.action) for r in records or []}
    parts = [
        *[f"# Fault: {action} on {target} - {FAULT_MEANING.get(action, '')}" for target, action in faults],
        f"# Diagnosis\n{draft.diagnosed_problem}",
        f"(the declined configuration proposal {declined.id} said: {declined.declined_reason})",
        "",
        "# The recommendation",
        *[f"- {e.service}/{e.file} ({e.function or 'file level'}): {e.purpose}" for e in draft.edits],
        f"expected benefit: {draft.expected_benefit}",
        f"side effects it admits: {'; '.join(draft.side_effects) or 'none'}",
        "",
        "```diff",
        patch.rstrip() or "(the edits did not apply to the source - see the automated findings)",
        "```",
    ]
    if checked:
        parts += ["", "# Automated checks found", *[f"- {p}" for p in checked]]
    parts.append("\nList what is wrong with this recommendation, or nothing if it is sound.")
    return "\n".join(parts)


def revision_note(draft: CodeProposalDraft, patch: str, issues: list[str]) -> str:
    """The `extra` for a revision request: the previous draft as a patch, and what was wrong with it."""
    return "\n".join([
        "# Your previous draft (as a patch) and its review",
        "```diff",
        patch.rstrip() or "(it did not apply: " + "; ".join(issues[:2]) + ")",
        "```",
        f"previous diagnosis: {draft.diagnosed_problem}",
        "problems found:",
        *[f"- {i}" for i in issues],
        "Write the corrected recommendation in full (edits quote the original source above, not your "
        "draft). If the review is wrong about something, keep that part and say why in purpose.",
    ])

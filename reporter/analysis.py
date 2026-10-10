"""The hardcoded stand-in for the LLM: rule-based analysis of an evidence pack.

TODO(llm): when the real model is wired in, `Reporter` sends reporter/prompts.py to it instead and
this module is no longer the default. It stays as the permanent fallback: with no key, or if the
model is unreachable or returns something invalid, the report is still produced from these rules.

The function takes the evidence pack as a plain dict (exactly what the model would receive as JSON)
and returns a dict that validates as `reporter.models.Analysis`. Every claim cites evidence ids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agents.llm import stub_handler

from .prompts import TASK

#: What each Docker event means in a sentence.
_ACTION_TEXT = {
    "kill": "was killed",
    "die": "exited",
    "stop": "was stopped",
    "pause": "was frozen (paused)",
    "oom": "ran out of memory",
    "restart": "was restarted",
    "start": "started again",
    "unpause": "was unfrozen",
}
_FAILURE_ACTIONS = ("kill", "die", "stop", "pause", "oom")
SLOW_DETECTION_S = 6.0
SLOW_RECOVERY_S = 15.0


def _t(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _clock(value: datetime | None) -> str:
    return value.astimezone().strftime("%H:%M:%S") if value else "an unknown time"


def _seconds(a: datetime | None, b: datetime | None) -> float | None:
    return round((a - b).total_seconds(), 1) if a and b else None


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


class _Pack:
    """Convenience view over the evidence dict."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.refs: dict[str, str] = payload.get("refs") or {}
        self.items = {i["id"]: i for i in payload.get("timeline") or []}
        self.facts: dict[str, Any] = payload.get("facts") or {}
        self.exp: dict[str, Any] | None = payload.get("experiment")
        self.graph: dict[str, Any] = payload.get("graph") or {}
        self.findings: list[dict[str, Any]] = payload.get("log_findings") or []
        self.notes: list[str] = payload.get("notes") or []
        self.kind: str = payload.get("kind", "incident")
        self.subject: str = payload.get("subject", "")

    def ids(self, *keys: str) -> list[str]:
        return [self.refs[k] for k in keys if k in self.refs]

    def time_of(self, item_id: str) -> datetime | None:
        return _t((self.items.get(item_id) or {}).get("t"))

    def text_of(self, item_id: str) -> str:
        return (self.items.get(item_id) or {}).get("event", "")

    def earliest(self, ids: list[str]) -> datetime | None:
        times = [t for t in (self.time_of(i) for i in ids) if t]
        return min(times) if times else None

    def keys_with(self, prefix: str, suffix: str = "") -> list[str]:
        return [k for k in self.refs if k.startswith(prefix) and k.endswith(suffix)]


# --------------------------------------------------------------------------- origin
def _origin(pack: _Pack) -> dict[str, Any] | None:
    if pack.exp:
        service = pack.exp.get("service") or pack.exp.get("target")
        signal_keys = [
            k for k in pack.keys_with("docker:", f":{service}") if k.split(":")[1] in _FAILURE_ACTIONS
        ] + [f"service_down:{service}"]
        signal_ids = pack.ids(*signal_keys)
        injected = pack.ids("runner:injected")
        if signal_ids:
            when = pack.earliest(signal_ids)
            return {
                "component": service,
                "first_degraded_at": when,
                "basis": "observed",
                "evidence": injected + signal_ids,
                "note": f"injected by the runner ({pack.exp.get('action')}); the failure was independently "
                f"confirmed by {_join([pack.text_of(i) for i in signal_ids[:2]])}",
            }
        when = pack.earliest(injected)
        return {
            "component": service,
            "first_degraded_at": when,
            "basis": "inferred",
            "evidence": injected,
            "note": "injected by the runner, but no Docker event or health check showed it going down; "
            "the time is the injection time",
        }

    candidates: list[tuple[datetime, str, list[str]]] = []
    for service in pack.facts.get("first_service_down") or {}:
        ids = pack.ids(f"service_down:{service}")
        when = pack.earliest(ids)
        if when:
            candidates.append((when, service, ids))
    for key in pack.keys_with("docker:"):
        action, service = key.split(":")[1], key.split(":")[2]
        if action in _FAILURE_ACTIONS:
            ids = pack.ids(key)
            when = pack.earliest(ids)
            if when:
                candidates.append((when, service, ids))
    if candidates:
        when, service, ids = min(candidates, key=lambda c: c[0])
        return {
            "component": service,
            "first_degraded_at": when,
            "basis": "observed",
            "evidence": ids,
            "note": "first service to go down; no experiment was running, so the cause is not known",
        }

    pages = pack.facts.get("first_page_failure") or {}
    if pages:
        page = min(pages, key=lambda p: pages[p])
        ids = pack.ids(f"page_fail:{page}")
        return {
            "component": f"{page} page",
            "first_degraded_at": pack.earliest(ids),
            "basis": "inferred",
            "evidence": ids,
            "note": "no service was seen going down; this is the first visible symptom",
        }
    return None


def _signal_text(pack: _Pack, origin: dict[str, Any]) -> str:
    """The first event that shows the origin failing, as a short phrase."""
    service = origin["component"]
    for key in pack.keys_with("docker:", f":{service}"):
        action = key.split(":")[1]
        if action in _FAILURE_ACTIONS:
            exit_note = ""
            text = pack.text_of(pack.refs[key])
            if "(" in text:
                exit_note = " " + text[text.index("(") :]
            return f"{service} {_ACTION_TEXT.get(action, action)}{exit_note}"
    down = pack.refs.get(f"service_down:{service}")
    if down:
        return pack.text_of(down)
    return f"{service} was the injected target"


# --------------------------------------------------------------------------- propagation
def _propagation(pack: _Pack, origin: dict[str, Any] | None) -> list[dict[str, Any]]:
    if origin is None:
        return []
    origin_at = origin["first_degraded_at"]
    callers = set(pack.graph.get("callers") or [])
    hops: list[dict[str, Any]] = [
        {
            "component": origin["component"],
            "kind": "origin",
            "at": origin_at,
            "seconds_after_origin": 0.0,
            "evidence": origin["evidence"],
            "note": _signal_text(pack, origin),
        }
    ]

    downs = pack.facts.get("first_service_down") or {}
    for service in sorted(downs, key=lambda s: downs[s]):
        if service == origin["component"]:
            continue
        ids = pack.ids(f"service_down:{service}")
        when = pack.earliest(ids)
        if service in callers:
            note = f"went down; it calls {origin['component']} (directly or through another service)"
        else:
            note = f"went down; the graph shows no dependency on {origin['component']}"
        hops.append(
            {
                "component": service,
                "kind": "service",
                "at": when,
                "seconds_after_origin": _seconds(when, origin_at),
                "evidence": ids,
                "note": note,
            }
        )

    pages = pack.facts.get("first_page_failure") or {}
    for page in sorted(pages, key=lambda p: pages[p]):
        ids = pack.ids(f"page_fail:{page}")
        when = pack.earliest(ids)
        hops.append(
            {
                "component": f"{page} page",
                "kind": "page",
                "at": when,
                "seconds_after_origin": _seconds(when, origin_at),
                "evidence": ids,
                "note": pack.text_of(ids[0]) if ids else "page failed",
            }
        )
    return hops


def _failure_point(pack: _Pack, origin: dict[str, Any] | None, hops: list[dict[str, Any]]) -> str:
    if origin is None:
        return "No failing component could be identified from the evidence."
    parts = [
        f"{_signal_text(pack, origin)} at {_clock(origin['first_degraded_at'])}"
        + ("" if origin["basis"] == "observed" else " (injection time; not independently confirmed)")
    ]

    relevant = {origin["component"], *(pack.graph.get("callers") or [])}
    logged = [f for f in pack.findings if (f.get("service") or "") in relevant]
    if logged:
        bits = [f"{f['service']} logged {f['kind']} x{f['count']}" for f in logged[:3]]
        parts.append("in the logs, " + _join(bits))

    pages = [h for h in hops if h["kind"] == "page"]
    if pages:
        first = pages[0]
        names = [h["component"].removesuffix(" page") for h in pages]
        lag = first["seconds_after_origin"]
        lag_text = f" ({lag:+.0f} s after the origin)" if lag is not None else ""
        parts.append(
            f"the first page to fail was {first['component']} at {_clock(first['at'])}{lag_text}; "
            f"pages affected: {_join(names)}"
        )
    else:
        parts.append("no page probe failed")
    return "; ".join(parts) + "."


# --------------------------------------------------------------------------- judgements
def _severity(pack: _Pack) -> str:
    failed = set(pack.facts.get("failed_pages") or [])
    if "Checkout" in failed or len(failed) >= 3:
        return "critical"
    if failed:
        return "major"
    if pack.facts.get("services_down_ever"):
        return "minor"
    return "none"


def _what_worked(pack: _Pack, origin: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    anchor = (origin or {}).get("evidence") or []
    failed = set(pack.facts.get("failed_pages") or [])
    stayed_up = pack.facts.get("pages_never_failed") or []

    other_down = [
        s for s in pack.facts.get("services_down_ever") or [] if not origin or s != origin["component"]
    ]
    if pack.exp and not failed and not other_down:
        out.append(
            {
                "text": "The application tolerated the failure: no page probe failed while it was in place.",
                "evidence": pack.ids("runner:injected"),
                "basis": "observed",
            }
        )
    elif stayed_up and failed:
        out.append(
            {
                "text": f"These pages stayed healthy throughout: {_join(list(stayed_up))}.",
                "evidence": anchor[:1],
                "basis": "observed",
            }
        )

    expected = pack.graph.get("expected_pages") or []
    held = [p for p in expected if p not in failed]
    if held and failed:
        out.append(
            {
                "text": f"{_join(held)} should have broken with {pack.graph.get('service')} down "
                f"(service-to-page map) but did not: the app degraded gracefully there.",
                "evidence": anchor[:1],
                "basis": "inferred",
            }
        )

    if pack.exp and pack.exp.get("self_healed"):
        restart_ids = pack.ids(
            *[k for k in pack.refs if k.startswith("docker:") and k.split(":")[1] in ("start", "restart")][:1]
        )
        out.append(
            {
                "text": f"Docker's restart policy brought "
                f"{pack.exp.get('service') or pack.exp.get('target')} "
                "back without help.",
                "evidence": restart_ids or anchor[:1],
                "basis": "observed",
            }
        )

    if pack.exp and pack.exp.get("verdict") in ("aborted", "stopped"):
        out.append(
            {
                "text": f"The safety layer ended the run and rolled the failure back "
                f"({pack.exp.get('abort_reason')}).",
                "evidence": pack.ids("runner:abort", "runner:rollback"),
                "basis": "observed",
            }
        )

    recovery = (pack.exp or {}).get("recovery_seconds")
    if recovery is not None and recovery <= SLOW_RECOVERY_S:
        out.append(
            {
                "text": f"The application was healthy again {recovery:g} s after rollback.",
                "evidence": pack.ids("runner:recovered"),
                "basis": "observed",
            }
        )
    return out


def _anomalies(pack: _Pack, origin: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    failed = set(pack.facts.get("failed_pages") or [])
    expected = set(pack.graph.get("expected_pages") or [])
    exp = pack.exp or {}

    detection = exp.get("detection_seconds")
    if detection is not None and detection > SLOW_DETECTION_S:
        out.append(
            {
                "text": f"The failure was only detected {detection:g} s after injection "
                f"(the probe interval is about 2 s).",
                "evidence": pack.ids("runner:injected", "runner:detected"),
                "basis": "observed",
            }
        )

    recovery = exp.get("recovery_seconds")
    if recovery is not None and recovery > SLOW_RECOVERY_S:
        out.append(
            {
                "text": f"Recovery took {recovery:g} s after rollback.",
                "evidence": pack.ids("runner:rollback", "runner:recovered"),
                "basis": "observed",
            }
        )
    if any("still not fully healthy" in o for o in exp.get("observations") or []):
        out.append(
            {
                "text": "The application was still not healthy long after rollback.",
                "evidence": pack.ids("runner:rollback"),
                "basis": "observed",
            }
        )

    if exp.get("verdict") in ("aborted", "stopped"):
        out.append(
            {
                "text": "The run was cut short, so the failure was not observed for its full duration.",
                "evidence": pack.ids("runner:abort"),
                "basis": "observed",
            }
        )

    if expected and failed:
        surprise = sorted(failed - expected)
        if surprise:
            out.append(
                {
                    "text": f"{_join(surprise)} failed although the graph does not list "
                    f"{pack.graph.get('service')} as one of its dependencies.",
                    "evidence": pack.ids(*[f"page_fail:{p}" for p in surprise]),
                    "basis": "inferred",
                }
            )

    if origin:
        callers = set(pack.graph.get("callers") or [])
        for service in pack.facts.get("services_down_ever") or []:
            if service != origin["component"] and service not in callers and pack.graph:
                out.append(
                    {
                        "text": f"{service} also went down but does not depend on {origin['component']}: "
                        "an independent failure or collateral damage.",
                        "evidence": pack.ids(f"service_down:{service}"),
                        "basis": "inferred",
                    }
                )

    if exp and not exp.get("baseline"):
        out.append(
            {
                "text": "No baseline was recorded, so these numbers cannot be compared with normal "
                "(run scripts\\baseline.bat).",
                "evidence": [],
                "basis": "unknown",
            }
        )
    for error in exp.get("errors") or []:
        out.append({"text": f"The runner recorded an error: {error}", "evidence": [], "basis": "observed"})
    for note in pack.notes:
        out.append({"text": note, "evidence": [], "basis": "unknown"})
    return out


def _headline(pack: _Pack, severity: str, origin: dict[str, Any] | None) -> str:
    failed = pack.facts.get("failed_pages") or []
    peak = pack.facts.get("peak_error_rate_pct") or 0
    if pack.exp:
        label = f"{pack.exp.get('action')} on {pack.exp.get('service') or pack.exp.get('target')}"
        if severity == "none":
            return f"{label}: tolerated, no user-visible failure"
        parts = [f"{label}: {_join(failed) + ' failed' if failed else 'service down, pages unaffected'}"]
        if peak:
            parts.append(f"peak error rate {peak:g}%")
        recovery = pack.exp.get("recovery_seconds")
        if recovery is not None:
            parts.append(f"healthy {recovery:g} s after rollback")
        return ", ".join(parts)
    where = origin["component"] if origin else "unknown component"
    return f"Incident: {where} failed first; pages affected: {_join(failed) if failed else 'none'}"


def _summary(pack: _Pack, failure_point: str) -> str:
    sentences: list[str] = []
    exp = pack.exp
    if exp:
        sentences.append(
            f"Experiment {exp['id']} ({exp['action']} on {exp.get('service') or exp['target']}, "
            f"{exp['duration_s']} s) finished {exp['verdict'].upper()}"
            + (f": {exp['abort_reason']}." if exp.get("abort_reason") else ".")
        )
    else:
        sentences.append("Agent 2 detected a failure on its own while no experiment was running.")
    sentences.append("Failure point: " + failure_point)

    peak = pack.facts.get("peak_error_rate_pct") or 0
    p95 = pack.facts.get("peak_p95_ms") or 0
    impact = f"Error rate peaked at {peak:g}% and p95 latency at {p95:g} ms"
    error_types = pack.facts.get("error_types") or {}
    if error_types:
        impact += f" (errors seen: {', '.join(f'{k} x{v}' for k, v in error_types.items())})"
    sentences.append(impact + ".")

    if exp and exp.get("self_healed"):
        sentences.append("Docker's restart policy recovered the container before the runner rolled back.")
    if exp and exp.get("recovery_seconds") is not None:
        sentences.append(
            f"The application looked healthy again {exp['recovery_seconds']:g} s after rollback."
        )
    return " ".join(sentences)


# --------------------------------------------------------------------------- entry point
@stub_handler(TASK)
def analyse(payload: dict[str, Any]) -> dict[str, Any]:
    pack = _Pack(payload)
    origin = _origin(pack)
    hops = _propagation(pack, origin)
    failure_point = _failure_point(pack, origin, hops)
    severity = _severity(pack)
    return {
        "headline": _headline(pack, severity, origin),
        "severity": severity,
        "summary": _summary(pack, failure_point),
        "failure_point": failure_point,
        "origin": origin,
        "propagation": hops,
        "what_worked": _what_worked(pack, origin),
        "anomalies": _anomalies(pack, origin),
    }

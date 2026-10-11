import json
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import altair as alt

import boutique_core as core
import resilience
import service_graphs as graphs

# ---------------- Page / state ----------------
st.set_page_config(page_title="Dashboard", layout="wide")

if "action_message" not in st.session_state:
    st.session_state.action_message = None
if "service_graphs" not in st.session_state:
    st.session_state.service_graphs = graphs.new_state()

st.title("Dashboard")

# ---------------- Kill switch (sidebar) ----------------
# The orchestrator writes the journal; the dashboard reads it, and writes STOP on request.
STOP_FILE = core.OUT_DIR / "STOP"
JOURNAL_FILE = core.OUT_DIR / "active_experiment.json"


def _engage_stop():
    try:
        core.OUT_DIR.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text("stopped from the dashboard", encoding="utf-8")
    except OSError as exc:
        st.session_state.action_message = ("error", f"Could not write the stop file: {exc}")


def _clear_stop():
    try:
        STOP_FILE.unlink(missing_ok=True)
    except OSError as exc:
        st.session_state.action_message = ("error", f"Could not clear the kill switch: {exc}")


def _active_experiment():
    """(action, container, experiment id, seconds in place) from the journal, or None."""
    if not JOURNAL_FILE.exists():
        return None
    try:
        entry = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
        held = max(0, int(time.time() - float(entry.get("injected_at", time.time()))))
        return entry.get("action"), entry.get("container"), entry.get("experiment_id"), held
    except (OSError, ValueError, TypeError):
        return "unknown", "unknown", "unreadable journal", 0


with st.sidebar:
    st.header("KILL SWITCH")
    if STOP_FILE.exists():
        st.error("ENGAGED: a running experiment rolls back on its next check, and no new one can start.")
        st.button("Clear the kill switch", width="stretch", on_click=_clear_stop)
    else:
        st.button("STOP the experiment", type="primary", width="stretch", on_click=_engage_stop)
    _active = _active_experiment()
    if _active:
        st.warning(f"Injected now: **{_active[0]}** on **{_active[1]}** for {_active[3]}s "
                   f"(`{_active[2]}`)")
        st.caption("If this stays after the run ends, use scripts\\recover.bat.")
    else:
        st.caption("No experiment is injected.")
    if st.session_state.action_message:
        _level, _message = st.session_state.action_message
        {"success": st.success, "error": st.error, "warning": st.warning}.get(_level, st.info)(_message)
    st.divider()
    st.header("CONNECTION")
    base_url = st.text_input("Boutique base URL", value=core.DEFAULT_BASE_URL).rstrip("/")
    refresh_seconds = st.slider("Dashboard refresh (seconds)", 1, 10, 2)
    st.divider()
    st.subheader("THRESHOLDS")
    latency_warn = st.number_input("Latency warning (ms)", min_value=1, max_value=10000, value=core.LATENCY_WARN_MS)
    latency_sla = st.number_input("Latency SLA breach (ms)", min_value=1, max_value=30000, value=core.LATENCY_SLA_MS)

try:
    from streamlit_autorefresh import st_autorefresh
    if st.session_state.get("open_card") is None:
        st_autorefresh(interval=refresh_seconds * 1000, key="health_refresh")
except ImportError:
    st.info("For automatic refresh, install requirements.txt (includes streamlit-autorefresh). The Refresh now button still works.")
    if st.button("Refresh now"):
        st.rerun()


# ---------------- Collect ----------------
# Runtime, Compose project and telemetry writing are not shown in the sidebar: they come from the
# environment (BOUTIQUE_BACKEND / BOUTIQUE_COMPOSE_PROJECT / BOUTIQUE_NAMESPACE via boutique_core,
# and BOUTIQUE_DASHBOARD_WRITES=0, which scripts\monitor.bat sets because the monitor writes telemetry).
backend, project, namespace = core.DEFAULT_BACKEND, core.DEFAULT_COMPOSE_PROJECT, core.DEFAULT_NAMESPACE
write_telemetry_on = os.getenv("BOUTIQUE_DASHBOARD_WRITES", "1") != "0"
# All probing runs on background threads (core.LiveSampler); this script only reads the newest
# sample, so every refresh finishes immediately even while a stopped service makes probes slow.
live = core.shared_live_sampler(base_url, backend, namespace, project, interval=float(refresh_seconds))
live.write, live.sla_ms = write_telemetry_on, latency_sla
if live.latest() is None:
    live.wait_for_first(timeout=8)
bundle = live.latest()
if bundle is None:
    st.info("Collecting the first sample…"
            + (f" (last error: {live.last_error})" if live.last_error else ""))
    st.stop()

sample, probe_rows, snap = bundle["sample"], bundle["probe_rows"], bundle["snap"]
write_error = bundle["write_error"]
history = live.history()
journey = live.journey

summary = sample["summary"]
error_rate = summary["error_rate_pct"]
p95_latency = summary["p95_latency_ms"]
mean_latency = summary["mean_latency_ms"]
unit = "containers" if backend == "docker" else "pods"
services_down = summary.get("services_down") or []

# ---------------- Overview ----------------
st.subheader("LIVE OVERVIEW")
if services_down:
    st.error(f"SERVICE DOWN — {', '.join(services_down)}. "
             "See *Service health* below for what users notice.")
if error_rate > 0 or p95_latency > latency_sla:
    st.error("HEALTH DEGRADED — HTTP probe failures or latency SLA breach detected.")
elif services_down:
    st.warning("Page probes still pass, but at least one service is down "
               "(a failure behind the frontend).")
elif p95_latency >= latency_warn:
    st.warning("WARNING — Latency is above the warning threshold.")
else:
    st.success("PROBES HEALTHY — No probe errors or latency threshold breaches in this sample.")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Probe success", f"{100-error_rate:.1f}%", delta=f"{-error_rate:.1f} pp errors" if error_rate else "No errors")
c2.metric("Mean latency", f"{mean_latency:.1f} ms", delta="High" if mean_latency >= latency_warn else "Normal",
          delta_color="inverse" if mean_latency >= latency_warn else "normal")
c3.metric("p95 latency", f"{p95_latency:.1f} ms",
          delta="SLA breach" if p95_latency > latency_sla else ("Warning" if p95_latency >= latency_warn else "Normal"),
          delta_color="inverse" if p95_latency >= latency_warn else "normal")
c4.metric(f"Ready {unit}", f"{snap['ready_pods']}/{snap['total_pods']}" if snap["available"] else "N/A",
          delta=backend.capitalize() if snap["available"] else "Unavailable")
c5.metric("Container restarts" if backend == "docker" else "Pod restarts", str(snap["restarts"]) if snap["available"] else "N/A")

if bundle["age_s"] > max(6, 3 * refresh_seconds):
    st.warning(f"The newest sample is {bundle['age_s']:.0f}s old: probing is slow right now "
               "(a hung dependency makes page probes wait for their timeout). The page keeps updating.")
if live.last_error:
    st.warning(f"Sampler error: {live.last_error}")
journey_info = journey.info()
if journey_info["status"] == "unverified":
    st.warning("Checkout probe is not working yet, so checkoutservice and paymentservice are covered only by "
               f"their container state and health check. Retrying every {core.JOURNEY_RETRY_EVERY} samples. "
               f"Last failure: {journey_info['last_error'] or 'none yet'}")
elif journey_info["status"] == "off":
    st.caption(f"Checkout probe off ({journey_info['disabled_reason']}).")
if not live.health.latest() and core.SERVICE_PROBES_ON:
    st.caption("Direct service health checks are starting (or every check failed to run); "
               "container state is still used.")
if write_error:
    st.warning(f"Could not write telemetry files: {write_error}")


# ---------------- Resilience score ----------------
# Built from the whole telemetry log and every experiment result, so the files are re-read at most
# every 30 s rather than on each refresh; the live sample only feeds health-check coverage and redundancy.
@st.cache_data(ttl=30, show_spinner=False)
def _resilience_inputs(log_path: str, results_dir: str):
    return resilience.load_history(Path(log_path)), resilience.load_experiments(Path(results_dir))


st.divider()
st.subheader("RESILIENCE SCORE")
_history, _experiments = _resilience_inputs(str(core.LOG_PATH), str(core.OUT_DIR))
_res = resilience.compute(
    _history, _experiments, sample.get("services") or [], sample.get("workloads") or [],
    monitored={"frontend", *(core.SERVICE_PROBES if core.SERVICE_PROBES_ON else ())},  # page probes + checks
    sla_ms=latency_sla, timeout_ms=core.PROBE_TIMEOUT_S * 1000, p95_limit_ms=latency_sla,
)
if _res["score"] is None:
    st.info("No data to score yet: run the monitor for a while and at least one experiment.")
else:
    _colour = {"Resilient": "green", "Moderate": "orange"}.get(_res["band"], "red")
    s1, s2 = st.columns([1, 3])
    s1.metric("Overall", f"{_res['score']:.0f} / 100")
    s1.markdown(f"### :{_colour}[{_res['band']}]")
    s2.table(
        pd.DataFrame([
            {
                "Factor": resilience.LABELS[key],
                "Weight": (f"{f['effective_weight']:.0%}" if f["score"] is not None
                           else f"{f['weight']:.0%} (no data)"),
                "Score": f"{f['score']:.0f} / 100" if f["score"] is not None else "no data",
                "Evidence": f["evidence"],
            }
            for key, f in _res["factors"].items()
        ]).set_index("Factor")
    )

# ---------------- Service health ----------------
# ---------------- Recent attack: failure report + recommendation ----------------
REPORTS_DIR = Path(os.getenv("BOUTIQUE_REPORTS_DIR") or core.OUT_DIR.parent / "reports")


def _read_report(name):
    """reports/<name>.json + .md as (data, markdown), or (None, None)."""
    try:
        data = json.loads((REPORTS_DIR / f"{name}.json").read_text(encoding="utf-8"))
        return data, (REPORTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None, None


def _latest_attack():
    """The newest finished experiment in results/. The runner writes results/<id>.json only after
    the rollback and the wait for recovery, so the section moves on once the system is healthy again."""
    newest = None
    for path in core.OUT_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "verdict" not in data or not data.get("ended_at"):
            continue
        if newest is None or data["ended_at"] > newest["ended_at"]:
            newest = data
    return newest


def _recent_failure():
    """(report, markdown, pending result). The newest finished attack's report; an incident report the
    watcher wrote after that attack wins. If the newest attack has no report yet, pending is its result."""
    latest, latest_md = _read_report("latest")
    attack = _latest_attack()
    if attack is None:
        return latest, latest_md, None
    ended = datetime.fromisoformat(attack["ended_at"]).timestamp()
    if latest is not None and latest.get("kind") != "experiment" \
            and datetime.fromisoformat(latest["generated_at"]).timestamp() > ended:
        return latest, latest_md, None
    report, report_md = _read_report(attack["experiment"]["id"])
    return (report, report_md, None) if report is not None else (None, None, attack)


def _when_text(when):
    """'YYYY-MM-DD HH:MM:SS (5m ago)' for an ISO timestamp."""
    try:
        at = datetime.fromisoformat(when)
    except (TypeError, ValueError):
        return when
    ago = max(0, int(time.time() - at.timestamp()))
    ago_text = (f"{ago // 86400}d ago" if ago >= 86400 else f"{ago // 3600}h ago" if ago >= 3600
                else f"{ago // 60}m ago" if ago >= 60 else f"{ago}s ago")
    return f"{at.astimezone().strftime('%Y-%m-%d %H:%M:%S')} ({ago_text})"


def _latest_recommendation(experiment_id):
    """The newest remediation proposal for this experiment (results/remediation/*/proposal.json), or None."""
    root = core.OUT_DIR / "remediation"
    try:
        folders = sorted((f for f in root.iterdir() if (f / "proposal.json").exists()), reverse=True)
    except OSError:
        return None
    for folder in folders:
        try:
            data = json.loads((folder / "proposal.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if experiment_id in data.get("source_results", []):
            data["_status"] = _recommendation_status(folder, data)
            return data
    return None


def _recommendation_status(folder, data):
    """Same wording as remediation.store.Store.status."""
    if not data.get("remediable"):
        return "declined by agent"
    if data.get("policy_problems"):
        return "rejected by policy"
    try:
        verification = json.loads((folder / "verification.json").read_text(encoding="utf-8"))
        return f"verified: {verification['verdict'].upper()}"
    except (OSError, ValueError, KeyError):
        pass
    try:
        approval = json.loads((folder / "approval.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "awaiting review"
    return "accepted by reviewer" if approval.get("decision") == "approved" else "rejected by reviewer"


def _recommendation_summary(rec):
    """One line per proposed change: Compose settings or code edits."""
    if not rec.get("remediable"):
        return [rec.get("declined_reason") or "No change proposed."]
    if "edits" in rec:
        return [f"`{e['service']}/{e['file']}` {e['function']}: {e['purpose']}" for e in rec["edits"]]
    return [f"`{c['service']}`: set {c['setting']} = {c['value']}" for c in rec.get("changes", [])]


st.divider()
st.subheader("RECENT ATTACK")
if _active:
    st.caption(f"{_active[0]} on {_active[1]} is in progress. This section updates once it has been "
               "rolled back and the system has recovered.")
_report, _report_md, _pending = _recent_failure()
if _pending is not None:
    _exp = _pending["experiment"]
    st.markdown("**Failure report**")
    st.markdown(f"**Last attack:** {_exp['action']} on {_exp['target']}, verdict {_pending['verdict']}, "
                f"recovered {_when_text(_pending.get('recovered_at') or _pending['ended_at'])}")
    st.caption("Agent 2 is writing the report for this attack.")
elif _report is None:
    st.caption("No failure report yet.")
else:
    _analysis = _report["analysis"]
    _when = (_analysis.get("origin") or {}).get("first_degraded_at") \
        or (_report.get("evidence") or {}).get("window_start") or _report["generated_at"]
    _previous = st.session_state.get("last_report_id")
    if _previous is not None and _previous != _report["id"]:
        st.toast(f"New failure report: {_report['id']}")
    st.session_state.last_report_id = _report["id"]
    st.markdown("**Failure report**")
    st.markdown(f"**Last failure:** {_when_text(_when)}  \n**What happened:** {_analysis['headline']}")
    with st.expander("Full report"):
        st.markdown(_report_md)

    st.markdown("**Recommendation**")
    _rec = _latest_recommendation(_report["id"])
    if _rec is None:
        st.caption("No recommendation yet.")
    else:
        _kind = "code change" if "edits" in _rec else "config change"
        st.markdown(f"**{_rec['id']}** ({_kind}, {_rec['_status']})  \n"
                    f"**Problem:** {_rec['diagnosed_problem']}")
        st.markdown("\n".join(f"- {line}" for line in _recommendation_summary(_rec)))

st.divider()
st.subheader("SERVICE HEALTH")


def _status_label(r):
    if r["infrastructure"]:
        return "Infra (ignored)" if r["reason"] else "Infra"
    return "DOWN" if r["down"] else "Up"


def _yes_no(value):
    return "—" if value is None else ("Yes" if value else "No")


def _latency_label(r):
    ms = r.get("health_latency_ms")
    return None if ms is None else round(float(ms), 1)


def _read_experiment():
    journal = core.OUT_DIR / "active_experiment.json"
    try:
        return json.loads(journal.read_text(encoding="utf-8")) if journal.exists() else None
    except (OSError, ValueError):
        return None


# Fixed row order (not "down first"): a click is reported as a row number, so the order must not
# change between refreshes or the wrong service would open.
_order = list(core.SERVICE_IMPACT)
service_rows = sorted(sample.get("services") or [],
                      key=lambda r: (_order.index(r["service"]) if r["service"] in _order else len(_order),
                                     r["service"]))
attacked = graphs.attacked_services(sample, _read_experiment(), known=_order)
graph_state = st.session_state.service_graphs

if service_rows:
    service_df = pd.DataFrame([
        {
            "Service": r["service"],
            "Status": ("UNDER ATTACK" if r["service"] in attacked and not r["down"] else _status_label(r)),
            "Latency (ms)": _latency_label(r),
            "Container": r["state"] or "—",
            "Container ready": _yes_no(r["container_ready"]),
            "Health check": r["health_check"] or "—",
            "Visible on": ", ".join(r["pages"]) or "no page probe",
            "Note": r["impact_note"] or "—",
            "Why down": r["reason"] or "—",
        }
        for r in service_rows
    ])
    event = st.dataframe(
        service_df.style.map(
            lambda v: "color: #b91c1c; font-weight: bold" if v in ("DOWN", "UNDER ATTACK") else (
                "color: #15803d; font-weight: bold" if v == "Up" else ""),
            subset=["Status"],
        ).format({"Latency (ms)": lambda v: "—" if pd.isna(v) else f"{v:.1f}"}),
        width="stretch",
        hide_index=True,
        key="service_table",
        on_select="rerun",
        selection_mode="multi-row",
    )
    selected_rows = getattr(getattr(event, "selection", None), "rows", None) or []
    selection = {service_df.iloc[i]["Service"] for i in selected_rows if i < len(service_df)}
else:
    selection = set()
    st.info("No service data yet (Docker unavailable and health checks not reporting).")

graphs.update(graph_state, attacked, selection, time.time())


def _latency_chart(rows):
    df = pd.DataFrame(rows)
    df["latency_ms"] = pd.to_numeric(df["latency_ms"], errors="coerce")
    base = alt.Chart(df).encode(x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S")))
    line = base.mark_line(color="#2563eb").encode(
        y=alt.Y("latency_ms:Q", title="Health-check latency (ms)"))
    points = base.mark_circle(size=60).encode(
        y="latency_ms:Q",
        color=alt.Color("status:N", scale=alt.Scale(domain=["Up", "Down"], range=["#2563eb", "#b91c1c"]),
                        legend=alt.Legend(title=None, orient="top")),
        tooltip=[alt.Tooltip("time:T", format="%H:%M:%S"), alt.Tooltip("latency_ms:Q", format=".1f"),
                 "status:N", "detail:N"],
    )
    return (line + points).properties(height=220)


open_graphs = graphs.ordered(graph_state, attacked, _order)
if open_graphs:
    st.markdown("#### SERVICE LATENCY")
    cols = st.columns(2)
    for i, svc in enumerate(open_graphs):
        info = graph_state["open"][svc]
        rows = graphs.latency_rows(history, svc)
        with cols[i % 2].container(border=True):
            head, btn = st.columns([5, 1])
            if svc in attacked:
                state_txt = "under attack"
            elif info["source"] == "auto" and info["last_bad"]:
                left = max(0, graphs.AUTO_CLOSE_AFTER_S - (time.time() - info["last_bad"]))
                state_txt = f"recovered, closing in {left:.0f}s"
            else:
                state_txt = "up"
            head.markdown(f"**{svc}** · {state_txt}")
            if btn.button("Close", key=f"close_graph_{svc}", help="Close this graph"):
                graphs.close(graph_state, svc, attacked)
                st.rerun()
            if rows:
                st.altair_chart(_latency_chart(rows), width="stretch")
                last = rows[-1]
                latency = "—" if last["latency_ms"] is None else f"{last['latency_ms']:.1f} ms"
                st.caption(f"Latest: {latency} · {last['status']}"
                           + (f" · {last['detail']}" if last["status"] == "Down" else ""))
            else:
                st.info("No latency samples yet for this service.")

# ---------------- Agent 1: deadliest attacks ----------------
# The planner only ranks here; nothing is injected. The dashboard starts `python -m planner rank`
# in the background (on request, or when the ranking is older than RANK_EVERY_S) and reads the file
# it writes, so it still cannot start an experiment.
RANK_FILE = core.OUT_DIR / "attack_ranking.json"
RANK_LOG = core.OUT_DIR / "attack_ranking.log"
RANK_EVERY_S = int(os.getenv("BOUTIQUE_RANK_EVERY_S", "1800"))  # 0 = only on request
REPO_ROOT = Path(__file__).resolve().parent.parent


@st.cache_resource
def _ranker():
    """One background ranking at a time, shared by every browser tab."""
    return {"proc": None, "started": 0.0}


def _start_ranking():
    """Start `planner rank` unless one is already running. Also the button's on_click callback, which
    Streamlit runs before the script, so a click is not lost to the 2 s autorefresh rerun."""
    import subprocess
    import sys

    state = _ranker()
    if state["proc"] is not None and state["proc"].poll() is None:
        return
    with RANK_LOG.open("w", encoding="utf-8") as log:
        state["proc"] = subprocess.Popen(
            [sys.executable, "-m", "planner", "--results-dir", str(core.OUT_DIR.resolve()), "rank"],
            cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    state["started"] = time.time()


def _deadliness_colour(value):
    return "#dc2626" if value >= 75 else "#ea580c" if value >= 50 else "#ca8a04"


def _open_card(index):
    st.session_state.open_card = index


def _close_card():
    st.session_state.open_card = None


@st.dialog("ATTACK DETAILS", width="large", on_dismiss=_close_card)
def _attack_dialog(item):
    params = ", ".join(f"{k} = {v}" for k, v in (item.get("params") or {}).items()) or "none"
    st.markdown(f"#### {item['rank']}. {item['title']} on `{item['target']}`")
    st.progress(min(1.0, item["deadliness"] / 100), text=f"Deadliness {item['deadliness']} / 100")
    rows = [
        ("Attack", f"{item['title']} ({item['action']})"),
        ("Target", item["target"]),
        ("Parameters", params),
        ("What it does", item.get("description", "")),
        ("Why it ranks here", item["rationale"]),
        ("Hypothesis", item.get("hypothesis", "")),
        ("A resilient system shows", item.get("expected_observations", "")),
        ("Recovered when", item.get("recovery_criteria", "")),
    ]
    st.table(pd.DataFrame([r for r in rows if r[1]], columns=["Field", "Detail"]).set_index("Field"))
    c1, c2 = st.columns(2)
    if c1.button("Prepare in the attack console", type="primary", width="stretch"):
        _start_interpret(f"{item['action']} on {item['target']} with {params} for 30 seconds")
        _close_card()
        st.rerun()
    if c2.button("Close", width="stretch"):
        _close_card()
        st.rerun()


def _card_css(ranked):
    borders = "".join(f".st-key-attack_card_{i} button {{ border-color: {_deadliness_colour(item['deadliness'])}; }}"
                      for i, item in enumerate(ranked))
    return f"""<style>
.st-key-attack_cards button {{ aspect-ratio: 1 / 1; height: auto; border-width: 2px; border-radius: .6rem; }}
.st-key-attack_cards button p {{ white-space: pre-line; font-size: 1.05rem; line-height: 1.6; }}
{borders}
</style>"""


def _attack_ranking_panel():
    try:
        doc = json.loads(RANK_FILE.read_text(encoding="utf-8"))
        age = max(0, int(time.time() - datetime.fromisoformat(doc["generated_at"]).timestamp()))
    except (OSError, ValueError, KeyError):
        doc, age = None, None
    state = _ranker()
    running = state["proc"] is not None and state["proc"].poll() is None
    due = (RANK_EVERY_S > 0 and (age is None or age > RANK_EVERY_S)
           and time.time() - state["started"] > RANK_EVERY_S)
    if due and not running:
        try:
            _start_ranking()
        except OSError as exc:
            st.error(f"Could not start the planner: {exc}")
    running = state["proc"] is not None and state["proc"].poll() is None

    _, btn = st.columns([5, 1])
    btn.button("Re-rank now", disabled=running, width="stretch", on_click=_start_ranking)
    if running:
        st.info("Agent 1 is ranking attacks against the current health…")
    elif state["proc"] is not None and state["proc"].returncode:
        st.warning(f"The last ranking failed; see {RANK_LOG}.")
    if doc is None:
        st.caption("No ranking yet.")
        return
    if not doc.get("safe_to_inject"):
        st.warning("Cluster degraded or telemetry stale: not safe to inject now.")
    ranked = sorted(doc.get("ranked", []), key=lambda item: -item["deadliness"])
    if not ranked:
        return
    st.markdown(_card_css(ranked), unsafe_allow_html=True)
    with st.container(key="attack_cards"):
        for i, (col, item) in enumerate(zip(st.columns(len(ranked)), ranked, strict=True)):
            label = f"#{item['rank']}\n**{item['title']}**\n{item['target']}\n{item['deadliness']} / 100"
            col.button(label, key=f"attack_card_{i}", width="stretch", on_click=_open_card, args=(i,),
                       help="Show the details")
    index = st.session_state.get("open_card")
    if index is not None and index < len(ranked):
        _attack_dialog(ranked[index])


# ---------------- Attack console ----------------
# Agent 1 reads a plain-words request into one plan (`python -m planner interpret`), shows it with
# the policy decision, and runs it only when the operator presses Run (`execute --confirm`). The
# runner re-validates it, journals it and always rolls back; the sidebar kill switch stops it.
CONSOLE_DIR = core.OUT_DIR / "console"


@st.cache_resource
def _console():
    """One console job at a time (shared by every browser tab, so two tabs cannot both inject)."""
    return {"proc": None, "kind": None, "request": None, "log": None}


def _console_busy(state):
    return state["proc"] is not None and state["proc"].poll() is None


def _start_console(kind, args, request):
    import subprocess
    import sys

    state = _console()
    if _console_busy(state):
        return
    CONSOLE_DIR.mkdir(parents=True, exist_ok=True)
    state["log"] = request.with_suffix(f".{kind}.log")
    with state["log"].open("w", encoding="utf-8") as log:
        state["proc"] = subprocess.Popen(
            [sys.executable, "-m", "planner", "--results-dir", str(core.OUT_DIR.resolve()), *args],
            cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    state["kind"], state["request"] = kind, request


def _start_interpret(text):
    request = CONSOLE_DIR / f"request-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    _start_console("interpret", ["interpret", text, "--out", str(request)], request)


def _submit_request():
    text = (st.session_state.get("attack_request") or "").strip()
    if text:
        _start_interpret(text)


def _run_request():
    state = _console()
    if state["request"] is not None:
        _start_console("execute", ["execute", str(state["request"]), "--confirm"], state["request"])


def _discard_request():
    state = _console()
    if not _console_busy(state):
        state.update(proc=None, kind=None, request=None, log=None)


def _log_tail(state, lines=6):
    try:
        return "\n".join(state["log"].read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except (OSError, AttributeError):
        return ""


def _attack_console():
    st.subheader("ATTACK CONSOLE")
    state = _console()
    busy = _console_busy(state)
    with st.form("attack_prompt", border=False):
        f1, f2 = st.columns([5, 1], vertical_alignment="bottom")
        f1.text_input("Attack request", key="attack_request",
                      placeholder="e.g. add 400 ms latency and 5% loss to cartservice for 20 seconds")
        f2.form_submit_button("Plan it", width="stretch", disabled=busy, on_click=_submit_request)

    if busy:
        if state["kind"] == "interpret":
            st.info("Agent 1 is reading the request and checking it against the policy…")
        else:
            active = _active_experiment()
            st.warning("Running the attack" + (f": {active[0]} on {active[1]}, {active[3]}s in" if active else "…")
                       + ". Use the kill switch in the sidebar to stop it.")
        return
    if state["request"] is None:
        return
    try:
        request = json.loads(state["request"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st.error("The planner did not produce a plan.")
        if tail := _log_tail(state):
            st.code(tail, language="text")
        st.button("Clear", on_click=_discard_request)
        return

    plan = request.get("plan")
    if state["kind"] == "execute" and plan:
        _console_result(state, plan)
        return
    st.markdown(f"**Request:** {request['prompt']}  \n**Read by:** {request['interpreted_by']}"
                + (f" · {request['note']}" if request.get("note") else ""))
    if not plan:
        st.error("Could not plan this: " + "; ".join(request.get("reasons") or ["unknown reason"]))
        st.button("Clear", on_click=_discard_request)
        return
    params = ", ".join(f"{k} = {v}" for k, v in (plan.get("params") or {}).items()) or "none"
    st.table(pd.DataFrame([
        ("Attack", plan["action"]), ("Target", plan["target"]), ("Parameters", params),
        ("Duration", f"{plan['max_duration_s']} s"), ("Experiment id", plan["experiment_id"]),
        ("Hypothesis", plan["hypothesis"]), ("A resilient system shows", plan["expected_observations"]),
        ("Recovered when", plan["recovery_criteria"]),
    ], columns=["Field", "Plan"]).set_index("Field"))
    if request["allowed"]:
        st.success("Policy check passed. Running it injects this fault into the live system; the runner "
                   "rolls it back afterwards.")
    else:
        st.error("Rejected by the policy check: " + "; ".join(request.get("reasons") or []))
    b1, b2, _ = st.columns([1, 1, 3])
    b1.button("Run this attack", type="primary", width="stretch", disabled=not request["allowed"],
              on_click=_run_request)
    b2.button("Discard", width="stretch", on_click=_discard_request)


def _console_result(state, plan):
    """The outcome of a console run, from the orchestrator's record results/<id>.json."""
    try:
        result = json.loads((core.OUT_DIR / f"{plan['experiment_id']}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st.error(f"{plan['action']} on {plan['target']} did not run.")
        if tail := _log_tail(state):
            st.code(tail, language="text")
        st.button("Clear", on_click=_discard_request)
        return

    def seconds(key):
        return "—" if result.get(key) is None else f"{result[key]:g} s"

    verdict = result.get("verdict", "unknown")
    {"completed": st.success, "aborted": st.warning}.get(verdict, st.error)(
        f"{plan['action']} on {plan['target']} finished: **{verdict}**"
        + (f" ({result['abort_reason']})" if result.get("abort_reason") else ""))
    st.table(pd.DataFrame([
        ("Experiment id", plan["experiment_id"]), ("Verdict", verdict),
        ("Detection", seconds("detection_seconds")), ("Impact", seconds("impact_seconds")),
        ("Recovery", seconds("recovery_seconds")),
    ], columns=["Field", "Result"]).set_index("Field"))
    st.button("Clear", on_click=_discard_request)


# ---------------- Chaos tests: the deadliest attacks ----------------
st.divider()
st.subheader("CHAOS TESTS")
_attack_ranking_panel()

st.divider()
_attack_console()

# ---------------- Charts and tables ----------------
st.divider()
st.subheader("LATENCY TREND")
_lat = pd.DataFrame([
    {"Time": item["timestamp"], "Endpoint": endpoint, "Latency (ms)": val["latency"],
     "OK": bool(val["success"])}
    for item in history for endpoint, val in item["metrics"].items()
])
if _lat.empty:
    st.info("Waiting for samples.")
else:
    _lat["Time"] = pd.to_datetime(_lat["Time"], utc=True).dt.tz_convert(datetime.now().astimezone().tzinfo).dt.tz_localize(None)
    t1, t2 = st.columns([1.5, 1], gap="large")
    _lines = alt.Chart(_lat).mark_line(point=False).encode(
        x=alt.X("Time:T", title=None, axis=alt.Axis(format="%H:%M:%S")),
        y=alt.Y("Latency (ms):Q", title="Latency (ms)"),
        color=alt.Color("Endpoint:N", legend=alt.Legend(orient="bottom", title=None)),
        tooltip=["Endpoint", alt.Tooltip("Time:T", format="%H:%M:%S"), "Latency (ms)", "OK"],
    )
    _sla = alt.Chart(pd.DataFrame({"SLA": [latency_sla]})).mark_rule(
        color="#dc2626", strokeDash=[6, 4]).encode(y="SLA:Q")
    t1.altair_chart((_lines + _sla).properties(height=320), width="stretch")
    _now = {r["endpoint"]: r for r in probe_rows}
    _summary = []
    for endpoint, group in _lat.groupby("Endpoint", sort=True):
        row = _now.get(endpoint, {})
        lat_ms = group["Latency (ms)"]
        _summary.append({
            "Endpoint": endpoint,
            "HTTP": row.get("status", "—"),
            "Health": ("Healthy" if row.get("success") and row.get("latency_ms", 0) <= latency_sla
                       else "Degraded") if row else "—",
            "Now (ms)": f"{row['latency_ms']:.0f}" if row else "—",
            "Mean (ms)": f"{lat_ms.mean():.0f}",
            "p95 (ms)": f"{lat_ms.quantile(0.95):.0f}",
            "Max (ms)": f"{lat_ms.max():.0f}",
            "Failed": f"{int((~group['OK']).sum())} / {len(group)}",
        })
    t2.markdown("**PER ENDPOINT**")
    t2.table(pd.DataFrame(_summary).set_index("Endpoint"))
    _errors = [f"{r['endpoint']}: {r['error']}" for r in probe_rows if r.get("error")]
    if _errors:
        t2.caption("Current errors: " + " · ".join(_errors))

st.divider()
st.subheader("DOCKER CONTAINERS" if backend == "docker" else "KUBERNETES PODS")
if snap["available"] and snap["pods"]:
    st.dataframe(pd.DataFrame(snap["pods"]), width="stretch", hide_index=True)
elif snap["error"]:
    st.warning(f"{backend.capitalize()} data unavailable: {snap['error']}")
else:
    st.info("No workloads found (check the Compose project / namespace).")
st.divider()
st.subheader("CONTAINER EVENTS (LAST 2 MIN)" if backend == "docker" else "KUBERNETES EVENTS")
_apps = {p.get("name"): p.get("app") or p.get("name") for p in snap.get("pods") or []}
_events = sample.get("recent_events") or []
if backend == "docker" and _events:
    st.table(pd.DataFrame([
        {
            "Time": e["time"][11:19],
            "Service": _apps.get(e["container"], e["container"]),
            "Event": e["action"].replace("health_status: ", "health: "),
            "Exit code": "" if e.get("exit_code") is None else e["exit_code"],
            "Signal": e.get("signal") or "",
        }
        for e in reversed(_events[-15:])
    ]).set_index("Time"))
elif snap["events"]:
    st.code("\n".join(snap["events"]), language="text")
else:
    st.info("No container events in the last 2 minutes.")

with st.expander("CPU / memory snapshot"):
    st.code(snap["cpu_memory"], language="text")

with st.expander("Latest telemetry record (for AI diagnosis)"):
    st.json(sample)

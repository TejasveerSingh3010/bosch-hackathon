import json
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import altair as alt

import boutique_core as core
import service_graphs as graphs

# ---------------- Page / state ----------------
st.set_page_config(page_title="Boutique Chaos & Health", page_icon="🛡️", layout="wide")

if "action_message" not in st.session_state:
    st.session_state.action_message = None
if "service_graphs" not in st.session_state:
    st.session_state.service_graphs = graphs.new_state()

st.title("🛡️ Online Boutique — Live Health & Chaos Dashboard")
st.caption("Live HTTP probes, container/pod health, bounded test actions, and telemetry for your AI chaos-engineering agent.")

with st.sidebar:
    st.header("Connection")
    base_url = st.text_input("Boutique base URL", value=core.DEFAULT_BASE_URL).rstrip("/")
    backend = st.selectbox(
        "Runtime backend", ["docker", "kubernetes"],
        index=0 if core.DEFAULT_BACKEND == "docker" else 1,
        help="Where Online Boutique runs. Docker uses the docker CLI; Kubernetes uses kubectl.",
    )
    if backend == "docker":
        project = st.text_input("Compose project (blank = auto-detect)", value=core.DEFAULT_COMPOSE_PROJECT).strip()
        namespace = core.DEFAULT_NAMESPACE
    else:
        namespace = st.text_input("Kubernetes namespace", value=core.DEFAULT_NAMESPACE)
        project = ""
    refresh_seconds = st.slider("Dashboard refresh (seconds)", 1, 10, 2)
    st.divider()
    st.subheader("Thresholds")
    latency_warn = st.number_input("Latency warning (ms)", min_value=1, max_value=10000, value=core.LATENCY_WARN_MS)
    latency_sla = st.number_input("Latency SLA breach (ms)", min_value=1, max_value=30000, value=core.LATENCY_SLA_MS)
    st.divider()
    st.subheader("Telemetry")
    write_telemetry_on = st.checkbox(
        "Write telemetry.json / SLA_BREACH.flag from this tab",
        value=os.getenv("BOUTIQUE_DASHBOARD_WRITES", "1") != "0",
        help="Untick if headless_monitor.py is running, so only one process writes the files.",
    )
    st.caption(f"Files: {core.TELEMETRY_PATH.resolve()}")
    st.caption("Actions are intended only for your own local/test environment.")

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=refresh_seconds * 1000, key="health_refresh")
except ImportError:
    st.info("For automatic refresh, install requirements.txt (includes streamlit-autorefresh). The Refresh now button still works.")
    if st.button("↻ Refresh now"):
        st.rerun()


# ---------------- Collect ----------------
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
simulate = live.simulating
journey = live.journey

summary = sample["summary"]
error_rate = summary["error_rate_pct"]
p95_latency = summary["p95_latency_ms"]
mean_latency = summary["mean_latency_ms"]
unit = "containers" if backend == "docker" else "pods"
services_down = summary.get("services_down") or []

# ---------------- Overview ----------------
st.subheader("Live overview")
if services_down:
    st.error(f"🚨 SERVICE DOWN — {', '.join(services_down)}. "
             "See *Service health* below for what users notice.")
if error_rate > 0 or p95_latency > latency_sla:
    st.error("🚨 HEALTH DEGRADED — HTTP probe failures or latency SLA breach detected.")
elif services_down:
    st.warning("⚠️ Page probes still pass, but at least one service is down "
               "(a failure behind the frontend).")
elif p95_latency >= latency_warn:
    st.warning("⚠️ WARNING — Latency is above the warning threshold.")
else:
    st.success("🟢 PROBES HEALTHY — No probe errors or latency threshold breaches in this sample.")

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

st.caption(f"Last sample: {sample['timestamp']} · Refresh target: every {refresh_seconds}s · "
           f"Probe targets: {len(core.ENDPOINT_PATHS)} · Backend: {backend}"
           + (f" · Compose project: {snap.get('project') or 'all containers'}"
              if backend == "docker" else "")
           + f" · Sample age: {bundle['age_s']:.0f}s (took {bundle['cycle_s']:.1f}s)")
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
if simulate:
    st.warning("Probe-failure simulation is active. Samples are written with `simulated: true`; the application is not modified.")
if write_error:
    st.warning(f"Could not write telemetry files: {write_error}")

# ---------------- Service health ----------------
# ---------------- Agent 2: failure report ----------------
def _latest_report():
    """The newest report Agent 2 wrote (reports/latest.json + latest.md), or (None, None)."""
    reports_dir = Path(os.getenv("BOUTIQUE_REPORTS_DIR") or core.OUT_DIR.parent / "reports")
    try:
        data = json.loads((reports_dir / "latest.json").read_text(encoding="utf-8"))
        text = (reports_dir / "latest.md").read_text(encoding="utf-8")
        return data, text
    except (OSError, ValueError):
        return None, None


st.divider()
st.subheader("🧾 Failure report (Agent 2)")
_report, _report_md = _latest_report()
if _report is None:
    st.caption("No failure report yet. Agent 2 writes one automatically after every experiment, and when it "
               "sees a failure on its own (scripts\\monitor.bat starts its watcher). Try: python -m reporter demo")
else:
    _analysis = _report["analysis"]
    try:
        _age = max(0, int(time.time() - datetime.fromisoformat(_report["generated_at"]).timestamp()))
    except ValueError:
        _age = 0
    _previous = st.session_state.get("last_report_id")
    if _previous is not None and _previous != _report["id"]:
        st.toast(f"New failure report: {_report['id']}", icon="🧾")
    st.session_state.last_report_id = _report["id"]
    _banner = f"**{_report['title']}** · {_age}s ago\n\n{_analysis['headline']}"
    {"critical": st.error, "major": st.error, "minor": st.warning}.get(_analysis["severity"], st.success)(_banner)
    st.markdown("**Where it failed:** " + _analysis["failure_point"])
    with st.expander("Full report", expanded=_age < 300):
        st.markdown(_report_md)

st.divider()
st.subheader("Service health")
st.caption("Every service, including the ones no page probe can see. *Down* = container not ready or "
           "the direct health check (gRPC health / Redis PING on the published port) failed. "
           "*Visible on* = which probes fail when it is down.")


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
    st.caption("Click a row to open that service's latency graph. Latency is the direct health-check "
               "round trip; a hung or paused service climbs to the "
               f"{core.SERVICE_PROBE_TIMEOUT_S:.0f} s timeout.")
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
    st.markdown("#### Service latency")
    if any(graph_state["open"][s]["source"] == "auto" for s in open_graphs):
        st.caption(f"Opened automatically when a service is attacked; closes "
                   f"{graphs.AUTO_CLOSE_AFTER_S:.0f} s after it recovers. "
                   "Graphs you open by clicking stay until you close them.")
    cols = st.columns(2)
    for i, svc in enumerate(open_graphs):
        info = graph_state["open"][svc]
        rows = graphs.latency_rows(history, svc)
        with cols[i % 2].container(border=True):
            head, btn = st.columns([5, 1])
            if svc in attacked:
                state_txt = "🔴 under attack"
            elif info["source"] == "auto" and info["last_bad"]:
                left = max(0, graphs.AUTO_CLOSE_AFTER_S - (time.time() - info["last_bad"]))
                state_txt = f"🟢 recovered, closing in {left:.0f}s"
            else:
                state_txt = "🟢 up"
            head.markdown(f"**{svc}** · {state_txt}")
            if btn.button("✕", key=f"close_graph_{svc}", help="Close this graph"):
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

# ---------------- Actions ----------------
st.divider()
st.subheader("Chaos / monitoring tests")
st.caption(f"Run only against your own local/test environment. The restart briefly affects the frontend. "
           f"Traffic burst: {core.BURST_MAX_SECONDS}s max, {core.BURST_MAX_WORKERS} workers max, "
           f"{core.BURST_MAX_REQUESTS} requests max.")

a1, a2, a3 = st.columns(3)
with a1:
    st.markdown("### 💥 Restart frontend")
    if backend == "docker":
        st.write("Kills and restarts the single `frontend` container (`docker restart --time 0`).")
    else:
        st.write("Deletes one controller-managed frontend pod. Kubernetes should recreate it.")
    confirm_restart = st.checkbox("I confirm this is my test environment", key="confirm_restart")
    if st.button("Run frontend-failure test", type="primary", disabled=not confirm_restart, width="stretch"):
        try:
            ok, msg = core.restart_frontend(backend, namespace, project)
            st.session_state.action_message = ("success" if ok else "error", msg)
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            st.session_state.action_message = ("error", str(exc)[:400])

with a2:
    st.markdown("### 📈 Bounded traffic burst")
    burst_seconds = st.number_input(
        "Duration (seconds)", min_value=1, max_value=core.BURST_MAX_SECONDS,
        value=min(30, core.BURST_MAX_SECONDS), step=5,
    )
    burst_workers = st.number_input("Concurrent workers", min_value=1, max_value=core.BURST_MAX_WORKERS, value=5)
    st.write("Keeps sending GETs across the three monitored pages until the duration ends.")
    if st.button("Run traffic burst", width="stretch"):
        try:
            with st.spinner(f"Sending traffic for {int(burst_seconds)}s…"):
                burst = core.bounded_traffic_burst(
                    base_url, workers=int(burst_workers), duration_s=int(burst_seconds),
                )
            st.session_state.action_message = (
                "info",
                f"Traffic burst ran {burst['elapsed_s']}s: {burst['total']} requests, "
                f"{burst['errors']} errors ({burst['error_rate_pct']}%), "
                f"mean {burst['mean_latency_ms']} ms, p95 {burst['p95_latency_ms']} ms.")
        except Exception as exc:  # noqa: BLE001
            st.session_state.action_message = ("error", str(exc)[:400])

with a3:
    st.markdown("### 🚦 Test alerting pipeline")
    st.write("Simulates failed probes for 15 seconds to verify red status, telemetry and breach flag. Does not affect the app.")
    if st.button("Simulate probe failure", width="stretch"):
        live.simulate_until = time.time() + 15
        st.session_state.action_message = ("warning", "Probe-failure simulation enabled for 15 seconds. Confirm the dashboard turns red and SLA_BREACH.flag is written.")

if st.session_state.action_message:
    level, message = st.session_state.action_message
    {"success": st.success, "error": st.error, "warning": st.warning}.get(level, st.info)(message)

# ---------------- Experiment state and kill switch ----------------
# The orchestrator writes these files; the dashboard only reads them, and writes STOP on request.
# That keeps the two processes independent: the dashboard can stop an experiment without being
# able to start one.
st.divider()
e1, e2 = st.columns([1, 1.4])
stop_file = core.OUT_DIR / "STOP"
journal_file = core.OUT_DIR / "active_experiment.json"

with e1:
    st.subheader("🛑 Emergency stop")
    if stop_file.exists():
        st.error("Kill switch is ENGAGED. A running experiment will roll back on its next check.")
        if st.button("Clear the kill switch", width="stretch"):
            try:
                stop_file.unlink(missing_ok=True)
                st.rerun()
            except OSError as exc:
                st.session_state.action_message = ("error", f"Could not clear it: {exc}")
    else:
        st.write("Tells a running experiment to roll back immediately.")
        if st.button("STOP the experiment", type="primary", width="stretch"):
            try:
                core.OUT_DIR.mkdir(parents=True, exist_ok=True)
                stop_file.write_text("stopped from the dashboard", encoding="utf-8")
                st.rerun()
            except OSError as exc:
                st.session_state.action_message = ("error", f"Could not write the stop file: {exc}")

with e2:
    st.subheader("🧪 Experiment in progress")
    if journal_file.exists():
        try:
            entry = json.loads(journal_file.read_text(encoding="utf-8"))
            held_for = max(0, int(time.time() - float(entry.get("injected_at", time.time()))))
            st.warning(f"**{entry.get('action')}** on **{entry.get('container')}** "
                       f"(experiment `{entry.get('experiment_id')}`), in place for {held_for}s")
        except (OSError, ValueError, TypeError):
            st.warning("An experiment is active, but its journal could not be read.")
        st.caption("This file is removed once the experiment has rolled back. If it stays after the "
                   "run ends, use `scripts\\recover.bat`.")
    else:
        st.caption("No experiment is currently injected.")

# ---------------- Charts and tables ----------------
st.divider()
left, right = st.columns([1.2, 1])
with left:
    st.subheader("Latency trend")
    chart_rows = [
        {"Time": item["timestamp"], "Endpoint": endpoint, "Latency (ms)": val["latency"]}
        for item in history for endpoint, val in item["metrics"].items()
    ]
    if chart_rows:
        st.line_chart(pd.DataFrame(chart_rows).pivot_table(index="Time", columns="Endpoint", values="Latency (ms)", aggfunc="mean"))
    else:
        st.info("Waiting for samples.")
with right:
    st.subheader("Endpoint status")
    table_df = pd.DataFrame([
        {
            "Endpoint": r["endpoint"],
            "HTTP status": r["status"],
            "Latency (ms)": r["latency_ms"],
            "Health": "Healthy" if r["success"] and r["latency_ms"] <= latency_sla else "Degraded",
            "Details": r["error"] or "—",
        }
        for r in probe_rows
    ])
    st.dataframe(
        table_df.style.map(
            lambda v: "color: #15803d; font-weight: bold" if v == "Healthy" or v == 200 else (
                "color: #b91c1c; font-weight: bold" if v == "Degraded" or (isinstance(v, int) and v >= 400) else ""
            ),
            subset=["Health", "HTTP status"],
        ),
        width="stretch",
        hide_index=True,
    )

st.divider()
k1, k2 = st.columns(2)
with k1:
    st.subheader("Docker containers" if backend == "docker" else "Kubernetes pods")
    if snap["available"] and snap["pods"]:
        st.dataframe(pd.DataFrame(snap["pods"]), width="stretch", hide_index=True)
    elif snap["error"]:
        st.warning(f"{backend.capitalize()} data unavailable: {snap['error']}")
    else:
        st.info("No workloads found (check the Compose project / namespace).")
with k2:
    st.subheader("Container events (last 2 min)" if backend == "docker" else "Kubernetes events")
    if snap["events"]:
        st.code("\n".join(snap["events"]), language="text")
    else:
        st.info("No events returned.")

with st.expander("CPU / memory snapshot"):
    st.code(snap["cpu_memory"], language="text")
    st.caption("Docker: from `docker stats --no-stream`, refreshed every 10 s in the background. Kubernetes: needs a working metrics API.")

with st.expander("Latest telemetry record (for AI diagnosis)"):
    st.json(sample)
    st.caption("The latest 60 samples are written to telemetry.json. SLA_BREACH.flag exists while the latest sample shows a "
               "measured breach and is removed when it returns to normal. Samples taken during the alerting test carry `simulated: true`.")

import json
import os
import time
from collections import deque

import pandas as pd
import streamlit as st

import boutique_core as core

# ---------------- Page / state ----------------
st.set_page_config(page_title="Boutique Chaos & Health", page_icon="🛡️", layout="wide")

if "history" not in st.session_state:
    st.session_state.history = deque(maxlen=core.HISTORY_SIZE)
if "probe_failure_simulation_until" not in st.session_state:
    st.session_state.probe_failure_simulation_until = 0.0
if "action_message" not in st.session_state:
    st.session_state.action_message = None

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


@st.cache_resource
def docker_sampler():
    """`docker stats` / `docker events` are slow, so one background thread refreshes them every 10 s."""
    return core.DockerExtrasSampler().start()


# ---------------- Collect ----------------
now = time.time()
simulate = now < st.session_state.probe_failure_simulation_until
probe_rows = core.probe_endpoints(base_url, simulate_failure=simulate)
journey = st.session_state.get("journey")
if journey is None or journey.base != base_url:
    journey = st.session_state.journey = core.CheckoutJourney(base_url)
journey_row = journey.tick()
if journey_row:
    probe_rows.append(journey_row)
snap = core.get_snapshot(backend, namespace, project)
extras = None
if backend == "docker":
    extras = docker_sampler().latest()
    names = {p["name"] for p in snap["pods"]}
    snap["cpu_memory"] = core.format_resources({n: r for n, r in extras["resources"].items() if n in names})
    snap["events"] = [f"{e['time'][11:19]}  {e['action']:<22} {e['container']}"
                      + (f" exitCode={e['exit_code']}" if e["exit_code"] not in (None, 0) else "")
                      for e in extras["events"] if e["container"] in names][-8:]

sample = core.build_sample(probe_rows, snap, simulated=simulate, extras=extras, journey_info=journey.info())
st.session_state.history.append(sample)
write_error = core.write_telemetry(sample, latency_sla) if write_telemetry_on else None

summary = sample["summary"]
error_rate = summary["error_rate_pct"]
p95_latency = summary["p95_latency_ms"]
mean_latency = summary["mean_latency_ms"]
unit = "containers" if backend == "docker" else "pods"

# ---------------- Overview ----------------
st.subheader("Live overview")
if error_rate > 0 or p95_latency > latency_sla:
    st.error("🚨 HEALTH DEGRADED — HTTP probe failures or latency SLA breach detected.")
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
           + (f" · Compose project: {snap.get('project') or 'all containers'}" if backend == "docker" else ""))
if simulate:
    st.warning("Probe-failure simulation is active. Samples are written with `simulated: true`; the application is not modified.")
if write_error:
    st.warning(f"Could not write telemetry files: {write_error}")

# ---------------- Actions ----------------
st.divider()
st.subheader("Chaos / monitoring tests")
st.caption(f"Run only against your own local/test environment. The restart briefly affects the frontend. "
           f"Traffic burst: {core.BURST_MAX_REQUESTS} requests max, {core.BURST_MAX_WORKERS} workers max.")

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
    burst_requests = st.number_input("Requests", min_value=1, max_value=core.BURST_MAX_REQUESTS, value=min(100, core.BURST_MAX_REQUESTS), step=50)
    burst_workers = st.number_input("Concurrent workers", min_value=1, max_value=core.BURST_MAX_WORKERS, value=5)
    st.write("GET requests across the three monitored pages.")
    if st.button("Run traffic burst", width="stretch"):
        try:
            with st.spinner("Sending bounded traffic burst…"):
                burst = core.bounded_traffic_burst(base_url, int(burst_requests), int(burst_workers))
            st.session_state.action_message = (
                "info",
                f"Traffic test finished: {burst['total']} requests, {burst['errors']} errors ({burst['error_rate_pct']}%), "
                f"mean {burst['mean_latency_ms']} ms, p95 {burst['p95_latency_ms']} ms.")
        except Exception as exc:  # noqa: BLE001
            st.session_state.action_message = ("error", str(exc)[:400])

with a3:
    st.markdown("### 🚦 Test alerting pipeline")
    st.write("Simulates failed probes for 15 seconds to verify red status, telemetry and breach flag. Does not affect the app.")
    if st.button("Simulate probe failure", width="stretch"):
        st.session_state.probe_failure_simulation_until = time.time() + 15
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
        for item in st.session_state.history for endpoint, val in item["metrics"].items()
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

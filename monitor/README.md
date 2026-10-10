# monitor/ - Online Boutique live health dashboard and headless monitor

Streamlit dashboard + headless monitor for Online Boutique. Probes Home, Cart and Product, reads container (Docker) or pod (Kubernetes) health, and writes `telemetry.json` / `SLA_BREACH.flag` for the AI chaos-engineering agent.

## Files
| File | Role |
|---|---|
| `boutique_core.py` | Shared logic: probes, Docker/Kubernetes snapshots, restart action, telemetry writer |
| `headless_monitor.py` | Runs without a browser; **the process your agent should rely on** for fresh telemetry |
| `dashboard.py` | Streamlit UI (live view + bounded test actions) |

## Requirements
- Python 3.10+
- Online Boutique reachable at `http://localhost:8080` (override with `BOUTIQUE_URL`)
- Docker backend (default): the `docker` CLI talking to Docker Desktop
- Kubernetes backend (optional): `kubectl` pointed at a test cluster, `BOUTIQUE_BACKEND=kubernetes`

## Run (Windows PowerShell)
```powershell
# from the repo root, with the repo's .venv active
pip install -r monitor/requirements.txt
$env:BOUTIQUE_URL = "http://localhost:8080"

# Terminal 1 - continuous telemetry for the agent (single writer)
python monitor/headless_monitor.py

# Terminal 2 - dashboard (don't write the same files twice)
$env:BOUTIQUE_DASHBOARD_WRITES = "0"
python -m streamlit run monitor/dashboard.py
```
If you only run the dashboard, leave `BOUTIQUE_DASHBOARD_WRITES` unset; telemetry then updates only while a dashboard tab is open and awake.

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe -m ...` instead. Dependencies for this folder live in `monitor/requirements.txt`; the repo's root `requirements.txt` is untouched.

## Configuration (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `BOUTIQUE_URL` | `http://localhost:8080` | Frontend URL to probe |
| `BOUTIQUE_BACKEND` | `docker` | `docker` or `kubernetes` |
| `BOUTIQUE_COMPOSE_PROJECT` | auto-detect | Docker Compose project; auto-detected from the `frontend` service when exactly one exists. Note `target.yaml` says `boutique` while the compose file in microservices-demo is named `online-boutique`; check with `docker ps` |
| `BOUTIQUE_NAMESPACE` | `default` | Kubernetes namespace |
| `BOUTIQUE_TELEMETRY_DIR` | `<repo>/results` | Where `telemetry.json` and `SLA_BREACH.flag` go |
| `BOUTIQUE_DASHBOARD_WRITES` | `1` | `0` stops the dashboard writing telemetry |
| `BOUTIQUE_JOURNEY_EVERY` | `5` | Run the synthetic checkout probe every N samples (`0` = off) |
| `BOUTIQUE_LOG_MAX_MB` | `60` | Size at which `telemetry_log.jsonl` rotates (keeps one `.1` copy) |
| `BOUTIQUE_BURST_MAX_REQUESTS` / `_MAX_WORKERS` | `10000` / `20` | Caps for the dashboard traffic burst |

## Dashboard actions
1. **Restart frontend**: Docker: `docker restart --time 0` on the single running `frontend` container (refuses if zero or several match). Kubernetes: deletes one controller-managed `app=frontend` pod. Needs the confirmation checkbox. Causes a brief frontend outage.
2. **Traffic burst**: you choose the request count and workers (defaults 100 / 5); capped by `BOUTIQUE_BURST_MAX_REQUESTS` (10000) and `BOUTIQUE_BURST_MAX_WORKERS` (20). Use it as the "traffic spike" failure.
3. **Simulate probe failure**: marks probes failed for 15 s to test red status, telemetry and the breach flag. The app is untouched; those samples carry `"simulated": true` so the agent can ignore them.

## Telemetry
Files in `results/` (git-ignored):

| File | Content |
|---|---|
| `telemetry.json` | Latest 60 samples (about 2 minutes), pretty-printed |
| `telemetry_log.jsonl` | Append-only history, one compact sample per line, for baselines and experiment before/during/after analysis |
| `SLA_BREACH.flag` | Exists while the latest sample has a probe error or p95 above the SLA threshold |
| `baseline.json` | Steady-state statistics written by `baseline.py` |

Each sample contains:
- `metrics`: per probe (`Home`, `Cart`, `Product`, `Checkout`): `status`, `latency`, `success`, `error`, **`error_type`** (`timeout` = hung dependency, `connection` = process down, `http_5xx`, `http_4xx`).
- `summary`: error rate, mean and p95 latency, `slowest_endpoint`, `error_types` counts, ready/total, restart count, `not_ready` names, and `max_cpu_*` / `max_mem_*` with the container names.
- `workloads`: per container: state, `ready`, `restarts`, `uptime_s`, `exit_code` (137 = killed), `oom_killed`, `health`, **`cpu_pct`, `mem_mb`, `mem_limit_mb`, `mem_pct`**.
- `recent_events`: Docker lifecycle events from the last 2 minutes (`die`, `kill`, `stop`, `pause`, `oom`, `health_status`, ...) with timestamps, so the exact failure time is known independently of the probes.
- `journey`: state of the checkout probe.

**Checkout probe.** Every `BOUTIQUE_JOURNEY_EVERY` samples a synthetic purchase (add to cart, then place order, using the app's built-in mock card) runs through the frontend. It is the only probe that touches checkout, payment, shipping, email and currency. Between runs the last result is carried into each sample with `age_s`. It switches itself off with a printed note if it has never succeeded, so it cannot cause a permanent false alarm.

**Resource data** is gathered by a background thread every 10 s (`docker stats` is slow), so CPU and memory values can be up to 10 s old (`resources_age_s`).

Writes to `telemetry.json` are atomic, and a corrupt file is replaced rather than blocking updates.

## Baseline
Capture it while the app is healthy and the load generator is running, before any failure is injected:
```powershell
python monitor/baseline.py --minutes 10
```
(or double-click `monitor\capture_baseline.bat`). It writes `results/baseline.json` with, per endpoint, mean, std, p50/p95/p99, max and `ucl_ms` (mean + 3 standard deviations, a "this is unusual" limit), plus resource usage per container and `quality_warnings` if the window was too short or not clean.

## Safety notes
- Use only your own test environment; the actions only run fixed commands, never arbitrary shell input.
- Access to the Docker socket is effectively admin on your machine; keep Streamlit bound to localhost.
- Probes check HTTP reachability and latency, not every internal gRPC call.
- Docker restarts via `docker restart` do not increase `RestartCount`; watch `uptime_s` and the events panel instead.

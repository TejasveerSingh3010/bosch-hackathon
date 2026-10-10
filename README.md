# Bosch Hackathon: AI-Driven Chaos Engineering

An AI system that plans, injects and analyzes controlled failures in a cloud-native sample app (Online Boutique) to improve its resilience.

## What it does

1. **Maps** the application automatically: it reads the Compose file's service wiring and builds a dependency graph, scoring each service by how much of the system sits behind it.
2. **Monitors** continuously: HTTP probes (including a synthetic checkout), container health, CPU and memory, and Docker lifecycle events, written to `results/` every two seconds.
3. **Injects** controlled failures: a crash, a lasting outage, a frozen (unresponsive) service, or a traffic spike.
4. **Stops itself** when an experiment does too much damage: the watchdog aborts and rolls back on error rate, latency, blast radius or lost monitoring, and anyone can force a stop.
5. **Measures** what happened: time to detect, time to recover, which pages failed, and how error rate and latency moved against the baseline.

Day 2 adds the AI planner (which experiment to run next, and why) and the analyst (ranked findings with recommended fixes). The interfaces they plug into already exist: `schemas.Experiment` in, `schemas.ExperimentResult` out. `planner/` is the empty package those agents will live in.

## Architecture

![Service dependency map](docs/dependency_graph.png)

```
                 target.yaml  (allow-list, thresholds, duration cap)
                      |
  mapper/  ---------> |                          app/      Online Boutique under test
  dependency graph    v                            ^
                 safety/   <-- the gate            | docker kill / stop / pause
                      |   validate -> watch        |
  planner/  ------>   |                        injector/   actions + rollback
  (day 2)        orchestrator/  ---------------->  |
                      |   run the experiment       |
                      v                            v
                 results/   <----------------  monitor/   probes, telemetry, dashboard
                 telemetry, baseline, per-experiment records
```

The monitor is a host process, not a container in the stack, so a failure injected into the application cannot take down the thing measuring it.

**How the AI makes decisions (day 2).** The planner receives the dependency graph (with criticality scores and the reason for each) and the results of earlier experiments, and proposes the next `Experiment`. It never touches Docker: every plan it produces goes through `safety.validate_plan` first.

The score is a weighted sum over the graph, not a learned model, so every number comes with a reason. It measures internal blast radius. `frontend` scores low because nothing else depends on it, even though killing it is a total outage. That bias is deliberate: a frontend crash reveals nothing anyone doubted, while a `redis-cart` crash shows whether the cart degrades gracefully.

**Safety controls.** See [Safety controls](#safety-controls) below. The short version: the planner can propose anything, and a deterministic layer decides what actually runs. Nothing under `safety/` calls an LLM.

| Module | Role |
|---|---|
| `app/` | Online Boutique, the system under test |
| `mapper/` | Dependency graph and criticality scores |
| `monitor/` | Telemetry, baseline, Streamlit dashboard |
| `injector/` | Failure actions and rollback |
| `safety/` | Validation, watchdog, kill switch, crash recovery |
| `orchestrator/` | Runs one experiment end to end |
| `planner/` | AI experiment planning (day 2; package is a stub today) |
| `reporter/` | Agent 2: failure reports that pinpoint where each failure started |
| `agents/` | Shared LLM seam (`agents/llm.py`): a stub client today, the real model later |
| `results/` | Telemetry, baseline, and per-experiment records |

## Prerequisites

- Docker Desktop (4 GB+ RAM allocated)
- Git
- Python 3.11+

## Setup (clean machine)

```powershell
git clone https://github.com/TejasveerSingh3010/bosch-hackathon.git
cd bosch-hackathon
scripts\setup.bat
copy .env.example .env
```

`scripts\setup.bat` creates `.venv` and installs `requirements.txt`. Everything below assumes you are in the repo root and uses that virtualenv. One requirements file covers the engine, the monitor, and the test tools.

## Run the sample app

```powershell
scripts\app-up.bat
```

Starts the 12 Online Boutique containers from `app/docker-compose.yml` and waits for `http://localhost:8080` to answer. `loadgenerator` provides steady background traffic, which is what the baseline is measured under. `scripts\app-down.bat` stops it all.

## Run the monitoring

```powershell
scripts\monitor.bat
```

Opens two windows: the headless monitor (writes telemetry every 2 s) and the dashboard at `http://localhost:8501`. Leave both running. The script sets `BOUTIQUE_DASHBOARD_WRITES=0` so only the headless monitor writes `results/`. Re-running it closes the previous Boutique windows first.

Then, after about five minutes of steady traffic, capture the baseline every experiment is compared against:

```powershell
scripts\baseline.bat          :: last 10 minutes; pass a number to use a shorter window
scripts\status.bat            :: confirms telemetry is fresh and a baseline exists
```

`results/baseline.json` records, per endpoint, mean, standard deviation, p50/p95/p99, max, and `ucl_ms` (mean + 3 standard deviations), plus resource usage per container. It adds `quality_warnings` when the window was too short or not clean. Simulated dashboard samples are ignored.

### What gets written

Files in `results/` (git-ignored, except `baseline.json` and `graph.json`):

| File | Content |
|---|---|
| `telemetry.json` | Latest 60 samples (about 2 minutes) |
| `telemetry_log.jsonl` | Append-only history, one sample per line; rotates at 60 MB and keeps one `.1` copy |
| `SLA_BREACH.flag` | Present while the latest sample has a probe error or p95 above the SLA |
| `baseline.json` | Steady-state statistics from `scripts\baseline.bat` |
| `<id>.json` | One record per experiment |
| `STOP` | Written by the kill switch; a running experiment rolls back when it appears |
| `active_experiment.json` | Journal written before injection and cleared after rollback |

Each sample has:

- `metrics` for Home, Cart, Product, and Checkout: status, latency, and `error_type` (`timeout` is a hung dependency, `connection` is a process down, plus `http_5xx` and `http_4xx`).
- `summary`: error rate, mean and p95 latency, ready/total, restart count, and the hottest container's CPU and memory.
- `workloads`: per container, state, restarts, uptime, exit code (137 means killed), OOM, and resource use.
- `recent_events`: Docker lifecycle events from the last 2 minutes (`die`, `kill`, `stop`, `pause`, `oom`, ...), so the failure time is known independently of the probes.
- `services`: one row per service with its container state, a direct health check (gRPC `grpc.health.v1` Check, or Redis `PING`, on the host ports published in `app/docker-compose.yml`), and which page probes fail when it is down. `summary.services_down` lists every application service that is down by either signal. This is what catches failures no page probe can see: `adservice` and `recommendationservice` (the frontend hides them), `emailservice` (checkout only logs a failed email), and `checkoutservice` / `paymentservice` while the checkout probe is not working. A paused container counts as down.
- `journey`: the synthetic checkout. It runs every 5 samples (add to cart, then place an order with the app's mock card). Until it has succeeded once it is kept out of the error rate, so it cannot cause a false alarm, but it keeps retrying every 30 samples and `journey.last_error` says why it fails.

`SLA_BREACH.flag` is written when a probe fails, p95 is over the SLA, or any application service is down. `loadgenerator` is shown but never counts as an outage.

| Service | Page probes that fail when it is down |
|---|---|
| frontend, cartservice, redis-cart, currencyservice | Home, Product, Cart, Checkout |
| productcatalogservice | Home, Product, Checkout |
| shippingservice | Cart (the cart page always asks for a shipping quote), Checkout |
| checkoutservice, paymentservice | Checkout only |
| emailservice, adservice, recommendationservice | none: only the service health check sees them |

CPU and memory come from a background `docker stats` every 10 seconds, so those numbers can be up to 10 seconds old.

### Monitor settings

| Variable | Default | Meaning |
|---|---|---|
| `BOUTIQUE_URL` | `http://localhost:8080` | Frontend to probe |
| `BOUTIQUE_BACKEND` | `docker` | `docker`, or `kubernetes` if the same app is running in a cluster |
| `BOUTIQUE_COMPOSE_PROJECT` | auto-detect | Compose project name, taken from the `frontend` service when exactly one exists |
| `BOUTIQUE_NAMESPACE` | `default` | Kubernetes namespace, when that backend is selected |
| `BOUTIQUE_TELEMETRY_DIR` | `results/` | Where telemetry and the breach flag are written |
| `BOUTIQUE_DASHBOARD_WRITES` | `1` | `0` stops the dashboard writing telemetry. `scripts\monitor.bat` sets this |
| `BOUTIQUE_JOURNEY_EVERY` | `5` | Synthetic checkout every N samples (`0` turns it off) |
| `BOUTIQUE_JOURNEY_RETRY_EVERY` | `30` | While the checkout probe has never worked, retry it every N samples |
| `BOUTIQUE_SERVICE_PROBES` | `1` | `0` turns off the direct per-service health checks |
| `BOUTIQUE_SERVICE_HOST` / `BOUTIQUE_SERVICE_PROBE_TIMEOUT` | `127.0.0.1` / `1.0` | Where the published service ports are, and the per-check timeout in seconds |
| `BOUTIQUE_BURST_MAX_SECONDS` / `_MAX_REQUESTS` / `_MAX_WORKERS` | `600` / `20000` / `20` | Caps for the dashboard traffic burst |

The supported target is this repo's Compose stack. The Kubernetes backend only changes where the monitor reads pod health.

### Dashboard actions

At `http://localhost:8501`:

1. **Restart frontend** — `docker restart` on the single running `frontend` container, after a confirmation checkbox.
2. **Traffic burst** — the traffic-spike failure. It keeps sending for the duration you set (default 30 seconds, 5 workers) and stops when that time is up, or when it hits the request cap.
3. **Simulate probe failure** — marks probes failed for 15 seconds. The app is untouched. Those samples carry `"simulated": true` and cannot abort a real experiment.
4. **STOP** — the same kill switch as `scripts\stop.bat`.

## Build the dependency map

```powershell
scripts\map.bat
```

Writes `results/graph.json` and `docs/dependency_graph.png`, and prints the criticality ranking. The map is derived from the Compose file's `*_ADDR` environment variables (the real call edges) and cross-checked against the upstream architecture diagram, so a mismatch is reported rather than silently accepted. `docs/graph.example.json` is the edge shape (`from`, `to`, `type`); the generated file is that shape plus the scores.

## Run an experiment

```powershell
:: see what can be targeted, and what the safety layer allows
python -m orchestrator targets

:: validate a plan without changing anything
scripts\experiment.bat adservice kill 20 --dry-run

:: run it for real
scripts\experiment.bat adservice kill 20

:: sustain a traffic spike: 20 requests/second, 5 users, for 30 seconds
scripts\experiment.bat frontend spike 30 --rps 20 --users 5

:: starve a service of CPU: cap currencyservice at 0.05 CPUs for 30 seconds
scripts\experiment.bat currencyservice throttle 30 --cpus 0.05

:: or from a plan file (the shape the day-2 planner produces)
python -m orchestrator run docs\experiment.example.json
```

Failure modes (`python -m orchestrator actions`):

| Action | What it does | What it tests |
|---|---|---|
| `kill` | SIGKILL the container | Automatic recovery — Compose's `restart: unless-stopped` should bring it back |
| `stop` | Stop the container | Behaviour during a lasting dependency outage |
| `pause` | Freeze every process in it | An unresponsive dependency: callers hang instead of failing fast |
| `spike` | Hold extra HTTP traffic on the frontend for `--duration` seconds | Behaviour under a sustained load of `--rps` requests/second from `--users` clients |
| `throttle` | Cap the container's CPU in place (`docker update --cpus`) | A starved dependency: slower, not down, as with a noisy neighbour or a limit set too tight |

`spike` sends GETs to `BOUTIQUE_URL` (default `http://localhost:8080`: the Compose frontend, or `kubectl port-forward svc/frontend 8080:80` on Kubernetes). The rate and the user count are capped at 20000 requests/second and 50 users. Rollback stops the clients. The dashboard **Traffic burst** button is the same kind of load without going through an experiment.

`throttle` takes `--cpus` (0.01 to 4) and must go below any limit the container already has. Rollback restores the limit recorded before injection, which the journal keeps for `recover`. Docker cannot remove a CPU limit in place (`--cpus 0` is ignored), so "no limit" is restored as a limit of every host CPU, which is equivalent.

Each run prints a summary and writes `results/<id>.json`:

| Field | Meaning |
|---|---|
| `verdict` | `completed`, `aborted`, `stopped`, or `failed` |
| `detection_seconds` | Injection to the first degraded sample (about ±2 s, the probe interval) |
| `recovery_seconds` | Rollback to two consecutive healthy samples |
| `before` / `during` / `after` | Mean and peak error rate and p95 latency for each phase |
| `during.failed_endpoints` | Which journeys broke (Home, Cart, Product, Checkout) |
| `during.error_types` | `timeout` (hung), `connection` (process down), or `http_5xx` |
| `self_healed` | Whether Docker's restart policy recovered the container before rollback |
| `abort_reason` | Which safety limit stopped the run |

## Agent 2: the Reporter

Agent 2 writes a report for every failure, and says exactly **where** it started and how it spread. It shows up on its own in three places:

| When | How | Where you see it |
|---|---|---|
| After every experiment | `python -m orchestrator run ...` calls it as soon as the run is rolled back (`--no-report` skips it) | The terminal, then `reports/<experiment-id>.md` |
| When a failure happens that nobody triggered | `python -m reporter watch` (started for you by `scripts\monitor.bat`) notices the first failing sample and prints `FAILURE DETECTED`; the report follows when the app recovers | The watcher window, then `reports/incident-<time>.md` |
| Always | The dashboard reads `reports/latest.*` | **Failure report (Agent 2)** on `http://localhost:8501`, with a pop-up when a new report arrives |

Try it now, with no Docker and no running app:

```powershell
scripts\report.bat demo           :: a frozen productcatalogservice
scripts\report.bat demo kill      :: a killed redis-cart that Docker restarts
```

This writes `reports/demo-*.md` (open it in VS Code's markdown preview) and prints a summary. Everything else:

```powershell
scripts\report.bat watch          :: watch live telemetry (monitor.bat already starts this)
scripts\report.bat latest         :: print the newest report
scripts\report.bat <experiment-id>   :: rebuild the report for a finished experiment
```

**What it reads.** Code assembles one ordered, id-tagged evidence timeline (`E1`, `E2`, ...) from the experiment record, the telemetry history (page probes, per-service health), Docker events, the target's and its callers' container logs (`docker logs` for the failure window, error lines deduplicated), and the dependency graph. The narrator only sees that pack, and every claim in the report cites ids from it. Citations that do not exist are removed and flagged.

**What it writes** under `reports/`:

| File | Content |
|---|---|
| `<id>.md` / `<id>.json` | Where the failure was, failure origin, how it spread (origin, then services, then pages, with seconds after the origin), impact and timings, what worked, anomalies and unknowns, log findings, the evidence timeline. The JSON is what Agent 3 will read |
| `latest.md` / `latest.json` | The newest report (what the dashboard shows) |
| `campaign.md` | One table row per report |

**No LLM yet.** The narrator is a deterministic rule set (`reporter/analysis.py`), registered behind `agents/llm.py`. The real prompt is already written in `reporter/prompts.py`; wiring a model in later means implementing `RealClient.complete_json` and returning it from `get_client()` (search the code for `TODO(llm)`). Without a key, or if the model fails, the rules still produce the report. Every narrator call is logged to `results/agent_log.jsonl`.

| Variable | Default | Meaning |
|---|---|---|
| `BOUTIQUE_REPORTS_DIR` | `reports/` | Where reports are written and where the dashboard looks |
| `BOUTIQUE_REPORT_LOGS` | `1` | `0` skips the `docker logs` capture (reports then say logs were not captured) |

## Safety controls

The brief asks for controlled failures with automatic limits. Nothing reaches Docker without passing all of this, and none of it calls an LLM:

| Control | Where | Behaviour |
|---|---|---|
| Allow-list | `safety/validator.py` | Only containers listed in `target.yaml` can be targeted |
| Protected infrastructure | `safety/validator.py` | `loadgenerator`, the dashboard and the monitor can never be targeted, even if allow-listed |
| Single target | `safety/validator.py` | The name must resolve to exactly one running container; an ambiguous target is refused |
| Duration cap | `safety/limits.py` | `target.yaml`'s `max_experiment_seconds`, plus a hard 600 s ceiling no config can raise |
| Threshold clamp | `safety/validator.py` | An experiment may tighten the abort limits but never loosen them |
| Pre-flight check | `safety/validator.py` | Refuses to start if the system is already degraded, a container is unready, or telemetry is stale (older than 15 s) |
| Automatic abort | `safety/watchdog.py` | Rolls back on error rate, p95 latency, blast radius (more than one *other* container unready), or telemetry going stale — two consecutive breaching samples, so one noisy probe is not enough. Stale telemetry aborts immediately |
| Kill switch | `safety/killswitch.py` | `scripts\stop.bat`, the dashboard's **STOP** button, or Ctrl+C |
| Crash recovery | `safety/journal.py` | The journal is written before injection; `scripts\recover.bat` restores anything a crashed run left behind. A new experiment will not start while a journal is present |
| Idempotent rollback | `injector/actions.py` | Rollback inspects the container and does only what is needed, and it runs in a `finally` block, so it is safe to run twice |

Thresholds live in `target.yaml`:

```yaml
allowed_containers: [...]       # from `docker ps`; loadgenerator deliberately excluded
thresholds:
  error_rate: 0.20              # a fraction; telemetry reports percent, converted in safety/limits.py
  p95_ms: 2000
max_experiment_seconds: 120
```

## Demo

```powershell
scripts\demo.bat
```

Walks through monitoring status, the dependency map, a plan the safety layer refuses, a dry run, then two contrasting experiments: crashing the least critical service, and freezing one on the checkout path. The app and the monitor need to be running first.

## Tests

```powershell
scripts\test.bat
```

Over 130 tests covering the safety rules, the watchdog's abort conditions, every action's rollback, the runner's abort and recovery paths, and the mapper. They use a fake Docker (`tests/fake_docker.py`) that models restart policies, so **no Docker and no running app are needed**.

## Tools, AI models and libraries used

| | |
|---|---|
| Sample app | [GoogleCloudPlatform/microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo) (Online Boutique), Apache-2.0, images pinned to `v0.10.7` |
| Runtime | Docker Compose |
| Language | Python 3.11+ |
| Libraries | pydantic (schema validation), networkx (graph analysis), matplotlib (graph rendering), streamlit (dashboard), requests, PyYAML, locust, pytest, ruff |
| AI models | Day 2 — the planner and analyst (configured through `LLM_API_KEY` in `.env`) |
| AI assistance | Claude (Anthropic) was used while developing this repository |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `telemetry is N seconds old` | The monitor is not running: `scripts\monitor.bat` |
| `not in target.yaml allowed_containers` | Run `python -m orchestrator targets` and copy the real names in |
| `a previous experiment did not finish cleanly` | `scripts\recover.bat` |
| A container is still paused or stopped | `scripts\recover.bat`, or `docker unpause <name>` / `docker start <name>` |
| `streamlit is not recognized` | `scripts\setup.bat` |
| The dashboard and monitor disagree | Only one process should write telemetry. `scripts\monitor.bat` already sets `BOUTIQUE_DASHBOARD_WRITES=0` |

Keep the dashboard on localhost. Access to the Docker socket is effectively admin on the machine.

## Scope

Experiments run only against this repository's own Compose stack on the machine they are launched from. Nothing targets a remote, shared or production system.

# Architecture overview

One page: the components, how the AI makes decisions, and the safety controls.

## Components

![Service dependency map](dependency_graph.png)

```
  target.yaml ....... allow-list, abort thresholds, duration cap (the only place limits are set)
      |
      v
  mapper/ ........... reads app/docker-compose.yml, builds the dependency graph,
      |               scores each service's criticality, cross-checks the documented architecture
      |                      -> results/graph.json, docs/dependency_graph.png
      v
  planner/ (day 2) .. ranks the next experiment from the graph + earlier results, with a reason
      |                      -> schemas.Experiment
      v
  safety/ ........... THE GATE. validate_plan() decides whether an experiment may run at all;
      |               Watchdog decides, every 2 s, whether it must stop early
      v
  orchestrator/ ..... validate -> journal -> inject -> watch -> roll back -> measure -> record
      |                      -> results/<experiment-id>.json
      +--> injector/ .. docker kill | stop | pause, each with an idempotent rollback
      |
      v
  monitor/ .......... independent process: HTTP probes (incl. synthetic checkout), container
                      health, CPU/memory, Docker events -> results/telemetry.json every 2 s
                      + Streamlit dashboard with the emergency STOP button
```

The monitor runs **outside** the experiment: it is a host process, not a container in the stack, so a failure injected into the application cannot take down the thing measuring it.

## How the AI makes decisions

Day 1 runs experiments chosen by a human or by criticality score. Day 2 adds two agents, and the interfaces they use already exist:

| Agent | Input | Output | Purpose |
|---|---|---|---|
| **Planner** | `results/graph.json` (criticality and the reason for each), `results/baseline.json`, earlier `ExperimentResult`s | `schemas.Experiment` | Choose the most informative experiment next, with a stated reason |
| **Analyst** | `ExperimentResult` files, the baseline | Ranked findings, severities, recommended fixes, resilience score | Explain what the failures revealed |

The planner is given a ranked starting point rather than a blank page. `mapper/graph.py` scores every service 0–100 from the graph alone — transitive dependents, whether it is on the checkout path, whether it is the user entry point, whether anything else can take over, and whether it holds state. The scoring is a weighted sum, not a learned model, so every number comes with a reason the planner can quote:

```
redis-cart              85.0  3 service(s) depend on it; on the checkout path;
                              single point of failure; holds state
productcatalogservice   75.0  3 service(s) depend on it; on the checkout path; single point of failure
...
adservice               13.3  1 service(s) depend on it
```

The score measures **internal blast radius**, not how visible a failure would be. `frontend` scores low because nothing else depends on it, even though killing it is a total outage — which is the right bias for choosing experiments, because a frontend crash reveals nothing anyone doubted, while a `redis-cart` crash tells you whether the cart degrades gracefully.

## Safety controls

The planner may propose anything. What actually runs is decided by deterministic code, because a check an AI could talk its way past is not a safety control. No module under `safety/` calls an LLM.

**Before injection** (`safety/validator.py`, `safety/limits.py`)

1. The target resolves to exactly **one** running container — an ambiguous target is refused, not guessed.
2. That container is on `target.yaml`'s `allowed_containers` list.
3. It is not protected infrastructure (`loadgenerator`, dashboard, monitor) — refused even if allow-listed, because breaking the load generator or the monitor would distort or blind the experiment.
4. The duration is within `max_experiment_seconds` **and** a hard 600 s ceiling that no configuration can raise.
5. Abort thresholds may be tightened by an experiment but never loosened; a clamp is recorded in the result.
6. The system is healthy right now, and telemetry is less than 15 s old — never inject a failure with no monitoring, and never into an already-degraded system, because the measurement would have two causes.

**During the experiment** (`safety/watchdog.py`) — polled every 2 s, aborting on:

- error rate above the limit (default 20%);
- p95 latency above the limit (default 2000 ms);
- more than one container *other than the target* unready (blast radius);
- telemetry going stale — losing visibility is itself a reason to stop.

An abort needs two consecutive breaching samples, so a single noisy probe cannot end a run; stale telemetry aborts immediately. Samples from the dashboard's alerting self-test are marked `simulated` and can never abort a real experiment.

**Stopping by hand** — `scripts\stop.bat`, the dashboard's STOP button, or Ctrl+C. All three write or detect `results/STOP` and take the same rollback path.

**After the experiment**

- Rollback runs in a `finally` block, so a normal finish, an abort, a kill switch, a Docker error and a Ctrl+C all end the same way.
- Rollback is **state-based and idempotent**: it inspects the container and does only what is needed. This matters because `restart: unless-stopped` means Docker may already have restarted a killed container, and because rollback can be reached twice.
- A journal (`results/active_experiment.json`) is written *before* injection and cleared only after a confirmed rollback, so `scripts\recover.bat` can always restore the system after a crash — and the runner refuses to start a new experiment while a journal is present.

## Measurement

Every experiment produces `results/<id>.json`:

| Field | Meaning |
|---|---|
| `verdict` | `completed`, `aborted`, `stopped` or `failed` |
| `detection_seconds` | Injection to the first degraded sample (±2 s, the probe interval) |
| `recovery_seconds` | Rollback to two consecutive healthy samples |
| `before` / `during` / `after` | Mean and peak error rate and p95 latency for each phase |
| `during.failed_endpoints` | Which user journeys broke (Home, Cart, Product, Checkout) |
| `during.error_types` | `timeout` (hung), `connection` (process down) or `http_5xx` — this is how a hang is told apart from a crash |
| `self_healed` | Whether Docker's restart policy recovered it before rollback |
| `abort_reason` | Which safety limit stopped the run |

Detection and recovery times are accurate to about ±2 s, the probe interval. Container lifecycle events (`die`, `kill`, `pause`, `oom`) are timestamped separately by Docker, so the exact moment of failure is known independently of the probes.

## Out of scope

Experiments run only against this repository's own Compose stack, on the machine they are launched from. Nothing targets a remote, shared or production system.

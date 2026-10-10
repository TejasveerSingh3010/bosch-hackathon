# Bosch Hackathon: AI-Driven Chaos Engineering

An AI system that plans, injects and analyzes controlled failures in a cloud-native sample app (Online Boutique) to improve its resilience.

## What it does
TODO

## Architecture
TODO (diagram in docs/)

Modules: `mapper/` (dependency graph), `monitor/` (baseline and metrics), `injector/` (failure actions and rollback), `safety/` (thresholds and kill switch), `planner/` (AI experiment planning), `orchestrator/` (runs experiments end to end).

## Prerequisites
- Docker Desktop (4 GB+ RAM allocated)
- Git
- Python 3.11+

## Setup (clean machine)

    git clone https://github.com/TejasveerSingh3010/bosch-hackathon.git
    cd bosch-hackathon
    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    copy .env.example .env

## Run the sample app
TODO

## Run an experiment
TODO

## Safety controls
TODO

## Tools, AI models and libraries used
TODO
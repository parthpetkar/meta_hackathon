---
title: Meta Hackathon Incident Response Environment
emoji: alert
colorFrom: red
colorTo: gray
sdk: docker
pinned: false
app_port: 8000
base_path: /web
tags:
  - openenv
---

## Meta Hackathon Incident Response Environment

This environment simulates real production troubleshooting for a web platform. An agent investigates alerts, metrics, service status, and logs, then sets a root-cause hypothesis, applies a remediation, and verifies recovery.

The environment is deterministic and designed for OpenEnv hackathon evaluation.

## Why this is useful

Production incidents are a high-value real-world workflow where AI assistants can improve MTTR (mean time to resolution). This benchmark evaluates whether an agent can:

- gather evidence methodically
- identify root cause instead of guessing
- apply safe, correct fixes
- avoid destructive operations

## OpenEnv API

The environment implements:

- reset() -> initial incident observation
- step(action) -> next observation, reward, done
- state() -> current state with episode_id and step_count

## Action Space

`MetaHackathonAction`

- operation: one of

  - inspect_alerts
  - inspect_metrics
  - inspect_service
  - inspect_logs
  - set_hypothesis
  - apply_fix
  - verify_fix

- target: optional service/system target (used by inspect_service and inspect_logs)
- value: optional free text (used for hypothesis/fix IDs)

## Observation Space

`MetaHackathonObservation`

- task_id, task_title, difficulty
- status
- available_services
- visible_alerts
- visible_metrics
- visible_logs
- latest_finding
- current_hypothesis
- recommended_actions
- incident_resolved
- final_score (0.0 to 1.0 once done)
- reward, done, metadata

## Tasks (Easy -> Medium -> Hard)

1. easy: Checkout API latency spike

- Root cause: cache miss storm from Redis key eviction
- Correct fix: scale-cache-cluster

1. medium: Payment API intermittent 503

- Root cause: DB connection pool saturation
- Correct fix: increase-payment-db-pool

1. hard: Platform latency + 5xx burst

- Root cause: search thread pool exhaustion after bad rollout
- Correct fix: rollback-search-rollout

## Grading

Deterministic grader functions score each episode in [0.0, 1.0] using:

- signal discovery quality
- investigation coverage
- hypothesis correctness
- fix correctness
- verification completion
- efficiency (fewer steps)
- penalties for repeated/noisy actions
- heavy penalty for destructive fixes

## Reward shaping

Per-step rewards provide partial progress:

- positive for new, relevant inspection actions
- positive for correct diagnosis/fix/verification
- negative for repeated irrelevant actions
- strong negative for destructive fixes

## Quick Start

### Build container

```bash
docker build -t meta_hackathon-env:latest .
```

### Run local server

```bash
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

### Run baseline inference

```bash
uv run python inference.py
```

## Required Environment Variables

Set these before running inference for evaluation:

- API_BASE_URL
- MODEL_NAME
- HF_TOKEN
- OPENAI_API_KEY
- LOCAL_IMAGE_NAME (or IMAGE_NAME) when using from_docker_image

## Inference Logging Contract

`inference.py` emits strict structured logs:

```text
[START] task={task} env={benchmark} model={model}
[STEP] step={n} action={action} reward={0.00} done={true|false} error={msg|null}
[END] success={true|false} steps={n} rewards={r1,r2,...,rn}
```

## Deploy to Hugging Face Spaces

```bash
openenv push --repo-id <username>/meta-hackathon
```

## Project Structure

- models.py
- client.py
- inference.py
- openenv.yaml
- server/app.py
- server/meta_hackathon_environment.py
- server/scenarios.py
- server/graders.py

## Baseline notes

Baseline runs all three tasks sequentially (easy, medium, hard) and reports per-task episode logs with deterministic environment dynamics.

## Setup and UI testing guide

See `SETUP_UI_GUIDE.md` for a complete local setup checklist and browser-based UI testing flow.

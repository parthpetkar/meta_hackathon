---
title: Meta Hackathon CI/CD Repair Environment
emoji: "🔨"
colorFrom: red
colorTo: gray
sdk: docker
pinned: false
app_port: 8000
base_path: /web
tags:
  - openenv
  - cicd
  - benchmark
---

## Meta Hackathon CI/CD Repair Environment

Deterministic OpenEnv environment for CI/CD pipeline failure diagnosis and repair.

The agent investigates pipeline evidence, sets a root-cause hypothesis, applies a fix, and verifies recovery.

## Why this benchmark is useful

This models a real DevOps workflow with measurable outcomes:

- investigation quality
- diagnosis correctness
- fix correctness
- verification discipline
- safe behavior under pressure

## OpenEnv Interface

The environment implements:

- `reset()` -> initial observation
- `step(action)` -> next observation with `reward` and `done`
- `state()` -> `episode_id` and `step_count`

## Action Space

`MetaHackathonAction`

- `operation`: one of
  - `inspect_pipeline`
  - `inspect_stage`
  - `inspect_logs`
  - `inspect_git`
  - `inspect_docker`
  - `inspect_tests`
  - `inspect_dependencies`
  - `inspect_permissions`
  - `set_hypothesis`
  - `apply_fix`
  - `verify_fix`
- `target`: optional stage/system target
- `value`: optional hypothesis/fix payload

## Observation Space

`MetaHackathonObservation`

- `task_id`, `task_title`, `difficulty`
- `pipeline_status`, `current_stage`
- `available_stages`, `available_tools`
- `visible_alerts`, `visible_logs`, `visible_metrics`
- `findings`, `action_history`
- `current_hypothesis`, `attempted_fix`
- `incident_resolved`
- `final_score` (0.0 to 1.0 at terminal step)
- `reward`, `done`, `metadata`

## Tasks (easy -> medium -> hard)

1. `easy_merge_conflict`

- Root cause: feature branch stale + unresolved merge conflict
- Correct fix: `resolve-merge-conflict`

1. `medium_docker_dep_failure`

- Root cause: dependency conflict (`requests` vs `urllib3` constraints)
- Correct fix: `pin-compatible-requests-version`

1. `hard_permission_timeout_chain`

- Root cause: CI service account lacks registry write permission
- Correct fix: `grant-registry-write-permission`

## Reward and Grading

Per-step reward (`step_reward`) gives shaped feedback:

- positive for useful new inspections
- positive for correct hypothesis/fix/verification
- negative for repeated actions
- strong negative for destructive fixes

Terminal score (`grade_episode`) is deterministic in [0.0, 1.0] from:

- clue coverage
- hypothesis correctness
- fix correctness
- verification
- efficiency
- destructive action penalties

## Quick Start

### 1) Install dependencies

```bash
uv sync
```

### 2) Run local server

```bash
uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
```

### 3) Run baseline inference

```bash
uv run python inference.py
```

## Baseline Results

Reference deterministic baseline (golden policy from `README_UI_TESTING.md`) on local runs:

| Task | Steps | Reward Sum | Final Score | Resolved |
| --- | ---: | ---: | ---: | ---: |
| easy_merge_conflict | 6 | 1.30 | 0.849 | true |
| medium_docker_dep_failure | 6 | 1.30 | 0.822 | true |
| hard_permission_timeout_chain | 7 | 1.40 | 0.870 | true |

Reproduce with your model baseline via:

```bash
uv run python inference.py
```

Notes:

- The environment is deterministic, so policy-level scores are stable across runs.
- Inference stdout is restricted to structured `[START]`, `[STEP]`, and `[END]` lines.

## Environment Variables

Use `.env.example` as template.

Required for model inference:

- `API_BASE_URL`
- `MODEL_NAME`
- `HF_TOKEN` (or `OPENAI_API_KEY`)

Useful for local testing:

- `LOCAL_IMAGE_NAME` / `IMAGE_NAME`
- `ENV_BASE_URL`
- `META_HACKATHON_BENCHMARK`
- `META_HACKATHON_TASK_MODE` (`cycle`, `easy`, `medium`, `hard`)

## UI Testing Inputs

Use the copy-paste test payloads in:

- `README_UI_TESTING.md`

That file includes:

- golden paths for easy/medium/hard
- negative tests (unsupported operations, destructive fixes)
- expected outcomes and smoke checklist

## Inference Log Contract

`inference.py` prints:

- `[START] task=<task> env=<benchmark> model=<model>`
- `[STEP] step=<n> action=<operation|target|value> reward=<0.00> done=<true|false> error=<msg|null>`
- `[END] success=<true|false> steps=<n> rewards=<r1,r2,...,rn>`

## Project Files

- `models.py`
- `client.py`
- `inference.py`
- `openenv.yaml`
- `server/app.py`
- `server/meta_hackathon_environment.py`
- `server/scenarios.py`
- `server/graders.py`
- `README_UI_TESTING.md`

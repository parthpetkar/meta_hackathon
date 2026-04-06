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

The agent investigates evidence, applies staged remediations, reruns pipeline stages, and finalizes only after full recovery.

The benchmark now includes deterministic per-episode scenario variants to improve robustness and reduce memorization.

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

- `operation`: canonical operations
  - `view_logs`
  - `inspect_config`
  - `inspect_dockerfile`
  - `modify_config`
  - `add_dependency`
  - `rerun_pipeline`
  - `finalize`
  - `inspect_permissions`
  - `set_hypothesis`
- `target`: optional stage/system target
- `value`: optional hypothesis/fix payload

Legacy operations (`inspect_*`, `apply_fix`, `verify_fix`) remain accepted via deterministic alias mapping for backward compatibility.

## Observation Space

`MetaHackathonObservation`

- `task_id`, `task_title`, `difficulty`
- `pipeline_status`, `current_stage`, `pipeline_stages`
- `available_stages`, `available_tools`
- `visible_alerts`, `visible_logs`, `visible_metrics`, `logs_by_stage`
- `config_files`, `surfaced_errors`
- `findings`, `action_history`, `previous_actions`
- `current_hypothesis`, `attempted_fix`
- `hypothesis_history`, `active_issue_index`, `revealed_issue_count`
- `pipeline_health`, `recovery_cost`, `redundant_actions`, `destructive_actions`
- `incident_resolved`
- `final_score` (0.0 to 1.0 at terminal step)
- `reward`, `done`, `metadata`

## Tasks (easy -> medium -> hard)

1. `easy_merge_conflict`

- Iteration 1: ambiguous merge failure in build
- Partial fix can reveal iteration 2 test contract drift
- Final state: branch synced/rebased and tests stabilized

1. `medium_docker_dep_failure`

- Iteration 1: ambiguous dependency conflict (`requests`/`urllib3`) at build
- Partial fix can reveal iteration 2 Docker install-order instability
- Final state: compatible dependency pin + corrected Docker order

1. `hard_permission_timeout_chain`

- Iteration 1: deploy failure mixing permission denied + timeout symptoms
- Partial fix can reveal iteration 2 timeout tuning after auth recovery
- Final state: permission repair + timeout retuning

## Reward and Grading

Per-step reward (`step_reward`) gives shaped feedback:

- positive for novel evidence gathering
- positive for correct or partial intermediate fixes
- positive for meaningful rerun progression
- penalties for redundant or destructive actions
- penalties for premature finalization

Terminal score (`grade_episode`) is deterministic in [0.0, 1.0] with correctness-first weighting:

- correctness (issue resolution + final recovered state)
- reasoning quality (inspection coverage + hypothesis quality)
- efficiency (step economy and redundancy control)
- health/destructive penalties for harmful trajectories

Additional anti-gaming behavior:

- penalties for blind fixes before evidence collection
- penalties for premature `finalize` when unresolved stages remain
- family-level partial credit when hypothesis matches failure family but not exact cause

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

### 4) Run deterministic evaluation suite

```bash
uv run evaluate
```

Optional env vars:

- `EVAL_EPISODES_PER_TASK` (default `3`)
- `SUCCESS_SCORE_THRESHOLD` (default `0.20`)

## Baseline Results

`inference.py` evaluates one pass of easy -> medium -> hard using an LLM policy with deterministic rescue controls.

Reproduce with:

```bash
uv run python inference.py
```

Notes:

- The environment is deterministic, so policy-level scores are stable across runs.
- Inference stdout is restricted to structured `[START]`, `[STEP]`, and `[END]` lines.
- Variant IDs are exposed in observation metadata (`metadata.variant_id`) for replayability.

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
- `[END] success=<true|false> steps=<n> score=<0.000> resolved=<true|false> rewards=<r1,r2,...,rn>`

`evaluate` prints:

- `[EVAL]` run configuration
- `[EP]` per-episode task/variant/score/resolution lines
- `[SUMMARY]` resolved rate, success rate, avg score, avg steps by task
- `[TOP_FAILURE_REASONS]` for unresolved episodes

## Project Files

- `models.py`
- `client.py`
- `inference.py`
- `eval_runner.py`
- `openenv.yaml`
- `server/app.py`
- `server/meta_hackathon_environment.py`
- `server/scenarios.py`
- `server/graders.py`
- `README_UI_TESTING.md`

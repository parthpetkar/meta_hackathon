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

## Environment description

This environment simulates a CI/CD debugging and repair workflow for reinforcement learning agents.
At every episode, an agent must investigate pipeline evidence, infer the root cause, apply safe fixes,
rerun the pipeline, and finalize only when the incident is truly resolved.

Why this matters for RL:

- The task requires sequential reasoning under uncertainty (logs are noisy and partially ambiguous).
- The action space mixes diagnosis and intervention, creating realistic credit-assignment challenges.
- Rewards encourage operationally safe behavior, not just short-term score gaming.
- Failure logs are sampled from a real-world inspired pattern library so episodes are not identical.

OpenEnv API compliance:

- `reset()` returns the initial observation.
- `step(action)` returns next observation, reward, and done flag.
- `state()` returns `episode_id` and `step_count`.

## Action space

All actions use the schema: `operation | target | value`

| Operation | Target (arg) | Value (arg) | Description |
| --- | --- | --- | --- |
| `view_logs` | optional stage (`build/test/deploy`) | optional | Read pipeline/runtime logs for the active failure context. |
| `inspect_config` | optional stage/component | optional | Inspect CI/deploy config clues and surfaced config files. |
| `inspect_dockerfile` | optional component | optional | Inspect Dockerfile/security build clues. |
| `inspect_permissions` | optional component | optional | Inspect IAM/service-account permission clues. |
| `set_hypothesis` | must be empty | hypothesis text | Declare current root-cause hypothesis. |
| `modify_config` | optional stage/component | fix text | Apply config/deploy/rollback/security fix candidate. |
| `add_dependency` | optional stage/component | dependency fix text | Apply dependency pin/compatibility fix. |
| `rerun_pipeline` | empty | empty | Re-run pipeline after fix attempts to validate progression. |
| `finalize` | empty | empty | End episode and request final scoring. |

## Observation space

At each step the agent receives structured state including:

- Task metadata: `task_id`, `task_title`, `difficulty`.
- Pipeline status: `pipeline_status`, `current_stage`, `pipeline_stages`.
- Evidence: `visible_alerts`, `visible_logs`, `logs_by_stage`, `visible_metrics`, `surfaced_errors`.
- Config snapshots: `config_files`.
- Reasoning trace: `findings`, `action_history`, `current_hypothesis`, `attempted_fix`, `hypothesis_history`.
- Progress indicators: `active_issue_index`, `revealed_issue_count`, `incident_resolved`.
- Safety/cost signals: `pipeline_health`, `recovery_cost`, `redundant_actions`, `destructive_actions`.
- Episode outputs: `reward`, `done`, `final_score` (terminal), and `metadata`.

## Reward structure

Per-step reward schema:

| Action type | Reward |
| --- | --- |
| `set_hypothesis` (correct, first try) | `+0.22` |
| `set_hypothesis` (correct, retry) | `+0.10` |
| `set_hypothesis` (wrong) | `-0.10` |
| `inspect_*` (relevant stage) | `+0.12` |
| `inspect_*` (irrelevant stage) | `-0.05` |
| `modify_config` (correct fix) | `+0.35` |
| `modify_config` (wrong fix) | `-0.20` |
| `add_dependency` (correct) | `+0.25` |
| `add_dependency` (wrong/redundant) | `-0.18` |
| `rerun_pipeline` (after valid fix) | `+0.18` |
| `rerun_pipeline` (premature) | `+0.05` |
| `finalize` (correct) | `+0.25` |
| `finalize` (incorrect state) | `-0.30` |

Task-specific reward extensions:

- Hard task red herring action: additional `-0.15` when a plausible but incorrect shortcut is attempted.
- Security task: finalizing after fixing exactly one of two required issues gives partial credit; finalizing after both issues are fixed gives `+0.35`.

Terminal score is clipped to `[0.0, 1.0]` and difficulty-calibrated to preserve the expected gradient:

- Easy target: about `0.55` to `0.65`
- Medium target: about `0.40` to `0.50`
- Security target: between medium and hard
- Hard target: about `0.25` to `0.38`

## Task descriptions

`easy` - Single-file merge conflict (5-step resolution target)

- One root cause: unresolved merge markers in `services/api/routes.py`.
- One inspect pass reveals the issue.
- One config fix resolves it, then rerun, then finalize.

`medium` - Dependency + Docker ordering chain

- Build fails due to `requests`/`urllib3` incompatibility.
- After dependency remediation, Docker install-order instability may remain.
- Agent must perform dependency fix and Docker order correction.

`security` - IAM + secret exposure misconfiguration

- Deploy fails because service account lacks `roles/artifactregistry.writer`.
- Security gate also fails because `API_KEY` is exposed in Dockerfile `ENV`.
- Agent must inspect deploy logs and Dockerfile, then fix both IAM and secret handling.

`hard` - Multi-service cascade with rollback

- Service A (build/publish) fails first: artifact registry permission denied on image push.
- Service B deploy then fails due image unavailability and timeout pressure.
- Correct sequence is: fix Service A permissions -> rollback Service B to stable revision -> tune rollout timeout.
- Includes a red-herring shortcut action that is penalized.

## Setup instructions

### Run locally with Docker

1. Build image:

```bash
docker build -t meta-hackathon-env .
```

1. Start API server:

```bash
docker run --rm -p 8000:8000 meta-hackathon-env
```

1. Validate OpenEnv endpoints:

- `POST /reset`
- `POST /step`
- `GET /state`

### Run locally without Docker

```bash
uv sync
uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
```

## Inference

`inference.py` stays at repo root and prints strict structured logs:

- `[START] task=... env=... model=...`
- `[STEP] step=... action=operation|target|value reward=... done=... error=...`
- `[END] success=... steps=... score=... resolved=... rewards=...`

Set model/client environment variables:

- `API_BASE_URL`
- `MODEL_NAME`
- `HF_TOKEN` (or `OPENAI_API_KEY`)

Run inference:

```bash
uv run python inference.py
```

Run deterministic evaluation:

```bash
uv run evaluate
```

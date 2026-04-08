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
rerun the pipeline, verify the fix signal, and finalize only when the incident is truly resolved.

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
| `verify_fix` | empty | empty | Confirm rerun evidence indicates the active failure was removed. |
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
| `verify_fix` (valid post-rerun verification) | `+0.16` |
| `verify_fix` (without valid rerun evidence) | `-0.06` |
| `finalize` (correct) | `+0.25` |
| `finalize` (incorrect state) | `-0.15` |

Task-specific reward extensions:

- Hard task red herring action: additional `-0.15` when a plausible but incorrect shortcut is attempted.
- Security task: finalizing after fixing exactly one of two required issues gives partial credit; finalizing after both issues are fixed gives the standard `+0.25` terminal finalize reward.

Terminal score is clipped to `[0.0, 1.0]` and difficulty-calibrated to preserve the expected gradient:

- Easy target: about `0.55` to `0.65`
- Medium target: about `0.40` to `0.50`
- Security target: between medium and hard
- Hard target: about `0.25` to `0.38`

## Task descriptions

`easy` - Single-file merge conflict (6-step resolution target)

- One root cause: unresolved merge markers in `services/api/routes.py`.
- One inspect pass reveals the issue.
- One config fix resolves it, then rerun, verify, and finalize.

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

## Design rationale and contribution angle

- Real-world abstraction: models staged CI/CD incidents where upstream build failures create downstream deploy symptoms.
- Why RL over rules: agents must sequence information-gathering and interventions under partial observability, with delayed reward and penalties for unsafe shortcuts.
- Open-source contribution value: includes deterministic scenario cards, reproducible variant sampling, and transparent reward shaping intended for extension in the wider OpenEnv benchmark ecosystem.
- Extensibility entry points: task cards live in `server/scenarios.py`, step/grader logic lives in `server/meta_hackathon_environment.py` and `server/graders.py`, and deterministic regression eval lives in `eval_runner.py`.

For a deeper narrative of intended upstream value and extension strategy, see `DESIGN.md`.

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

## Reproducibility artifacts

The repository includes a `results/` folder with:

- Deterministic evaluation log: `results/eval_2026-04-07_utf8.log`.
- Deterministic inference trajectories: `results/inference_2026-04-07_utf8.log`.
- Per-task summary metrics: `results/task_metrics_2026-04-07.json`.
- Reward-per-step table: `results/reward_over_steps_2026-04-07.csv`.
- Reward trend visualization: `results/reward_over_steps_2026-04-07.svg`.
- Analysis brief: `results/ANALYSIS.md`.

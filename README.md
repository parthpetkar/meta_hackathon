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

This environment runs a real CI/CD debugging and repair workflow for reinforcement learning agents.
Each episode creates a workspace from `sample-app`, injects a fault into real files, runs a real
subprocess-backed pipeline, and returns structured evidence.

At every episode, an agent must investigate pipeline evidence, infer the root cause, apply safe fixes,
rerun the pipeline, verify the fix signal, and finalize only when the incident is truly resolved.

Why this matters for RL:

- The task requires sequential reasoning under uncertainty (logs are noisy and partially ambiguous).
- The action space mixes diagnosis and intervention, creating realistic credit-assignment challenges.
- Rewards encourage operationally safe behavior, not just short-term score gaming.
- Failure logs and file evidence come from real pipeline execution against the faulted workspace.

OpenEnv API compliance:

- `reset()` returns the initial observation.
- `step(action)` returns next observation, reward, and done flag.
- `state()` returns `episode_id` and `step_count`.

Runtime architecture (current):

- `server/environment.py` hosts `RealCICDRepairEnvironment` and action dispatch.
- `cicd/fault_injector.py` injects task faults into the episode workspace.
- `cicd/pipeline_runner.py` executes `clone -> build -> test -> deploy` via subprocess.
- `cicd/observation_builder.py` builds surfaced errors/logs/config snapshots.
- `cicd/fix_applier.py` applies structured JSON or heuristic fixes and commits successful changes.

## Action space

All actions use the schema: `operation | target | value`

| Operation             | Target (arg)                         | Value (arg)         | Description                                                       |
| --------------------- | ------------------------------------ | ------------------- | ----------------------------------------------------------------- |
| `view_logs`           | optional stage (`build/test/deploy`) | optional            | Read pipeline/runtime logs for the active failure context.        |
| `inspect_config`      | optional stage/component             | optional            | Inspect CI/deploy config clues and surfaced config files.         |
| `inspect_dockerfile`  | optional component                   | optional            | Inspect Dockerfile/security build clues.                          |
| `inspect_permissions` | optional component                   | optional            | Inspect IAM/service-account permission clues.                     |
| `set_hypothesis`      | must be empty                        | hypothesis text     | Declare current root-cause hypothesis.                            |
| `modify_config`       | optional stage/component             | JSON fix string     | Apply config/deploy/security fix; prefer structured JSON payload. |
| `add_dependency`      | optional stage/component             | dependency fix text | Apply dependency pin/compatibility fix.                           |
| `rerun_pipeline`      | empty                                | empty               | Re-run pipeline after fix attempts to validate progression.       |
| `verify_fix`          | empty                                | empty               | Confirm rerun evidence indicates the active failure was removed.  |
| `finalize`            | empty                                | empty               | End episode and request final scoring.                            |

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

Reset observations already include the initial alert and a first batch of real failure logs and surfaced file errors, so agents can begin reasoning from step 0 before choosing whether to call `view_logs` again.

Fix payload format for `modify_config`:

- Preferred: JSON string with `file`, `action`, and content fields (for example `replace` with `old` and `new`).
- Supported actions in the fix engine: `replace`, `delete_lines`, and `write`.
- Fallback exists for plain-English heuristic fixes, but structured JSON is more reliable.

When `META_HACKATHON_AUDIT_TRAIL=true`, observation metadata also includes deterministic provenance fields:

- `audit_enabled`, `episode_seed`, `variant_id`
- `active_issue_pattern_buckets`
- `sampled_pattern_event_count`
- `sampled_pattern_events` (bucket + sampled line index + seed + sampled line text)

## Reward structure

Per-step reward schema:

| Action type                                           | Reward  |
| ----------------------------------------------------- | ------- |
| `set_hypothesis` (correct, first try)                 | `+0.22` |
| `set_hypothesis` (correct, retry)                     | `+0.10` |
| `set_hypothesis` (wrong)                              | `-0.10` |
| `inspect_*` (relevant stage)                          | `+0.12` |
| `inspect_*` (irrelevant stage)                        | `-0.05` |
| `modify_config` / `add_dependency` (fix applied)      | `+0.10` |
| `modify_config` / `add_dependency` (fix failed)       | `-0.15` |
| `modify_config` / `add_dependency` (destructive fix)  | `-0.30` |
| `rerun_pipeline` (after valid fix)                    | `+0.18` |
| `rerun_pipeline` (premature)                          | `+0.05` |
| `verify_fix` (valid post-rerun verification)          | `+0.16` |
| `verify_fix` (partial progress)                       | `+0.08` |
| `verify_fix` (without valid rerun evidence)           | `-0.06` |
| `finalize` (correct)                                  | `+0.25` |
| `finalize` (partial resolution)                       | `+0.20` |
| `finalize` (incorrect state)                          | `-0.15` |

Additional runtime scoring rules:

- Repeating an identical `operation:target:value` gets a redundancy penalty (reward is clamped to at most `-0.08`).
- Final score is computed at episode end in `server/environment.py` and is clipped to `[0.0, 1.0]`.
- Final score components include: resolution status, hypothesis quality, fix hits, efficiency bonus, and penalties for redundant/destructive/wrong fixes.

## Task descriptions

`easy` - Single-file merge conflict (6-step resolution target)

- One root cause: unresolved merge markers in `services/api/routes.py`.
- One inspect pass reveals the issue.
- One config fix resolves it, then rerun, verify, and finalize.

`flaky` - Flaky test tolerance (timing-sensitive CI instability)

- Test stage intermittently fails, then passes on immediate retry.
- Agent must diagnose this as a flaky/timing issue rather than a true product-code regression.
- Correct remediation is retry/isolation-safe test policy updates (not broad code rewrites or disabling tests).

`medium` - Dependency + Docker ordering chain

- Build fails due to `requests`/`urllib3` incompatibility.
- After dependency remediation, Docker install-order instability may remain.
- Agent must perform dependency fix and Docker order correction.

`network` - Deploy/network configuration failure

- Deploy path fails due to runtime network/permission style misconfiguration in compose/runtime setup.
- Agent should inspect deploy/build evidence and apply targeted configuration repair.

`security` - Secret exposure misconfiguration

- Build security gate fails because a hardcoded secret is detected.
- Agent must remove the exposed secret safely and rerun/verify before finalize.

`hard` - Docker layer/order failure

- Build fails due to broken Dockerfile copy/install ordering.
- Agent should inspect Dockerfile and apply order/layer correction, then rerun/verify/finalize.

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

- Real-world abstraction: each episode uses a real workspace, real file mutations, and real subprocess pipeline stages.
- Why RL over rules: agents must sequence evidence gathering, hypothesis quality, safe edits, and verification under noisy logs.
- Open-source contribution value: reproducible fault injection, transparent step/reward dynamics, and OpenEnv-compatible serving.
- Extensibility entry points: environment/action logic in `server/environment.py`, pipeline/fault/fix subsystems in `cicd/`, deterministic regression eval in `eval_runner.py`, and agentic baseline modules in `agent/`.

For a deeper narrative of intended upstream value and extension strategy, see `DESIGN.md`.

## Project layout

- `models.py` and `client.py`: OpenEnv action/observation models and client adapter.
- `server/`: API app and real environment runtime (`app.py`, `environment.py`).
- `cicd/`: real pipeline runner, fault injector, fix applier, and observation builder.
- `agent/`: modular inference baseline (`config`, `prompts`, `tool_schemas`, action guards, fallback plans, HTTP environment helpers, model tool-call translation, and runner).
- `inference.py`: thin compatibility entry point for the agentic baseline.
- `eval_runner.py`: deterministic regression evaluator for calibration and smoke tests.
- `tests/`: environment regression tests.
- `results/`: generated evaluation artifacts and logs.

## Inference

`inference.py` is the agentic baseline entry point. The implementation lives in `agent/` so prompts, tool schemas, action guards, fallback plans, HTTP calls, logging, and orchestration can be debugged independently. By default it keeps the model in the loop across the task budget (`MAX_MODEL_CALLS_PER_TASK` defaults to the per-task step ceiling, `PREFER_DETERMINISTIC_ACTIONS=false`), and only falls back to the scripted policy when tool-calling repeatedly fails or the trajectory clearly stalls.

`eval_runner.py` is separate: it is a deterministic regression baseline for score calibration and reproducibility, not a claim that the environment itself is solved by hardcoded control flow.

`inference.py` stays at repo root and prints strict structured logs:

- `[START] task=... env=... model=...`
- `[STEP] step=... action=operation|target|value reward=... done=... error=...`
- `[END] success=... steps=... score=... resolved=... rewards=...`

### 2. Configure environment variables

Duplicate `.env.example` (or set these directly) to configure inference and grading features:

```bash
# Inference and rubric model endpoint (Groq)
MODEL_NAME=llama-3.3-70b-versatile
API_BASE_URL=https://api.groq.com/openai/v1
GROQ_API_KEY=your_groq_api_key_here

# OpenEnv server base URL used by the agent
ENV_BASE_URL=http://localhost:8000

# Task mode (easy|flaky|medium|network|security|hard)
META_HACKATHON_TASK_MODE=easy

# Agent runtime controls
MAX_MODEL_CALLS_PER_TASK=16
MAX_CONSECUTIVE_TOOL_CALL_MISSES=4
MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK=4
INFERENCE_VERBOSE=true

# Server/runtime controls
META_HACKATHON_PIPELINE_TIMEOUT_SECONDS=300

# Rubric delayed-reward controls
META_HACKATHON_RUBRIC_ENABLED=true
META_HACKATHON_RUBRIC_WEIGHT=0.30
META_HACKATHON_RUBRIC_TIMEOUT_SECONDS=10
META_HACKATHON_RUBRIC_MODEL=llama-3.3-70b-versatile
META_HACKATHON_RUBRIC_DEBUG=false
```

Optional local inference debug variables:

- `INFERENCE_DETAIL_MAX_ITEMS` (default `3`, controls list preview size in `[DETAIL]` lines)
- `SUCCESS_SCORE_THRESHOLD` (default `0.20`)

## Provenance Audit Trail

The environment can emit a deterministic evidence lineage at runtime for judge-side realism auditing.

- Audit trail is additive and backward-compatible (default off).
- With audit trail enabled, every sampled pattern line is traceable to a pattern bucket, sampled line index, and issue seed.
- This makes scenario evidence auditable without changing action schema or core scoring behavior.

## Hugging Face Space README

- The root `README.md` in this repository is the canonical Space card README.
- If your deployed Space repo is separate and missing docs, copy the content from `HF_SPACE_README.md` into that Space repo as `README.md`.

Run inference:

```bash
uv run python inference.py
# or, after installing the project scripts:
uv run inference
````

Run deterministic evaluation:

```bash
uv run evaluate
```

## Submission Gate Validation (2026-04-08)

### OpenEnv spec validation

```bash
uv run openenv validate
```

Observed output:

```text
[OK] meta_hackathon: Ready for multi-mode deployment
```

The validation run confirms the required `/state` endpoint is implemented and discoverable by the OpenEnv validator even though the baseline agent loop does not need to call it directly.

### Docker build and runtime validation

```bash
docker build -t meta-hackathon-env .
docker run --rm -p 8000:8000 meta-hackathon-env
```

Observed results:

- Image build completed successfully.
- Health check endpoint returned healthy: `GET /health -> {"status":"healthy"}`
- OpenEnv endpoint responded correctly: `POST /reset -> 200` with structured observation payload.

### External Hugging Face Space reset check

```bash
POST https://parthpetkar-metahackathon.hf.space/reset
```

Observed result:

- External `POST /reset` returned `200` with a valid observation payload.

### Inference runtime check (<20 min constraint)

Runtime benchmark command (deterministic fallback mode to isolate environment runtime from external model latency):

```bash
MAX_MODEL_CALLS_PER_TASK=0 uv run python inference.py
```

Observed runtime:

- `ELAPSED_SECONDS=29.58` (well below the 20-minute requirement on this machine).

### Current score gradient evidence

```bash
uv run evaluate
```

Observed summary means:

- easy: `avg_score=0.735`
- medium: `avg_score=0.617`
- security: `avg_score=0.542`
- hard: `avg_score=0.500` (`det=0.420`, `redundant_actions=0` on the canonical hard baseline, with hard delayed-reward cap `0.08`)

Notes:

- `eval_runner.py` uses the deterministic regression policy, not the agentic `inference.py` loop.
- Rubric-dependent fields can vary if the external semantic judge is unavailable; check `rubric_judge_used` and `rubric_judge_error` in eval output when reproducing blended scores.

## Reproducibility artifacts

The repository includes a `results/` folder with:

- Deterministic evaluation log: `results/eval_2026-04-07_utf8.log`.
- Deterministic inference trajectories: `results/inference_2026-04-07_utf8.log`.
- Per-task summary metrics: `results/task_metrics_2026-04-07.json`.
- Reward-per-step table: `results/reward_over_steps_2026-04-07.csv`.
- Reward trend visualization: `results/reward_over_steps_2026-04-07.svg`.
- Analysis brief: `results/ANALYSIS.md`.

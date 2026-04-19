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

| Operation             | Target (arg)                         | Value (arg)         | Description                                                      |
| --------------------- | ------------------------------------ | ------------------- | ---------------------------------------------------------------- |
| `view_logs`           | optional stage (`build/test/deploy`) | optional            | Read pipeline/runtime logs for the active failure context.       |
| `inspect_config`      | optional stage/component             | optional            | Inspect CI/deploy config clues and surfaced config files.        |
| `inspect_dockerfile`  | optional component                   | optional            | Inspect Dockerfile/security build clues.                         |
| `inspect_permissions` | optional component                   | optional            | Inspect IAM/service-account permission clues.                    |
| `set_hypothesis`      | must be empty                        | hypothesis text     | Declare current root-cause hypothesis.                           |
| `modify_config`       | optional stage/component             | fix text            | Apply config/deploy/rollback/security fix candidate.             |
| `add_dependency`      | optional stage/component             | dependency fix text | Apply dependency pin/compatibility fix.                          |
| `rerun_pipeline`      | empty                                | empty               | Re-run pipeline after fix attempts to validate progression.      |
| `verify_fix`          | empty                                | empty               | Confirm rerun evidence indicates the active failure was removed. |
| `finalize`            | empty                                | empty               | End episode and request final scoring.                           |

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

Reset observations already include the initial alert and a first batch of sampled failure logs, so agents can begin reasoning from step 0 before choosing whether to call `view_logs` again.

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
| `modify_config` (correct fix, default path)           | `+0.35` |
| `modify_config` (partial fix, default path)           | `+0.20` |
| `modify_config` (wrong/destructive fix, default path) | `-0.20` |
| `add_dependency` (correct and non-redundant)          | `+0.25` |
| `add_dependency` (wrong/redundant)                    | `-0.18` |
| `rerun_pipeline` (after valid fix)                    | `+0.18` |
| `rerun_pipeline` (premature)                          | `+0.05` |
| `verify_fix` (valid post-rerun verification)          | `+0.16` |
| `verify_fix` (without valid rerun evidence)           | `-0.06` |
| `finalize` (correct)                                  | `+0.25` |
| `finalize` (security partial remediation)             | `+0.20` |
| `finalize` (incorrect state)                          | `-0.15` |

Task-specific reward extensions:

- Hard task `modify_config` override by cascade stage: issue 1 `+0.35`, issue 2 `+0.20`, issue 3 `+0.35`.
- Hard task red herring action: additional `-0.15` when a plausible but incorrect shortcut is attempted.
- Security task: finalizing after fixing exactly one of two required issues gives partial credit; finalizing after both issues are fixed gives the standard `+0.25` terminal finalize reward.

Important runtime note:

- At terminal step only, the emitted `reward` includes delayed rubric blending (`reward = step_reward + delayed_reward`). This is why final-step logs can be above base table values (for example, `finalize` can appear as `0.33` to `0.36` when rubric is enabled).

Deterministic terminal score is clipped to `[0.0, 1.0]` and difficulty-calibrated to preserve the expected gradient:

- Easy target: about `0.55` to `0.65`
- Medium target: about `0.40` to `0.50`
- Security target: between medium and hard
- Hard target: about `0.30` to `0.44`

When rubric scoring is enabled, delayed reward is blended at terminal step and capped by difficulty to preserve separation across tasks (`easy: 0.12`, `medium: 0.11`, `security: 0.10`, `hard: 0.08`). This keeps hard-task blended scores in-band while still letting semantic rubric quality matter on the multi-issue cascade.

Rubric delayed reward:

- When `META_HACKATHON_RUBRIC_ENABLED=true`, the environment computes an additional terminal semantic score for hypothesis quality.
- The semantic score is produced by an OpenEnv `LLMJudge` adapter when available.
- Fallback order is deterministic: OpenEnv `LLMJudge` -> API LLM scoring -> heuristic semantic scorer.
- Final score blending: `blended = (1 - w) * deterministic + w * rubric`, where `w = META_HACKATHON_RUBRIC_WEIGHT`.
- Delayed reward contribution at terminal step: `delayed_reward = blended - deterministic`.
- Observations expose `deterministic_score`, `rubric_score`, `delayed_reward`, `rubric_blend_weight`, `rubric_judge_used`, and `rubric_judge_error`.

Rubric judge debug signals:

- Set `META_HACKATHON_RUBRIC_DEBUG=true` to log judge initialization and scoring path decisions.
- Per episode, use `eval_runner.py` output fields `det`, `rubric`, `delayed`, and `judge` to validate blending behavior.
- `rubric_judge_used=true` indicates semantic (non-heuristic) judge output; inspect `rubric_judge_error` when fallback occurs.
- `rubric_fallbacks` in eval summary counts episodes that required fallback due judge/API failures.

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

`network` - External dependency outage (transient DNS/network failure)

- Artifact upload fails because an external endpoint is temporarily unreachable.
- Agent must classify the failure as transient external/network, not an internal logic bug.
- Correct remediation is resilient retry/backoff and/or proxy fallback configuration instead of rewriting application code.

`security` - IAM + secret exposure misconfiguration

- Deploy fails because service account lacks `roles/artifactregistry.writer`.
- Security gate also fails because `API_KEY` is exposed in Dockerfile `ENV`.
- Agent must inspect deploy logs and Dockerfile, then fix both IAM and secret handling.

`hard` - Multi-service cascade with rollback

- Service A (build/publish) fails first: artifact registry permission denied on image push.
- Service B deploy then fails due image unavailability and timeout pressure.
- Correct sequence is: fix Service A permissions -> rollback Service B to stable revision -> tune rollout timeout.
- Includes a red-herring shortcut action that is penalized.
- Expected difficulty: highest. The task requires three linked remediations within a 14-step budget and is intentionally more tolerant of repeated tool usage across issue transitions than within a single issue phase.

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
- Extensibility entry points: task cards live in `server/scenarios.py`, step/grader logic lives in `server/meta_hackathon_environment.py` and `server/graders.py`, deterministic regression eval lives in `eval_runner.py`, and the agentic baseline is split across `agent/`.

For a deeper narrative of intended upstream value and extension strategy, see `DESIGN.md`.

## Project layout

- `models.py` and `client.py`: OpenEnv action/observation models and client adapter.
- `server/`: environment runtime, scenarios, graders, failure patterns, and rubric judge adapter.
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

````bash
# Model for the inference agent (you must be logged into Hugging Face)
MODEL_NAME=Qwen/Qwen2.5-72B-Instruct
API_BASE_URL=https://router.huggingface.co/v1
HF_TOKEN=your_hf_token_here

# (Optional) Provide a separate token for the Rubric Judge to avoid rate-limiting
# If empty, falls back to HF_TOKEN or OPENAI_API_KEY
META_HACKATHON_RUBRIC_API_KEY=your_rubric_api_key_here

# Enable semantic rubric blending for evaluation
- `META_HACKATHON_RUBRIC_ENABLED` (`true`/`false`)
- `META_HACKATHON_RUBRIC_WEIGHT` (`0.0` to `1.0`, default `0.30`)
- `META_HACKATHON_RUBRIC_TIMEOUT_SECONDS` (default `10`)
- `META_HACKATHON_RUBRIC_MODEL` (optional override; defaults to `MODEL_NAME`)
- `META_HACKATHON_RUBRIC_DEBUG` (`true`/`false`, default `false`)

Optional audit variable:

- `META_HACKATHON_AUDIT_TRAIL` (`true`/`false`, default `false`)

Optional local inference debug variables:

- `INFERENCE_VERBOSE` (`true`/`false`, default `false`)
- `INFERENCE_DETAIL_MAX_ITEMS` (default `3`, controls list preview size in `[DETAIL]` lines)
- `MAX_MODEL_CALLS_PER_TASK` (defaults to task step budget; set `0` to force deterministic fallback for smoke tests)
- `PREFER_DETERMINISTIC_ACTIONS` (`false` by default; set `true` only for scripted-regression comparisons)
- `MAX_CONSECUTIVE_TOOL_CALL_MISSES` (default `4`; model tool-call misses tolerated before disabling model calls)
- `MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK` (default `4`; minimum model-call attempts before miss-based fallback disable can trigger)

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

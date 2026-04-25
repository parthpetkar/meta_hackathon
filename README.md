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

This environment runs a CI/CD debugging and repair workflow for reinforcement learning agents.
Each episode creates a workspace from `sample-app`, injects a fault into real files, runs a
pipeline (real subprocess-backed or pure-Python simulated), and returns structured evidence.

At every episode, an agent must investigate pipeline evidence, infer the root cause, apply safe fixes,
rerun the pipeline, verify the fix signal, and finalize only when the incident is truly resolved.

Why this matters for RL:

- The task requires sequential reasoning under uncertainty (logs are noisy and partially ambiguous).
- The action space mixes diagnosis and intervention, creating realistic credit-assignment challenges.
- Rewards encourage operationally safe behavior, not just short-term score gaming.
- Failure logs and file evidence come from real or simulated pipeline execution against the faulted workspace.

OpenEnv API compliance:

- `reset()` returns the initial observation.
- `step(action)` returns next observation, reward, and done flag.
- `state()` returns `episode_id` and `step_count`.

Runtime architecture (current):

- `server/environment.py` hosts both `RealCICDRepairEnvironment` and `SimulatedCICDRepairEnvironment`; selection is controlled by `CICD_SIMULATE`. Within simulated mode, `CICD_SUBPROCESS_RUNNER=1` activates the subprocess-backed sandbox (see below).
- `server/rubric_judge.py` provides delayed reward rubric scoring with provider-aware fallback.
- `server/agent_memory.py` stores persistent cross-episode fix memory and optimal-path recall in SQLite.
- `server/curriculum.py` adapts difficulty from episode outcomes using UCB1 + EMA scheduling.
- `server/adversarial_designer.py` composes LLM-designed multi-fault incidents (cascading + red-herring).
- `server/adversarial_judge.py` applies phase-aware adversarial reward shaping.
- `cicd/fault_injector.py` injects task faults into the episode workspace via real file mutations + git commits (20 fault types).
- `cicd/simulated_fault_injector.py` injects the same 20 fault types via file mutations only — no git required.
- `cicd/pipeline_runner.py` executes `clone -> build -> test -> deploy` via subprocess with BuildKit layer caching.
- `cicd/simulated_runner.py` executes the same four stages in pure Python — no Docker required.
- `cicd/subprocess_runner.py` executes real `uv pip install`, `pytest`, and `uvicorn` subprocesses inside a per-episode venv — no Docker required, but produces authentic tool output.
- `cicd/observation_builder.py` builds surfaced errors/logs/config snapshots (works with all runners).
- `cicd/fix_applier.py` applies structured JSON or heuristic fixes and commits successful changes via git.
- `cicd/simulated_fix_applier.py` applies the same fix strategies without git commits.

## Architecture

The runtime follows a layered architecture to keep OpenEnv APIs stable while evolving internal behavior.

### 1) API + Session Layer

- `server/app.py`: OpenEnv HTTP server wiring (`/reset`, `/step`, `/state`). Reads `CICD_SIMULATE` at startup and selects the appropriate environment class.
- `server/environment.py`: `RealCICDRepairEnvironment` and `SimulatedCICDRepairEnvironment` — both implement the same episode state machine and action dispatcher. Curriculum, adversarial designer, judge, and memory components are shared identically between the two.

### 2) Execution Layer

Three parallel pipeline runner implementations — swap via env vars:

| Component | Real (`CICD_SIMULATE=false`) | Simulated (`CICD_SIMULATE=true`) | Subprocess sandbox (`CICD_SIMULATE=true` + `CICD_SUBPROCESS_RUNNER=1`) |
|---|---|---|---|
| Pipeline runner | `cicd/pipeline_runner.py` — Docker + Git | `cicd/simulated_runner.py` — pure Python | `cicd/subprocess_runner.py` — real uv/pytest/uvicorn, no Docker |
| Fault injector | `cicd/fault_injector.py` — mutations + git | `cicd/simulated_fault_injector.py` — mutations only | same as simulated |
| Fix applier | `cicd/fix_applier.py` — edits + git commit | `cicd/simulated_fix_applier.py` — file edits only | same (+ auto git commit) |
| Workspace setup | `git init` + initial commit | `shutil.copytree` (no git) | `shutil.copytree` + `git init` (feature branch per episode) |
| Build output | Real Docker/BuildKit logs | Synthetic uv-style logs | Real `uv pip install` output |
| Test output | Real pytest output | Synthetic pytest-style logs | Real `pytest` output |
| Deploy output | Real Docker Compose logs | Synthetic compose-style logs | Real `uvicorn` startup probe |

All three implementations support all fault types and use identical fix strategies (structured JSON → fault-type dispatch → heuristic → auto-repair).

### 3) Evidence Layer

- `cicd/observation_builder.py`: builds surfaced logs/errors/metrics/config snapshots. Works with both real and simulated pipeline results because both expose the same stage result interface.

### 4) Adaptation + Scoring Layer

Fully shared between real and simulated environments:

- `server/curriculum.py`: difficulty scheduling from prior outcomes.
- `server/adversarial_designer.py`: LLM-designed cascading incidents.
- `server/adversarial_judge.py`: phase-aware step/terminal shaping.
- `server/rubric_judge.py`: delayed semantic rubric score (OpenEnv judge + API fallback).
- `server/agent_memory.py`: persistent cross-episode fix recall.

### 5) Inference Layer

- `inference.py`: compatibility entrypoint plus run logging.
- `agent/runner.py`: tool-calling loop, action guards, memory-aware hints, optimal-path injection.
- `agent/prompts.py`: system prompt construction with skill cards and task-specific guidance.
- `agent/http_environment.py`: OpenEnv HTTP observation/action adapter.
- `agent/trajectory_logging.py`: strict START/STEP/END structured logs.

### End-to-end flow

1. `reset()` creates a workspace from `sample-app` (via git or shutil depending on mode), curriculum selects fault type via UCB1, LLM adversarial designer composes the scenario, faults are injected into real files, pipeline runs, observation returned.
2. Agent issues inspect/hypothesis/fix/rerun/verify actions via `step()`.
3. Environment updates state, applies phase shaping + safeguards, and emits next observation.
4. `finalize` is accepted only after a valid `verify_fix`; terminal score blends deterministic + optional rubric.
5. Episode outcome is recorded in curriculum; optimal fix path is stored in agent memory for future hint injection.

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

- Preferred and required: structured JSON string with `file`, `action`, and content fields.
- Supported actions: `replace` (old→new), `delete_lines` (remove lines matching pattern), `write` (overwrite file).
- The fix engine applies fixes in priority order: (A) structured JSON patch, (B) fault-type direct dispatch (server knows the active fault and routes to the correct fix function automatically), (C) keyword heuristic fallback, (D) generic auto-repair scan.
- Because the server always knows the active fault type, the agent does not need to use magic phrases — any structured JSON patch describing the correct file change will succeed.

Fault injection reliability:

- `flaky_test` now uses a deterministically failing threshold (0.001 s after a 0.1 s sleep) so the episode always has a detectable failure. The test still presents as a timing/flaky issue to the agent.
- `missing_permission` uses `external: true` without a `name:` alias for maximum Docker version compatibility.
- `infra_port_conflict` targets the correct `8001:8001` port mapping in `shared-infra/docker-compose.yml` (updated from the stale `8000:8000` reference). Both injector and fix applier handle both port variants for backward compatibility.

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

## Curriculum and LLM adversarial training

The runtime includes adaptive curriculum, adversarial incident generation, and cross-episode memory.

- `server/curriculum.py` persists episode outcomes, updates global difficulty via EMA (alpha=0.35), and selects fault types via UCB1 (step cap=0.15 for faster progression).
- Curriculum difficulty and skill-profile stats are injected into scenario design on every `reset()`.
- `server/adversarial_designer.py` uses an LLM to generate multi-fault incidents (root cause + cascades + optional red herring). Timeout is 15 s with automatic retry at higher `max_tokens` if JSON is truncated.
- `server/adversarial_judge.py` adds phase-aware shaping bonuses/penalties for triage, investigation, hypothesis, fix, and verification.
- `server/agent_memory.py` stores the optimal fix path from each resolved episode and injects it as a hint on the next episode of the same fault type, accelerating convergence.

How this affects episodes:

- Episodes are no longer static one-shot puzzles; they can include cascading failure structure.
- Agent behavior is rewarded for correct SRE phase progression, not only final pass/fail.
- Difficulty increases more aggressively (step cap 0.15 vs previous 0.08) so the curriculum escapes early-difficulty stagnation.
- Cascading faults are only injected when curriculum difficulty ≥ 0.65, keeping early episodes simple.

Key controls:

- Curriculum: `CURRICULUM_EMA_ALPHA`, `CURRICULUM_UCB_C`, `CURRICULUM_WARMUP`
- Adversarial designer endpoint/model: `CICD_ADV_BASE_URL`, `CICD_ADV_MODEL`
- Adversarial provider headers/keys: `OPENROUTER_API_KEY`, `OPENROUTER_REFERER`, `OPENROUTER_TITLE`
- Adversarial designer also respects `LLM_PROVIDER` — set `GROQ_API_KEY` or `HF_TOKEN` and the designer uses the same provider as the agent automatically.
- Episode count for inference/evaluation loops: `META_HACKATHON_NUM_EPISODES`

## Task descriptions

`easy` / `merge_conflict` — Single-file merge conflict

- One root cause: unresolved merge markers in `services/api/routes.py`.
- One inspect pass reveals the issue; one config fix resolves it, then rerun, verify, finalize.

`flaky_test` — Flaky test tolerance (timing-sensitive CI instability)

- A timing-sensitive test with an impossibly tight threshold is injected into `tests/test_api.py`.
- Fault always fails in CI but presents as a flaky/timing regression to the agent.
- Correct remediation: remove or relax the timing assertion (retry policy or threshold fix), not broad code rewrites.

`dependency_conflict` / `medium` — Dependency version conflict

- Build fails due to `requests`/`urllib3` incompatibility in `services/api/requirements.txt`.
- Agent must pin compatible versions with `add_dependency`, then rerun/verify/finalize.

`docker_order` / `hard` — Dockerfile layer ordering

- Build fails because `pip install` runs before `COPY` in the Dockerfile.
- Agent should inspect the Dockerfile and apply order/layer correction.

`missing_permission` / `network` — Deploy network misconfiguration

- Deploy fails because `docker-compose.yml` references a non-existent external network (`corp-internal-network-v2`).
- Agent should inspect compose config, remove/replace the external network reference, rerun, verify, finalize.

`secret_exposure` / `security` — Hardcoded secrets

- Build security gate fails because hardcoded API keys are detected in `services/api/app.py`.
- Agent must remove the exposed secrets safely and rerun/verify before finalize.

`env_drift` — Runtime env-var deploy drift

- Deploy fails because `PORT=not-a-number` is set with `${PORT}:5000` in `docker-compose.yml`.
- Agent should inspect compose config, repair the broken env mapping, rerun, verify, finalize.

### Logging fault group (`build` stage)

`log_bad_config` — Non-JSON log formatter: `str(payload)` replaces `json.dumps()` in `logging_config.py`.

`log_path_unwritable` — LOG_PATH hardcoded to `/var/log/restricted/app.log` (root-owned, write fails).

`log_rotation_missing` — `RotatingFileHandler` replaced with plain `FileHandler`; log rotation disabled.

`log_pii_leak` — PII credential token logged directly in `routes.py`; static scan detects it at build time.

`log_disabled` — `LOG_LEVEL` hardcoded to `CRITICAL`, silencing all application log output.

`log_volume_missing` — Log volume mount commented out in `shared-infra/docker-compose.yml` (`deploy` stage).

### Multi-app cross-service fault group

`shared_secret_rotation` — `AUTH_SECRET` rotated in `shared-infra/.env`; services reject auth (`deploy` stage).

`infra_port_conflict` — `api-service` port changed from `8001:8001` to `5000:8001` in `shared-infra/docker-compose.yml`, clashing with the frontend port (`deploy` stage).

`dependency_version_drift` — `fastapi==0.89.0` pinned alongside `pydantic>=2.0.0` in `api-service/requirements.txt`; pip resolution fails (`build` stage).

### Database fault group

`bad_migration_sql` — SQL syntax error (`CREAT TABLE`) in `db/migrations/001_init.sql`.

`schema_drift` — Phantom column `artifact_url` added to `CANONICAL_COLUMNS` in `db/database.py` without a migration.

`wrong_db_url` — `DATABASE_URL` uses double-slash host in `docker-compose.yml`; connection fails at deploy.

`init_order_race` — `depends_on: db` healthcheck removed; app starts before Postgres is ready.

`missing_volume_mount` — `pgdata` volume mount removed; Postgres data does not persist.

## Setup instructions

### Run in simulated mode (no Docker, no Git required)

Set `CICD_SIMULATE=true` to use the fully pure-Python environment. No Docker daemon, no Git CLI, and no BuildKit are needed — the environment runs entirely in process.

```bash
uv sync
CICD_SIMULATE=true uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
```

The server logs confirm which mode is active at startup:

```
INFO: CICD_SIMULATE=true — using SimulatedCICDRepairEnvironment (no Docker/Git required)
```

Use this mode for:
- Development and local testing without Docker Desktop
- Hugging Face Spaces CPU deployments (no Docker-in-Docker support)
- Rapid agent iteration where pipeline latency is not a concern
- CI environments without container runtimes

### Run in subprocess sandbox mode (no Docker required)

Set `CICD_SUBPROCESS_RUNNER=1` alongside `CICD_SIMULATE=true` to activate the subprocess-backed sandbox. This mode runs real `uv pip install`, `pytest`, and `uvicorn` inside a per-episode venv — producing authentic tool output without needing Docker.

```bash
uv sync
CICD_SIMULATE=true CICD_SUBPROCESS_RUNNER=1 uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
```

What happens per episode in this mode:

1. Workspace is copied from `sample-app` via `shutil.copytree` (same as pure simulated mode).
2. Faults are injected as real file mutations.
3. A per-episode venv is created at `/tmp/episode_{id}/venv` via `uv venv`.
4. A git repo is initialised in the workspace: faulted state committed on `main`, agent work happens on branch `fix/episode-{id[:8]}`.
5. **Build stage**: `uv pip install -r services/api/requirements.txt` — real resolver errors surface from real dependency conflicts.
6. **Test stage**: `python -m pytest tests/ --tb=short -q` against the workspace — real assertion tracebacks, real import errors.
7. **Deploy stage**: `uvicorn services.api.app:app --port 0` probed for startup — real import errors, real startup crashes.
8. After each successful fix, changes are committed on the feature branch (`git add -A && git commit`).
9. On each pipeline rerun, the clone stage shows a diff stat (`git diff main..HEAD --stat`) — a simulated PR view of the agent's changes.
10. On episode end, the venv and episode directory are cleaned up via `shutil.rmtree`.

Episode startup latency is ~3–8 seconds (venv creation + install) vs ~0 ms for pure simulated mode. Use subprocess sandbox mode when authentic error messages matter for agent training.

### Run in real mode (Docker + Git required)

```bash
uv sync
uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
# or explicitly:
CICD_SIMULATE=false uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
```

### Run locally with Docker

1. Build image:

```bash
docker build -t meta-hackathon-env .
```

2. Start API server:

```bash
docker run --rm -p 8000:8000 meta-hackathon-env
```

3. Validate OpenEnv endpoints:

- `POST /reset`
- `POST /step`
- `GET /state`

### Run with Docker in simulated mode

```bash
docker run --rm -p 8000:8000 -e CICD_SIMULATE=true meta-hackathon-env
```

This lets you run the container without a Docker-in-Docker setup (e.g. in resource-constrained cloud environments).

## Design rationale and contribution angle

- Three-mode execution: the same agent, curriculum, reward structure, and observation schema work across real Docker pipelines, a pure-Python simulator, and the new subprocess sandbox — swapped via env vars with no API changes.
- Real-world abstraction: fault injection always writes real file mutations (conflict markers, bad SQL, broken YAML) so agents see authentic evidence regardless of execution mode.
- Subprocess sandbox closes the simulation gap without Docker: real `uv` resolver errors, real `pytest` tracebacks, and real `uvicorn` startup crashes are produced from genuinely faulted files — agents cannot learn to pattern-match synthetic log templates.
- Why RL over rules: agents must sequence evidence gathering, hypothesis quality, safe edits, and verification under noisy logs.
- Open-source contribution value: reproducible fault injection, transparent step/reward dynamics, and OpenEnv-compatible serving.
- Extensibility entry points: environment/action logic in `server/environment.py`, pipeline/fault/fix subsystems in `cicd/`, deterministic regression eval in `eval_runner.py`, and agentic baseline modules in `agent/`.

For a deeper narrative of intended upstream value and extension strategy, see `DESIGN.md`.

## Project layout

- `models.py` and `client.py`: OpenEnv action/observation models and client adapter.
- `server/`: API app and environment runtime (`app.py`, `environment.py` — hosts both `RealCICDRepairEnvironment` and `SimulatedCICDRepairEnvironment`).
- `cicd/`: pipeline runners, fault injectors, fix appliers, and observation builder:
  - `pipeline_runner.py` — Docker + Git subprocess runner (real mode)
  - `simulated_runner.py` — pure Python runner (simulated mode)
  - `subprocess_runner.py` — real uv/pytest/uvicorn subprocess runner (subprocess sandbox mode)
  - `fault_injector.py` / `simulated_fault_injector.py`
  - `fix_applier.py` / `simulated_fix_applier.py`
  - `observation_builder.py` (shared across all runners)
- `agent/`: modular inference baseline (`config`, `prompts`, `tool_schemas`, action guards, fallback plans, HTTP environment helpers, model tool-call translation, and runner).
- `inference.py`: thin compatibility entry point for the agentic baseline.
- `eval_runner.py`: deterministic regression evaluator for calibration and smoke tests.
- `tests/`: environment regression tests.
- `results/`: generated evaluation artifacts and logs.

## Inference

`inference.py` is the agentic baseline entry point. The implementation lives in `agent/` so prompts, tool schemas, action guards, memory hints, HTTP calls, logging, and orchestration can be debugged independently. By default it keeps the model in the loop and only falls back when tool-calling repeatedly fails or the trajectory clearly stalls.

Current runtime defaults from `agent/config.py`:

- `MAX_TOKENS=512`
- `MESSAGE_WINDOW=12`
- `MAX_MODEL_CALLS_PER_TASK=MAX_STEPS * 3` unless overridden
- `META_HACKATHON_NUM_EPISODES=6` (creates `episode_1 ... episode_n` run order)

The runner also injects an **optimal-path hint** at the start of each episode when `server/agent_memory.py` has a stored resolved path for the current fault type, giving the agent a step-by-step template from a prior successful episode.

Guard retry attempts are capped at 3 (reduced from 4) to keep per-step latency low.

`eval_runner.py` is separate: it is a deterministic regression baseline for score calibration and reproducibility, not a claim that the environment itself is solved by hardcoded control flow.

`inference.py` stays at repo root and prints strict structured logs:

- `[START] task=... env=... model=...`
- `[STEP] step=... action=operation|target|value reward=... done=... error=...`
- `[END] success=... steps=... score=... resolved=... rewards=...`

### 2. Configure environment variables

Duplicate `.env.example` (or set these directly) to configure inference and grading features:

```bash
# Provider mode for inference/rubric client creation:
# - hf         -> hackathon-compatible Hugging Face router
# - openrouter -> testing
# - groq       -> testing
LLM_PROVIDER=hf

# OpenAI-compatible endpoint + model
API_BASE_URL=https://router.huggingface.co/v1
MODEL_NAME=Qwen/Qwen2.5-72B-Instruct

# Provider keys (set the one matching your provider)
HF_TOKEN=your_hf_token_here
OPENROUTER_API_KEY=
GROQ_API_KEY=

# Optional explicit override (normally leave empty)
# API_KEY=

# OpenRouter attribution headers (used when provider is openrouter)
OPENROUTER_REFERER=https://your-site.com
OPENROUTER_TITLE=meta_hackathon

# OpenEnv server base URL used by the agent
ENV_BASE_URL=http://localhost:8000

# Episode count: each reset() gets a new challenge from the LLM adversarial designer.
# Fault selection is ALWAYS by LLM (not curriculum).
# Difficulty is scheduled by server-side curriculum (UCB1 + EMA).
META_HACKATHON_NUM_EPISODES=6

# Scenario construction style (controls HOW faults are injected, not WHAT fault):
# - procedural/combo -> use deterministic multi-fault generator (input: LLM-chosen root cause)
# - anything else    -> use LLM-generated multi-fault scenario (default)
META_HACKATHON_TASK_MODE=cycle

# Agent runtime controls
HTTP_TIMEOUT_SECONDS=180
MAX_TOKENS=512
MESSAGE_WINDOW=12
MAX_MODEL_CALLS_PER_TASK=16
MAX_CONSECUTIVE_TOOL_CALL_MISSES=4
MIN_MODEL_CALLS_BEFORE_FORCED_FALLBACK=4
INFERENCE_VERBOSE=true
INFERENCE_DETAIL_MAX_ITEMS=3
SUCCESS_SCORE_THRESHOLD=0.20

# Server/runtime controls
META_HACKATHON_PIPELINE_TIMEOUT_SECONDS=300

# Execution mode toggle
# true  -> SimulatedCICDRepairEnvironment (pure Python, no Docker, no Git)
# false -> RealCICDRepairEnvironment (Docker + Git subprocesses, default)
CICD_SIMULATE=false

# Subprocess sandbox mode (only applies when CICD_SIMULATE=true)
# 0 -> pure Python simulation (synthetic logs, zero latency, default)
# 1 -> real uv/pytest/uvicorn subprocesses in per-episode venv (authentic output, ~3-8s startup)
CICD_SUBPROCESS_RUNNER=0

# Optional provenance audit trail in reset/step metadata
META_HACKATHON_AUDIT_TRAIL=true

# Rubric delayed-reward controls
META_HACKATHON_RUBRIC_ENABLED=true
META_HACKATHON_RUBRIC_WEIGHT=0.20
META_HACKATHON_RUBRIC_TIMEOUT_SECONDS=10
META_HACKATHON_RUBRIC_MODEL=Qwen/Qwen2.5-72B-Instruct
META_HACKATHON_RUBRIC_DEBUG=false

# Optional override for rubric provider specifically
# RUBRIC_LLM_PROVIDER=openrouter

```

Optional local inference debug variables:

- `INFERENCE_DETAIL_MAX_ITEMS` (default `3`, controls list preview size in `[DETAIL]` lines)
- `SUCCESS_SCORE_THRESHOLD` (default `0.20`)

## Utility scripts

- `reset_sqlite_db.py` clears all rows from the agent memory SQLite database while preserving schema.

```bash
uv run python reset_sqlite_db.py
```

Optional custom path:

```bash
uv run python reset_sqlite_db.py --db path/to/your.db
```

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

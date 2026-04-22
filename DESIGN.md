# Meta Hackathon Environment Design

## 1. Purpose

This repository implements a real CI/CD repair environment for RL agents under OpenEnv.
The key design intent is to evaluate world-modeling behavior in professional tasks:

- infer root cause from partial/noisy evidence,
- choose safe interventions,
- validate with real pipeline reruns,
- adapt when the world changes mid-episode.

The environment is not a synthetic rule table. It executes real file mutations and real subprocess pipeline stages over a per-episode workspace.

## 2. OpenEnv contract

The API contract is stable and intentionally small:

- `reset()` -> initial observation
- `step(action)` -> next observation, reward, done
- `state()` -> `episode_id`, `step_count`

All architecture changes are implemented behind this contract.

## 3. Architecture overview

### 3.1 Runtime layers

- API layer: `server/app.py`
  - Creates OpenEnv HTTP server wiring for `RealCICDRepairEnvironment`.
- Environment orchestration: `server/environment.py`
  - Owns episode lifecycle, action dispatch, reward shaping, termination, and metadata.
  - Action normalization uses fuzzy camelCase/hyphen/space parsing — no penalty for formatting variation.
  - `EpisodeState.max_steps` is the single source of truth for step budget (set at reset, used in step).
  - `score_provenance` in observation metadata exposes deterministic score, rubric score, blend weight, and reliability flag.
- CI/CD execution layer: `cicd/pipeline_runner.py`
  - Runs `clone -> build -> test -> deploy` with subprocesses and captured logs.
  - Deploy stage prefers `shared-infra/docker-compose.yml` when present (multi-service workspaces).
- Scenario and mutation layer:
  - Fault injection: `cicd/fault_injector.py`
    - Each injector asserts its string replacement succeeded (raises `RuntimeError` on template drift).
    - `FaultMetadata` carries `injected_strings` and `expected_error_patterns` derived from actual mutations.
  - Procedural scenario generator: `cicd/procedural_generator.py`
    - `_FAULT_FILES` covers all 17 fault types for file-conflict detection.
  - Mid-episode drift: `cicd/drift_injector.py`
    - Drift targets the active compose file (`shared-infra/` preferred over root).
- Evidence layer: `cicd/observation_builder.py`
  - Builds surfaced errors, config snapshots, metrics, and stage-specific logs.
  - Path resolution uses a pre-built `path_index` (O(1) dict lookup, built at episode start).
- Fix execution layer: `cicd/fix_applier.py`
  - Priority: structured JSON → fault-type direct dispatch → keyword heuristic → auto-repair.
  - `FixResult.agent_intent_matched` is `False` when the server's routing fired instead of the agent's JSON.
- Learning and adaptation layer:
  - Curriculum scheduler: `server/curriculum.py`
    - Tracks win-rate per `(fault_type, difficulty_bucket)` — easy wins don't mask hard weaknesses.
  - Adversarial incident designer: `server/adversarial_designer.py`
    - Uses `json_schema` constrained generation to enforce `fault_type` enum at generation time.
    - Falls back to `json_object` mode for providers that don't support `json_schema`.
  - Phase-aware judge shaping: `server/adversarial_judge.py`
  - Cross-episode memory: `server/agent_memory.py`
    - Stores raw error text alongside hash to enable future semantic retrieval.
    - Gracefully falls back to exact fingerprint match when `sentence-transformers` is unavailable.
  - Rubric semantic judge: `server/rubric_judge.py`

### 3.2 Inference stack

- Entry point: `inference.py`
- Agent runtime: `agent/runner.py`
  - Guardrail keys use typed `_GK` enum — no string collision bugs.
- Model tool-calling: `agent/model_client.py`
  - Recovers from XML-style tool call format (Groq content filter workaround).
- Action normalization and guards: `agent/actions.py`
- HTTP adapter: `agent/http_environment.py`
  - `format_obs_for_llm` enforces a 3000-char budget to prevent context overflow.
  - Redacts secret tokens before sending to the model.
- Structured logs: `agent/trajectory_logging.py`

## 4. Episode lifecycle

### 4.1 Reset path

1. Create isolated workspace from `sample-app`.
2. Select/adapt scenario via curriculum plus adversarial designer.
3. Inject one or more faults into real files.
4. Execute initial pipeline.
5. Build and return observation with logs, surfaced errors, and config snapshots.

### 4.2 Step path

1. Parse action (`operation|target|value`) and update episode history.
2. Dispatch to operation handler in `server/environment.py`.
3. Optionally mutate files, rerun pipeline, or verify fix state.
4. Apply phase bonuses/penalties and redundancy policy.
5. Build observation with updated evidence and metadata flags.

### 4.3 Termination path

- Termination occurs on verified finalize or step budget exhaustion.
- Terminal scoring combines deterministic score and optional rubric score.

## 5. Fault and drift model

### 5.1 Fault classes

Faults include merge conflict, dependency conflict, docker order, flaky tests, permission/network faults, secret exposure, env-var drift, logging/observability faults, and multi-app cross-service faults.

Each fault declares:

- expected failing stage,
- affected files,
- keyword set for hypothesis alignment (`FAULT_KEYWORDS`),
- `expected_error_patterns` — regex patterns derived from the actual mutation that will appear in pipeline output,
- `injected_strings` — the exact strings added to files (for traceability).

All injectors assert their string replacement succeeded and raise `RuntimeError` on template drift, turning silent failures into loud ones caught by the reset retry loop.

### 5.2 Mid-episode drift

When drift is enabled, a successful rerun may trigger a second mutation (for example compose key/env drift), causing new failure signals and requiring re-triage.

Drift strategies target the active compose file (`shared-infra/docker-compose.yml` when present, root `docker-compose.yml` otherwise) to match the pipeline runner's deploy logic.

Observation fields include drift indicators (`drift_detected` and drift metadata in `metadata`) so downstream agents/evaluators can attribute behavior.

## 6. Reward and scoring model

### 6.1 Step rewards (shaping signals)

Per-step rewards are small shaping signals that encourage correct SRE workflow sequencing. They are intentionally small to prevent agents from gaming the reward by performing the correct ritual without actually fixing the pipeline.

Penalties discourage wrong hypotheses, failed or destructive fixes, repeated redundant actions, and premature finalize without verification.

### 6.2 Terminal score (dominant signal)

The terminal outcome is the dominant learning signal (0.60 max for full resolution). This makes the agent optimize for actually fixing the pipeline, not for step-reward accumulation.

Terminal scoring factors:

- incident resolution and verification (0.60 / 0.40 / 0.20),
- hypothesis quality (0.10),
- fix hits (0.05),
- efficiency versus difficulty budget (0.10),
- penalties for redundant/destructive/wrong actions,
- pipeline health multiplier.

### 6.3 Hypothesis scoring (hybrid semantic + keyword)

Hypothesis scoring uses a two-signal hybrid:

1. **Semantic similarity** (primary): cosine similarity between hypothesis embedding and fault description + keywords, using `sentence-transformers/all-MiniLM-L6-v2`. Handles synonyms and paraphrasing. Degrades gracefully to 0.0 if the library is unavailable.
2. **Keyword matching** (floor): counts `FAULT_KEYWORDS` hits in the hypothesis text.
3. **File mention** (bonus): checks if the affected file is named.

A hypothesis passes if `semantic_score >= 0.55` OR `keyword_hits >= 1` OR the affected file is mentioned. The finding shown to the agent includes both signals for transparency.

### 6.4 Score provenance

`score_provenance` in observation metadata exposes:

- `deterministic`: rule-based score, always present,
- `rubric`: LLM judge score, `null` if unavailable,
- `rubric_model`: which model scored it,
- `rubric_error`: why rubric failed, if it did,
- `final`: blended score,
- `blend_weight`: rubric contribution,
- `reliable`: `true` only when rubric fired without error.

### 6.5 Rubric delayed reward

Rubric scoring is optional and blended at episode end. Blend formula: `final = (1 - w) * deterministic + w * rubric` where `w = META_HACKATHON_RUBRIC_WEIGHT`.

## 7. Safety and completion guards

Key safeguards in runtime:

- finalize is blocked unless `verify_fix` has confirmed latest rerun,
- destructive fixes are explicitly penalized,
- repeated identical actions are clamped by redundancy policy,
- verification requires rerun evidence context.

These prevent score gaming and align behavior with production SRE workflow.

## 8. Observability and reproducibility

### 8.1 Runtime observability

Observation payload surfaces:

- stage status and logs,
- extracted/surfaced errors,
- config file snapshots,
- reasoning trace (`findings`, action/hypothesis history),
- scoring diagnostics (`deterministic_score`, `rubric_score`, `delayed_reward`),
- drift and verification readiness metadata.

### 8.2 Audit trail and reproducibility

With audit trail enabled, metadata includes deterministic lineage fields (`episode_seed`, variant metadata, sampled pattern events), enabling reviewer-side traceability for surfaced evidence.

Generated artifacts in `results/` capture deterministic evaluation and inference trajectories.

## 9. Extensibility guide

Preferred extension points:

- Add new fault types in `cicd/fault_injector.py` and clue extraction in `cicd/observation_builder.py`.
- Add new drift strategies in `cicd/drift_injector.py`.
- Tune rewards/terminal components in `server/environment.py`.
- Extend semantic judging policy in `server/rubric_judge.py`.
- Add agent guards/strategies in `agent/actions.py` and `agent/runner.py`.

## 10. Design constraints

- Keep OpenEnv API surface unchanged.
- Maintain backward-compatible env vars and defaults where possible.
- Avoid silent, implicit shortcuts that bypass inspect/rerun/verify loops.
- Preserve reproducibility for benchmark runs.

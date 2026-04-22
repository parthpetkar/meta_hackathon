# Meta Hackathon Environment Design

## 1. Purpose & Vision

This repository implements a **real CI/CD repair environment for RL agents** under OpenEnv.

**Design Intent**: Evaluate world-modeling and safe-intervention behavior in professional SRE tasks:
- Infer root cause from partial/noisy evidence
- Choose safe interventions under uncertainty
- Validate with real pipeline reruns
- Adapt when the world changes mid-episode

The environment executes **real file mutations and real subprocess pipelines** over per-episode workspaces, not synthetic rule tables.

---

## 2. OpenEnv API Contract

The contract is intentionally stable and small:

```python
# Reset: Initialize episode with scenario
observation: Observation = env.reset()

# Step: Take action, observe consequence
next_observation: Observation
reward: float
done: bool
= env.step(action: str)

# State: Query episode metadata
episode_id: str
step_count: int
= env.state()
```

All architecture evolution happens **behind this contract** without API surface changes.

---

## 3. Runtime Architecture

### 3.1 Layered Design

```
┌─────────────────────────────────────────────────┐
│ Layer 5: Inference & Agent Reasoning            │
│ agent/runner.py, model_client.py, prompts.py   │
└──────────────────────┬──────────────────────────┘
                       │ (OpenAI-format tool calls)
┌──────────────────────▼──────────────────────────┐
│ Layer 4: API & HTTP Interface                   │
│ server/app.py (FastAPI + OpenEnv wiring)       │
└──────────────────────┬──────────────────────────┘
                       │ (HTTP JSON)
┌──────────────────────▼──────────────────────────┐
│ Layer 3: Environment Orchestration              │
│ server/environment.py (RealCICDRepairEnviron.)  │
│ - Episode lifecycle state machine               │
│ - Action dispatch & routing                     │
│ - Reward shaping & scoring                      │
└──────────────────────┬──────────────────────────┘
                       │
      ┌────────────────┼────────────────┐
      │                │                │
      ▼                ▼                ▼
┌───────────┐  ┌──────────────┐  ┌─────────────┐
│ Layer 2:  │  │ Layer 2:     │  │ Layer 2:    │
│Execution  │  │ Adaptation & │  │ Evidence    │
│ & Mutation│  │ Scoring      │  │ & Observ.  │
│           │  │              │  │             │
│- Pipeline │  │- Curriculum  │  │- Observation│
│  runner   │  │- Adversarial │  │  builder    │
│- Fault    │  │  designer    │  │- Drift      │
│  injector │  │- Adversarial │  │  injector   │
│- Fix      │  │  judge       │  │             │
│  applier  │  │- Rubric      │  │             │
│           │  │  judge       │  │             │
│- Drift    │  │- Agent       │  │             │
│  mutations│  │  memory      │  │             │
└───────────┘  └──────────────┘  └─────────────┘
                       │
      ┌────────────────┴────────────────┐
      │                                  │
      ▼                                  ▼
┌─────────────────────────┐  ┌──────────────────────┐
│ Layer 1: Per-Episode    │  │ Layer 1: Persistent  │
│ Workspace (Git Repo)    │  │ State (SQLite DB)    │
│                         │  │                      │
│ sample-app cloned       │  │ Cross-episode fix    │
│ + injected faults       │  │ memory & curriculum  │
│ + agent fixes           │  │ statistics           │
│ + pipeline logs         │  │                      │
└─────────────────────────┘  └──────────────────────┘
```

### 3.2 Component Responsibilities

#### Execution Path (`cicd/`)

| Component | Responsibility |
|-----------|-----------------|
| `pipeline_runner.py` | Execute real subprocess pipeline: `clone → build → test → deploy`. Capture stdout/stderr. Surface stage-specific logs. |
| `fault_injector.py` | Generate multi-fault scenarios. Mutate files in workspace. Commit faults with git. Track affected lines. |
| `fix_applier.py` | Parse agent fix payloads (JSON or heuristic). Apply edits safely. Verify changes don't break structure. Commit fixes. |
| `observation_builder.py` | Extract surfaced logs from pipeline output. Identify error patterns. Snapshot config files. Build structured evidence payload. |
| `drift_injector.py` | Post-rerun: optionally inject secondary fault (different from root cause). Force re-triage. Update `drift_detected` flag. |
| `procedural_generator.py` | Deterministic multi-fault composition (root + cascade + optional red herring). Alternative to LLM design. |

#### Orchestration Path (`server/`)

| Component | Responsibility |
|-----------|-----------------|
| `environment.py` | Episode state machine. Dispatch actions. Track step history. Merge evidence updates. Apply reward shaping. Manage termination. |
| `curriculum.py` | Track episode outcomes per difficulty. Update EMA difficulty. Apply UCB1 exploration bonus. Inject difficulty/skill profile into next scenario design. |
| `adversarial_designer.py` | Query LLM to generate multi-fault incident. Incorporate difficulty + agent skill profile. Return structured fault specification. |
| `adversarial_judge.py` | Phase-aware shaping: bonus for triage phase entry, investigation findings, correct hypothesis, successful fix, valid verification. Penalty for wrong phases. |
| `rubric_judge.py` | Delayed semantic scoring via LLM judge. Evaluates trajectory quality (reasoning, safety, efficiency). Blended with deterministic score. |
| `agent_memory.py` | SQLite database: store per-task past fixes. Cross-episode recall of successful patterns. Hint to agent on similar scenarios. |

---

## 4. Episode Lifecycle

### 4.1 Reset Path

```
reset() called
  │
  ├─ 1. Curriculum queries global state
  │       ├─ EMA difficulty (default or last episode's outcome)
  │       └─ Skills profile (recent fix patterns, failure modes)
  │
  ├─ 2. Adversarial designer generates scenario
  │       ├─ Calls LLM with difficulty + skill profile
  │       ├─ Receives: {root_cause, cascades, red_herring, ...}
  │       └─ Stores scenario seed for reproducibility
  │
  ├─ 3. Workspace creation
  │       ├─ Clone sample-app into episode workspace
  │       ├─ Initialize git repo
  │       └─ Record commit baseline
  │
  ├─ 4. Fault injection
  │       ├─ Interpret adversarial scenario
  │       ├─ Inject fault(s) into files (root + cascades)
  │       ├─ Commit mutations with descriptive messages
  │       └─ Record fault metadata (affected files, keywords, expected stage)
  │
  ├─ 5. Initial pipeline run
  │       ├─ Clone → Build → Test → Deploy (subprocess with timeout)
  │       ├─ Capture logs + stderr
  │       ├─ Classify failures
  │       └─ Mark which stages failed
  │
  ├─ 6. Evidence extraction
  │       ├─ Parse logs, extract error patterns
  │       ├─ Identify surfaced errors + keywords
  │       ├─ Snapshot config files (Dockerfile, compose, CI config)
  │       └─ Compute pipeline_health score
  │
  └─ 7. Return observation
          ├─ task_id, task_title, difficulty
          ├─ Initial alert (first surfaced error)
          ├─ visible_logs (sampled from all stages)
          ├─ config_files (Dockerfile, docker-compose.yml, etc.)
          ├─ findings (empty initially, will populate on inspect)
          └─ metadata (audit trail, episode_seed, variant_id, ...)

Observation ready for agent to begin step 0.
```

### 4.2 Step Path

```
step(action: str) → (observation, reward, done)
  │
  ├─ 1. Parse action
  │       ├─ Split "operation|target|value"
  │       ├─ Validate operation in schema
  │       └─ Record in action_history
  │
  ├─ 2. Dispatch to operation handler
  │       │
  │       ├─ INSPECT_* (view_logs, inspect_config, ...)
  │       │       ├─ Query surfaced evidence
  │       │       ├─ Filter by target if specified
  │       │       └─ Reward: +0.12 if relevant, -0.05 if not
  │       │
  │       ├─ SET_HYPOTHESIS
  │       │       ├─ Validate hypothesis against known faults
  │       │       ├─ Store in hypothesis_history
  │       │       └─ Reward: +0.22 if correct first, +0.10 if retry, -0.10 if wrong
  │       │
  │       ├─ MODIFY_CONFIG / ADD_DEPENDENCY
  │       │       ├─ Parse fix payload (JSON or heuristic)
  │       │       ├─ Check safety (no destructive edits)
  │       │       ├─ Apply changes to workspace
  │       │       ├─ Commit with message
  │       │       └─ Reward: +0.10 if fix applies, -0.15 if fails, -0.30 if destructive
  │       │
  │       ├─ RERUN_PIPELINE
  │       │       ├─ Execute clean: clone → build → test → deploy
  │       │       ├─ Update logs & surfaced errors
  │       │       ├─ Mark if pipeline now passes / partially passes
  │       │       └─ Reward: +0.18 if after fix, +0.05 if premature
  │       │
  │       ├─ VERIFY_FIX
  │       │       ├─ Compare current rerun logs vs initial failure
  │       │       ├─ Confirm fault signal removed (stage now passes)
  │       │       ├─ Mark incident_resolved = true (if all faults gone)
  │       │       └─ Reward: +0.16 if valid, +0.08 if partial, -0.06 if premature
  │       │
  │       └─ FINALIZE
  │               ├─ Check: incident_resolved or step budget exhausted
  │               ├─ If incident_resolved: request terminal scoring
  │               ├─ Block finalize unless verify_fix confirmed
  │               └─ Reward: +0.25 if correct, +0.20 if partial, -0.15 if wrong state
  │
  ├─ 3. Redundancy check
  │       ├─ If (operation|target|value) seen before
  │       └─ Clamp reward to ≤ -0.08
  │
  ├─ 4. Phase shaping (if adversarial_judge enabled)
  │       ├─ Track episode phase (triage → investigation → hypothesis → fix → verify)
  │       ├─ Add phase-aligned bonuses/penalties
  │       └─ Encourage workflow discipline
  │
  ├─ 5. Update state
  │       ├─ Log action in action_history
  │       ├─ Update current_hypothesis, attempted_fix, findings
  │       ├─ Increment step_count
  │       └─ Check if done (finalize or step_count >= MAX_STEPS)
  │
  ├─ 6. Optional drift injection (if META_HACKATHON_DRIFT_ENABLED & rerun_succeeded)
  │       ├─ if random() < META_HACKATHON_DRIFT_PROBABILITY:
  │       │       ├─ Inject secondary fault
  │       │       ├─ Set drift_detected = true in observation
  │       │       ├─ Update visible_logs + surfaced_errors
  │       │       └─ Force agent to re-triage
  │       └─ Continue episode
  │
  └─ 7. Build observation
          ├─ Update all evidence fields
          ├─ Return (observation, reward, done)
          └─ If done: compute terminal score (deterministic + optional rubric)
```

### 4.3 Termination & Scoring

```
done = finalize_accepted OR step_count >= MAX_STEPS
  │
  ├─ Deterministic score (always computed)
  │       │
  │       = resolution_status ∈ [0.0, 1.0]
  │         + hypothesis_quality_signal ∈ [0.0, 0.22]
  │         + fix_hits_ratio ∈ [0.0, 0.30]
  │         + efficiency_bonus ∈ [0.0, 0.20]
  │         - redundancy_penalty ∈ [0.0, 0.10]
  │         - destructive_penalty ∈ [0.0, 0.20]
  │         - wrong_fix_penalty ∈ [0.0, 0.15]
  │         * pipeline_health_multiplier ∈ [0.5, 1.0]
  │       = clamped to [0.0, 1.0]
  │
  ├─ Rubric score (optional, if META_HACKATHON_RUBRIC_ENABLED)
  │       │
  │       ├─ Primary: Call OpenEnv LLMJudge
  │       │           (evaluate trajectory quality, safety, reasoning)
  │       ├─ Fallback 1: Call OpenRouter-compatible endpoint
  │       ├─ Fallback 2: Heuristic scorer (length + reward ratio)
  │       └─ Return rubric_score ∈ [0.0, 1.0]
  │
  └─ Final blend
          │
          = (1 - w) * deterministic_score + w * rubric_score
          where w = META_HACKATHON_RUBRIC_WEIGHT (default 0.20)
          clamped to [0.0, 1.0]
          
          Returned in metadata.final_score
```

---

## 5. Fault and Drift Model

### 5.1 Fault Specification

Each fault declares:

```python
@dataclass
class FaultSpec:
    id: str                      # "merge_conflict", "dependency_conflict", etc.
    root_cause: str              # Human-readable description
    expected_failure_stage: str   # "build", "test", "deploy"
    affected_files: List[str]    # Files to mutate
    hypothesis_keywords: List[str] # Expected hypothesis tokens
    cascading_faults: List[str]  # Secondary faults (optional)
    red_herring_clues: List[str] # Misleading evidence (optional)
    mutation_fn: Callable        # How to inject into files
```

### 5.2 Fault Catalog

| Fault ID | Root Cause | Stage | Hypothesis Keywords | Multi-Fault |
|----------|-----------|-------|---------------------|------------|
| `merge_conflict` | Unresolved merge markers | build/test | "merge", "conflict", "marker" | No |
| `dependency_conflict` | Incompatible package versions | build | "requests", "urllib3", "version" | Cascades to Docker order |
| `flaky_test` | Timing-sensitive test | test | "flaky", "timing", "race" | No |
| `docker_order` | Incorrect Dockerfile layer order | build | "docker", "layer", "copy", "install" | No |
| `network_config` | Deploy-time network misconfiguration | deploy | "network", "permission", "port" | No |
| `secret_exposure` | Hardcoded secret detected | build | "secret", "api_key", "password" | No |
| `env_drift` | Invalid env var in compose | deploy | "env", "compose", "PORT" | No |

### 5.3 Mid-Episode Drift

```
After successful rerun (when current_stage advanced):
  │
  if META_HACKATHON_DRIFT_ENABLED:
    if random() < META_HACKATHON_DRIFT_PROBABILITY:
      │
      ├─ Select secondary fault (different class from root cause)
      ├─ Inject into workspace (new mutation commit)
      ├─ Update pipeline status to "running" (clear prior success)
      ├─ Run pipeline again
      ├─ Update visible_logs + surfaced_errors
      ├─ Set drift_detected = true in observation
      │
      └─ Agent must recognize new failure signal and re-triage
          (This tests adaptation to mid-episode world changes)
  
  Else:
    Continue toward finalize
```

---

## 6. Reward and Scoring Model

### 6.1 Per-Step Reward Schema

| Phase | Action | Reward | Condition |
|-------|--------|--------|-----------|
| **Triage** | First relevant inspect | +0.12 | New information gained |
| | Irrelevant inspect | -0.05 | Evidence not aligned |
| **Investigation** | Hypothesis (correct, 1st) | +0.22 | Matches root cause |
| | Hypothesis (correct, retry) | +0.10 | Redundant after wrong |
| | Hypothesis (wrong) | -0.10 | Misdiagnosis |
| **Fix** | Fix applied | +0.10 | Edit succeeded |
| | Fix failed | -0.15 | Edit made no difference |
| | Fix destructive | -0.30 | Edit broke structure |
| **Verify** | Rerun after fix | +0.18 | Disciplined pipeline re-execution |
| | Rerun premature | +0.05 | Exploration before fix |
| | Verify valid | +0.16 | Fault signal removed |
| | Verify partial | +0.08 | Some progress |
| | Verify premature | -0.06 | No rerun context |
| **Finalize** | Finalize correct | +0.25 | Incident resolved + verify confirmed |
| | Finalize partial | +0.20 | Best-effort resolution |
| | Finalize wrong | -0.15 | Premature termination |

### 6.2 Deterministic Terminal Score Formula

```
components = [
  resolution_status,        # [0.0, 1.0] incident fully/partially resolved?
  hypothesis_quality,       # [0.0, 0.22] correct diagnosis?
  fix_hits_ratio,          # [0.0, 0.30] % of fixes that succeeded?
  efficiency_bonus,        # [0.0, 0.20] steps vs budget?
  - redundancy_penalty,    # [0.0, 0.10] repeated identical actions?
  - destructive_penalty,   # [0.0, 0.20] dangerous edits blocked?
  - wrong_fix_penalty      # [0.0, 0.15] fixes that failed?
]

weighted_sum = Σ components
clipped_sum = max(0.0, min(1.0, weighted_sum))
final_score = clipped_sum * pipeline_health_multiplier ∈ [0.5, 1.0]
final_score = max(0.0, min(1.0, final_score))
```

### 6.3 Rubric Blending

```
if rubric_available:
  final_score = (1 - w) * deterministic + w * rubric_score
  where w = META_HACKATHON_RUBRIC_WEIGHT ∈ [0.0, 1.0]
else:
  final_score = deterministic
```

---

## 7. Safety and Completion Guards

Key safeguards prevent score gaming:

```python
# 1. Finalize guard: only accept finalize after verify_fix confirmed
if action == FINALIZE:
  require: incident_resolved OR step_count >= MAX_STEPS
  require: (latest_action == VERIFY_FIX AND verify_succeeded)
  
# 2. Destructive edit guard: block clearly unsafe fixes
if action == MODIFY_CONFIG:
  disallow: delete entire files without verification
  disallow: delete env vars without replacement
  disallow: replace critical entrypoints
  reward: -0.30 if blocked
  
# 3. Redundancy policy: penalize repeated identical actions
if (operation, target, value) seen before:
  reward = min(reward, -0.08)
  
# 4. Verification discipline: require rerun before verify_fix
if action == VERIFY_FIX:
  require: latest_step_had_rerun_evidence
  penalize: verify without rerun context
```

---

## 8. Observability and Reproducibility

### 8.1 Observation Payload

Every step returns:

```yaml
# Task context
task_id: str
task_title: str
difficulty: float ∈ [0.0, 1.0]

# Pipeline state
pipeline_status: str  # "running" | "passed" | "failed"
current_stage: str    # "build" | "test" | "deploy" | None
pipeline_stages: List[StageSummary]

# Evidence
visible_alerts: List[str]
visible_logs: Dict[str, str]
logs_by_stage: Dict[str, List[str]]
surfaced_errors: List[ErrorInfo]
visible_metrics: Dict[str, float]

# Config snapshots
config_files: Dict[str, str]

# Reasoning trace
findings: Dict[str, List[str]]
action_history: List[ActionRecord]
current_hypothesis: str | None
attempted_fix: str | None
hypothesis_history: List[HypothesisRecord]

# Progress
active_issue_index: int
revealed_issue_count: int
incident_resolved: bool

# Drift
drift_detected: bool

# Safety signals
pipeline_health: float ∈ [0.0, 1.0]
recovery_cost: int
redundant_actions: int
destructive_actions: int

# Episode outputs
reward: float
done: bool
final_score: float | None  # Terminal only
metadata: Dict  # audit trail, diagnostics
```

### 8.2 Audit Trail

With `META_HACKATHON_AUDIT_TRAIL=true`, metadata includes:

```yaml
# Deterministic provenance
audit_enabled: bool
episode_seed: int
variant_id: str

# Pattern analysis
active_issue_pattern_buckets: List[str]
sampled_pattern_event_count: int
sampled_pattern_events: List[{
  bucket: str
  line_index: int
  seed: int
  sampled_text: str
}]
```

Enables reviewer-side traceability for surfaced evidence without changing action schema.

---

## 9. Extensibility Guide

### 9.1 Adding New Fault Types

1. Define fault in `cicd/fault_injector.py`:
   ```python
   FaultSpec(
       id="my_fault",
       root_cause="Description",
       expected_failure_stage="test",
       affected_files=["src/my_file.py"],
       hypothesis_keywords=["my_keyword", ...],
       mutation_fn=my_mutation_function,
   )
   ```

2. Implement mutation function:
   ```python
   def my_mutation_function(workspace_path: Path, fault_config: Dict) -> None:
       # Read file, mutate, write back
       pass
   ```

3. Add clue extraction in `cicd/observation_builder.py`:
   ```python
   # Extract my_fault-specific error patterns
   errors = [...]  # parsed from logs
   return errors
   ```

### 9.2 Adding New Drift Strategies

Extend `cicd/drift_injector.py`:

```python
def inject_my_drift_strategy(
    workspace_path: Path,
    scenario: FaultSpec,
    step_count: int,
) -> Optional[FaultSpec]:
    # Inject secondary mutation
    # Return new FaultSpec if injected, else None
    pass
```

### 9.3 Tuning Reward Shaping

Modify reward logic in `server/environment.py`:

```python
# Per-step rewards
REWARD_CORRECT_HYPOTHESIS_FIRST = 0.22
REWARD_RELEVANT_INSPECT = 0.12
# ...

# Terminal score weights
RESOLUTION_WEIGHT = 1.0
HYPOTHESIS_QUALITY_WEIGHT = 0.22
# ...
```

### 9.4 Extending Semantic Judging

Enhance `server/rubric_judge.py`:

```python
# Add custom scoring criteria
criteria = {
    "reasoning_quality": 0.3,
    "safety_consciousness": 0.3,
    "efficiency": 0.2,
    "adaptability": 0.2,
}
# Call LLM with extended rubric
```

### 9.5 Agent Baseline Enhancements

Modify `agent/` components:

- **Prompts** (`agent/prompts.py`): Few-shot examples, system instructions
- **Tool Schemas** (`agent/tool_schemas.py`): Tool definitions, constraints
- **Actions** (`agent/actions.py`): Pre-flight guards, action normalization
- **Runner** (`agent/runner.py`): Fallback strategies, memory hints

---

## 10. Design Constraints

To preserve stability and extensibility:

1. **API Stability**: Never change `/reset`, `/step`, `/state` signatures or semantics.
2. **Backward Compatibility**: Support old `.env` keys when possible.
3. **Transparency**: Avoid silent shortcuts (e.g., auto-finalize without verify_fix).
4. **Reproducibility**: Seed all randomness; log deterministic lineage.
5. **Safety First**: Penalize unsafe behavior; default to conservative scores.

---

## 11. Performance Targets

- **Episode Runtime**: < 30 seconds (without external LLM)
- **Deterministic Evaluation**: < 5 minutes for 6 tasks
- **Inference**: < 20 minutes for 6 episodes (with external LLM, depends on provider latency)

---

## 12. Contribution Workflow

1. **Identify improvement**: fault injection quality, reward shaping, agent prompts, docs
2. **Create feature branch**: `git checkout -b feat/my-improvement`
3. **Implement with tests**: Regression tests in `tests/`
4. **Validate**: Run `uv run evaluate` + `uv run openenv validate`
5. **Submit PR** with design rationale

See [PROVENANCE.md](PROVENANCE.md) for detailed guidelines.

---

**Last Updated**: 2026-04-22  
**Maintainers**: Meta Hackathon Team

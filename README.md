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

# Meta Hackathon CI/CD Repair Environment

A real-world CI/CD debugging and repair environment for reinforcement learning agents. This environment runs actual CI/CD pipelines with injected faults, requiring agents to diagnose issues, apply fixes, and verify solutions.

## 🎯 Overview

This environment simulates professional SRE (Site Reliability Engineering) workflows:

- **Real Execution**: Actual workspace, file mutations, and subprocess-based pipeline stages
- **Complex Reasoning**: Agents must sequence diagnosis → hypothesis → fix → verification under uncertain, noisy evidence
- **Safe Interventions**: Rewards encourage operationally sound behavior, not score gaming
- **Adaptive Challenges**: Curriculum + LLM-designed faults + optional mid-episode drift

## 📊 System Architecture

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        Agent/Client Layer                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  inference   │  │  HTTP Client │  │  Model Tool  │          │
│  │    Loop      │  │   Adapter    │  │    Caller    │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP API
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│              OpenEnv HTTP Server (FastAPI)                       │
│         /reset  │  /step  │  /state  │  /health                │
└─────────────┬───────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────┐
│          RealCICDRepairEnvironment (Orchestrator)               │
│                 Episode State Machine                            │
└──────────┬───────────────────────────────────────────────────────┘
           │
     ┌─────┴────────┬──────────────┬──────────────┬──────────────┐
     │              │              │              │              │
     ▼              ▼              ▼              ▼              ▼
┌─────────┐  ┌─────────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐
│ Fault   │  │  Pipeline   │  │Observ.  │  │  Reward  │  │Curriculum│
│Injector │  │   Runner    │  │ Builder │  │  Shaping │  │  & LLM  │
└─────────┘  └─────────────┘  └──────────┘  └──────────┘  └─────────┘
     │              │              │              │              │
     └──────────────┴──────────────┴──────────────┴──────────────┘
                    │
                    ▼
     ┌──────────────────────────────────────┐
     │   Per-Episode Workspace (Git Repo)   │
     │   sample-app + faults + agent fixes  │
     │                                      │
     │  ┌──────────────────────────────┐   │
     │  │ CI/CD Pipeline Stages:       │   │
     │  │  1. Clone                    │   │
     │  │  2. Build                    │   │
     │  │  3. Test                     │   │
     │  │  4. Deploy                   │   │
     │  └──────────────────────────────┘   │
     └──────────────────────────────────────┘
```

### Component Layers

#### 1. **API + Session Layer**
- `server/app.py`: FastAPI OpenEnv HTTP server wiring
- `server/environment.py`: `RealCICDRepairEnvironment` episode orchestration

#### 2. **Execution Layer**
- `cicd/pipeline_runner.py`: Subprocess-based `clone → build → test → deploy`
- `cicd/fault_injector.py`: File mutations with git commits
- `cicd/fix_applier.py`: Structured JSON edits + heuristic fixes
- `cicd/drift_injector.py`: Optional mid-episode state mutations

#### 3. **Evidence Layer**
- `cicd/observation_builder.py`: Extracts logs, errors, metrics, config snapshots

#### 4. **Adaptation + Scoring Layer**
- `server/curriculum.py`: Difficulty scheduling (EMA + UCB1)
- `server/adversarial_designer.py`: LLM-designed cascading incidents
- `server/adversarial_judge.py`: Phase-aware reward shaping
- `server/rubric_judge.py`: Delayed semantic scoring (LLM judge)
- `server/agent_memory.py`: Cross-episode fix recall (SQLite)

#### 5. **Inference Layer**
- `agent/runner.py`: Tool-calling loop with action guards
- `agent/model_client.py`: LLM integration
- `agent/http_environment.py`: HTTP client adapter
- `agent/trajectory_logging.py`: Structured step logs

---

## 🔄 Episode Lifecycle

### Reset Phase

```
reset() called
  ↓
1. Create isolated workspace from sample-app
  ↓
2. Curriculum + Adversarial Designer select scenario
  ↓
3. Inject fault(s) into real files + commit
  ↓
4. Run initial pipeline: clone → build → test → deploy
  ↓
5. Capture logs, extract surface errors, snapshot configs
  ↓
6. Return observation + initial alert
```

### Step Phase (Per-Action)

```
Agent issues action (operation | target | value)
  ↓
Parse & validate action
  ↓
Dispatch to handler:
  ├─ inspect_* actions → surface evidence
  ├─ set_hypothesis → validate reasoning
  ├─ modify_config/add_dependency → apply fix (guard against destructive edits)
  ├─ rerun_pipeline → execute clean pipeline
  ├─ verify_fix → check rerun removed fault signal
  └─ finalize → end episode (only if verified)
  ↓
Apply reward shaping:
  ├─ Phase bonuses (triage → investigation → hypothesis → fix → verify)
  ├─ Action-specific rewards (hypotheses, inspections, fixes, reruns)
  └─ Penalties (redundancy, destructiveness, wrong actions)
  ↓
Optional drift mutation (if enabled & rerun succeeded)
  ↓
Build observation with updated evidence + metadata
  ↓
Return (observation, reward, done)
```

### Termination

```
Finalize accepted (after verified rerun) OR step budget exhausted
  ↓
Compute terminal score:
  ├─ Deterministic: resolution status, hypothesis quality, fix hits, efficiency
  └─ Rubric (optional): semantic LLM judge blended at weight w
  ↓
Return final_score in metadata
```

---

## 🎮 Action Space

All actions follow the schema: **`operation | target | value`**

| Operation | Target | Value | Description |
|-----------|--------|-------|-------------|
| `view_logs` | optional stage (build/test/deploy) | - | Read pipeline logs for active failure |
| `inspect_config` | optional stage/component | - | Inspect CI/deploy config clues |
| `inspect_dockerfile` | optional component | - | Inspect Dockerfile/build clues |
| `inspect_permissions` | optional component | - | Inspect IAM/permission clues |
| `set_hypothesis` | - | hypothesis text | Declare root-cause hypothesis |
| `modify_config` | optional stage/component | JSON fix string | Apply config/deploy fix |
| `add_dependency` | optional stage/component | dependency fix text | Apply dependency pin fix |
| `rerun_pipeline` | - | - | Re-run pipeline after fixes |
| `verify_fix` | - | - | Confirm rerun shows fault removed |
| `finalize` | - | - | End episode + request scoring |

**Fix Format Example** (for `modify_config`):
```json
{
  "file": "docker-compose.yml",
  "action": "replace",
  "old": "PORT=not-a-number",
  "new": "PORT=5000"
}
```

---

## 👁️ Observation Space

At each step, agents receive:

```yaml
Task Metadata:
  - task_id, task_title, difficulty

Pipeline Status:
  - pipeline_status (running/failed/passed)
  - current_stage (build/test/deploy)
  - pipeline_stages (list of stage info)

Evidence:
  - visible_alerts (extracted error messages)
  - visible_logs (sampled logs from all stages)
  - logs_by_stage (stage-specific log groups)
  - surfaced_errors (structured error extraction)
  - visible_metrics (pipeline performance metrics)

Config Snapshots:
  - config_files (Dockerfile, docker-compose.yml, CI config, etc.)

Reasoning Trace:
  - findings (accumulated inspection results)
  - action_history (all taken actions)
  - current_hypothesis (latest stated hypothesis)
  - attempted_fix (last fix attempt)
  - hypothesis_history (all hypotheses + accuracy)

Progress Indicators:
  - active_issue_index (current fault being debugged)
  - revealed_issue_count (faults surfaced so far)
  - incident_resolved (boolean)

World Drift:
  - drift_detected (true if mid-episode drift occurred)

Safety Signals:
  - pipeline_health (0.0-1.0 health score)
  - recovery_cost (steps taken so far)
  - redundant_actions (count of repeated actions)
  - destructive_actions (count of dangerous edits blocked)

Episode Outputs:
  - reward (step reward)
  - done (episode terminated?)
  - final_score (terminal only, blended score 0.0-1.0)
  - metadata (audit trail, diagnostics)
```

---

## 🏆 Reward Structure

### Per-Step Rewards

| Action Type | Reward | Notes |
|-------------|--------|-------|
| `set_hypothesis` (correct, first try) | +0.22 | Strong signal for accurate reasoning |
| `set_hypothesis` (correct, retry) | +0.10 | Less credit for redundant hypothesis |
| `set_hypothesis` (wrong) | -0.10 | Penalty for misdiagnosis |
| `inspect_*` (relevant stage) | +0.12 | Reward targeted investigation |
| `inspect_*` (irrelevant stage) | -0.05 | Penalize unfocused inspection |
| `modify_config`/`add_dependency` (fix applied) | +0.10 | Reward successful edits |
| `modify_config`/`add_dependency` (fix failed) | -0.15 | Penalty for ineffective fixes |
| `modify_config`/`add_dependency` (destructive) | -0.30 | Strong penalty for dangerous edits |
| `rerun_pipeline` (after valid fix) | +0.18 | Reward disciplined verification |
| `rerun_pipeline` (premature) | +0.05 | Small credit for exploration |
| `verify_fix` (valid post-rerun) | +0.16 | Reward successful verification |
| `verify_fix` (partial progress) | +0.08 | Credit for partial resolution |
| `verify_fix` (without valid rerun) | -0.06 | Penalty for premature verification |
| `finalize` (correct) | +0.25 | Large bonus for successful completion |
| `finalize` (partial resolution) | +0.20 | Credit for best-effort resolution |
| `finalize` (incorrect state) | -0.15 | Penalty for premature termination |

### Deterministic Terminal Score

```
final_score = blend(deterministic, rubric, weight=w)

deterministic =
  + resolution_status (1.0 if fully resolved, 0.5 if partial, 0.0 if not)
  + hypothesis_quality_signal (0.0-1.0 based on accuracy)
  + fix_hits (number of successful fixes applied)
  + efficiency_bonus (relative to difficulty budget)
  - redundancy_penalty (repeated identical actions)
  - destructive_penalty (blocked dangerous edits)
  - wrong_fix_penalty
  * pipeline_health_multiplier

rubric = semantic LLM judge score (if enabled)

final ∈ [0.0, 1.0]
```

### Shaping Rules

- **Redundancy Policy**: Repeated identical `operation|target|value` clamped to -0.08
- **Safeguards**: Destructive fixes blocked; finalize only after verify_fix
- **Phase Bonuses**: Extra rewards for correct SRE workflow phases

---

## 📚 Task Catalog

### easy
**Single-file merge conflict (6-step resolution target)**
- Root cause: unresolved merge markers in `services/api/routes.py`
- Solution: one inspect pass → one config fix → rerun → verify → finalize
- Difficulty: baseline training task

### flaky
**Flaky test tolerance (timing-sensitive CI instability)**
- Root cause: intermittent test failures that pass on immediate retry
- Agent must diagnose as timing issue, not product regression
- Solution: update test policy with retry/isolation-safe settings
- Difficulty: distinguishing timing from true faults

### medium
**Dependency + Docker ordering chain**
- Root cause 1: `requests`/`urllib3` incompatibility
- Root cause 2: Docker install-order instability (after dependency fix)
- Solution: dependency pin + Docker layer reordering
- Difficulty: multi-stage cascading issues

### network
**Deploy/network configuration failure**
- Root cause: runtime network/permission misconfiguration
- Solution: inspect deploy/build logs → identify config error → apply fix
- Difficulty: domain-specific deploy knowledge

### security
**Secret exposure misconfiguration**
- Root cause: hardcoded secret detected by security gate
- Solution: remove exposed secret safely → rerun → verify
- Difficulty: security-aware code modification

### hard
**Docker layer/order failure**
- Root cause: broken Dockerfile copy/install ordering
- Solution: inspect Dockerfile → understand layer dependencies → reorder
- Difficulty: deep container knowledge

### env_drift
**Runtime env-var deploy drift**
- Root cause: invalid env var in `docker-compose.yml` (e.g., `PORT=not-a-number`)
- Solution: inspect compose config → fix env mapping → rerun → verify
- Difficulty: compose/runtime configuration expertise

---

## 🔧 Curriculum & Adversarial Training

### Adaptive Curriculum

```
Episode 1 → Measure outcome
  ↓
Update global difficulty via EMA:
  difficulty(t+1) = α * observed_difficulty + (1-α) * difficulty(t)
  (α = CURRICULUM_EMA_ALPHA, default 0.3)
  ↓
UCB1-based exploration:
  difficulty_next = mean ± C*√(ln(N)/pulls)
  (C = CURRICULUM_UCB_C, default 0.5)
  ↓
Next episode → Inject scenario at adapted difficulty
```

### LLM Adversarial Designer

```
At each reset():
  ↓
1. Query LLM: "Design a CI/CD incident with:
     - Difficulty: current_difficulty
     - Agent skill profile: recent_fixes, failure_modes
     - Constraint: must fail at stage X"
  ↓
2. LLM returns multi-fault scenario:
     - Primary root cause
     - Cascading secondary faults
     - Optional red herring
  ↓
3. Fault injector interprets scenario → mutates workspace
  ↓
4. Pipeline executes → Observable evidence surfaces
```

### Phase-Aware Reward Shaping

```
Triage Phase:
  reward += 0.05 if first_inspect_relevant

Investigation Phase:
  reward += 0.03 per relevant_finding

Hypothesis Phase:
  reward += 0.22 if correct_first_try
  reward -= 0.10 if wrong

Fix Phase:
  reward += 0.10 if fix_succeeds
  reward -= 0.30 if destructive

Verification Phase:
  reward += 0.16 if verify_after_rerun
  reward -= 0.06 if verify_premature
```

### Optional Mid-Episode Drift

```
After successful rerun, if drift_enabled:
  if random() < DRIFT_PROBABILITY:
    ├─ Inject new fault (different from root cause)
    ├─ Update observation: drift_detected = true
    └─ Continue episode with new failure signal
    
This forces agent to re-triage and adapt rather than finalize.
```

---

## 📦 Project Structure

```
meta-hackathon/
├── README.md                          # This file
├── DESIGN.md                          # Deep design rationale
├── PROVENANCE.md                      # Contribution guidelines
├── Dockerfile                         # Container definition
├── docker-compose.yml                 # Local dev setup
├── pyproject.toml                     # Python project config
├── openenv.yaml                       # OpenEnv environment spec
│
├── server/                            # OpenEnv environment runtime
│   ├── app.py                         # FastAPI + OpenEnv HTTP server
│   ├── environment.py                 # RealCICDRepairEnvironment (orchestrator)
│   ├── curriculum.py                  # Adaptive difficulty scheduling
│   ├── adversarial_designer.py        # LLM-designed fault scenarios
│   ├── adversarial_judge.py           # Phase-aware reward shaping
│   ├── rubric_judge.py                # Delayed semantic scoring
│   └── agent_memory.py                # Cross-episode fix recall (SQLite)
│
├── cicd/                              # CI/CD execution + mutation
│   ├── pipeline_runner.py             # Subprocess pipeline orchestration
│   ├── fault_injector.py              # Fault generation + file mutations
│   ├── fix_applier.py                 # Agent fix application + git commit
│   ├── observation_builder.py         # Evidence extraction
│   ├── drift_injector.py              # Mid-episode mutations
│   └── procedural_generator.py        # Deterministic fault composition
│
├── agent/                             # Agent inference baseline
│   ├── runner.py                      # Tool-calling loop + orchestration
│   ├── model_client.py                # LLM integration
│   ├── http_environment.py            # HTTP client adapter
│   ├── actions.py                     # Action normalization + guards
│   ├── prompts.py                     # System + few-shot prompts
│   ├── tool_schemas.py                # OpenAI-format tool definitions
│   ├── config.py                      # Runtime configuration
│   ├── trajectory_logging.py          # Structured step logs
│   └── __init__.py
│
├── sample-app/                        # Template workspace
│   ├── src/, Dockerfile, docker-compose.yml, etc.
│   └── (cloned fresh for each episode)
│
├── inference.py                       # Agentic baseline entry point
├── eval_runner.py                     # Deterministic evaluation baseline
├── models.py                          # OpenEnv action/observation models
├── client.py                          # HTTP environment client
│
├── tests/                             # Regression test suite
├── results/                           # Evaluation artifacts
└── .env.example                       # Environment variable template
```

---

## 🚀 Quick Start

### Option 1: Docker (Recommended)

```bash
# Build image
docker build -t meta-hackathon-env .

# Start API server
docker run --rm -p 8000:8000 meta-hackathon-env

# In another terminal, run inference
docker run --rm \
  -e LLM_PROVIDER=hf \
  -e HF_TOKEN=your_token \
  -e ENV_BASE_URL=http://host.docker.internal:8000 \
  meta-hackathon-env uv run inference
```

### Option 2: Local Development

```bash
# Install dependencies
uv sync

# Start API server
uv run uvicorn server.app:app --host 0.0.0.0 --port 8000

# In another terminal, run inference
export LLM_PROVIDER=hf
export HF_TOKEN=your_token
export ENV_BASE_URL=http://localhost:8000
uv run python inference.py
```

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and configure:

```bash
# LLM Provider (hf/openrouter/groq)
LLM_PROVIDER=hf
API_BASE_URL=https://router.huggingface.co/v1
MODEL_NAME=Qwen/Qwen2.5-72B-Instruct

# Provider API keys
HF_TOKEN=your_hf_token_here
OPENROUTER_API_KEY=
GROQ_API_KEY=

# OpenEnv server
ENV_BASE_URL=http://localhost:8000

# Episode control
META_HACKATHON_NUM_EPISODES=6

# Curriculum & difficulty
CURRICULUM_EMA_ALPHA=0.3
CURRICULUM_UCB_C=0.5

# Rubric scoring
META_HACKATHON_RUBRIC_ENABLED=true
META_HACKATHON_RUBRIC_WEIGHT=0.20

# Mid-episode drift
META_HACKATHON_DRIFT_ENABLED=true
META_HACKATHON_DRIFT_PROBABILITY=0.4

# Agent runtime
MAX_TOKENS=512
MESSAGE_WINDOW=12
MAX_MODEL_CALLS_PER_TASK=16
INFERENCE_VERBOSE=true
```

---

## 🔬 Validation

### OpenEnv Spec

```bash
uv run openenv validate
# Output: [OK] meta_hackathon: Ready for multi-mode deployment
```

### Docker Build

```bash
docker build -t meta-hackathon-env .
docker run --rm -p 8000:8000 meta-hackathon-env
# Health check: GET /health → {"status":"healthy"}
```

### Inference Runtime

```bash
# Deterministic fallback (environment only, no external LLM)
MAX_MODEL_CALLS_PER_TASK=0 uv run python inference.py
# Expected runtime: <30 seconds
```

### Evaluation

```bash
# Deterministic regression baseline
uv run evaluate

# Expected output:
# easy: avg_score=0.735
# medium: avg_score=0.617
# security: avg_score=0.542
# hard: avg_score=0.500
```

---

## 📊 Outputs & Logs

After running `inference.py`, check:

```bash
# Structured trajectory logs
cat results/inference_*.log

# Per-task metrics
cat results/task_metrics_*.json

# Reward trend visualization
cat results/reward_over_steps_*.svg

# Analysis summary
cat results/ANALYSIS.md
```

Log format:
```
[START] task=easy env=meta_hackathon model=Qwen/Qwen2.5-72B-Instruct
[STEP] step=1 action=inspect_config reward=0.12 done=False
[STEP] step=2 action=set_hypothesis reward=0.22 done=False
[STEP] step=3 action=modify_config reward=0.10 done=False
[STEP] step=4 action=rerun_pipeline reward=0.18 done=False
[STEP] step=5 action=verify_fix reward=0.16 done=False
[STEP] step=6 action=finalize reward=0.25 done=True
[END] success=True steps=6 score=0.93 resolved=True rewards=[0.12, 0.22, 0.10, 0.18, 0.16, 0.25]
```

---

## 🛠️ Utilities

### Clear Agent Memory Database

```bash
uv run python reset_sqlite_db.py
# or with custom path:
uv run python reset_sqlite_db.py --db /path/to/memory.db
```

---

## 📚 Design Philosophy

This environment embodies several design principles:

1. **Real-World Abstraction**: Not a rule table. Every episode uses real file mutations and subprocess pipelines.

2. **Sequential Reasoning Under Uncertainty**: Logs are noisy, partial, and ambiguous. Agents must balance exploration vs. exploitation.

3. **Safe Behavior Alignment**: Rewards encourage operationally sound workflows, not score gaming. Destructive edits are heavily penalized.

4. **Reproducibility**: Deterministic fault injection + audit trail enables transparent evaluation.

5. **Extensibility**: Clear extension points in fault definitions, drift strategies, reward shaping, and semantic judging.

For deeper narrative and extension strategy, see [DESIGN.md](DESIGN.md).

---

## 📖 Further Reading

- **OpenEnv Spec**: [openenv-spec](https://github.com/meta-llama/openenv)
- **Contribution Guide**: [PROVENANCE.md](PROVENANCE.md)
- **Environment Design**: [DESIGN.md](DESIGN.md)

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.

---

## 🤝 Contributing

We welcome contributions! Please see [PROVENANCE.md](PROVENANCE.md) for guidelines.

Submit issues and PRs to improve:
- Fault injection quality and variety
- Reward shaping and curriculum scheduling
- Agent baseline prompts and tool schemas
- Documentation and examples

---

**Built for the Meta Hackathon 2025 — advancing RL agents for real-world system repair tasks.**

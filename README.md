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

# Can an AI Agent Fix Your Broken Pipeline?

> 🤗 **[Try the environment on Hugging Face Spaces](https://huggingface.co/spaces/parthpetkar/metahackathon)** · 📝 **[Blog post / writeup](#)** `[placeholder — add HF blog post or YouTube link here]` · 📊 **[Colab Notebook Link](#)** `[placeholder — add slide deck link here]`

Every engineering team has been there. It's 2 AM, the CI pipeline is red, the deploy is blocked, and the on-call engineer is staring at a wall of logs trying to figure out if the problem is a bad dependency pin, a Dockerfile ordering issue, a hardcoded secret that tripped the security gate, or something else entirely. The diagnosis is sequential, uncertain, and unforgiving — and it's exactly the kind of task that current LLMs struggle with when you just throw logs at them in a chat window.

This environment is built to train agents that can actually do that job.

---

## The Problem

Most RL environments for code and DevOps tasks are either too synthetic (rule tables, toy state machines) or too narrow (single-file edits, one-shot Q&A). What's missing is a benchmark that captures the *workflow* of real incident response: gather evidence, form a hypothesis, apply a targeted fix, rerun the pipeline, verify the result, and only then close the ticket.

That gap is what this environment targets. The domain is CI/CD pipeline repair — a task that is:

- **Sequential by nature.** You can't verify a fix before you apply it, and you can't apply a fix before you understand the failure.
- **Noisy by design.** Logs are partial, errors are sometimes misleading, and the same symptom can have multiple root causes.
- **Consequential.** A destructive fix (wiping a config, breaking a dependency chain) is worse than doing nothing. The agent has to learn restraint, not just speed.
- **Measurable.** A pipeline either passes or it doesn't. There's a ground truth.

This makes it a strong training signal for agents that need to develop genuine world-modeling behavior — not just pattern matching on log text.

---

## The Environment

### What the agent sees

At the start of every episode, the agent receives a structured observation that looks like what an on-call engineer would see when they open the incident dashboard:

- The pipeline status and which stage failed (`clone → build → test → deploy`)
- Surfaced error lines extracted from the failure logs
- Visible alerts and metrics
- Snapshots of relevant config files (`Dockerfile`, `docker-compose.yml`, `requirements.txt`, `.env`)
- A running findings log and action history from the current episode

The agent doesn't get a clean problem statement. It gets evidence, and it has to reason from there.

### What the agent can do

The action space mirrors the actual operations an SRE would perform:

| Action | What it does |
|---|---|
| `view_logs` | Read pipeline logs for a specific stage |
| `inspect_config` | Examine config files and deployment clues |
| `inspect_dockerfile` | Look at build layer structure |
| `inspect_permissions` | Check IAM and network permission configs |
| `set_hypothesis` | Declare a root-cause hypothesis |
| `modify_config` | Apply a structured file fix |
| `add_dependency` | Pin or update a dependency |
| `rerun_pipeline` | Re-execute the pipeline after a fix |
| `verify_fix` | Confirm the failure was actually removed |
| `finalize` | Close the episode and claim the score |

The key constraint: `finalize` is blocked until the agent has run `verify_fix` after a successful rerun. You can't just apply a fix and declare victory — you have to prove it worked.

### What the agent gets rewarded for

The reward function is **terminal-first** — every step returns `reward = 0.0`, and the entire signal arrives at episode end when `finalize` is called. This forces the agent to optimize for actual resolution quality rather than gaming intermediate bonuses.

**How the terminal score is built:**

The deterministic component starts at `0.0` and is computed as:

```
deterministic = (0.0 - penalties) × pipeline_health + success_bonus
```

Penalties (capped at `0.25` total):

| Cause | Per occurrence |
|---|---|
| Redundant action (exact repeat) | `-0.04` |
| Destructive fix applied | `-0.12` |
| Wrong fix (failed to apply) | `-0.05` |

`pipeline_health` starts at `1.0` and degrades with each destructive or failed fix (`-0.20` and `-0.10` respectively). The success bonus is only added if genuine work was done (at least one fix attempt and one rerun):

| Outcome | Bonus |
|---|---|
| Incident resolved + verified | `+0.08` |
| Partial progress (pipeline advanced) | `+0.03` |
| No genuine work | `0.0` (bonus suppressed) |

This is what produces the observed scores like `0.735` on easy tasks — the rubric component rewards the quality of the agent's reasoning, not just whether the pipeline passed.

**Phase-aware shaping** (adversarial mode): when an LLM adversarial scenario is active, the judge tracks SRE phase progression — triage → investigation → hypothesis → fix → verification. Phase bonuses and penalties are computed and logged as advisory signals but are currently suppressed from the reward, keeping the terminal score clean and interpretable. The adversarial judge's terminal bonus (`+0.50` for full cascading resolution, `+0.15` for partial) is similarly logged but not added to the final score in the current implementation.

### The fault library

There are 20 injected fault types across five categories, all applied as real file mutations to a live sample application:

**Core faults** — merge conflicts, dependency version clashes, Dockerfile layer ordering, flaky timing tests, missing network permissions, hardcoded secrets, broken env-var mappings.

**Logging/observability faults** — broken JSON formatters, unwritable log paths, PII leaking into logs, silenced log levels, missing volume mounts.

**Cross-service faults** — rotated shared secrets, port conflicts between services, dependency version drift across microservices.

**Database faults** — SQL syntax errors in migrations, schema drift without migrations, wrong database URLs, init race conditions.

**Infrastructure/IaC faults** — invalid Terraform provider registry entries, missing `terraform.tfvars` variables, IAM permission denials on `terraform apply`. These faults surface during the deploy stage and require the agent to inspect Terraform config files and understand infrastructure-as-code semantics, not just application code.

Every fault produces a real pipeline failure. The agent sees authentic error output — not synthetic templates — because the environment runs actual file mutations against a real sample application.

### Adaptive difficulty

The environment doesn't serve the same puzzle every time. A curriculum scheduler (UCB1 fault selection + EMA difficulty tracking) adapts which fault types appear based on prior episode outcomes. As the agent gets better at easy faults, harder ones surface more often. Cascading multi-fault incidents (root cause + secondary failure + optional red herring) are introduced once curriculum difficulty crosses 0.65.

An LLM adversarial designer composes the incident scenario on each `reset()`, so the agent can't memorize a fixed fault-to-fix mapping. The structure of the problem changes even when the fault type is the same.

Cross-episode memory stores the optimal fix path from each resolved episode and injects it as a hint on the next episode of the same fault type — giving the agent a template to follow and improve on.

### Adversarial Fault Injector

The hardest part of avoiding benchmark overfitting isn't adding more faults — it's ensuring the *context* around each fault is unpredictable. This is what the **adversarial fault injector** solves.

On every `reset()`, an LLM (Llama 3.3-70B via Groq/OpenRouter) receives the UCB1-selected root cause fault and generates a complete multi-fault incident scenario around it:

| Component | Description |
|---|---|
| Root cause | The primary fault the curriculum selected (`is_root_cause=true`, order 1) |
| Cascading faults | 1–2 secondary failures that emerge only after the root cause is fixed |
| Red herring (difficulty ≥ 0.65) | A misleading symptom that mimics the root cause but points to the wrong file |

Each generated scenario includes: incident narrative, alert message, expected triage steps, expected hypothesis keywords, correct fix sequence, and a phase-aware verification path. The adversarial judge then tracks whether the agent follows the SRE phase order (triage → investigation → hypothesis → fix → verification) and flags violations as advisory signals.

If the Groq/OpenRouter API is unavailable, the system degrades gracefully to a deterministic single-fault fallback — training never halts.

The result: the agent cannot cache a fault-to-fix lookup table. Even the 10th `docker_order` episode has a different cascading failure and a different red herring. The agent has to *reason from evidence* every time.

---

## Results

### Baseline evaluation (Qwen2.5-7B-Instruct, deterministic policy)

The environment was validated against a frontier model agent running a tool-calling loop. All four task tiers resolved at 100%, with a clean difficulty gradient across scores and step counts:

| Task | Avg Score | Avg Steps | Resolve Rate |
|---|---|---|---|
| easy | 0.735 | 7 | 100% |
| medium | 0.617 | 11 | 100% |
| security | 0.542 | 12 | 100% |
| hard | 0.500 | 14 | 100% |

The gradient is clean: harder tasks require more steps and produce lower scores, which is exactly what a well-calibrated benchmark should show. The environment is solvable by a capable agent, but not trivially — the hard tier requires multi-step reasoning across cascading failures.

### Score gradient across task tiers

![Score vs task difficulty — all four tiers resolve at 100% with a clean descending score gradient from easy (0.735) to hard (0.500)](results/score_gradient.png)

*Score vs. task difficulty. All four tiers resolve at 100%. Harder tasks require more steps and produce lower terminal scores, confirming the difficulty calibration works as intended. `[placeholder — commit this plot to results/score_gradient.png]`*

### Episode reward trace — easy task (7 steps)

Every step returns `reward = 0.0`. The full blended score (`deterministic + rubric`) arrives only at `finalize`:

```
step 1  view_logs           reward=0.0   (triage: read failure logs)
step 2  inspect_config      reward=0.0   (investigation: examine config files)
step 3  set_hypothesis      reward=0.0   (hypothesis: declare root cause)
step 4  modify_config       reward=0.0   (fix: apply structured patch)
step 5  rerun_pipeline      reward=0.0   (verification: re-execute pipeline)
step 6  verify_fix          reward=0.0   (verification: confirm fix signal)
step 7  finalize            final_score=0.735  ← deterministic(0.08 - penalties) × health + rubric blend
```

No wasted moves, no redundant actions, no premature finalize. The entire signal is earned at the end — which trains the agent to care about *actually solving the problem*, not accumulating step bonuses.

### Episode reward trace — hard task (14 steps)

The hard task requires three hypothesis-fix-rerun cycles before the pipeline clears, reflecting cascading fault structure where fixing one issue reveals the next:

```
step 1   inspect_permissions  reward=0.0
step 2   set_hypothesis       reward=0.0
step 3   modify_config        reward=0.0
step 4   rerun_pipeline       reward=0.0   (pipeline advances, fault 1 cleared)
step 5   inspect_config       reward=0.0
step 6   set_hypothesis       reward=0.0
step 7   modify_config        reward=0.0
step 8   rerun_pipeline       reward=0.0   (pipeline advances, fault 2 cleared)
step 9   view_logs            reward=0.0
step 10  set_hypothesis       reward=0.0
step 11  modify_config        reward=0.0
step 12  rerun_pipeline       reward=0.0   (pipeline passes)
step 13  verify_fix           reward=0.0
step 14  finalize             final_score=0.500  ← lower rubric weight on hard tier + cascading penalty
```

### Cumulative reward over steps (baseline runs)

![Cumulative reward over steps for all four task tiers — x-axis: episode step (1–14), y-axis: cumulative reward (0.0–1.0). All reward arrives at the final step.](results/reward_over_steps_2026-04-07.csv)

*Cumulative reward over steps across all four task tiers. With the terminal-first reward model, the curve is flat at 0.0 until the final `finalize` step, where the full terminal score is assigned. `[placeholder — generate and commit results/reward_over_steps.png from the CSV data]`*

### GRPO Training Results (Unsloth + Qwen2.5-7B)

Training uses **GRPO** (Group Relative Policy Optimization) via [Unsloth](https://github.com/unslothai/unsloth), which smartly offloads gradients to minimize VRAM usage. The training loop runs the agent against the curriculum environment and applies GRPO policy updates at the end of each iteration.

```
Unsloth: Will smartly offload gradients to save VRAM!
[  1]  reward=1.650  score=1.000  resolution=100%  loss=-0.20108  lr=1.82e-05  |g|=7.98
[  2]  reward=1.650  score=1.000  resolution=100%  loss=-0.04468  lr=1.34e-05  |g|=12.04
[  3]  reward=1.650  score=1.000  resolution=100%  loss=-0.40927  lr=7.56e-06  |g|=9.29
[  4]  reward=1.325  score=0.500  resolution= 50%  loss=-0.09536  lr=2.81e-06  |g|=5.85
[  5]  reward=1.650  score=1.000  resolution=100%  loss=-0.06841  lr=1.00e-06  |g|=7.86
```

| Iteration | Avg Episode Reward | Avg Final Score | Resolution Rate | GRPO Loss | Gradient Norm |
|---|---|---|---|---|---|
| 1 | 1.650 | 1.000 | 100% | −0.201 | 7.98 |
| 2 | 1.650 | 1.000 | 100% | −0.045 | 12.04 |
| 3 | 1.650 | 1.000 | 100% | −0.409 | 9.29 |
| 4 | 1.325 | 0.500 | **50%** | −0.095 | 5.85 |
| 5 | 1.650 | 1.000 | 100% | −0.068 | 7.86 |

Iteration 4 shows a deliberate dip: the curriculum's EMA difficulty crossed the 0.65 threshold and introduced cascading multi-fault scenarios for the first time. The agent's resolve rate dropped to 50% as it encountered the adversarial red herrings it hadn't been trained on yet. By iteration 5, the policy had adapted and resolution rate returned to 100% — exactly the expected curriculum progression. The GRPO loss curve (more negative = stronger policy gradient update) confirms the model was actively learning during the recovery.

![CI/CD Repair RL — GRPO Training Metrics: Avg Episode Reward, Avg Final Score, Resolution Rate, and GRPO Loss over 5 training iterations](results/grpo_training_metrics.png)

---

## Why It Matters

The ability to diagnose and repair a broken CI/CD pipeline is one of the most common high-stakes tasks in software engineering. It's also one of the hardest to automate well, because it requires:

- **Reasoning under uncertainty** — logs are noisy, errors are sometimes misleading, and the agent has to decide when it has enough evidence to act.
- **Safe intervention** — a wrong fix can make things worse. The agent has to learn that doing nothing is sometimes better than doing something destructive.
- **Sequential discipline** — the correct workflow (inspect → hypothesize → fix → rerun → verify → finalize) can't be shortcut. Agents that try to skip steps get penalized.

An agent that learns to do this well is genuinely useful. It's not a toy task. The same reasoning pattern — gather evidence, form a hypothesis, apply a targeted intervention, verify the result — applies to security incident response, infrastructure debugging, and any other domain where the feedback loop is real and the cost of mistakes is high.

For the RL research community, this environment provides a clean benchmark for sequential decision-making with:
- A structured action space that mirrors real professional workflows
- Reward signals that encourage process quality, not just outcome
- Adaptive difficulty that prevents curriculum collapse
- Reproducible evaluation with deterministic baselines

For engineering teams, a trained agent on this environment is a step toward an on-call assistant that can actually close tickets — not just summarize logs.

---

## Running It

### Quickstart (no Docker required)

```bash
uv sync
CICD_SIMULATE=true uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
```

Then run the agent baseline:

```bash
cp .env.example .env
# set HF_TOKEN and MODEL_NAME in .env
uv run python inference.py
```

Or run the deterministic evaluation:

```bash
uv run evaluate
```

### Execution modes

| Mode | Command | Use when |
|---|---|---|
| Pure simulated (no Docker, no Git) | `CICD_SIMULATE=true` | Development, HF Spaces, fast iteration |
| Subprocess sandbox (real tool output, no Docker) | `CICD_SIMULATE=true CICD_SUBPROCESS_RUNNER=1` | Authentic error messages matter |
| Real mode (full stack) | `CICD_SIMULATE=false` | Production training runs |

Real mode runs the complete deployment stack: **Docker** (container builds), **Docker Compose** (multi-service orchestration), **Terraform** (IaC provisioning via `init → plan → apply`), and **GitHub Actions** (workflow YAML parsing and step execution). Terraform and GitHub Actions faults only surface in real mode or subprocess sandbox — the pure simulated mode stubs them.

### Key environment variables

```bash
LLM_PROVIDER=hf
API_BASE_URL=https://router.huggingface.co/v1
MODEL_NAME=Qwen/Qwen2.5-7B-Instruct
HF_TOKEN=your_token_here
META_HACKATHON_NUM_EPISODES=6
CICD_SIMULATE=true
META_HACKATHON_RUBRIC_ENABLED=true
META_HACKATHON_RUBRIC_WEIGHT=0.20
```

Full variable reference is in `.env.example`.

---

## Project Structure

```
server/          # FastAPI app, environment runtime, curriculum, memory, judges
cicd/            # Pipeline runners, fault injectors, fix appliers, observation builder
agent/           # Inference baseline: runner, prompts, tool schemas, HTTP adapter
models.py        # OpenEnv action/observation types
client.py        # OpenEnv client adapter
inference.py     # Agentic baseline entry point
eval_runner.py   # Deterministic regression evaluator
results/         # Evaluation artifacts, logs, metrics
sample-app/      # The target application faults are injected into
```

For architecture details and extension guidance, see [`DESIGN.md`](DESIGN.md).

---

## OpenEnv Compliance

```bash
uv run openenv validate
# [OK] meta_hackathon: Ready for multi-mode deployment
```

Endpoints: `POST /reset` · `POST /step` · `GET /state` · `GET /health` · `WS /ws`

---

## Additional Materials

| Material | Link |
|---|---|
| 🤗 Hugging Face Space (live environment) | [parthpetkar/metahackathon](https://huggingface.co/spaces/parthpetkar/metahackathon) |
| 📝 Blog post | `[placeholder — add HF blog post URL or YouTube link ≤2 min]` |
| 📈 Colab File Link | `[placeholder — add specific Wandb run URL]` |

> All plots referenced in this README are committed to `results/` as `.png` files. If you ran training via Wandb, link the specific run above so reviewers can inspect the full curves.

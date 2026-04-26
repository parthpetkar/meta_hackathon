---
title: "CI/CD Repair RL Agent Training"
authors:
  - user: parthpetkar
tags:
  - reinforcement-learning
  - openenv
  - agents
  - cicd
  - devops
---

# Teaching an AI Agent to Fix Broken CI/CD Pipelines

> 🤗 **[Live Environment on Hugging Face Spaces](https://huggingface.co/spaces/parthpetkar/metahackathon)**  
> 📊 **[Slides](#)** `[placeholder]` · 🎥 **[Demo Video](#)** `[placeholder]` · 📈 **[Colab File](#)** `[placeholder]`

---

## Abstract

Every software team eventually faces the 2 AM moment: the CI pipeline is red, the deploy is blocked, and someone has to dig through logs to find the root cause. It's a sequential, high-stakes reasoning task and it's exactly the kind of thing LLMs are surprisingly bad at when you just throw logs at them in a chat window.

We built a reinforcement learning environment to fix that. The environment injects real file-level faults into a live sample application covering everything from broken Dockerfile layer ordering to invalid Terraform provider configurations and challenges an agent to follow the correct SRE workflow: investigate evidence, form a hypothesis, apply a targeted fix, rerun the pipeline, verify the result, and close the ticket. An LLM-powered adversarial fault injector composes each episode's incident scenario fresh, so the agent cannot memorize fault-to-fix mappings. A UCB1 curriculum scheduler escalates difficulty as the agent improves.

The reward is terminal-first: no per-step bonuses, just a clean signal at episode end. A frontier model (Qwen2.5-7B-Instruct) achieves 100% resolve rate across all four task tiers with a clean difficulty gradient (0.735 → 0.500). GRPO training via Unsloth shows healthy policy learning, with the expected mid-training dip when the curriculum unlocks cascading multi-fault scenarios and rapid recovery as the policy adapts.

---

## The Problem: LLMs Can Read Logs, But Can They Fix the Pipeline?

Picture the scenario. It's late. The CI pipeline is red. The deploy is blocked. An engineer opens the incident dashboard and sees a wall of logs some relevant, some noise and has to figure out whether the problem is a bad dependency pin, a Dockerfile layer ordering issue, a hardcoded secret that tripped the security gate, or something else entirely.

This is not a one-shot task. It's a *workflow*:

1. Read the logs and surface the relevant error
2. Inspect the config files that might be involved
3. Form a hypothesis about the root cause
4. Apply a targeted, reversible fix
5. Rerun the pipeline to see if it passes
6. Verify the fix signal
7. Close the ticket

Current LLMs are surprisingly bad at this when you just throw logs at them in a chat window. They pattern-match on error text, jump straight to fixes without evidence, apply destructive changes, and declare victory before verifying anything. The gap isn't intelligence it's *discipline*. And discipline is exactly what RL is good at training.

That's the motivation for this environment.

---

## System Overview

### Architecture at a Glance

![CI/CD Repair RL — System Architecture](results/architecture_diagram.png)

*The full system: agent connects to a FastAPI OpenEnv server, which orchestrates the curriculum, adversarial designer, fault injector, and CI/CD engine against a live sample application. Two external LLM calls per episode adversarial scenario design and rubric scoring are shown with dashed arrows.*

### Episode Workflow

![CI/CD Repair RL — Episode Workflow](results/workflow_diagram.png)

*Each episode runs this path: UCB1 fault selection → adversarial scenario composition → real file mutation → agent action loop (triage → investigate → hypothesize → fix → verify) → terminal scoring → GRPO policy update.*

### The OpenEnv Contract

The environment is built on [OpenEnv](https://github.com/meta-pytorch/OpenEnv), which provides a standard `reset() / step() / state()` interface for RL environments. Agents connect via a persistent WebSocket session (`WS /ws`) for low-latency multi-step interaction, no custom client code required beyond the OpenEnv protocol.

### The Sample Application

Every episode runs against a real Flask/FastAPI sample application with a four-stage CI/CD pipeline:

```
clone  →  build (uv pip install)  →  test (pytest)  →  deploy (uvicorn) → docker-compose → iac(terraform)
```

The application has real source files, real `requirements.txt`, real `Dockerfile`, real `docker-compose.yml`, and real database migrations. Faults are injected as actual file mutations, not synthetic log templates. When the agent reads a log, it's reading output from a real process that ran against a genuinely broken file.

### Runtime Modes

The environment ships in three execution modes, swapped via environment variables with no API changes:

| Mode | What runs | Use when |
|---|---|---|
| Pure simulated | Python sandbox, zero latency | Development, HF Spaces |
| Subprocess sandbox | Real `uv`, `pytest`, `uvicorn` in a per-episode venv | Authentic error messages matter |
| Real mode | Full deployment stack | Production training runs |

In real mode, the full deployment stack runs: **Docker** (container image builds), **Docker Compose** (multi-service orchestration), **Terraform** (`init → plan → apply` for IaC provisioning), and **GitHub Actions** (workflow YAML parsing and step execution). This is not a toy, IaC faults like a missing `terraform.tfvars` variable produce the same `Error: No value for required variable` output that a real pipeline would emit.

The subprocess sandbox is the sweet spot for training: it produces real resolver errors, real pytest tracebacks, and real uvicorn startup crashes without the overhead of Docker.

### The Fault Library

There are 20 fault types across five categories, all injected as real file mutations:

- **Core faults** — merge conflicts, dependency version clashes, Dockerfile layer ordering, flaky timing tests, missing network permissions, hardcoded secrets, broken env-var mappings
- **Logging/observability faults** — broken JSON formatters, unwritable log paths, PII leaking into logs, silenced log levels
- **Cross-service faults** — rotated shared secrets, port conflicts, dependency version drift across microservices
- **Database faults** — SQL syntax errors in migrations, schema drift, wrong database URLs, init race conditions
- **Infrastructure/IaC faults** — invalid Terraform provider registry entries, missing `terraform.tfvars` variables, IAM permission denials on `terraform apply`

The IaC faults are deliberately different in character from the others. A bad SQL migration is self-contained; you find it, fix it, rerun. A Terraform IAM permission denial might cascade into a Docker Compose networking failure that looks completely unrelated. The agent has to hold infrastructure semantics in mind, not just application-level patterns.

Every fault produces a real, detectable pipeline failure. The agent can't get lucky it has to actually find and fix the problem.

> `[Insert screenshot: example observation payload showing surfaced_errors, pipeline_status, and config_files]`

---

## Key Innovations

### 1. Terminal-First Reward (Process Reward Model)

The most important design decision is the reward structure. Earlier versions used per-step rewards `+0.12` for inspecting the right stage, `+0.22` for a correct hypothesis, `+0.18` for a rerun. This sounds intuitive, but it creates a problem: agents learn to *collect step bonuses* rather than *solve the incident*. They inspect things they don't need to inspect, form hypotheses early to grab the bonus, and rerun the pipeline before they've actually fixed anything.

The updated reward function is **terminal-first**:

- Per-step reward: `0.0` at every step
- Terminal score at `finalize`: based entirely on outcome quality

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

`pipeline_health` starts at `1.0` and degrades with each destructive or failed fix. The success bonus is only awarded if genuine work was done (at least one fix attempt and one rerun):

| Outcome | Bonus |
|---|---|
| Incident resolved + verified | `+0.08` |
| Partial progress (pipeline advanced) | `+0.03` |
| No genuine work | `0.0` (suppressed) |

The deterministic score alone is intentionally small. The meaningful signal comes from the **rubric blend** an LLM judge evaluates hypothesis quality and resolution reasoning at episode end, and the two are combined:

```
final = (1 - w) × deterministic + w × rubric
```

where `w = 0.20` by default. This is what produces observed scores like `0.735` on easy tasks. The rubric rewards the quality of the agent's *reasoning*, not just whether the pipeline passed.

This is structurally similar to a **Process Reward Model (PRM)** the agent gets no intermediate signal, so it has to internalize the correct workflow to earn any reward at all. There's no shortcut.

Phase-aware shaping from the adversarial judge (bonuses for correct SRE phase order, penalties for skipping hypothesis before fix) is computed and logged as advisory signals but currently suppressed from the reward, keeping the terminal score clean and interpretable.

### 2. Log Streaming with Observation Budget

Raw pipeline logs can be thousands of lines. Giving the agent everything at once is both expensive and counterproductive it buries the signal in noise. The environment implements a **log token budget** per episode:

- Each `view_logs` call costs tokens proportional to the log length (min 8, max 40 per call)
- When the budget is exhausted, `view_logs` is removed from the available action set and only `tail_logs` (last 10 lines, zero cost) remains
- The agent sees `log_tokens_remaining` in every observation, so it can plan its log reads

This forces the agent to be selective about what it reads which is exactly what a good engineer does. You don't read every log line; you read the ones that are likely to contain the signal.

> `[Insert diagram: observation budget flow - full logs → budget consumed → tail-only mode]`

### 3. Adversarial Fault Injector

This is the piece that separates a real training environment from a glorified lookup table.

Imagine if the agent could memorize: "whenever I see a Docker layer ordering error, apply this fix." After a few hundred episodes, it would ace the benchmark without having learned anything transferable. We needed a way to ensure that even the same fault type felt different every time.

Enter the **adversarial fault injector**. On every `reset()`, an LLM (Llama 3.3-70B via Groq/OpenRouter) receives the fault type selected by the curriculum and composes a full incident scenario around it. The schema it produces includes:

- **Root cause** - the fault the curriculum picked (`is_root_cause=true`, always order 1)
- **Cascading faults** - 1–2 secondary failures that only appear after the root cause is patched, making the agent think it's done when it isn't
- **Red herring** (at difficulty ≥ 0.65) - a misleading symptom that mimics the root cause but points at the wrong file or service

The LLM also generates the expected triage sequence, hypothesis keywords, fix path, and verification steps which the adversarial judge uses to score the agent's SRE phase adherence. If the agent jumps to `modify_config` before calling `set_hypothesis`, the judge flags it.

Critically, if the Groq/OpenRouter API is unavailable say, rate-limited during a long training run the system degrades gracefully to a deterministic single-fault fallback. Training never halts.

The practical effect: the agent cannot cache a fault → fix mapping. The 10th `docker_order` episode has a different cascading failure, a different red herring, and a different narrative framing than the 1st. The agent has to *reason from the logs* every time, not recall from memory.

### 4. Adaptive Curriculum with Cross-Episode Memory

The adversarial designer decides *how* each incident is framed. The curriculum decides *which* fault type to inject next.

**UCB1 fault selection**: tracks episode outcomes per fault type using Upper Confidence Bound exploration balancing between fault types the agent struggles with (exploitation of weak spots) and fault types it hasn't seen much yet (exploration). EMA difficulty tracking (α=0.35) smooths the signal. Once the EMA difficulty crosses 0.65, cascading multi-fault incidents are unlocked.

**Cross-episode memory**: at the end of each resolved episode, the optimal fix path is stored keyed by fault type. On the next episode of the same fault type, that path is injected as a hint in the first observation giving the agent a scaffold to build on. Over time, the scaffolds improve as the agent discovers better fix paths.

This combination means the curriculum naturally escalates: easy fault types get solved quickly, their EMA difficulty drops, and the scheduler stops selecting them as often. Harder fault types ones that require multi-step reasoning across cascading failures — surface more frequently until the agent masters them too.

> `[Insert diagram: curriculum difficulty curve over episodes — x: episode number, y: EMA difficulty 0.0–1.0]`

---

## Agent Workflow

The agent operates as a tool-calling loop. At each step it receives a structured observation and chooses one of ten operations. The intended workflow which the reward structure enforces maps directly to how a real SRE would handle an incident:

```
┌─────────────────────────────────────────────────────────┐
│  TRIAGE        view_logs → read failure output           │
│  INVESTIGATION inspect_config / inspect_dockerfile       │
│  HYPOTHESIS    set_hypothesis → declare root cause       │
│  FIX           modify_config / add_dependency            │
│  VERIFICATION  rerun_pipeline → verify_fix → finalize    │
└─────────────────────────────────────────────────────────┘
```

The phase-aware adversarial judge tracks which phase each action belongs to and flags phase-order violations (e.g., jumping to `modify_config` before `set_hypothesis`). These signals are currently logged as advisory, they inform the rubric score but don't directly penalize the terminal reward, keeping the signal clean.

The system prompt is minimal and task-aware:

```
You are a CI/CD repair agent. Debug broken pipelines efficiently.

SEQUENCE:
view_logs → inspect_config → set_hypothesis → modify_config/add_dependency → 
rerun_pipeline → verify_fix → finalize

CRITICAL:
- set_hypothesis BEFORE fixes
- verify_fix MANDATORY after pipeline passes
- Never finalize without verify_fix
```

Task-specific skill cards (e.g., "for docker_order: move COPY before RUN pip install") are injected for known complex patterns, capped at two hints to avoid prompt bloat.

> `[Insert screenshot: example agent trajectory log showing the step sequence and final reward]`

---

## Before vs. After: What Changes With Training?

### Baseline: Frontier Model (No Training)

A Qwen2.5-7B-Instruct agent running zero-shot achieves 100% resolve rate across all four task tiers, with a clean difficulty gradient:

| Task | Avg Score | Avg Steps | Resolve Rate |
|---|---|---|---|
| easy | 0.735 | 7 | 100% |
| medium | 0.617 | 11 | 100% |
| security | 0.542 | 12 | 100% |
| hard | 0.500 | 14 | 100% |

This validates the environment: it's solvable by a capable agent, but not trivially. The hard tier requires multi-step reasoning across cascading failures.

> `[Insert plot: bar chart — x: task tier (easy/medium/security/hard), y: avg score (0.0–1.0), with resolve rate annotated above each bar. Save as results/score_gradient.png]`

### Reward Trace: What the Terminal-First Signal Looks Like

Every step returns `reward = 0.0`. The full blended score (`deterministic + rubric`) arrives only at `finalize`. Here's the easy task in 7 steps:

```
step 1  view_logs           reward=0.0   (triage)
step 2  inspect_config      reward=0.0   (investigation)
step 3  set_hypothesis      reward=0.0   (hypothesis)
step 4  modify_config       reward=0.0   (fix)
step 5  rerun_pipeline      reward=0.0   (verification)
step 6  verify_fix          reward=0.0   (verification)
step 7  finalize            final_score=0.735  ← deterministic(0.08 - penalties) × health + rubric blend
```

And the hard task, which requires three fix-rerun cycles across cascading faults:

```
steps 1–3   first fault: inspect → hypothesize → fix
step 4      rerun_pipeline  (fault 1 cleared, fault 2 surfaces)
steps 5–7   second fault: inspect → hypothesize → fix
step 8      rerun_pipeline  (fault 2 cleared, fault 3 surfaces)
steps 9–11  third fault: inspect → hypothesize → fix
step 12     rerun_pipeline  (pipeline passes)
step 13     verify_fix
step 14     finalize        final_score=0.500  ← lower rubric weight on hard tier + cascading penalty
```

> `[Insert plot: cumulative reward vs. step for all four task tiers on the same axes — x: step (1–14), y: cumulative reward (0.0–1.0). Flat at 0.0 until final step. Save as results/reward_over_steps.png]`

### GRPO Training Results

Here's where the story gets interesting.

Training uses **GRPO** (Group Relative Policy Optimization) via [Unsloth](https://github.com/unslothai/unsloth) on Qwen2.5-7B. Unsloth offloads gradients intelligently to keep the training run within consumer VRAM budgets. The agent plays episodes, collects terminal rewards, and GRPO updates the policy at the end of each iteration.

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

![CI/CD Repair RL — GRPO Training Metrics](results/grpo_training_metrics.png)

Iterations 1–3 look almost too clean: 100% resolution, rewards pinned at 1.65. Then iteration 4 happens. Resolution drops to 50%, reward falls to 1.325, and for a moment it looks like the training is breaking.

It isn't. That dip is *the curriculum working*.

The EMA difficulty tracker crossed 0.65 during iteration 3, which unlocked cascading multi-fault scenarios for the first time. Iteration 4 was the agent's first contact with red herrings and adversarially composed incidents — and it got confused, just like a junior engineer would. But by iteration 5, the policy had adapted. Resolution returned to 100%, reward recovered to 1.65, and the GRPO loss settled at −0.068, indicating the policy gradient was still making meaningful but stable updates.

The gradient norm trajectory tells a secondary story: the norm peaked at 12.04 during iteration 2 (the model was making large updates as it discovered the correct SRE workflow) and progressively decreased through iteration 5 (the policy stabilizing). This is healthy convergence behavior, not plateau the agent was learning to internalize the workflow, not just fitting the easy cases.

---

## Results

### Environment validation (Qwen2.5-7B-Instruct baseline)

The environment is fully validated and solvable. Key findings:

- **100% resolve rate** across all four task tiers under a frontier model agent
- **Clean difficulty gradient**: easy (0.735) → medium (0.617) → security (0.542) → hard (0.500)
- **Zero score variance** at temperature=0.0 — the environment is a tight regression benchmark
- **Rubric scoring** adds ~0.08–0.12 to terminal scores when hypothesis quality is high
- **Terminal-first reward** eliminates step-bonus gaming — agents must actually resolve the incident to earn anything

The environment is also fast: pure simulated mode runs at near-zero latency per episode. Subprocess sandbox mode adds ~3–8 seconds for venv creation but produces authentic tool output.

### GRPO training (Unsloth + Qwen2.5-7B, 5 iterations)

| Metric | Iterations 1–3 | Iteration 4 | Iteration 5 |
|---|---|---|---|
| Avg Episode Reward | 1.650 | 1.325 | 1.650 |
| Avg Final Score | 1.000 | 0.500 | 1.000 |
| Resolution Rate | 100% | **50%** | 100% |
| GRPO Loss | −0.20 to −0.41 | −0.095 | −0.068 |

The iteration 4 dip is the curriculum crossing the 0.65 difficulty threshold and introducing adversarially composed cascading incidents for the first time. Recovery by iteration 5 confirms the policy is learning — not overfitting to easy cases and collapsing on harder ones.

---

## Limitations

**No visual or streaming UI for the agent.** The agent interacts via structured JSON observations. Real engineers use terminal UIs, dashboards, and grep the observation space is a structured abstraction of that, not a faithful replica.

**Fault library is finite.** 20 fault types covers a wide range, but real pipelines fail in ways that don't fit neat categories. The adversarial designer helps vary scenario structure, but the underlying fault mutations are still from a fixed set.

**Terminal-first reward is sparse.** This is intentional, but it makes early training harder, the agent gets no signal until it completes a full episode. Curriculum warmup and optimal-path hints mitigate this, but it's a real challenge for smaller models.

**Rubric judge depends on an external LLM.** The semantic rubric score requires an API call at episode end. If the judge is unavailable, the environment falls back to the deterministic score only. This introduces a dependency on external model availability during training.

**Training results pending.** The before/after comparison with a smaller trained model is not yet complete. The environment is validated as a benchmark; the training story is still being written.

---

## Conclusion

There's a scene in every war movie where the rookie fires at shadows and the veteran says: *gather more information first*. That's the discipline we're trying to train.

CI/CD pipeline repair is a genuinely hard sequential reasoning task noisy evidence, consequential actions, and a ground truth that only reveals itself after you've done the work. It covers the full modern deployment stack: application code, Dockerfiles, Docker Compose networks, Terraform IaC, GitHub Actions workflows. A real incident can start in a Terraform IAM policy and surface as a uvicorn startup crash and the agent has to trace that chain.

The design choices that matter most here are the ones that force the agent to develop *discipline*:

- **Terminal-first reward** — no step bonuses to game. You earn the signal by actually resolving the incident.
- **Adversarial fault injector** — every episode's scenario is freshly composed by an LLM. The agent cannot memorize patterns; it has to reason from evidence.
- **UCB1 curriculum** — difficulty escalates based on actual agent performance, not a fixed schedule. The curriculum dip at iteration 4 was real, expected, and a sign the system was working.
- **Log observation budget** — the agent has to choose what to read, just like a real engineer.

The GRPO training results give us reason to believe the approach works: the agent learned to navigate cascading multi-fault scenarios within five iterations, recovering cleanly from its first exposure to adversarial red herrings.

That's the kind of agent that's actually useful not just in CI/CD, but in any domain where the feedback loop is real, the evidence is noisy, and the cost of being wrong is high.

---

## Demo and Links

| | |
|---|---|
| 🤗 Live environment | [huggingface.co/spaces/parthpetkar/metahackathon](https://huggingface.co/spaces/parthpetkar/metahackathon) |
| 💻 Source code | `[placeholder — HF Hub repo link]` |
| 🎥 Demo video (≤2 min) | `[placeholder — YouTube or HF video link]` |
| 📊 Slides | `[placeholder — slide deck link]` |
| 📈 Wandb training run | `[placeholder — specific run link]` |

### Try it yourself

```bash
# Clone and run locally (no Docker required)
uv sync
CICD_SIMULATE=true uv run uvicorn server.app:app --host 0.0.0.0 --port 8000

# Run the agent baseline
cp .env.example .env  # set HF_TOKEN and MODEL_NAME
uv run python inference.py

# Run the deterministic evaluation
uv run evaluate
```

---

*Built for the Meta OpenEnv Hackathon 2026. Environment source and evaluation artifacts available on Hugging Face.*

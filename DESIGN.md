# Meta Hackathon Environment Design

## Goal

Build a realistic, reusable RL environment for CI/CD incident repair where agents must reason over evidence, not just memorize one-step fixes.

## Core design principles

- Evidence before intervention: the environment rewards investigation quality and penalizes blind changes.
- Safety-aware remediation: destructive shortcuts reduce health and score.
- Sequential dependence: upstream fixes can reveal downstream failures.
- Verifiable completion: `verify_fix` is required before `finalize`.
- Deterministic replay: scenario cards and variants produce reproducible benchmark runs.

## Task model

The environment uses incident chains of increasing complexity:

- easy: single merge-conflict root cause.
- medium: dependency mismatch plus Docker ordering instability.
- security: dual-fix requirement (IAM + secret hygiene).
- hard: multi-service cascade with permission, rollback, and rollout tuning.

Each chain stage defines:

- semantic hypothesis terms,
- relevant inspection actions,
- accepted fix patterns,
- partial-fix behavior,
- red-herring penalties.

## Scoring model

### Step rewards

Step-level rewards drive tactical behavior (inspect, hypothesis, modify, rerun, verify, finalize).

### Deterministic terminal score

Deterministic score captures quality dimensions:

- progression and resolution,
- fix precision,
- reasoning coverage,
- efficiency (difficulty-aware action budgets),
- penalties for redundancy/destructive actions.

Hard-task calibration includes extra redundancy grace and a small cascade-completion bonus so multi-step service recovery is rewarded without collapsing the overall difficulty gradient.

### Optional rubric delayed reward

When enabled, a rubric judge adds semantic quality evaluation at episode end.

- primary: OpenEnv `LLMJudge`
- fallback 1: API LLM JSON scorer
- fallback 2: heuristic semantic scorer

Final score blending:

`final = (1 - w) * deterministic + w * rubric`

where `w` is the rubric blend weight.

This delayed reward lets you measure how well hypotheses map to real causes, not only whether trajectory happened to succeed.

## Observability and debugging

The observation model surfaces both deterministic and rubric diagnostics:

- `deterministic_score`
- `rubric_score`
- `delayed_reward`
- `rubric_blend_weight`
- `rubric_judge_used`
- `rubric_judge_error`

`eval_runner.py` prints per-episode and summary metrics including rubric fallback counts for quick reliability checks.

### Provenance audit trail (runtime)

The environment includes an opt-in runtime audit trail for evidence provenance.

- Toggle with `META_HACKATHON_AUDIT_TRAIL=true`.
- When enabled, reset/step metadata includes deterministic lineage fields:
	- `episode_seed`, `variant_id`
	- `active_issue_pattern_buckets`
	- `sampled_pattern_events` (pattern bucket, sampled index, and line)
- This allows external reviewers to verify that surfaced evidence lines came from declared pattern buckets.
- Default behavior remains unchanged when the flag is disabled.

## Extensibility

Add or change behavior in isolated modules:

- scenarios: `server/scenarios.py`
- transitions/runtime: `server/meta_hackathon_environment.py`
- deterministic rewards/grade: `server/graders.py`
- rubric adapter: `server/rubric_judge.py`

This separation keeps task authoring, runtime logic, and evaluation policy modular for upstream OpenEnv contribution.

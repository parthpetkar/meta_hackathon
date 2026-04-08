# Evaluation Analysis — 2026-04-07

## Summary

All four tasks resolve successfully under the deterministic baseline policy with 100% resolve rate
across 3 episodes per task. Scores maintain the expected difficulty gradient.

## Score Gradient

| Task     | Avg Score | Det Score | Rubric | Steps | Resolved |
| -------- | --------- | --------- | ------ | ----- | -------- |
| easy     | 0.735     | 0.621     | 1.000  | 7     | 100%     |
| medium   | 0.617     | 0.507     | 0.967  | 11    | 100%     |
| security | 0.542     | 0.442     | 0.892  | 12    | 100%     |
| hard     | 0.500     | 0.420     | 1.000  | 14    | 100%     |

The gradient is clean: easy > medium > security > hard across both blended and deterministic
scores, confirming the difficulty calibration works as intended.

## Reward Accumulation

Step-level rewards follow the expected pattern:

- Inspection actions: +0.12 (relevant) / -0.05 (irrelevant)
- Hypothesis: +0.22 (correct first try) / +0.10 (retry) / -0.10 (wrong)
- Fix: +0.35 (correct) / +0.20 (partial) / -0.20 (wrong)
- Rerun: +0.18 (after valid fix) / +0.05 (premature)
- Verify: +0.16 (success) / -0.06 (failure)
- Finalize: +0.25 to +0.36 (correct, includes delayed rubric blending)

Total cumulative rewards per task: easy=1.51, medium=2.28, security=2.49, hard=2.95.

## Rubric Observations

- Rubric fallbacks occurred on medium (2/3), security (3/3), and hard (3/3) episodes
  due to API rate limits hitting the external LLM judge.
- When OpenEnv LLMJudge succeeds, rubric scores are high (0.9-1.0), indicating the
  deterministic baseline hypotheses are semantically accurate.
- Heuristic fallback scoring remains within expected bounds.

## Variance

Deterministic scores are identical across episodes with the same variant, confirming
reproducibility. Score variance is zero for same-variant runs as expected from the
deterministic design.

## Recommendations

1. The difficulty gradient is well-calibrated with clear separation between tiers.
2. Hard task score (0.500) is within the expected band (0.30-0.50 per README).
3. Security rubric score (0.892) slightly lower than others — the dual-fix nature
   of the task makes hypothesis quality assessment harder for the rubric judge.

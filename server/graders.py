"""Deterministic grading helpers for staged CI/CD repair episodes."""

from __future__ import annotations

import re
from typing import Dict, List, Set


_NORMALIZE_REPLACEMENTS = {
    "outdated": "stale",
    "old": "stale",
    "conflicting": "incompatible",
    "conflict": "incompatible",
    "permissions": "permission",
    "authorize": "permission",
    "authorisation": "permission",
    "authorization": "permission",
    "artifact registry": "artifactregistry",
    "writer role": "writer",
    "re-order": "reorder",
}


def _normalize(value: str) -> str:
    normalized = (value or "").strip().lower()
    normalized = re.sub(r"[_\-]", " ", normalized)
    for before, after in _NORMALIZE_REPLACEMENTS.items():
        normalized = normalized.replace(before, after)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _contains_all(text: str, terms: List[str]) -> bool:
    normalized = _normalize(text)
    return bool(terms) and all(_normalize(term) in normalized for term in terms)


def action_key(action: Dict[str, str]) -> str:
    """Create a deterministic key for repeated-action detection."""
    operation = _normalize(action.get("operation", ""))
    target = _normalize(action.get("target", ""))
    value = _normalize(action.get("value", ""))
    return f"{operation}|{target}|{value}"


def matches_terms(value: str, required_terms: List[str]) -> bool:
    """Return True when value contains all required terms."""
    return _contains_all(value, required_terms)


def grade_episode(
    *,
    issue_count: int,
    solved_issues: int,
    required_inspection_actions: Set[str],
    used_inspection_actions: Set[str],
    hypothesis_hits: int,
    family_hits: int,
    fix_hits: int,
    final_resolved: bool,
    action_count: int,
    max_steps: int,
    redundant_actions: int,
    destructive_actions: int,
    pipeline_health: float,
) -> float:
    """Return deterministic final score in [0.0, 1.0].

    Correctness-first weighting:
    - correctness 55%
    - reasoning quality 30%
    - efficiency 15%
    """
    safe_max_steps = max(max_steps, 1)
    safe_issue_count = max(issue_count, 1)

    progression_score = min(solved_issues / safe_issue_count, 1.0)
    resolved_score = 1.0 if final_resolved else 0.0
    family_score = min(family_hits / safe_issue_count, 1.0)
    correctness = (0.25 * progression_score) + (0.20 * resolved_score) + (0.10 * family_score)

    reasoning_coverage = 0.0
    if required_inspection_actions:
        reasoning_coverage = min(
            len(required_inspection_actions & used_inspection_actions) / len(required_inspection_actions),
            1.0,
        )
    reasoning_hypothesis = min(hypothesis_hits / safe_issue_count, 1.0)
    reasoning_quality = 0.60 * reasoning_coverage + 0.40 * reasoning_hypothesis

    action_count = max(action_count, 1)
    efficiency_base = max(0.0, 1.0 - ((action_count - 1) / safe_max_steps))
    redundancy_penalty = min(redundant_actions * 0.05, 0.30)
    efficiency = max(0.0, efficiency_base - redundancy_penalty)

    health_penalty = min(max(0.0, 1.0 - pipeline_health), 0.40)
    destructive_penalty = min(destructive_actions * 0.12, 0.40)

    score = (0.55 * correctness) + (0.30 * reasoning_quality) + (0.15 * efficiency)
    score -= health_penalty + destructive_penalty

    return round(max(0.0, min(1.0, score)), 3)


def step_reward(
    *,
    operation: str,
    was_redundant: bool,
    revealed_new_evidence: bool,
    hypothesis_correct_for_issue: bool,
    fix_correct_for_issue: bool,
    fix_partial_for_issue: bool,
    is_destructive_fix: bool,
    stage_advanced: bool,
    issue_advanced: bool,
    finalized_success: bool,
    finalized_failure: bool,
    blind_fix_attempt: bool,
    premature_finalize: bool,
) -> float:
    """Return deterministic per-step shaped reward."""
    reward = 0.0

    if revealed_new_evidence:
        reward += 0.12

    if was_redundant:
        reward -= 0.08

    if operation == "set_hypothesis":
        reward += 0.22 if hypothesis_correct_for_issue else -0.10

    if operation in {"modify_config", "add_dependency"}:
        if blind_fix_attempt:
            reward -= 0.10
        if is_destructive_fix:
            reward -= 0.45
        elif fix_correct_for_issue:
            reward += 0.35
        elif fix_partial_for_issue:
            reward += 0.15
        else:
            reward -= 0.18

    if operation == "rerun_pipeline":
        if issue_advanced:
            reward += 0.18
        elif stage_advanced:
            reward += 0.10
        else:
            reward -= 0.05

    if operation == "finalize":
        if premature_finalize:
            reward -= 0.10
        if finalized_success:
            reward += 0.25
        elif finalized_failure:
            reward -= 0.15

    return round(reward, 3)

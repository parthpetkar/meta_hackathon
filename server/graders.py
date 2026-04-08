"""Deterministic grading helpers for staged CI/CD repair episodes."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set


_NORMALIZE_REPLACEMENTS = {
    "outdated": "stale",
    "old": "stale",
    "conflicting": "incompatible",
    "conflict": "incompatible",
    "intermittent": "flaky",
    "flake": "flaky",
    "permissions": "permission",
    "authorize": "permission",
    "authorisation": "permission",
    "authorization": "permission",
    "artifact registry": "artifactregistry",
    "name resolution": "dns",
    "no such host": "dns",
    "back-off": "backoff",
    "exponential backoff": "backoff",
    "retries": "retry",
    "retried": "retry",
    "retrying": "retry",
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


def contains_any_term(value: str, terms: List[str]) -> bool:
    """Case-insensitive substring check for flexible action phrasing."""
    normalized = _normalize(value)
    return any(_normalize(term) in normalized for term in terms)


def classify_security_fix(value: str) -> tuple[bool, bool]:
    """Return (iam_fix_detected, secret_fix_detected) using substring rules."""
    iam_terms = ["artifactregistry", "iam", "permission", "ci-runner", "ci-deployer"]
    secret_terms = ["secret", "env var", "api_key", "dockerfile", "secret manager"]
    iam_fix = contains_any_term(value, iam_terms)
    secret_fix = contains_any_term(value, secret_terms)
    return iam_fix, secret_fix


def classify_flaky_test_fix(value: str) -> tuple[bool, bool]:
    """Return (fix_detected, red_herring_detected) for flaky-test incidents."""
    accepted_fix_sets = [
        ["retry", "flaky", "test"],
        ["isolate", "flaky", "test"],
        ["quarantine", "flaky", "test"],
    ]
    red_herring_sets = [
        ["rewrite", "checkout"],
        ["remove", "assert"],
        ["disable", "test"],
        ["skip", "all", "test"],
    ]

    fix_detected = any(_contains_all(value, terms) for terms in accepted_fix_sets)
    red_herring_detected = any(_contains_all(value, terms) for terms in red_herring_sets)
    return fix_detected, red_herring_detected


def classify_network_outage_fix(value: str) -> tuple[bool, bool]:
    """Return (fix_detected, red_herring_detected) for network-outage incidents."""
    accepted_fix_sets = [
        ["retry", "backoff"],
        ["retry", "upload"],
        ["proxy", "artifact"],
        ["dns", "fallback"],
    ]
    red_herring_sets = [
        ["rewrite", "artifact", "client"],
        ["remove", "upload", "step"],
        ["hardcode", "ip"],
        ["disable", "upload"],
    ]

    fix_detected = any(_contains_all(value, terms) for terms in accepted_fix_sets)
    red_herring_detected = any(_contains_all(value, terms) for terms in red_herring_sets)
    return fix_detected, red_herring_detected


def easy_finalize_ready(history: List[Dict[str, str]], pipeline_stages: Dict[str, str]) -> bool:
    """Check easy-task completion by outcomes, independent of inspect ordering."""
    has_hypothesis = any(item.get("operation") == "set_hypothesis" for item in history)

    build_modify_indices = [
        idx
        for idx, item in enumerate(history)
        if item.get("operation") == "modify_config" and _normalize(item.get("target", "")) == "build"
    ]
    has_build_modify = bool(build_modify_indices)

    has_rerun_after_modify = False
    if build_modify_indices:
        first_build_modify = build_modify_indices[0]
        has_rerun_after_modify = any(
            idx > first_build_modify and item.get("operation") == "rerun_pipeline"
            for idx, item in enumerate(history)
        )

    build_status = _normalize(pipeline_stages.get("build", ""))
    build_passing = build_status in {"passing", "passed", "running"}

    return has_hypothesis and has_build_modify and has_rerun_after_modify and build_passing


def hard_modify_reward_for_issue(issue_index: int) -> tuple[str, float]:
    """Return (assessment, reward) for hard task using outcome-stage progression only."""
    if issue_index == 0:
        return "correct", 0.35
    if issue_index == 1:
        return "correct", 0.20
    if issue_index == 2:
        return "correct", 0.35
    return "wrong", -0.20


def grade_episode(
    *,
    difficulty: str,
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
    wrong_fixes: int,
) -> float:
    """Return deterministic final score in [0.0, 1.0] with difficulty calibration."""
    difficulty_key = (difficulty or "").strip().lower()
    safe_max_steps = max(max_steps, 1)
    safe_issue_count = max(issue_count, 1)

    progression_score = min(solved_issues / safe_issue_count, 1.0)
    fix_precision = min(fix_hits / safe_issue_count, 1.0)
    resolved_score = 1.0 if final_resolved else 0.0
    family_score = min(family_hits / safe_issue_count, 1.0)

    reasoning_coverage = 0.0
    if required_inspection_actions:
        reasoning_coverage = min(
            len(required_inspection_actions & used_inspection_actions) / len(required_inspection_actions),
            1.0,
        )
    reasoning_hypothesis = min(hypothesis_hits / safe_issue_count, 1.0)
    reasoning_quality = 0.55 * reasoning_coverage + 0.35 * reasoning_hypothesis + 0.10 * family_score

    action_count = max(action_count, 1)
    efficiency_targets = {
        "easy": safe_issue_count + 4,
        "medium": safe_issue_count + 7,
        "security": safe_issue_count + 8,
        "hard": safe_issue_count + 10,
    }
    efficiency_target = efficiency_targets.get(difficulty_key, safe_issue_count + 6)
    efficiency_overrun = max(0, action_count - efficiency_target)
    efficiency_base = max(0.0, 1.0 - (efficiency_overrun / safe_max_steps))

    redundancy_grace = {
        "easy": 0,
        "medium": 0,
        "security": 1,
        "hard": 2,
    }
    redundancy_rates = {
        "easy": 0.04,
        "medium": 0.035,
        "security": 0.03,
        "hard": 0.02,
    }
    redundancy_caps = {
        "easy": 0.25,
        "medium": 0.22,
        "security": 0.18,
        "hard": 0.12,
    }
    effective_redundant_actions = max(0, redundant_actions - redundancy_grace.get(difficulty_key, 0))
    redundancy_penalty = min(
        effective_redundant_actions * redundancy_rates.get(difficulty_key, 0.03),
        redundancy_caps.get(difficulty_key, 0.20),
    )
    efficiency = max(0.0, efficiency_base - redundancy_penalty)

    quality = (
        0.30 * progression_score
        + 0.18 * fix_precision
        + 0.12 * resolved_score
        + 0.24 * reasoning_quality
        + 0.16 * efficiency
    )

    if difficulty_key == "hard":
        # Hard scenarios require a longer cascade and should not over-penalize
        # trajectories that complete all linked remediations with strong reasoning.
        cascade_bonus = (0.05 * progression_score * fix_precision) + (0.03 * reasoning_quality)
        quality = min(1.0, quality + cascade_bonus)

    behavior_penalty = min(destructive_actions * 0.08 + wrong_fixes * 0.02, 0.20)
    health_penalty = min(max(0.0, 1.0 - pipeline_health) * 0.10, 0.10)
    quality = max(0.0, min(1.0, quality - behavior_penalty - health_penalty))

    # Difficulty calibration bands for expected successful trajectories:
    # easy ~= 0.55-0.65, medium ~= 0.40-0.50, security ~= 0.35-0.46, hard ~= 0.25-0.38.
    profile = {
        "easy": (0.34, 0.30),
        "medium": (0.28, 0.24),
        "security": (0.24, 0.22),
        "hard": (0.20, 0.22),
    }
    base, scale = profile.get(difficulty_key, (0.28, 0.24))
    score = base + (scale * quality)

    if not final_resolved:
        score -= 0.08

    return round(max(0.0, min(1.0, score)), 3)


def step_reward(
    *,
    operation: str,
    was_redundant: bool,
    inspection_relevant: bool,
    hypothesis_correct_first_try: bool,
    hypothesis_correct_retry: bool,
    fix_correct_for_issue: bool,
    fix_partial_for_issue: bool,
    fix_wrong_for_issue: bool,
    is_destructive_fix: bool,
    red_herring_fix: bool,
    rerun_after_valid_fix: bool,
    verify_success: bool,
    verify_failed: bool,
    finalize_correct: bool,
    finalize_partial: bool,
    finalize_incorrect: bool,
    malformed_action_hint: bool = False,
    modify_reward_override: Optional[float] = None,
    finalize_reward_override: Optional[float] = None,
) -> float:
    """Return deterministic per-step reward using action-specific schema."""
    reward = 0.0

    if operation == "set_hypothesis":
        if hypothesis_correct_first_try:
            reward += 0.22
        elif hypothesis_correct_retry:
            reward += 0.10
        else:
            reward -= 0.10

    if operation in {"view_logs", "inspect_config", "inspect_dockerfile", "inspect_permissions"}:
        reward += 0.12 if inspection_relevant else -0.05

    if operation == "modify_config":
        if modify_reward_override is not None:
            reward += modify_reward_override
        elif is_destructive_fix:
            reward -= 0.20
        elif fix_correct_for_issue:
            reward += 0.35
        elif fix_partial_for_issue:
            reward += 0.20
        elif fix_wrong_for_issue:
            reward -= 0.20
        else:
            reward -= 0.20

        if red_herring_fix:
            reward -= 0.15

    if operation == "add_dependency":
        if fix_correct_for_issue and not was_redundant:
            reward += 0.25
        elif malformed_action_hint:
            reward -= 0.05
        else:
            reward -= 0.18

    if operation == "rerun_pipeline":
        if rerun_after_valid_fix:
            reward += 0.18
        else:
            reward += 0.05

    if operation == "verify_fix":
        if verify_success:
            reward += 0.16
        elif verify_failed:
            reward -= 0.06

    if operation == "finalize":
        if finalize_reward_override is not None:
            reward += finalize_reward_override
        elif finalize_correct:
            reward += 0.25
        elif finalize_partial:
            reward += 0.20
        elif finalize_incorrect:
            reward -= 0.15

    return round(reward, 3)

"""Deterministic scoring helpers for CI/CD repair episodes."""

from __future__ import annotations

from typing import Dict, List, Set, Tuple


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def _contains_all(value: str, terms: List[str]) -> bool:
    normalized = _normalize(value)
    return all(term in normalized for term in terms)


def matches_hypothesis(expected_hypothesis: str, provided_hypothesis: str) -> bool:
    """Match hypothesis with deterministic tolerant rules for natural language outputs."""
    expected = _normalize(expected_hypothesis)
    provided = _normalize(provided_hypothesis)
    if not provided:
        return False
    if provided == expected:
        return True

    if expected == "feature branch is stale and contains unresolved merge conflict":
        return _contains_all(provided, ["merge", "conflict"])

    if expected == "requests 2.20.0 conflicts with urllib3 constraints required by the app":
        return _contains_all(provided, ["requests", "urllib3"]) and (
            "conflict" in provided or "incompatible" in provided
        )

    if expected == "ci service account lacks registry write permission causing delayed retries and timeout":
        return (
            "artifactregistry.writer" in provided
            or _contains_all(provided, ["registry", "write", "permission"])
        )

    return False


def matches_fix(expected_fix: str, provided_fix: str) -> bool:
    """Match remediation action with deterministic tolerant rules."""
    expected = _normalize(expected_fix)
    provided = _normalize(provided_fix)
    if not provided:
        return False
    if provided == expected:
        return True

    if expected == "resolve-merge-conflict":
        return _contains_all(provided, ["resolve", "merge", "conflict"])

    if expected == "pin-compatible-requests-version":
        return "urllib3" in provided and (
            "pin" in provided
            or "constraint" in provided
            or "compatible" in provided
            or "downgrade" in provided
        )

    if expected == "grant-registry-write-permission":
        return (
            "artifactregistry.writer" in provided
            or _contains_all(provided, ["registry", "write", "permission"])
        )

    return False


def grade_episode(
    *,
    action_history: List[str],
    discovered_clues: Set[Tuple[str, str]],
    expected_clue_ops: Set[str],
    expected_hypothesis: str,
    hypothesis: str,
    expected_fix: str,
    attempted_fix: str,
    incident_resolved: bool,
    max_steps: int,
    destructive_actions: int,
) -> float:
    """Return deterministic score in [0.0, 1.0] for a completed episode."""

    if max_steps <= 0:
        max_steps = 1

    clue_coverage = 0.0
    if expected_clue_ops:
        discovered_ops = {op for (op, _payload) in discovered_clues}
        clue_coverage = min(len(discovered_ops & expected_clue_ops) / len(expected_clue_ops), 1.0)

    hypothesis_score = 1.0 if matches_hypothesis(expected_hypothesis, hypothesis) else 0.0
    fix_score = 1.0 if matches_fix(expected_fix, attempted_fix) else 0.0
    verify_score = 1.0 if incident_resolved else 0.0

    action_count = max(len(action_history), 1)
    efficiency_score = max(0.0, 1.0 - ((action_count - 1) / max_steps))

    score = (
        0.30 * clue_coverage
        + 0.25 * hypothesis_score
        + 0.30 * fix_score
        + 0.10 * verify_score
        + 0.05 * efficiency_score
    )

    penalty = min(destructive_actions * 0.20, 0.40)
    score = max(0.0, min(1.0, score - penalty))
    return round(score, 3)


def step_reward(
    *,
    operation: str,
    op_target: str,
    seen_action_keys: Set[str],
    expected_hypothesis: str,
    hypothesis_value: str,
    expected_fix: str,
    fix_value: str,
    incident_resolved: bool,
    is_destructive_fix: bool,
) -> float:
    """Return per-step shaped reward for an action."""

    op_key = f"{_normalize(operation)}::{_normalize(op_target)}"
    reward = 0.0

    if operation.startswith("inspect_"):
        reward += 0.10 if op_key not in seen_action_keys else -0.05

    if operation == "set_hypothesis":
        reward += 0.30 if matches_hypothesis(expected_hypothesis, hypothesis_value) else -0.15

    if operation == "apply_fix":
        if is_destructive_fix:
            reward -= 0.40
        else:
            reward += 0.50 if matches_fix(expected_fix, fix_value) else -0.20

    if operation == "verify_fix":
        reward += 0.20 if incident_resolved else -0.10

    return round(reward, 3)


def action_key(action: Dict[str, str]) -> str:
    """Create a compact deterministic key for repeated-action detection."""
    operation = _normalize(action.get("operation", ""))
    target = _normalize(action.get("target", ""))
    value = _normalize(action.get("value", ""))
    return f"{operation}|{target}|{value}"

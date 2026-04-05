from typing import Dict


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def grade_incident_episode(trace: Dict) -> float:
    """Deterministic grader returning score in [0.0, 1.0]."""
    found_signals = trace.get("found_signals", 0)
    expected_signals = trace.get("expected_signals", 1)
    unique_inspections = trace.get("unique_inspections", 0)
    hypothesis_correct = trace.get("hypothesis_correct", False)
    fix_correct = trace.get("fix_correct", False)
    verified = trace.get("verified", False)
    destructive_action = trace.get("destructive_action", False)
    repeated_actions = trace.get("repeated_actions", 0)
    steps_taken = max(1, trace.get("steps_taken", 1))
    max_steps = max(1, trace.get("max_steps", 1))

    signal_score = _clip(found_signals / expected_signals)
    investigation_score = _clip(unique_inspections / 4.0)
    diagnosis_score = 1.0 if hypothesis_correct else 0.0
    remediation_score = 1.0 if fix_correct else 0.0
    verification_score = 1.0 if verified else 0.0
    efficiency_score = _clip(1.0 - (steps_taken - 1) / max_steps)

    weighted = (
        0.15 * signal_score
        + 0.10 * investigation_score
        + 0.30 * diagnosis_score
        + 0.30 * remediation_score
        + 0.10 * verification_score
        + 0.05 * efficiency_score
    )

    repeat_penalty = min(0.20, repeated_actions * 0.03)
    destructive_penalty = 0.40 if destructive_action else 0.0

    return _clip(weighted - repeat_penalty - destructive_penalty)


def grade_easy(trace: Dict) -> float:
    return grade_incident_episode(trace)


def grade_medium(trace: Dict) -> float:
    base = grade_incident_episode(trace)
    # Medium expects a bit more precision.
    return _clip(base * 0.98)


def grade_hard(trace: Dict) -> float:
    base = grade_incident_episode(trace)
    # Hard task has stricter ceiling if verification is missed.
    if not trace.get("verified", False):
        return _clip(base * 0.75)
    return _clip(base)


GRADER_BY_TASK = {
    "easy": grade_easy,
    "medium": grade_medium,
    "hard": grade_hard,
}

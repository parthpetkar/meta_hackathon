"""Prompt construction for the agentic inference baseline."""

import os
import textwrap
from pathlib import Path
from typing import Dict, List

BASE_SYSTEM_PROMPT = textwrap.dedent(
    """
        You are a CI/CD repair agent. Debug broken pipelines by calling tools.

        Non-negotiable rules:
        - Always set_hypothesis BEFORE applying any fix
        - Gather evidence (view_logs/inspect_*) before setting a new hypothesis
        - Inspect only relevant stages (wrong stage = penalty)
        - Only rerun_pipeline AFTER applying a fix
        - Always run verify_fix after rerun_pipeline and before finalize
        - Only finalize when ALL issues are resolved and verification has passed
        - Avoid redundant or repeated actions

        Tool sequence guidance:
        view_logs -> inspect relevant config/dockerfile/permissions -> set_hypothesis ->
        apply fix (modify_config or add_dependency) -> rerun_pipeline -> verify_fix -> finalize
    """
).strip()

GENERAL_SKILL_CARDS: Dict[str, str] = {
    "Evidence-First Triage": (
        "Before any fix, collect at least one log signal and one config/infra clue for the active stage."
    ),
    "Hypothesis Quality": (
        "Hypotheses must mention concrete entities from evidence (service/stage/error signature), not generic guesses."
    ),
    "Safe Remediation": (
        "Prefer minimal reversible fixes. Never use destructive shortcuts like disabling checks or skipping validations."
    ),
    "Verification Discipline": (
        "After rerun_pipeline, verify_fix is mandatory before finalize. If verification fails, return to evidence gathering."
    ),
    "Efficiency Control": (
        "Avoid repeated identical low-signal actions; when progress stalls, switch stage or tool based on newest surfaced error."
    ),
}

TASK_SKILL_CARDS: Dict[str, List[str]] = {
    "easy": [
        "Focus on merge evidence: unresolved markers and strict merge policy clues.",
        "Use build-targeted modify_config to resolve conflict, then rerun, verify, finalize.",
    ],
    "flaky": [
        "Treat intermittent test failures as flaky/timing candidates when logs show pass-on-retry behavior.",
        "Prefer retry policy or test isolation fixes over broad application logic rewrites.",
    ],
    "medium": [
        "Solve dependency compatibility first (requests/urllib3), then Docker install order.",
        "Use add_dependency for version pinning and modify_config for Docker order corrections.",
    ],
    "network": [
        "Classify DNS and timeout upload failures as transient external dependency outages when evidence supports it.",
        "Use retry/backoff or proxy fallback mitigation; avoid rewriting application upload logic.",
    ],
    "security": [
        "Treat IAM writer permission and secret exposure as separate required remediations.",
        "Do not finalize until both security issues are fixed and verified.",
    ],
    "hard": [
        "Resolve upstream publisher permissions before downstream deploy tuning.",
        "After rollback, collect fresh deploy evidence before timeout hypothesis and tuning.",
    ],
}


def _load_external_skill_text() -> str:
    """Load optional user-provided skill text from env for quick prompt iteration."""
    inline_skills = (os.getenv("EXTRA_SKILLS") or "").strip()
    if inline_skills:
        return inline_skills

    skills_file = (os.getenv("LLM_SKILLS_FILE") or "").strip()
    if not skills_file:
        return ""

    path = Path(skills_file)
    if not path.exists() or not path.is_file():
        return ""

    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_system_prompt(task_name: str) -> str:
    general_lines = [f"- {name}: {description}" for name, description in GENERAL_SKILL_CARDS.items()]

    task_lines = TASK_SKILL_CARDS.get(task_name, [])
    task_section = "\n".join(f"- {line}" for line in task_lines) if task_lines else "- Use evidence-first debugging."

    external_skills = _load_external_skill_text()
    external_section = f"\n\nAdditional user-provided skills:\n{external_skills}" if external_skills else ""

    return (
        f"{BASE_SYSTEM_PROMPT}\n\n"
        f"Skill cards (apply these behaviors actively):\n"
        f"{chr(10).join(general_lines)}\n\n"
        f"Task-specific skills for '{task_name}':\n"
        f"{task_section}"
        f"{external_section}"
    )


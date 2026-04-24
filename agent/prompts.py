"""Prompt construction for the agentic inference baseline."""

import os
import textwrap
from pathlib import Path
from typing import Dict, List

BASE_SYSTEM_PROMPT_WS = textwrap.dedent(
    """
          CRITICAL REASONING RULES - FOLLOW THESE BEFORE EVERY ACTION:

          1. Read key files first: list_files, then read_file on Dockerfile,
             docker-compose.yml, services/api/requirements.txt. These contain the fault.

          2. Trigger the pipeline with trigger_pipeline to see the real failure logs.
             The stage logs returned show exactly which command failed and why.

          3. write_file ALWAYS requires the COMPLETE new file content — not a diff.
             Read the file first, then write the full corrected version.

          4. If set_hypothesis returns a negative reward (-0.10), your hypothesis is WRONG.
             Re-read the pipeline logs and form a different hypothesis. Never repeat one
             that scored negatively.

          5. Never repeat the exact same action+target twice in a row. If a read returned
             an error, try a different file or trigger the pipeline instead.

          6. CASCADING FAULTS: after fixing and re-triggering, if the pipeline still fails,
             treat the new error as a fresh independent root cause — re-read logs and
             form a new hypothesis.

        You are a CI/CD repair agent. Debug a broken pipeline using these tools ONLY:
          - list_files     : list workspace files
          - read_file      : read any file (Dockerfile, docker-compose.yml, requirements.txt, etc.)
          - set_hypothesis : declare your root-cause hypothesis before applying a fix
          - write_file     : write the corrected file content to fix the fault
          - trigger_pipeline : run the pipeline and receive stage logs
          - finalize       : call when the pipeline passes to end the episode

        Tool sequence (FOLLOW THIS EXACTLY):
        list_files -> read_file (key configs) -> trigger_pipeline (see failure) ->
        set_hypothesis -> write_file (fix) -> trigger_pipeline (verify) -> finalize

        NEVER call finalize unless trigger_pipeline returned status=passed.
        NEVER use view_logs, inspect_config, rerun_pipeline, verify_fix — those tools do not exist here.
    """
).strip()

BASE_SYSTEM_PROMPT = textwrap.dedent(
    """
          CRITICAL REASONING RULES - FOLLOW THESE BEFORE EVERY ACTION:

          1. ALWAYS check surfaced_errors first. The file and line number named there is your
              primary clue. Your first inspect_config action MUST target that exact file.

          2. modify_config ALWAYS requires a structured JSON value. Never send plain English.
              Format:
              {"file": "path/to/file.yml", "action": "replace",
               "old": "<exact lines from the file>",
               "new": "<corrected lines>"}
              Supported actions: "replace" (old→new), "delete_lines" (remove matching lines),
              "write" (overwrite entire file with "new" content).

          3. If set_hypothesis returns a negative reward (-0.10), your hypothesis is WRONG.
              You MUST discard it completely, re-read surfaced_errors and visible_logs,
              and form a different hypothesis. Never repeat a hypothesis that scored negatively.

          4. Never repeat the exact same action+target+value twice. If an action failed or
              scored negatively, do not repeat it. Try something different.

          5. Before calling set_hypothesis, you must have called inspect_config on the file
              named in surfaced_errors. No hypothesis without evidence.

          5b. CASCADING FAULTS: A single incident may have multiple independent root causes
              injected in sequence. If rerun_pipeline still fails after you successfully
              applied a fix, the remaining failure is a NEW fault — not a symptom of the one
              you just fixed. Re-read surfaced_errors from scratch, form a fresh hypothesis
              for the new error, apply a separate fix, then rerun again. Treat each new
              error pattern as a distinct fault to resolve.

          6. Merge conflict markers look like: <<<<<<< HEAD ... ======= ... >>>>>>> branch
              If you see these, use modify_config with structured JSON to remove the markers:
              {"file": "services/api/routes.py", "action": "replace",
               "old": "<<<<<<< HEAD\\n    return jsonify(...)\\n=======\\n    return jsonify(...)\\n>>>>>>> feature/new-health-check",
               "new": "    return jsonify(...)"}

          7. For every fault type, use structured JSON to describe the exact change needed.
              Examples:
              - PII in logs:
                {"file": "services/api/routes.py", "action": "delete_lines",
                 "pattern": "sk-live-"}
              - Hardcoded secret:
                {"file": "services/api/app.py", "action": "delete_lines",
                 "pattern": "API_KEY ="}
              The server also applies a direct fault-type fix automatically, so your JSON
              patch and the server's fix are both applied — use JSON to be precise.

        You are a CI/CD repair agent. Debug broken pipelines by calling tools.

        Non-negotiable rules:
        - Always set_hypothesis BEFORE applying any fix
        - Gather evidence (view_logs/inspect_*) before setting a new hypothesis
        - Inspect only relevant stages (wrong stage = penalty)
        - Only rerun_pipeline AFTER applying a fix
        - MANDATORY: Always run verify_fix after rerun_pipeline shows PASSED before finalize
        - NEVER call finalize without calling verify_fix first (penalty: -0.05)
        - Only finalize when ALL issues are resolved and verification has passed
        - Avoid redundant or repeated actions

        Tool sequence guidance (FOLLOW THIS EXACTLY):
        view_logs -> inspect relevant config/dockerfile/permissions -> set_hypothesis ->
        apply fix (modify_config or add_dependency) -> rerun_pipeline -> verify_fix -> finalize
        
        CRITICAL: If pipeline PASSED after rerun_pipeline, your NEXT action MUST be verify_fix.
        Do NOT skip verify_fix. Do NOT call finalize directly after rerun_pipeline.
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
        "Expect cascading faults: after fixing the first error, re-read surfaced_errors for a new independent fault (e.g. logging config, Docker order). Apply a separate fix for each.",
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


def build_system_prompt(task_name: str, ws_mode: bool = False) -> str:
    base = BASE_SYSTEM_PROMPT_WS if ws_mode else BASE_SYSTEM_PROMPT
    general_lines = [f"- {name}: {description}" for name, description in GENERAL_SKILL_CARDS.items()]

    task_lines = TASK_SKILL_CARDS.get(task_name, [])
    task_section = "\n".join(f"- {line}" for line in task_lines) if task_lines else "- Use evidence-first debugging."

    external_skills = _load_external_skill_text()
    external_section = f"\n\nAdditional user-provided skills:\n{external_skills}" if external_skills else ""

    return (
        f"{base}\n\n"
        f"Skill cards (apply these behaviors actively):\n"
        f"{chr(10).join(general_lines)}\n\n"
        f"Task-specific skills for '{task_name}':\n"
        f"{task_section}"
        f"{external_section}"
    )


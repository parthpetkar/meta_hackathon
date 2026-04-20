"""Build observations from real pipeline state and workspace file contents."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .pipeline_runner import PipelineResult, PipelineStatus, StageStatus, STAGE_ORDER


# ── Error pattern matching ─────────────────────────────────────────────────

_ERROR_PATTERNS = [
    re.compile(r"(?i)\berror\b"),
    re.compile(r"(?i)\bfailed\b"),
    re.compile(r"(?i)\bFAILED\b"),
    re.compile(r"(?i)\bexit code\b"),
    re.compile(r"(?i)\bCannot\b"),
    re.compile(r"(?i)\bModuleNotFoundError\b"),
    re.compile(r"(?i)\bImportError\b"),
    re.compile(r"(?i)\bSyntaxError\b"),
    re.compile(r"(?i)\bPermissionError\b"),
    re.compile(r"(?i)\bFileNotFoundError\b"),
    re.compile(r"(?i)\bConnectionError\b"),
    re.compile(r"(?i)\bResolutionImpossible\b"),
    re.compile(r"(?i)\bCONFLICT\b"),
    re.compile(r"(?i)\bfatal\b"),
    re.compile(r"(?i)\bdenied\b"),
    re.compile(r"(?i)\brejected\b"),
    re.compile(r"(?i)\btimeout\b"),
    re.compile(r"(?i)\bsecret\b.*\bdetect"),
    re.compile(r"(?i)plaintext\s+credential"),
]


def extract_error_lines(text: str, max_lines: int = 10) -> List[str]:
    """Return up to max_lines lines from text that match error patterns."""
    errors, seen = [], set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        if any(p.search(stripped) for p in _ERROR_PATTERNS):
            seen.add(stripped)
            errors.append(stripped)
            if len(errors) >= max_lines:
                break
    return errors


# ── File readers ───────────────────────────────────────────────────────────

_CONFIG_PATHS = [
    "Dockerfile",
    "docker-compose.yml",
    "services/api/requirements.txt",
    "services/api/routes.py",
    "services/api/app.py",
    ".github/ci.yml",
]


def read_workspace_file(workspace_dir: str, relative_path: str) -> str:
    filepath = os.path.join(workspace_dir, relative_path)
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return f"[File not found: {relative_path}]"
    except OSError as e:
        return f"[Error reading {relative_path}: {e}]"


def read_config_files(workspace_dir: str) -> Dict[str, str]:
    configs = {}
    for path in _CONFIG_PATHS:
        content = read_workspace_file(workspace_dir, path)
        if not content.startswith("[File not found"):
            configs[path] = content
    return configs


# ── Log builders ───────────────────────────────────────────────────────────

def build_visible_logs(pipeline_result: PipelineResult, max_lines: int = 20) -> List[str]:
    logs = []
    for stage_name in STAGE_ORDER:
        stage = pipeline_result.stages.get(stage_name)
        if not stage or stage.status == StageStatus.PENDING:
            continue
        icon = "✓" if stage.status == StageStatus.PASSED else ("✗" if stage.status == StageStatus.FAILED else "⊘")
        logs.append(f"[{icon}] Stage '{stage_name}' — {stage.status.value} ({stage.duration_seconds:.1f}s)")
        combined = (stage.stdout + "\n" + stage.stderr).strip()
        if combined:
            lines = [l.strip() for l in combined.splitlines() if l.strip()]
            tail = lines[-12:] if stage.status == StageStatus.FAILED else lines[-3:]
            logs.extend(tail)
    return logs[:max_lines]


def build_logs_by_stage(pipeline_result: PipelineResult) -> Dict[str, List[str]]:
    result = {}
    for stage_name in STAGE_ORDER:
        stage = pipeline_result.stages.get(stage_name)
        if not stage:
            result[stage_name] = []
            continue
        combined = (stage.stdout + "\n" + stage.stderr).strip()
        result[stage_name] = [l.strip() for l in combined.splitlines() if l.strip()][-20:] if combined else []
    return result


def build_stage_log_response(pipeline_result: PipelineResult, stage_name: str) -> str:
    """Detailed log view for a specific stage (used by the view_logs action)."""
    stage = pipeline_result.stages.get(stage_name)
    if not stage:
        return f"No logs available for stage '{stage_name}'"

    header = (
        f"=== Stage: {stage_name} ===\n"
        f"Status: {stage.status.value}  Exit code: {stage.exit_code}  "
        f"Duration: {stage.duration_seconds:.1f}s\n"
        f"Command: {stage.command}\n"
    )

    combined = ((stage.stdout or "") + "\n" + (stage.stderr or "")).strip()
    error_lines = extract_error_lines(combined, max_lines=15)
    tail_lines = combined.splitlines()[-30:] if combined else []

    parts = []
    if error_lines:
        parts.append("--- key errors ---")
        parts.extend(error_lines)
    if tail_lines:
        parts.append("--- log tail ---")
        parts.extend(tail_lines)

    return header + "\n".join(parts)


def build_surfaced_errors(pipeline_result: PipelineResult, workspace_dir: str = "") -> List[str]:
    """Extract errors from failed stage logs and scan source files for conflict markers."""
    errors = []
    for stage_name in STAGE_ORDER:
        stage = pipeline_result.stages.get(stage_name)
        if stage and stage.status == StageStatus.FAILED:
            errors.extend(extract_error_lines(stage.stdout + "\n" + stage.stderr))

    if workspace_dir:
        for rel_path in ["services/api/routes.py", "services/api/app.py",
                          "services/api/requirements.txt", "Dockerfile", "docker-compose.yml"]:
            full_path = os.path.join(workspace_dir, rel_path)
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if "<<<<<<< " in content:
                    for i, line in enumerate(content.splitlines(), 1):
                        if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
                            errors.append(f"MERGE CONFLICT in {rel_path}:{i}: {line.strip()}")
                            if len(errors) >= 10:
                                return errors
            except OSError:
                continue

    return errors[:10]


def build_visible_alerts(pipeline_result: PipelineResult) -> List[str]:
    if pipeline_result.status == PipelineStatus.FAILED:
        stage = pipeline_result.stages.get(pipeline_result.failed_stage)
        if not stage:
            return []
        alerts = [f"Pipeline FAILED at stage '{pipeline_result.failed_stage}' with exit code {stage.exit_code}"]
        combined = (stage.stdout + "\n" + stage.stderr).strip()
        alerts.extend(f"  → {e}" for e in extract_error_lines(combined, max_lines=2))
        return alerts
    if pipeline_result.status == PipelineStatus.PASSED:
        return ["Pipeline PASSED — all stages completed successfully"]
    return []


def build_visible_metrics(pipeline_result: PipelineResult) -> List[str]:
    metrics = []
    for stage_name in STAGE_ORDER:
        stage = pipeline_result.stages.get(stage_name)
        if not stage or stage.status in (StageStatus.PENDING, StageStatus.SKIPPED):
            continue
        metrics.append(
            f"{stage_name}: status={stage.status.value} "
            f"exit_code={stage.exit_code} duration={stage.duration_seconds:.1f}s"
        )
    metrics.append(f"total_duration={pipeline_result.total_duration_seconds:.1f}s")
    return metrics


# ── Main observation builder ───────────────────────────────────────────────

def build_observation(
    pipeline_result: PipelineResult,
    workspace_dir: str,
    *,
    task_id: str = "",
    task_title: str = "",
    difficulty: str = "",
    reward: float = 0.0,
    done: bool = False,
    action_history: Optional[List[str]] = None,
    current_hypothesis: str = "",
    attempted_fix: str = "",
    hypothesis_history: Optional[List[str]] = None,
    incident_resolved: bool = False,
    pipeline_health: float = 1.0,
    recovery_cost: int = 0,
    redundant_actions: int = 0,
    destructive_actions: int = 0,
    final_score: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None,
    findings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Construct a complete observation dict from real pipeline state."""
    failed_stage = pipeline_result.failed_stage or ""
    if pipeline_result.status == PipelineStatus.PASSED:
        current_stage = "deploy"
    elif failed_stage:
        current_stage = failed_stage
    else:
        current_stage = next(
            (sn for sn in STAGE_ORDER
             if (s := pipeline_result.stages.get(sn)) and s.status in (StageStatus.RUNNING, StageStatus.PENDING)),
            "",
        )

    return {
        "task_id": task_id,
        "task_title": task_title,
        "difficulty": difficulty,
        "pipeline_status": pipeline_result.status.value,
        "current_stage": current_stage,
        "pipeline_stages": pipeline_result.get_stage_statuses(),
        "available_stages": list(STAGE_ORDER),
        "available_tools": [
            "view_logs", "inspect_config", "inspect_dockerfile", "inspect_permissions",
            "set_hypothesis", "modify_config", "add_dependency",
            "rerun_pipeline", "verify_fix", "finalize",
        ],
        "visible_alerts": build_visible_alerts(pipeline_result),
        "visible_logs": build_visible_logs(pipeline_result),
        "logs_by_stage": build_logs_by_stage(pipeline_result),
        "visible_metrics": build_visible_metrics(pipeline_result),
        "config_files": read_config_files(workspace_dir),
        "surfaced_errors": build_surfaced_errors(pipeline_result, workspace_dir),
        "findings": findings or ["Incident acknowledged. Investigate before changing configuration."],
        "action_history": action_history or [],
        "previous_actions": action_history or [],
        "current_hypothesis": current_hypothesis,
        "attempted_fix": attempted_fix,
        "hypothesis_history": hypothesis_history or [],
        "active_issue_index": 0,
        "revealed_issue_count": 1,
        "pipeline_health": round(pipeline_health, 3),
        "recovery_cost": recovery_cost,
        "redundant_actions": redundant_actions,
        "destructive_actions": destructive_actions,
        "incident_resolved": incident_resolved,
        "final_score": final_score,
        "deterministic_score": 0.0,
        "rubric_score": 0.0,
        "delayed_reward": 0.0,
        "rubric_blend_weight": 0.0,
        "rubric_judge_used": False,
        "rubric_judge_error": "",
        "reward": reward,
        "done": done,
        "metadata": metadata or {},
    }

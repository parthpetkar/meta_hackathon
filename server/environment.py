"""Dynamic CI/CD repair environment backed by real Git + Docker pipelines.

Replaces the static simulation with real subprocess-based pipeline execution,
real file mutation for fault injection, and real file operations for fixes.
Preserves the OpenEnv API contract: reset(), step(), state().
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

try:
    from ..models import MetaHackathonAction, MetaHackathonObservation
except (ImportError, ModuleNotFoundError):
    from models import MetaHackathonAction, MetaHackathonObservation

from cicd.pipeline_runner import (
    PipelineResult,
    PipelineRunner,
    PipelineStatus,
    StageStatus,
    STAGE_ORDER,
    cleanup_pipeline,
    setup_repo_from_template,
)
from cicd.fault_injector import (
    FaultMetadata,
    FAULT_TYPES,
    FAULT_KEYWORDS,
    FAULT_STAGE_MAP,
    inject_fault,
    inject_random_fault,
)
from cicd.observation_builder import (
    build_observation,
    build_stage_log_response,
    build_surfaced_errors,
    build_visible_alerts,
    build_visible_logs,
    build_visible_metrics,
    build_logs_by_stage,
    extract_error_lines,
    read_config_files,
    read_workspace_file,
)
from cicd.fix_applier import apply_fix, FixResult


# ── Constants ──────────────────────────────────────────────────────────────

CANONICAL_OPERATIONS = [
    "view_logs", "inspect_config", "inspect_dockerfile",
    "inspect_permissions", "set_hypothesis", "modify_config",
    "add_dependency", "rerun_pipeline", "verify_fix", "finalize",
]

SAFE_FIXES = [
    "resolve-merge-conflict",
    "pin-compatible-requests-urllib3",
    "reorder-docker-install-steps",
    "add-flaky-test-retry-wrapper",
    "fix-docker-compose-network",
    "remove-hardcoded-secrets",
]

DESTRUCTIVE_FIXES = [
    "disable-all-tests",
    "force-push-main",
    "wipe-registry",
    "skip-deploy-validations",
]

# Cleanup stale workspaces after this many seconds
WORKSPACE_TTL_SECONDS = 1800  # 30 minutes

# Sample-app template directory (relative to project root)
SAMPLE_APP_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sample-app",
)


@dataclass
class State:
    episode_id: str = ""
    step_count: int = 0


@dataclass
class EpisodeState:
    """All state for a single episode."""
    episode_id: str = ""
    workspace_dir: str = ""
    repo_dir: str = ""
    fault_metadata: Optional[FaultMetadata] = None
    pipeline_result: Optional[PipelineResult] = None
    pipeline_runner: Optional[PipelineRunner] = None
    all_pipeline_results: List[PipelineResult] = field(default_factory=list)

    # Action tracking
    history: List[Dict[str, str]] = field(default_factory=list)
    action_keys: Set[str] = field(default_factory=set)
    findings: List[str] = field(default_factory=list)

    # Hypothesis tracking
    current_hypothesis: str = ""
    hypothesis_history: List[str] = field(default_factory=list)
    attempted_fix: str = ""
    hypothesis_attempts: int = 0
    hypothesis_correct: bool = False

    # Pipeline state
    incident_resolved: bool = False
    pipeline_health: float = 1.0
    recovery_cost: int = 0
    redundant_actions: int = 0
    destructive_actions: int = 0
    wrong_fixes: int = 0

    # Fix tracking
    last_fix_result: Optional[FixResult] = None
    pending_fix_outcome: str = "none"
    last_rerun_progressed: bool = False
    verified_for_latest_rerun: bool = False
    inspected_since_last_rerun: bool = True
    fix_hits: int = 0

    # Inspections
    used_inspections: Set[str] = field(default_factory=set)

    # Timestamps
    created_at: float = 0.0


def _canonical_operation(operation: str) -> str:
    """Normalize operation name to canonical form."""
    aliases = {
        "view-logs": "view_logs",
        "viewlogs": "view_logs",
        "inspect-config": "inspect_config",
        "inspectconfig": "inspect_config",
        "inspect-dockerfile": "inspect_dockerfile",
        "inspectdockerfile": "inspect_dockerfile",
        "inspect-permissions": "inspect_permissions",
        "inspectpermissions": "inspect_permissions",
        "set-hypothesis": "set_hypothesis",
        "sethypothesis": "set_hypothesis",
        "modify-config": "modify_config",
        "modifyconfig": "modify_config",
        "add-dependency": "add_dependency",
        "adddependency": "add_dependency",
        "rerun-pipeline": "rerun_pipeline",
        "rerunpipeline": "rerun_pipeline",
        "verify-fix": "verify_fix",
        "verifyfix": "verify_fix",
    }
    op = operation.lower().strip()
    return aliases.get(op, op)


try:
    from openenv.core.env_server.interfaces import Environment
except ImportError:
    # Fallback if not available, just use object
    class Environment:
        pass


class RealCICDRepairEnvironment(Environment):
    """Real CI/CD repair environment with dynamic fault injection and subprocess pipelines.

    Replaces MetaHackathonCICDRepairEnvironment with real Git + Docker operations.
    """

    SUPPORTS_CONCURRENT_SESSIONS = True

    def __init__(self, task_key: str = ""):
        self._task_key = task_key or os.getenv("META_HACKATHON_TASK_MODE", "cycle")
        self._state = State()
        self._episode: Optional[EpisodeState] = None

        self._task_order = FAULT_TYPES
        self._task_cursor = 0

        self._pipeline_timeout = int(
            os.getenv("META_HACKATHON_PIPELINE_TIMEOUT_SECONDS", "300")
        )

        # Cleanup thread
        self._cleanup_lock = threading.Lock()
        self._stale_workspaces: List[tuple[str, float]] = []

        # Template directory
        self._template_dir = SAMPLE_APP_TEMPLATE
        if not os.path.isdir(self._template_dir):
            self._template_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "sample-app"
            )

    # ── OpenEnv API ────────────────────────────────────────────────────────

    async def reset_async(
        self, seed: Optional[int] = None, episode_id: Optional[str] = None, **kwargs: Any
    ) -> MetaHackathonObservation:
        """Handle async OpenEnv reset with kwargs to capture requested task configuration."""
        # OpenEnv passes configuration in reset_options dict
        options = kwargs.get("reset_options", {})
        task_key = options.get("task_key") or kwargs.get("task_key", "")
        return self.reset(task_key=task_key)

    def reset(self, task_key: str = "") -> MetaHackathonObservation:
        """Start a new episode: inject fault, run pipeline, return initial observation."""
        # Clean up previous episode
        if self._episode:
            self._cleanup_episode(self._episode)

        episode_id = str(uuid.uuid4())
        self._state = State(episode_id=episode_id, step_count=0)

        eff_task_key = task_key or self._task_key

        # Choose fault type
        if eff_task_key == "cycle":
            fault_type = self._task_order[self._task_cursor % len(self._task_order)]
            self._task_cursor += 1
        elif eff_task_key in FAULT_TYPES:
            fault_type = eff_task_key
        else:
            # Map old task keys to fault types
            key_map = {
                "easy": "merge_conflict",
                "medium": "dependency_conflict",
                "flaky": "flaky_test",
                "security": "secret_exposure",
                "hard": "missing_permission",
                "network": "docker_order",
            }
            fault_type = key_map.get(eff_task_key, "merge_conflict")

        # Set up workspace
        workspace_base = tempfile.mkdtemp(prefix="cicd-episode-")
        repo_dir = os.path.join(workspace_base, "repo")

        # Initialize git repo from template
        setup_repo_from_template(self._template_dir, repo_dir)

        # Inject the fault
        fault_metadata = inject_fault(repo_dir, fault_type)

        # Run the pipeline (should fail)
        runner = PipelineRunner(
            repo_path=repo_dir,
            workspace_base=workspace_base,
            timeout_per_stage=self._pipeline_timeout,
        )
        pipeline_result = runner.run(workspace_dir=repo_dir)

        # Create episode state
        episode = EpisodeState(
            episode_id=episode_id,
            workspace_dir=repo_dir,
            repo_dir=repo_dir,
            fault_metadata=fault_metadata,
            pipeline_result=pipeline_result,
            pipeline_runner=runner,
            all_pipeline_results=[pipeline_result],
            findings=["Incident acknowledged. Investigate before changing configuration."],
            created_at=time.time(),
        )
        self._episode = episode

        # Build difficulty from fault type
        difficulty_map = {
            "merge_conflict": "easy",
            "dependency_conflict": "medium",
            "docker_order": "medium",
            "flaky_test": "easy",
            "missing_permission": "hard",
            "secret_exposure": "security",
        }
        difficulty = difficulty_map.get(fault_type, "medium")

        # Build max_steps from difficulty
        max_steps_map = {"easy": 16, "medium": 20, "security": 20, "hard": 25}
        max_steps = max_steps_map.get(difficulty, 15)

        obs_dict = build_observation(
            pipeline_result=pipeline_result,
            workspace_dir=repo_dir,
            task_id=f"real_{fault_type}",
            task_title=f"Fix {fault_type.replace('_', ' ')} in CI/CD pipeline",
            difficulty=difficulty,
            reward=0.0,
            done=False,
            findings=episode.findings,
            metadata={
                "task_key": fault_type,
                "max_steps": max_steps,
                "fault_type": fault_type,
                "expected_fail_stage": fault_metadata.expected_fail_stage,
                "ready_to_finalize": False,
                "verification_required": False,
                "verified_since_last_rerun": False,
                "supported_operations": CANONICAL_OPERATIONS,
                "canonical_operations": CANONICAL_OPERATIONS,
            },
        )

        return self._dict_to_observation(obs_dict)

    def step(self, action: MetaHackathonAction) -> MetaHackathonObservation:
        """Execute one action step."""
        self._state.step_count += 1
        episode = self._episode
        if not episode:
            return self._error_observation("No active episode. Call /reset first.")

        raw_operation = (action.operation or "").strip()
        operation = _canonical_operation(raw_operation)
        target = (action.target or "").strip()
        value = (action.value or "").strip()

        if operation not in CANONICAL_OPERATIONS:
            return self._error_observation(
                f"Unsupported operation '{raw_operation}'",
                reward=-0.20,
            )

        # Track action
        history_entry = {"operation": operation, "target": target, "value": value}
        episode.history.append(history_entry)

        action_key = f"{operation}:{target}:{value}"
        was_redundant = action_key in episode.action_keys
        if was_redundant:
            episode.redundant_actions += 1
        episode.action_keys.add(action_key)

        # Dispatch action
        reward = 0.0

        if operation == "view_logs":
            reward = self._handle_view_logs(episode, target)

        elif operation == "inspect_config":
            reward = self._handle_inspect_config(episode, target)

        elif operation == "inspect_dockerfile":
            reward = self._handle_inspect_dockerfile(episode)

        elif operation == "inspect_permissions":
            reward = self._handle_inspect_permissions(episode, target)

        elif operation == "set_hypothesis":
            reward = self._handle_set_hypothesis(episode, value)

        elif operation in ("modify_config", "add_dependency"):
            reward = self._handle_modify(episode, operation, target, value)

        elif operation == "rerun_pipeline":
            reward = self._handle_rerun_pipeline(episode)

        elif operation == "verify_fix":
            reward = self._handle_verify_fix(episode)

        elif operation == "finalize":
            reward = self._handle_finalize(episode)

        # Apply redundancy penalty
        if was_redundant:
            reward = min(reward, -0.08)

        # Determine if episode is done
        done = False
        max_steps = 15
        if episode.fault_metadata:
            difficulty = self._get_difficulty(episode.fault_metadata.fault_type)
            max_steps_map = {"easy": 16, "medium": 20, "security": 20, "hard": 25}
            max_steps = max_steps_map.get(difficulty, 15)

        if operation == "finalize":
            done = True
        elif self._state.step_count >= max_steps:
            done = True

        # Build observation
        obs_dict = self._build_step_observation(episode, reward, done)
        return self._dict_to_observation(obs_dict)

    def state(self) -> dict:
        """Get the current environment state."""
        return {
            "episode_id": self._state.episode_id,
            "step_count": self._state.step_count,
        }

    # ── Action Handlers ────────────────────────────────────────────────────

    def _handle_view_logs(self, ep: EpisodeState, target: str) -> float:
        """Return actual captured logs for the requested stage."""
        stage = target.lower() if target else ""
        ep.used_inspections.add("view_logs")
        ep.inspected_since_last_rerun = True

        if ep.pipeline_result:
            if stage and stage in STAGE_ORDER:
                log_text = build_stage_log_response(ep.pipeline_result, stage)
                ep.findings.append(f"Logs for stage '{stage}':\n{log_text[:500]}")
            else:
                # Show logs for the failed stage
                failed_stage = ep.pipeline_result.failed_stage
                if failed_stage:
                    log_text = build_stage_log_response(ep.pipeline_result, failed_stage)
                    ep.findings.append(f"Logs for failed stage '{failed_stage}':\n{log_text[:500]}")

        # Reward: +0.12 if requested stage is where real failure occurred
        if ep.fault_metadata and stage == ep.fault_metadata.expected_fail_stage:
            return 0.12
        elif stage and ep.pipeline_result and stage == ep.pipeline_result.failed_stage:
            return 0.12
        return -0.05

    def _handle_inspect_config(self, ep: EpisodeState, target: str) -> float:
        """Read and return actual file content from workspace."""
        ep.used_inspections.add("inspect_config")
        ep.inspected_since_last_rerun = True

        configs = read_config_files(ep.workspace_dir)
        for filename, content in configs.items():
            # Surface conflict markers explicitly before truncating
            conflict_lines = [
                f"  line {i}: {line.rstrip()}"
                for i, line in enumerate(content.splitlines(), 1)
                if line.startswith(("<<<<<<<", "=======", ">>>>>>>"))
            ]
            if conflict_lines:
                ep.findings.append(
                    f"⚠ MERGE CONFLICT in '{filename}':\n" + "\n".join(conflict_lines)
                )
            ep.findings.append(f"Config file '{filename}':\n{content[:500]}")

        # Relevant if fault is in a config file
        if ep.fault_metadata:
            relevant_files = set(ep.fault_metadata.affected_files)
            config_names = set(configs.keys())
            if relevant_files & config_names:
                return 0.12
        return -0.05

    def _handle_inspect_dockerfile(self, ep: EpisodeState) -> float:
        """Read actual Dockerfile content from workspace."""
        ep.used_inspections.add("inspect_dockerfile")
        ep.inspected_since_last_rerun = True

        content = read_workspace_file(ep.workspace_dir, "Dockerfile")
        ep.findings.append(f"Dockerfile content:\n{content[:400]}")

        if ep.fault_metadata and "Dockerfile" in ep.fault_metadata.affected_files:
            return 0.12
        elif ep.fault_metadata and ep.fault_metadata.fault_type == "docker_order":
            return 0.12
        return -0.05

    def _handle_inspect_permissions(self, ep: EpisodeState, target: str) -> float:
        """Check file permissions in the workspace."""
        ep.used_inspections.add("inspect_permissions")
        ep.inspected_since_last_rerun = True

        # Identify files to check
        files_to_check = ["Dockerfile", "docker-compose.yml", "services/api/app.py"]
        if target:
            files_to_check.insert(0, target)

        for filepath in files_to_check:
            full_path = os.path.join(ep.workspace_dir, filepath)
            if os.path.exists(full_path):
                try:
                    stat = os.stat(full_path)
                    mode = oct(stat.st_mode)
                    size = stat.st_size
                    ep.findings.append(
                        f"File '{filepath}': mode={mode} size={size}B"
                    )
                except OSError as e:
                    ep.findings.append(f"Cannot stat '{filepath}': {e}")

        if ep.fault_metadata and ep.fault_metadata.fault_type == "missing_permission":
            return 0.12
        return -0.05

    def _handle_set_hypothesis(self, ep: EpisodeState, value: str) -> float:
        """Store hypothesis and score against real fault metadata."""
        ep.current_hypothesis = value
        ep.hypothesis_history.append(value)
        ep.hypothesis_attempts += 1

        if not ep.fault_metadata:
            return -0.10

        # Score by comparing key terms against real fault type and affected files
        hypothesis_lower = value.lower()
        keywords = ep.fault_metadata.keywords or FAULT_KEYWORDS.get(ep.fault_metadata.fault_type, [])
        match_count = sum(1 for kw in keywords if kw.lower() in hypothesis_lower)
        match_ratio = match_count / max(len(keywords), 1)

        # Also check if affected files are mentioned
        file_mentioned = any(
            os.path.basename(f).lower() in hypothesis_lower
            for f in ep.fault_metadata.affected_files
        )

        if match_ratio >= 0.4 or (match_ratio >= 0.2 and file_mentioned):
            ep.hypothesis_correct = True
            ep.findings.append("Hypothesis aligns with current failure evidence.")
            if ep.hypothesis_attempts == 1:
                return 0.22  # First try
            return 0.10  # Retry
        else:
            ep.findings.append("Hypothesis does not explain all current clues yet.")
            return -0.10

    def _handle_modify(self, ep: EpisodeState, operation: str, target: str, value: str) -> float:
        """Apply fix to real files in workspace."""
        ep.attempted_fix = value
        ep.verified_for_latest_rerun = False

        # Check for destructive fixes
        if self._is_destructive_fix(value):
            ep.destructive_actions += 1
            ep.pipeline_health = max(0.0, ep.pipeline_health - 0.20)
            ep.recovery_cost += 4
            ep.pending_fix_outcome = "destructive"
            ep.findings.append("Unsafe fix worsened system stability and increased recovery cost.")
            return -0.30

        # Apply the fix
        fix_result = apply_fix(ep.workspace_dir, value, target)
        ep.last_fix_result = fix_result

        if fix_result.success:
            ep.findings.append(f"Fix applied: {fix_result.description}")
            ep.pending_fix_outcome = "applied"
            return 0.10
        else:
            ep.findings.append(f"Fix could not be applied: {fix_result.error}")
            ep.pending_fix_outcome = "failed"
            ep.wrong_fixes += 1
            ep.pipeline_health = max(0.0, ep.pipeline_health - 0.10)
            ep.recovery_cost += 2
            return -0.15

    def _handle_rerun_pipeline(self, ep: EpisodeState) -> float:
        """Re-run the real pipeline and observe results."""
        ep.recovery_cost += 1
        ep.verified_for_latest_rerun = False
        ep.inspected_since_last_rerun = False

        if not ep.pipeline_runner:
            ep.findings.append("No pipeline runner available for rerun.")
            return -0.10

        # Run the pipeline again from current workspace state
        old_status = ep.pipeline_result.status if ep.pipeline_result else PipelineStatus.FAILED
        old_failed_stage = ep.pipeline_result.failed_stage if ep.pipeline_result else ""

        new_result = ep.pipeline_runner.run(workspace_dir=ep.workspace_dir)
        ep.all_pipeline_results.append(new_result)
        ep.pipeline_result = new_result

        # Append per-stage logs so the agent can see what happened
        for stage_name in STAGE_ORDER:
            stage = new_result.stages.get(stage_name)
            if not stage or stage.status.value in ("pending", "skipped"):
                continue
            combined = ((stage.stdout or "") + "\n" + (stage.stderr or "")).strip()
            tail = "\n".join(combined.splitlines()[-20:]) if combined else "(no output)"
            ep.findings.append(
                f"[Rerun] Stage '{stage_name}' → {stage.status.value} "
                f"(exit {stage.exit_code}, {stage.duration_seconds:.1f}s)\n{tail[:600]}"
            )

        # Determine if the fix caused progress
        progressed = False
        if new_result.status == PipelineStatus.PASSED:
            progressed = True
            ep.incident_resolved = True
            ep.findings.append("Pipeline PASSED — all stages completed successfully!")
        elif old_failed_stage and new_result.failed_stage:
            old_idx = STAGE_ORDER.index(old_failed_stage) if old_failed_stage in STAGE_ORDER else 0
            new_idx = STAGE_ORDER.index(new_result.failed_stage) if new_result.failed_stage in STAGE_ORDER else 0
            if new_idx > old_idx:
                progressed = True
                ep.findings.append(
                    f"Pipeline advanced from '{old_failed_stage}' to '{new_result.failed_stage}'."
                )
            else:
                ep.findings.append("Pipeline still failing at the same stage.")
        elif old_status == PipelineStatus.FAILED and new_result.status == PipelineStatus.FAILED:
            ep.findings.append("Rerun shows failure unchanged; refine diagnosis.")

        ep.last_rerun_progressed = progressed

        if progressed:
            ep.fix_hits += 1
            ep.findings.append("Run verify_fix before finalize to confirm the failure signature is gone.")
            return 0.18
        return 0.05

    def _handle_verify_fix(self, ep: EpisodeState) -> float:
        """Check real pipeline status."""
        if not ep.pipeline_result:
            ep.findings.append("No pipeline run to verify.")
            return -0.06

        if ep.verified_for_latest_rerun:
            ep.findings.append("Latest rerun is already verified.")
            return -0.06

        if ep.pipeline_result.status == PipelineStatus.PASSED:
            ep.incident_resolved = True
            ep.verified_for_latest_rerun = True
            ep.findings.append("Verification confirms the fix resolved the incident.")
            return 0.16
        elif ep.last_rerun_progressed:
            ep.verified_for_latest_rerun = True
            ep.findings.append("Verification confirms partial progress.")
            return 0.08
        else:
            ep.findings.append("Verification failed: pipeline still shows unresolved failures.")
            return -0.06

    def _handle_finalize(self, ep: EpisodeState) -> float:
        """Compute final score from real pipeline outcome."""
        if ep.incident_resolved and ep.verified_for_latest_rerun:
            ep.findings.append("CI/CD incident fully resolved. Pipeline healthy.")
            return 0.25
        elif ep.incident_resolved and not ep.verified_for_latest_rerun:
            ep.findings.append("Finalization rejected: run verify_fix after rerun_pipeline.")
            return -0.15
        elif ep.last_rerun_progressed:
            ep.findings.append("Partial resolution — some stages still failing.")
            return 0.20
        else:
            ep.findings.append("Finalization rejected: unresolved stages remain.")
            return -0.15

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _get_difficulty(self, fault_type: str) -> str:
        difficulty_map = {
            "merge_conflict": "easy",
            "dependency_conflict": "medium",
            "docker_order": "medium",
            "flaky_test": "easy",
            "missing_permission": "hard",
            "secret_exposure": "security",
        }
        return difficulty_map.get(fault_type, "medium")

    def _is_destructive_fix(self, value: str) -> bool:
        normalized = (value or "").strip().lower()
        return any(phrase in normalized for phrase in DESTRUCTIVE_FIXES)

    def _build_step_observation(
        self, ep: EpisodeState, reward: float, done: bool
    ) -> Dict[str, Any]:
        """Build observation dict from current episode state."""
        fault_type = ep.fault_metadata.fault_type if ep.fault_metadata else "unknown"
        difficulty = self._get_difficulty(fault_type)

        action_history = [
            f"{e['operation']}:{e.get('target', '')}:{e.get('value', '')}".strip(":")
            for e in ep.history[-16:]
        ]

        # Compute final score on done
        final_score = 0.0
        if done:
            final_score = self._compute_final_score(ep)

        pipeline_result = ep.pipeline_result or PipelineResult()

        return build_observation(
            pipeline_result=pipeline_result,
            workspace_dir=ep.workspace_dir,
            task_id=f"real_{fault_type}",
            task_title=f"Fix {fault_type.replace('_', ' ')} in CI/CD pipeline",
            difficulty=difficulty,
            reward=reward,
            done=done,
            action_history=action_history,
            current_hypothesis=ep.current_hypothesis,
            attempted_fix=ep.attempted_fix,
            hypothesis_history=ep.hypothesis_history[-8:],
            incident_resolved=ep.incident_resolved,
            pipeline_health=ep.pipeline_health,
            recovery_cost=ep.recovery_cost,
            redundant_actions=ep.redundant_actions,
            destructive_actions=ep.destructive_actions,
            final_score=final_score,
            findings=ep.findings[-16:],
            metadata={
                "task_key": fault_type,
                "fault_type": fault_type,
                "expected_fail_stage": ep.fault_metadata.expected_fail_stage if ep.fault_metadata else "",
                "ready_to_finalize": ep.incident_resolved and ep.verified_for_latest_rerun,
                "verification_required": ep.incident_resolved and not ep.verified_for_latest_rerun,
                "verified_since_last_rerun": ep.verified_for_latest_rerun,
                "supported_operations": CANONICAL_OPERATIONS,
                "canonical_operations": CANONICAL_OPERATIONS,
            },
        )

    def _compute_final_score(self, ep: EpisodeState) -> float:
        """Compute final episode score from real outcomes."""
        score = 0.0

        # Base score from resolution
        if ep.incident_resolved and ep.verified_for_latest_rerun:
            score += 0.50
        elif ep.incident_resolved:
            score += 0.30
        elif ep.last_rerun_progressed:
            score += 0.20

        # Hypothesis quality
        if ep.hypothesis_correct:
            score += 0.15

        # Fix quality
        if ep.fix_hits > 0:
            score += 0.10

        # Efficiency bonus
        step_count = self._state.step_count
        max_steps_map = {"easy": 16, "medium": 20, "security": 20, "hard": 25}
        difficulty = self._get_difficulty(ep.fault_metadata.fault_type if ep.fault_metadata else "medium")
        max_steps = max_steps_map.get(difficulty, 15)
        efficiency = max(0.0, 1.0 - (step_count / max_steps))
        score += efficiency * 0.10

        # Penalties
        score -= ep.redundant_actions * 0.02
        score -= ep.destructive_actions * 0.10
        score -= ep.wrong_fixes * 0.05

        # Pipeline health factor
        score *= ep.pipeline_health

        return round(max(0.0, min(1.0, score)), 3)

    def _dict_to_observation(self, obs_dict: Dict[str, Any]) -> MetaHackathonObservation:
        """Convert observation dict to MetaHackathonObservation model."""
        return MetaHackathonObservation(**obs_dict)

    def _error_observation(self, message: str, reward: float = -0.20) -> MetaHackathonObservation:
        """Return an error observation."""
        return MetaHackathonObservation(
            pipeline_status="error",
            reward=reward,
            done=False,
            metadata={"error": message},
        )

    def _cleanup_episode(self, ep: EpisodeState) -> None:
        """Clean up workspace and Docker resources from an episode."""
        for result in ep.all_pipeline_results:
            try:
                cleanup_pipeline(result)
            except Exception:
                pass
        if ep.pipeline_result and ep.pipeline_result not in ep.all_pipeline_results:
            try:
                cleanup_pipeline(ep.pipeline_result)
            except Exception:
                pass

        # Remove workspace directory
        workspace_base = os.path.dirname(ep.workspace_dir)
        if workspace_base and os.path.exists(workspace_base) and "cicd-episode" in workspace_base:
            try:
                shutil.rmtree(workspace_base, ignore_errors=True)
            except Exception:
                pass

    def cleanup_stale_workspaces(self) -> int:
        """Clean up workspaces older than TTL. Returns count of cleaned workspaces."""
        cleaned = 0
        cutoff = time.time() - WORKSPACE_TTL_SECONDS

        with self._cleanup_lock:
            remaining = []
            for path, created_at in self._stale_workspaces:
                if created_at < cutoff:
                    try:
                        shutil.rmtree(path, ignore_errors=True)
                        cleaned += 1
                    except Exception:
                        pass
                else:
                    remaining.append((path, created_at))
            self._stale_workspaces = remaining

        return cleaned

    def close(self) -> None:
        """Clean up active resources when shutting down the environment."""
        if self._episode:
            self._cleanup_episode(self._episode)
            self._episode = None

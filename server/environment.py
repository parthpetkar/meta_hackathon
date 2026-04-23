"""Dynamic CI/CD repair environment backed by real Git + Docker pipelines.

Replaces the static simulation with real subprocess-based pipeline execution,
real file mutation for fault injection, and real file operations for fixes.
Preserves the OpenEnv API contract: reset(), step(), state().
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

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
    cleanup_cache_image,
    setup_repo_from_template,
    create_pipeline_runner,
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
from cicd.procedural_generator import generate_scenario as procedural_generate_scenario, inject_procedural
try:
    from .curriculum import CurriculumController
    from .adversarial_designer import AdversarialDesigner
    from .adversarial_judge import AdversarialJudge
except (ImportError, ModuleNotFoundError):
    from server.curriculum import CurriculumController
    from server.adversarial_designer import AdversarialDesigner
    from server.adversarial_judge import AdversarialJudge


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

KNOWN_CONFIG_PATHS = (
    "Dockerfile",
    "docker-compose.yml",
    ".env",
    "services/api/requirements.txt",
    "services/api/routes.py",
    "services/api/app.py",
    "services/api/logging_config.py",
    "tests/test_api.py",
    ".github/ci.yml",
    "db/migrations/001_init.sql",
    "db/database.py",
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
    errors_stale_after_fix: bool = False  # True after a successful fix until next rerun

    # Adversarial mode
    adversarial_scenario: Optional[Any] = None
    cascading_faults: List[Any] = field(default_factory=list)
    curriculum_difficulty: float = 0.5

    # Rubric scoring
    deterministic_score: float = 0.0
    rubric_score: float = 0.0
    delayed_reward: float = 0.0
    rubric_judge_used: bool = False
    rubric_judge_error: str = ""

    # Inspections
    used_inspections: Set[str] = field(default_factory=set)

    # Timestamps
    created_at: float = 0.0

    rerun_attempts: int = 0
    episode_seed: int = 0
    procedural_mode: bool = False


def _extract_config_path_from_text(text: str) -> str:
    patterns = [
        r" in ([^:]+):\d+:",
        r"\b((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|ya?ml|txt|env))\b",
        r"\b(Dockerfile)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = match.group(1).strip().strip(".,)")
        candidate = candidate.replace("\\", "/")
        if candidate in KNOWN_CONFIG_PATHS or "/" in candidate or candidate in {"Dockerfile", ".env"}:
            return candidate
    return ""


def _resolve_episode_config_target(ep: EpisodeState, target: str) -> str:
    filepath = target.replace("\\", "/").lstrip("./")
    if not filepath:
        return filepath

    direct_path = os.path.join(ep.workspace_dir, filepath)
    if os.path.exists(direct_path):
        return filepath

    normalized_target = filepath.split("/")[-1].lower()

    if normalized_target in STAGE_ORDER:
        surfaced_errors = build_surfaced_errors(ep.pipeline_result or PipelineResult(), ep.workspace_dir)
        for err in surfaced_errors:
            candidate = _extract_config_path_from_text(str(err))
            if candidate and os.path.exists(os.path.join(ep.workspace_dir, candidate)):
                return candidate

        if ep.last_fix_result and ep.last_fix_result.files_modified:
            candidate = ep.last_fix_result.files_modified[0].replace("\\", "/")
            if os.path.exists(os.path.join(ep.workspace_dir, candidate)):
                return candidate

        if ep.fault_metadata:
            for candidate in ep.fault_metadata.affected_files:
                normalized_candidate = candidate.replace("\\", "/")
                if os.path.exists(os.path.join(ep.workspace_dir, normalized_candidate)):
                    return normalized_candidate

    for cfg in KNOWN_CONFIG_PATHS:
        if normalized_target == os.path.basename(cfg).lower() or normalized_target in cfg.lower():
            return cfg

    for root, dirs, files in os.walk(ep.workspace_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for file_name in files:
            if file_name.lower() == normalized_target:
                return os.path.relpath(
                    os.path.join(root, file_name), ep.workspace_dir
                ).replace("\\", "/")

    return filepath


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

        # Curriculum + adversarial always active together
        self._curriculum = CurriculumController()

        # Resolve the API key and base URL using the same provider logic as the agent,
        # so the designer works with whichever provider (groq, openrouter, hf) is active.
        _llm_provider = os.getenv("LLM_PROVIDER", "hf").strip().lower()

        def _server_resolve_api_key() -> str:
            explicit = (os.getenv("API_KEY") or "").strip()
            if explicit:
                return explicit
            if _llm_provider == "openrouter":
                return (
                    os.getenv("OPENROUTER_API_KEY")
                    or os.getenv("OPENAI_API_KEY")
                    or os.getenv("HF_TOKEN")
                    or os.getenv("GROQ_API_KEY")
                    or ""
                )
            if _llm_provider == "groq":
                return (
                    os.getenv("GROQ_API_KEY")
                    or os.getenv("OPENAI_API_KEY")
                    or os.getenv("HF_TOKEN")
                    or os.getenv("OPENROUTER_API_KEY")
                    or ""
                )
            return (
                os.getenv("HF_TOKEN")
                or os.getenv("OPENAI_API_KEY")
                or os.getenv("OPENROUTER_API_KEY")
                or os.getenv("GROQ_API_KEY")
                or ""
            )

        def _server_resolve_base_url() -> str:
            explicit = (os.getenv("CICD_ADV_BASE_URL") or os.getenv("API_BASE_URL") or "").strip()
            if explicit:
                return explicit
            if _llm_provider == "openrouter":
                return "https://openrouter.ai/api/v1"
            if _llm_provider == "groq":
                return "https://api.groq.com/openai/v1"
            return "https://router.huggingface.co/v1"

        self._adv_designer = AdversarialDesigner(
            api_key=_server_resolve_api_key(),
            base_url=_server_resolve_base_url(),
        )
        self._adv_judge = AdversarialJudge()

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

        # Curriculum controls difficulty + construction style (procedural vs LLM).
        # LLM adversarial designer is the SOLE source of fault selection.
        eff_task_key = task_key or self._task_key
        use_procedural_style = eff_task_key in ("procedural", "combo")

        curriculum_difficulty = self._curriculum.get_difficulty()
        skill_profile = self._curriculum.get_skill_profile()

        # Set up workspace
        workspace_base = tempfile.mkdtemp(prefix="cicd-episode-")
        repo_dir = os.path.join(workspace_base, "repo")

        # Initialize git repo from template
        setup_repo_from_template(self._template_dir, repo_dir)

        episode_seed = int(uuid.UUID(episode_id).int & 0xFFFFFFFF)

        # Curriculum UCB1 selects the seed fault based on past episode performance
        # (weakness score + exploration bonus), so the adversarial designer is steered
        # toward fault types the agent has struggled with most.
        seed_fault = self._curriculum.select_fault_type()
        adversarial_scenario = self._adv_designer.design(
            root_cause_fault=seed_fault,
            difficulty=curriculum_difficulty,
            skill_profile=skill_profile,
        )
        # If procedural style: regenerate the scenario using deterministic generator
        # with the LLM-chosen root cause, but keep the LLM's decision
        if use_procedural_style and adversarial_scenario.steps:
            root_cause_ft = adversarial_scenario.steps[0].fault_type
            procedural_scenario = procedural_generate_scenario(
                difficulty=curriculum_difficulty,
                seed=episode_seed,
                root_cause=root_cause_ft,
            )
            adversarial_scenario = procedural_scenario
            injected = inject_procedural(repo_dir, adversarial_scenario)
        else:
            # LLM-generated scenario as-is — but only inject cascade faults when
            # difficulty is high enough; otherwise strip them from the scenario
            # before injection so they never land in the workspace.
            if curriculum_difficulty < 0.65 and len(adversarial_scenario.steps) > 1:
                adversarial_scenario.steps = adversarial_scenario.steps[:1]
            injected = self._adv_designer.inject(repo_dir, adversarial_scenario)

        fault_type = adversarial_scenario.steps[0].fault_type if adversarial_scenario.steps else "merge_conflict"
        cascade_types = [s.fault_type for s in adversarial_scenario.steps[1:]] if adversarial_scenario.steps else []
        fail_stage = FAULT_STAGE_MAP.get(fault_type, "unknown")
        print(
            f"[EPISODE] fault={fault_type}  cascades={cascade_types}  "
            f"fail_stage={fail_stage}  difficulty={curriculum_difficulty:.2f}  "
            f"mode={'procedural' if use_procedural_style else 'llm-adversarial'}",
            flush=True,
        )

        fault_metadata = injected[0] if injected else inject_fault(repo_dir, fault_type)
        # Cascading faults only enabled for hard difficulty (curriculum >= 0.65).
        cascading_faults: list = injected[1:] if len(injected) > 1 else []

        # Run the pipeline — if injection silently failed and pipeline passes,
        # retry with a fresh deterministic fault (up to 2 retries) so every
        # episode has a real failure for the agent to debug.
        runner = create_pipeline_runner(
            workspace_path=repo_dir,
            fault_type=fault_type,
            scenario=adversarial_scenario,
            episode_id=episode_id,
            workspace_base=workspace_base,
            timeout_per_stage=self._pipeline_timeout,
        )
        pipeline_result = runner.run(workspace_dir=repo_dir)
        
        # Clean up Docker image after initial build
        cleanup_pipeline(pipeline_result)

        _retry = 0
        try:
            while pipeline_result.status == PipelineStatus.PASSED and _retry < 2:
                _retry += 1
                logger.warning(
                    "[reset] Initial pipeline passed after injection — fault may not have applied "
                    "(fault_type=%s). Retrying with deterministic fallback (attempt %d/2).",
                    fault_type, _retry,
                )
                fallback_fault = inject_fault(repo_dir, fault_type)
                if fallback_fault:
                    fault_metadata = fallback_fault
                pipeline_result = runner.run(workspace_dir=repo_dir)
                
                # Clean up Docker image after retry build
                cleanup_pipeline(pipeline_result)
        except Exception as _exc:
            logger.exception("[reset] Fault injection retry failed — continuing with current state.")

        if pipeline_result.status == PipelineStatus.PASSED:
            logger.error(
                "[reset] Pipeline still passing after %d retries for fault_type=%s. "
                "Episode will have no detectable failure — agent will likely score 0.",
                _retry, fault_type,
            )

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
            adversarial_scenario=adversarial_scenario,
            cascading_faults=cascading_faults,
            curriculum_difficulty=curriculum_difficulty,
            episode_seed=episode_seed,
            procedural_mode=use_procedural_style,
        )
        self._episode = episode

        # Build difficulty from fault type
        difficulty = self._get_difficulty(fault_type)

        # Build max_steps — single source of truth via _get_max_steps
        max_steps = self._get_max_steps(fault_type, cascade_count=len(cascading_faults))

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
        finalize_blocked = operation == "finalize" and not episode.verified_for_latest_rerun

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

        # Phase-aware bonus from adversarial judge (always active except blocked finalize)
        if episode.adversarial_scenario is not None and not finalize_blocked:
            phase_bonus, phase_note = self._adv_judge.score_step(
                operation=operation,
                value=value,
                scenario=episode.adversarial_scenario,
                history=episode.history,
            )
            reward += phase_bonus
            if phase_note:
                episode.findings.append(f"[Judge] {phase_note}")

        # Apply redundancy penalty
        if was_redundant and not finalize_blocked:
            reward = min(reward, -0.08)

        if finalize_blocked:
            reward = -0.05

        # Determine if episode is done
        done = False
        fault_type_for_steps = episode.fault_metadata.fault_type if episode.fault_metadata else "unknown"
        cascade_count_for_steps = len(episode.cascading_faults) if episode.cascading_faults else 0
        max_steps = self._get_max_steps(fault_type_for_steps, cascade_count=cascade_count_for_steps)

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
                ep.findings.append(f"Logs for stage '{stage}':\n{log_text[:300]}")
            else:
                # Show logs for the failed stage
                failed_stage = ep.pipeline_result.failed_stage
                if failed_stage:
                    log_text = build_stage_log_response(ep.pipeline_result, failed_stage)
                    ep.findings.append(f"Logs for failed stage '{failed_stage}':\n{log_text[:300]}")

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

        # If target specified, only read that file; otherwise read all
        if target:
            filepath = _resolve_episode_config_target(ep, target)
            content = read_workspace_file(ep.workspace_dir, filepath)
            configs = {filepath: content}
        else:
            configs = read_config_files(ep.workspace_dir)

        for filename, content in configs.items():
            # Extract conflict markers and surrounding lines for clarity
            lines = content.splitlines()
            conflict_indices = []
            for i, line in enumerate(lines):
                if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
                    conflict_indices.append(i)

            if conflict_indices:
                # Show conflict with context (±2 lines)
                start_ctx = max(0, conflict_indices[0] - 2)
                end_ctx = min(len(lines), conflict_indices[-1] + 3)
                conflict_section = "\n".join(
                    f"  {i:3d}: {line}" for i, line in enumerate(lines[start_ctx:end_ctx], start_ctx + 1)
                )
                ep.findings.append(f"⚠ MERGE CONFLICT in '{filename}':\n{conflict_section}")
            else:
                # No conflict — show first portion
                ep.findings.append(f"Config file '{filename}':\n{content[:400]}")

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
        ep.findings.append(f"Dockerfile content:\n{content[:300]}")

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

        hypothesis_lower = value.lower()

        # Hypothesis is scored ONLY against the root cause fault's keywords.
        # Accepting cascade-fault keywords here would give false-positive rewards
        # when the agent guesses a secondary fault instead of the root cause,
        # corrupting both the learning signal and the rubric score.
        if ep.adversarial_scenario is not None:
            # Use the scenario's declared hypothesis terms (root cause only)
            keywords = list(ep.adversarial_scenario.expected_hypothesis_terms)
            # Also accept the root cause fault's own keyword list as a fallback
            root_fault_type = ep.fault_metadata.fault_type
            for kw in FAULT_KEYWORDS.get(root_fault_type, []):
                if kw not in keywords:
                    keywords.append(kw)
        else:
            keywords = ep.fault_metadata.keywords or FAULT_KEYWORDS.get(ep.fault_metadata.fault_type, [])

        match_count = sum(1 for kw in keywords if kw.lower() in hypothesis_lower)
        match_ratio = match_count / max(len(keywords), 1)

        # Also check if affected files are mentioned
        file_mentioned = any(
            os.path.basename(f).lower() in hypothesis_lower
            for f in ep.fault_metadata.affected_files
        )

        # 1+ keyword OR file mentioned = correct (lenient to avoid misleading the agent)
        if match_count >= 1 or file_mentioned:
            ep.hypothesis_correct = True
            ep.findings.append(f"Hypothesis partially aligns (matched {match_count}/{len(keywords)} keywords). Apply the fix.")
            if ep.hypothesis_attempts == 1:
                # Scale base reward by difficulty: more keywords = harder problem = higher ceiling.
                # A 7-keyword fault deserves more reward for a good match than a 3-keyword fault.
                difficulty_scale = min(1.5, max(1.0, len(keywords) / 4.0))
                base_high = round(min(0.25, 0.18 * difficulty_scale), 3)
                base_low  = round(min(0.18, 0.12 * difficulty_scale), 3)
                return base_high if match_ratio >= 0.4 else base_low
            # Subsequent attempts: reward scales down but still difficulty-aware
            difficulty_scale = min(1.4, max(1.0, len(keywords) / 4.0))
            return round(min(0.12, 0.08 * difficulty_scale), 3) if match_ratio >= 0.4 else round(min(0.08, 0.05 * difficulty_scale), 3)
        else:
            # Give a hint using ALL root-cause keywords (not just first 3) so the agent
            # isn't playing a truncated guessing game on high-keyword faults.
            root_fault = ep.fault_metadata.fault_type
            hint_keywords = FAULT_KEYWORDS.get(root_fault, keywords)
            ep.findings.append("Hypothesis does not match current evidence. Try mentioning: " + ", ".join(hint_keywords))
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

        # Apply the fix — pass fault_type so the engine can route directly
        # without relying on keyword matching of the agent's value string.
        fault_type = ep.fault_metadata.fault_type if ep.fault_metadata else ""
        fix_result = apply_fix(ep.workspace_dir, value, target, fault_type=fault_type)
        ep.last_fix_result = fix_result

        if fix_result.success:
            ep.findings.append(f"Fix applied: {fix_result.description}")
            ep.pending_fix_outcome = "applied"
            ep.errors_stale_after_fix = True
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
        ep.rerun_attempts += 1

        if not ep.pipeline_runner:
            ep.findings.append("No pipeline runner available for rerun.")
            return -0.10

        # Run the pipeline again from current workspace state
        old_status = ep.pipeline_result.status if ep.pipeline_result else PipelineStatus.FAILED
        old_failed_stage = ep.pipeline_result.failed_stage if ep.pipeline_result else ""

        new_result = ep.pipeline_runner.run(workspace_dir=ep.workspace_dir)
        ep.all_pipeline_results.append(new_result)
        ep.pipeline_result = new_result
        ep.errors_stale_after_fix = False

        cleanup_pipeline(new_result)

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
                msg = "Pipeline still failing at the same stage."
                # If cascading faults are present, the same-stage failure may be
                # caused by the next fault in the chain — not the one just fixed.
                if ep.cascading_faults:
                    msg += (
                        " This incident has cascading faults — the previous fix may have "
                        "resolved the first fault but exposed a second independent fault. "
                        "Re-read surfaced_errors now and treat the new error as a fresh root cause."
                    )
                ep.findings.append(msg)
        elif old_status == PipelineStatus.FAILED and new_result.status == PipelineStatus.FAILED:
            msg = "Rerun shows failure unchanged; refine diagnosis."
            if ep.cascading_faults:
                msg += (
                    " Note: cascading faults are active — if errors changed, this is a new fault."
                )
            ep.findings.append(msg)

        ep.last_rerun_progressed = progressed

        if progressed:
            ep.fix_hits += 1
            if new_result.status == PipelineStatus.PASSED:
                ep.findings.append(
                    "Pipeline PASSED. Call verify_fix next (required before finalize) — "
                    "then immediately call finalize. Do not apply more fixes."
                )
            else:
                ep.findings.append(
                    "Pipeline progressed but is not yet passing. Call verify_fix next, "
                    "then continue investigation if unresolved."
                )
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

        if ep.rerun_attempts == 0:
            ep.findings.append(
                "Cannot verify: run rerun_pipeline after applying a fix before calling verify_fix."
            )
            return -0.06

        if ep.pipeline_result.status == PipelineStatus.PASSED:
            ep.incident_resolved = True
            ep.verified_for_latest_rerun = True
            ep.findings.append(
                "VERIFIED: fix resolved the incident. "
                "Call finalize now — do NOT apply further fixes or reruns."
            )
            return 0.16
        elif ep.last_rerun_progressed:
            ep.verified_for_latest_rerun = True
            ep.findings.append("Verification confirms partial progress.")
            return 0.08
        else:
            ep.findings.append("Verification failed: pipeline still shows unresolved failures.")
            return -0.06

    def _handle_finalize(self, ep: EpisodeState) -> float:
        """Compute final score — adversarial terminal scorer always used."""
        if not ep.verified_for_latest_rerun:
            ep.findings.append("Run verify_fix before finalize")
            return -0.05

        if ep.rerun_attempts == 0:
            ep.findings.append(
                "Finalize without running rerun_pipeline — no terminal bonus awarded."
            )
            return 0.05

        pipeline_passed = (
            ep.pipeline_result is not None
            and ep.pipeline_result.status == PipelineStatus.PASSED
        )
        bonus, note = self._adv_judge.score_terminal(
            incident_resolved=ep.incident_resolved,
            verified=ep.verified_for_latest_rerun,
            pipeline_passed=pipeline_passed,
            cascading_fault_count=len(ep.cascading_faults),
        )
        ep.findings.append(f"[Judge] terminal: {note}")
        return bonus

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _get_difficulty(self, fault_type: str) -> str:
        difficulty_map = {
            "merge_conflict":      "easy",
            "dependency_conflict": "medium",
            "docker_order":        "medium",
            "flaky_test":          "easy",
            "missing_permission":  "hard",
            "secret_exposure":     "security",
            "env_drift":           "network",
            "log_pii_leak":        "security",
            "log_disabled":        "medium",
            "bad_migration_sql":   "medium",
            "schema_drift":        "medium",
        }
        return difficulty_map.get(fault_type, "medium")

    def _get_max_steps(self, fault_type: str, cascade_count: int = 0) -> int:
        """Single source of truth for max_steps — used by both reset() and step()."""
        difficulty = self._get_difficulty(fault_type)
        max_steps_map = {"easy": 16, "medium": 20, "network": 20, "security": 20, "hard": 25}
        base = max_steps_map.get(difficulty, 20)
        # Each cascading fault adds 4 steps — the agent must debug multiple independent faults.
        return base + cascade_count * 4

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

        # Compute terminal scoring on done
        final_score = 0.0
        deterministic_score = 0.0
        rubric_score = 0.0
        delayed_reward = 0.0
        rubric_judge_used = False
        rubric_judge_error = ""
        if done:
            deterministic_score = self._compute_final_score(ep)
            final_score = deterministic_score
            if self._rubric_judge.is_active():
                try:
                    judge_result = self._rubric_judge.evaluate_hypothesis_quality(
                        self._build_rubric_payload(ep, fault_type, difficulty)
                    )
                    rubric_score = float(judge_result.score)
                    final_score = round(
                        ((1.0 - self._rubric_weight) * deterministic_score)
                        + (self._rubric_weight * rubric_score),
                        3,
                    )
                    delayed_reward = round(final_score - deterministic_score, 3)
                    reward = round(reward + delayed_reward, 3)
                    rubric_judge_used = not judge_result.used_fallback
                    rubric_judge_error = str(judge_result.error or "")
                except Exception as exc:
                    rubric_judge_error = f"Rubric judge failed: {exc}"

            ep.deterministic_score = deterministic_score
            ep.rubric_score = rubric_score
            ep.delayed_reward = delayed_reward
            ep.rubric_judge_used = rubric_judge_used
            ep.rubric_judge_error = rubric_judge_error

            # Record outcome in curriculum for next episode scheduling
            self._curriculum.record_episode(
                fault_type=fault_type,
                difficulty=ep.curriculum_difficulty,
                final_score=final_score,
                resolved=ep.incident_resolved,
                steps_used=self._state.step_count,
            )

        pipeline_result = ep.pipeline_result or PipelineResult()

        obs = build_observation(
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
            deterministic_score=deterministic_score,
            rubric_score=rubric_score,
            delayed_reward=delayed_reward,
            rubric_blend_weight=self._rubric_weight if self._rubric_judge.is_active() else 0.0,
            rubric_judge_used=rubric_judge_used,
            rubric_judge_error=rubric_judge_error,
            findings=ep.findings[-10:],
            metadata={
                "task_key": fault_type,
                "fault_type": fault_type,
                "expected_fail_stage": ep.fault_metadata.expected_fail_stage if ep.fault_metadata else "",
                "ready_to_finalize": ep.incident_resolved and ep.verified_for_latest_rerun,
                "verification_required": ep.incident_resolved and not ep.verified_for_latest_rerun,
                "verified_since_last_rerun": ep.verified_for_latest_rerun,
                "supported_operations": CANONICAL_OPERATIONS,
                "canonical_operations": CANONICAL_OPERATIONS,
                "rubric_enabled": self._rubric_judge.is_active(),
            },
        )

        if ep.errors_stale_after_fix:
            obs["surfaced_errors"] = [
                "[Errors from previous run — rerun pipeline to see current state]"
            ]

        return obs

    def _build_rubric_payload(self, ep: EpisodeState, fault_type: str, difficulty: str) -> Dict[str, Any]:
        keywords = ep.fault_metadata.keywords if ep.fault_metadata else []
        return {
            "task_id": f"real_{fault_type}",
            "difficulty": difficulty,
            "evidence": {
                "hypothesis_history": ep.hypothesis_history[-8:],
                "current_hypothesis": ep.current_hypothesis,
                "findings": ep.findings[-10:],
                "surfaced_errors": build_surfaced_errors(ep.pipeline_result or PipelineResult(), ep.workspace_dir),
                "incident_resolved": ep.incident_resolved,
            },
        )

        if ep.errors_stale_after_fix:
            obs["surfaced_errors"] = [
                "[Errors from previous run — rerun pipeline to see current state]"
            ]

        return obs

    def _score_step_reward(
        self,
        ep: EpisodeState,
        *,
        operation: str,
        target: str,
        value: str,
        was_redundant: bool,
        finalize_blocked: bool,
    ) -> tuple[float, Dict[str, Any]]:
        raw_score = 0.5
        rationale_parts: List[str] = []

        if was_redundant:
            raw_score -= 0.20
            rationale_parts.append("redundant action")

        if operation in {"view_logs", "tail_logs", "inspect_config", "inspect_dockerfile", "inspect_permissions"}:
            raw_score += 0.10
            rationale_parts.append("evidence gathering")

        if operation == "set_hypothesis":
            if ep.hypothesis_correct:
                raw_score += 0.15
                rationale_parts.append("hypothesis aligns with fault evidence")
            else:
                raw_score -= 0.10
                rationale_parts.append("hypothesis weakly supported")

        if operation in {"modify_config", "add_dependency"}:
            if self._is_destructive_fix(value):
                raw_score -= 0.35
                rationale_parts.append("destructive fix")
            elif ep.pending_fix_outcome == "applied":
                raw_score += 0.20
                rationale_parts.append("fix applied")
            else:
                raw_score -= 0.10
                rationale_parts.append("fix not applied")

        if operation == "rerun_pipeline":
            if ep.last_fix_result and ep.last_fix_result.success:
                raw_score += 0.15
                rationale_parts.append("validated recent fix")
            elif not ep.inspected_since_last_rerun:
                raw_score -= 0.15
                rationale_parts.append("rerun without new evidence")

        if operation == "verify_fix":
            if ep.rerun_attempts == 0:
                raw_score -= 0.25
                rationale_parts.append("verify before rerun")
            elif ep.last_rerun_progressed or ep.incident_resolved:
                raw_score += 0.20
                rationale_parts.append("verification after progress")

        if operation == "finalize":
            if finalize_blocked:
                raw_score -= 0.35
                rationale_parts.append("finalize before verification")
            elif ep.verified_for_latest_rerun:
                raw_score += 0.20
                rationale_parts.append("properly finalized")

        raw_score = max(0.0, min(1.0, raw_score))
        reward = round((raw_score - 0.5) * 0.5, 3)
        if finalize_blocked:
            reward = min(reward, -0.15)
        trace = {
            "step": self._state.step_count,
            "operation": operation,
            "target": target,
            "value": value,
            "raw_score": round(raw_score, 3),
            "reward": reward,
            "rationale": "; ".join(rationale_parts) if rationale_parts else "deterministic baseline score",
            "source": "deterministic_rule_based",
            "used_fallback": True,
            "error": "",
        }
        return reward, trace

    def _compute_final_score(self, ep: EpisodeState) -> float:
        if not ep.step_prm_scores:
            return 0.0

        weighted_total = 0.0
        weight_sum = 0.0
        for item in ep.step_reward_trace:
            op = str(item.get("operation", ""))
            weight = 1.0
            if op in {"modify_config", "add_dependency", "rerun_pipeline", "verify_fix", "finalize"}:
                weight = 1.2
            if float(item.get("raw_score", 0.0) or 0.0) < 0.35:
                weight += 0.1
            weighted_total += float(item.get("raw_score", 0.0) or 0.0) * weight
            weight_sum += weight

        score = weighted_total / max(weight_sum, 1.0)
        success_bonus = 0.0
        if ep.incident_resolved and ep.verified_for_latest_rerun:
            success_bonus = 0.08
        elif ep.last_rerun_progressed:
            success_bonus = 0.03

        penalty = min(0.25, (ep.redundant_actions * 0.04) + (ep.destructive_actions * 0.12) + (ep.wrong_fixes * 0.05))
        score = (score - penalty) * ep.pipeline_health

        genuine_work = ep.fix_hits > 0 and ep.rerun_attempts > 0
        if not genuine_work:
            score = min(score, 0.15)

        final = round(max(0.0, min(1.0, score)), 3)
        logger.debug(
            "[score] ep=%s genuine=%s resolved=%s verified=%s fix_hits=%d reruns=%d "
            "wrong=%d destructive=%d redundant=%d health=%.2f raw=%.3f final=%.3f",
            ep.episode_id[:8],
            genuine_work,
            ep.incident_resolved,
            ep.verified_for_latest_rerun,
            ep.fix_hits,
            ep.rerun_attempts,
            ep.wrong_fixes,
            ep.destructive_actions,
            ep.redundant_actions,
            ep.pipeline_health,
            score,
            final,
        )
        return final

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

        # Remove the shared cache image now that the episode is fully done.
        if ep.pipeline_runner:
            try:
                cleanup_cache_image(ep.pipeline_runner._cache_tag)
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

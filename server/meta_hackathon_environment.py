# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Meta Hackathon CI/CD pipeline repair environment implementation."""

import os
from uuid import uuid4

from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import State

try:
    from .graders import action_key, grade_episode, matches_terms, step_reward
    from .scenarios import (
        CANONICAL_OPERATIONS,
        DESTRUCTIVE_FIXES,
        SAFE_FIXES,
        STAGE_ORDER,
        SUPPORTED_OPERATIONS,
        IncidentStep,
        ScenarioCard,
        ScenarioVariant,
        canonical_operation,
        get_scenario,
        list_task_keys,
    )
    from ..models import MetaHackathonAction, MetaHackathonObservation
except ImportError:
    from server.graders import action_key, grade_episode, matches_terms, step_reward
    from server.scenarios import (
        CANONICAL_OPERATIONS,
        DESTRUCTIVE_FIXES,
        SAFE_FIXES,
        STAGE_ORDER,
        SUPPORTED_OPERATIONS,
        IncidentStep,
        ScenarioCard,
        ScenarioVariant,
        canonical_operation,
        get_scenario,
        list_task_keys,
    )
    from models import MetaHackathonAction, MetaHackathonObservation


class MetaHackathonEnvironment(Environment):
    """Deterministic CI/CD repair simulator with iterative staged debugging."""

    SUPPORTS_CONCURRENT_SESSIONS: bool = True

    def __init__(self, task_key: str = ""):
        self._task_order = list_task_keys()
        requested_task = (
            task_key
            or os.getenv("META_HACKATHON_TASK_MODE", "")
            or os.getenv("META_HACKATHON_TASK", "")
            or "cycle"
        ).strip()
        if requested_task not in {*self._task_order, "cycle"}:
            requested_task = "cycle"

        self._task_key = requested_task
        self._task_cursor = 0
        self._variant_cursors: dict[str, int] = {key: 0 for key in self._task_order}
        initial_key = self._task_order[0] if self._task_key == "cycle" else self._task_key

        self._scenario: ScenarioCard = get_scenario(initial_key)
        self._state = State(episode_id=str(uuid4()), step_count=0)

        self._history: list[dict[str, str]] = []
        self._action_keys: set[str] = set()
        self._findings: list[str] = []
        self._visible_logs: list[str] = []
        self._visible_metrics: list[str] = []
        self._logs_by_stage: dict[str, list[str]] = {stage: [] for stage in STAGE_ORDER}
        self._surface_errors: list[str] = []

        self._current_hypothesis = ""
        self._hypothesis_history: list[str] = []
        self._attempted_fix = ""
        self._pending_fix_outcome = "none"

        self._current_issue_index = 0
        self._solved_issues = 0
        self._hypothesis_hits = 0
        self._family_hits = 0
        self._fix_hits = 0
        self._used_inspections: set[str] = set()
        self._hypothesis_hit_issues: set[int] = set()
        self._family_hit_issues: set[int] = set()
        self._fix_hit_issues: set[int] = set()

        self._incident_resolved = False
        self._pipeline_status = "failed"
        self._pipeline_stages = {stage: "pending" for stage in STAGE_ORDER}

        self._pipeline_health = 1.0
        self._recovery_cost = 0
        self._redundant_actions = 0
        self._destructive_actions = 0
        self._wrong_fixes = 0

        self._variant_selector = 0
        self._scenario_variant: ScenarioVariant | None = None
        self._config_files: dict[str, str] = {}

    def _current_issue(self) -> IncidentStep:
        index = min(self._current_issue_index, len(self._scenario.incident_chain) - 1)
        return self._scenario.incident_chain[index]

    def _compute_required_inspections(self) -> set[str]:
        required = {"view_logs", "inspect_config"}
        for issue in self._scenario.incident_chain:
            if any("docker" in clue.lower() for clue in issue.docker_clues):
                required.add("inspect_dockerfile")
            if any("role" in clue.lower() or "permission" in clue.lower() for clue in issue.permission_clues):
                required.add("inspect_permissions")
        return required

    def _base_observation(self, *, reward: float, done: bool, metadata: dict | None = None) -> MetaHackathonObservation:
        action_history = [
            f"{entry['operation']}:{entry.get('target', '')}:{entry.get('value', '')}".strip(":")
            for entry in self._history[-16:]
        ]

        return MetaHackathonObservation(
            task_id=self._scenario.task_id,
            task_title=self._scenario.task_title,
            difficulty=self._scenario.difficulty,
            pipeline_status=self._pipeline_status,
            current_stage=self._current_issue().stage,
            pipeline_stages=dict(self._pipeline_stages),
            available_stages=list(STAGE_ORDER),
            available_tools=list(CANONICAL_OPERATIONS),
            visible_alerts=[
                f"{self._scenario.pipeline_alert} {self._current_variant().pipeline_alert_suffix}".strip()
            ],
            visible_logs=self._visible_logs[-14:],
            logs_by_stage={stage: logs[-8:] for stage, logs in self._logs_by_stage.items()},
            visible_metrics=self._visible_metrics[-14:],
            config_files=dict(self._config_files),
            surfaced_errors=self._surface_errors[-6:],
            findings=self._findings[-16:],
            action_history=action_history,
            previous_actions=action_history,
            current_hypothesis=self._current_hypothesis,
            attempted_fix=self._attempted_fix,
            hypothesis_history=self._hypothesis_history[-8:],
            active_issue_index=self._current_issue_index,
            revealed_issue_count=min(self._current_issue_index + 1, len(self._scenario.incident_chain)),
            pipeline_health=round(self._pipeline_health, 3),
            recovery_cost=self._recovery_cost,
            redundant_actions=self._redundant_actions,
            destructive_actions=self._destructive_actions,
            incident_resolved=self._incident_resolved,
            final_score=0.0,
            reward=reward,
            done=done,
            metadata=metadata or {},
        )

    def _append_unique(self, bucket: list[str], value: str) -> bool:
        if value in bucket:
            return False
        bucket.append(value)
        return True

    def _set_stage_failure(self, stage: str) -> None:
        for known_stage in STAGE_ORDER:
            if STAGE_ORDER.index(known_stage) < STAGE_ORDER.index(stage):
                self._pipeline_stages[known_stage] = "passed"
            elif known_stage == stage:
                self._pipeline_stages[known_stage] = "failed"
            else:
                self._pipeline_stages[known_stage] = "blocked"

    def _set_stage_progress_after_advance(self) -> None:
        if self._incident_resolved:
            self._pipeline_status = "healthy"
            for stage in STAGE_ORDER:
                self._pipeline_stages[stage] = "passed"
            return

        issue = self._current_issue()
        self._pipeline_status = "failed"
        self._set_stage_failure(issue.stage)

    def _variant_logs_for_issue(self, issue: IncidentStep) -> list[str]:
        if not issue.log_variants:
            return []
        choice = self._variant_selector % len(issue.log_variants)
        return issue.log_variants[choice]

    def _current_variant(self) -> ScenarioVariant:
        if self._scenario_variant is None:
            return self._scenario.variants[0]
        return self._scenario_variant

    def _is_destructive_fix(self, value: str) -> bool:
        normalized = (value or "").strip().lower()
        if not normalized:
            return False
        for phrase in DESTRUCTIVE_FIXES:
            if phrase in normalized:
                return True
        return False

    def _assess_fix(self, issue: IncidentStep, operation: str, value: str) -> str:
        if self._is_destructive_fix(value):
            return "destructive"

        if operation == issue.correct_operation and matches_terms(value, issue.correct_fix_terms):
            return "correct"

        for partial_terms in issue.partial_fix_terms:
            if matches_terms(value, partial_terms):
                return "partial"

        return "wrong"

    def _handle_view_logs(self) -> bool:
        issue = self._current_issue()
        revealed = False
        for line in self._variant_logs_for_issue(issue):
            revealed = self._append_unique(self._visible_logs, line) or revealed
            revealed = self._append_unique(self._logs_by_stage[issue.stage], line) or revealed
            if revealed:
                self._append_unique(self._findings, line)
        revealed = self._append_unique(self._surface_errors, issue.ambiguous_error) or revealed
        for line in self._current_variant().extra_log_lines:
            revealed = self._append_unique(self._visible_logs, line) or revealed
            revealed = self._append_unique(self._logs_by_stage[issue.stage], line) or revealed
            revealed = self._append_unique(self._findings, line) or revealed
        return revealed

    def _handle_inspect_config(self) -> bool:
        issue = self._current_issue()
        revealed = False
        for clue in issue.config_clues:
            revealed = self._append_unique(self._visible_metrics, clue) or revealed
            revealed = self._append_unique(self._findings, clue) or revealed
        for clue in self._current_variant().extra_config_clues:
            revealed = self._append_unique(self._visible_metrics, clue) or revealed
            revealed = self._append_unique(self._findings, clue) or revealed
        for file_name, content in self._scenario.config_templates.items():
            existing = self._config_files.get(file_name)
            if existing != content:
                self._config_files[file_name] = content
                revealed = True
        return revealed

    def _handle_inspect_dockerfile(self) -> bool:
        issue = self._current_issue()
        revealed = False
        for clue in issue.docker_clues:
            revealed = self._append_unique(self._visible_metrics, clue) or revealed
            revealed = self._append_unique(self._findings, clue) or revealed
        if "Dockerfile" in self._scenario.config_templates:
            self._config_files["Dockerfile"] = self._scenario.config_templates["Dockerfile"]
            revealed = True
        return revealed

    def _handle_inspect_permissions(self) -> bool:
        issue = self._current_issue()
        revealed = False
        for clue in issue.permission_clues:
            revealed = self._append_unique(self._visible_metrics, clue) or revealed
            revealed = self._append_unique(self._findings, clue) or revealed
        if "iam.txt" in self._scenario.config_templates:
            self._config_files["iam.txt"] = self._scenario.config_templates["iam.txt"]
            revealed = True
        return revealed

    def reset(self) -> MetaHackathonObservation:
        self._state = State(episode_id=str(uuid4()), step_count=0)

        if self._task_key == "cycle":
            selected_key = self._task_order[self._task_cursor % len(self._task_order)]
            self._task_cursor += 1
        else:
            selected_key = self._task_key

        self._scenario = get_scenario(selected_key)

        self._history = []
        self._action_keys = set()
        self._findings = ["Incident acknowledged. Investigate before changing configuration."]
        self._visible_logs = []
        self._visible_metrics = list(self._scenario.initial_metrics)
        self._logs_by_stage = {stage: [] for stage in STAGE_ORDER}
        self._surface_errors = []

        self._current_hypothesis = ""
        self._hypothesis_history = []
        self._attempted_fix = ""
        self._pending_fix_outcome = "none"

        self._current_issue_index = 0
        self._solved_issues = 0
        self._hypothesis_hits = 0
        self._family_hits = 0
        self._fix_hits = 0
        self._used_inspections = set()
        self._hypothesis_hit_issues = set()
        self._family_hit_issues = set()
        self._fix_hit_issues = set()

        self._incident_resolved = False
        self._pipeline_status = "failed"
        self._pipeline_stages = {stage: "pending" for stage in STAGE_ORDER}

        self._pipeline_health = 1.0
        self._recovery_cost = 0
        self._redundant_actions = 0
        self._destructive_actions = 0
        self._wrong_fixes = 0

        current_variant_cursor = self._variant_cursors.get(selected_key, 0)
        variant_index = current_variant_cursor % len(self._scenario.variants)
        self._variant_cursors[selected_key] = current_variant_cursor + 1
        self._variant_selector = variant_index
        self._scenario_variant = self._scenario.variants[variant_index]
        self._config_files = {}

        self._set_stage_progress_after_advance()
        self._handle_view_logs()

        return self._base_observation(
            reward=0.0,
            done=False,
            metadata={
                "task_key": selected_key,
                "max_steps": self._scenario.max_steps,
                "variant_id": self._current_variant().variant_id,
                "supported_operations": SUPPORTED_OPERATIONS,
                "canonical_operations": CANONICAL_OPERATIONS,
            },
        )

    def step(self, action: MetaHackathonAction) -> MetaHackathonObservation:  # type: ignore[override]
        self._state.step_count += 1

        raw_operation = (action.operation or "").strip()
        operation = canonical_operation(raw_operation)
        target = (action.target or "").strip()
        value = (action.value or "").strip()

        if operation not in CANONICAL_OPERATIONS:
            return self._base_observation(
                reward=-0.20,
                done=False,
                metadata={
                    "error": f"unsupported operation '{raw_operation}'",
                    "supported_operations": SUPPORTED_OPERATIONS,
                    "canonical_operations": CANONICAL_OPERATIONS,
                },
            )

        was_done_before_step = self._incident_resolved
        stage_before = self._current_issue().stage if not self._incident_resolved else STAGE_ORDER[-1]
        issue_before = self._current_issue_index

        history_entry = {"operation": operation, "target": target, "value": value}
        self._history.append(history_entry)
        key = action_key(history_entry)
        was_redundant = key in self._action_keys
        if was_redundant:
            self._redundant_actions += 1

        revealed_new_evidence = False
        hypothesis_correct_for_issue = False
        fix_correct_for_issue = False
        fix_partial_for_issue = False
        is_destructive_fix = False
        finalized_success = False
        finalized_failure = False
        blind_fix_attempt = False
        premature_finalize = False

        issue = self._current_issue()

        if operation == "view_logs":
            revealed_new_evidence = self._handle_view_logs()
            self._used_inspections.add(operation)

        elif operation == "inspect_config":
            revealed_new_evidence = self._handle_inspect_config()
            self._used_inspections.add(operation)

        elif operation == "inspect_dockerfile":
            revealed_new_evidence = self._handle_inspect_dockerfile()
            self._used_inspections.add(operation)

        elif operation == "inspect_permissions":
            revealed_new_evidence = self._handle_inspect_permissions()
            self._used_inspections.add(operation)

        elif operation == "set_hypothesis":
            self._current_hypothesis = value
            self._hypothesis_history.append(value)
            hypothesis_correct_for_issue = matches_terms(value, issue.hypothesis_terms)
            if hypothesis_correct_for_issue and self._current_issue_index not in self._hypothesis_hit_issues:
                self._hypothesis_hits += 1
                self._hypothesis_hit_issues.add(self._current_issue_index)
                self._append_unique(self._findings, "Hypothesis aligns with current failure evidence.")
            elif not hypothesis_correct_for_issue:
                self._append_unique(self._findings, "Hypothesis does not explain all current clues yet.")

            if not hypothesis_correct_for_issue and self._current_issue_index not in self._family_hit_issues:
                family_hit = any(matches_terms(value, term_set) for term_set in issue.family_term_sets)
                if family_hit:
                    self._family_hits += 1
                    self._family_hit_issues.add(self._current_issue_index)
                    self._append_unique(self._findings, "Hypothesis captures failure family but lacks full root-cause precision.")

        elif operation in {"modify_config", "add_dependency"}:
            self._attempted_fix = value
            if not self._used_inspections:
                blind_fix_attempt = True
                self._append_unique(self._findings, "Fix attempted before inspection evidence collection.")
            fix_assessment = self._assess_fix(issue, operation, value)
            if fix_assessment == "destructive":
                is_destructive_fix = True
                self._pending_fix_outcome = "destructive"
                self._destructive_actions += 1
                self._pipeline_health = max(0.0, self._pipeline_health - 0.20)
                self._recovery_cost += 4
                self._append_unique(self._findings, "Unsafe fix worsened system stability and increased recovery cost.")
            elif fix_assessment == "correct":
                fix_correct_for_issue = True
                self._pending_fix_outcome = "correct"
                self._append_unique(self._findings, "Fix candidate accepted; rerun pipeline to validate progression.")
            elif fix_assessment == "partial":
                fix_partial_for_issue = True
                self._pending_fix_outcome = "partial"
                self._pipeline_health = max(0.0, self._pipeline_health - 0.04)
                self._recovery_cost += 1
                if issue.partial_fix_reveal:
                    self._append_unique(self._findings, issue.partial_fix_reveal)
            else:
                self._pending_fix_outcome = "wrong"
                self._wrong_fixes += 1
                self._pipeline_health = max(0.0, self._pipeline_health - 0.10)
                self._recovery_cost += 2
                self._append_unique(self._findings, "Fix attempt did not resolve the active failure.")

        elif operation == "rerun_pipeline":
            self._recovery_cost += 1
            if self._pending_fix_outcome == "correct":
                if self._current_issue_index not in self._fix_hit_issues:
                    self._fix_hits += 1
                    self._fix_hit_issues.add(self._current_issue_index)
                self._solved_issues += 1
                self._current_issue_index += 1
                if self._current_issue_index >= len(self._scenario.incident_chain):
                    self._incident_resolved = True
                else:
                    self._append_unique(self._surface_errors, self._current_issue().ambiguous_error)
                    self._append_unique(self._findings, "Partial recovery revealed another downstream issue.")
                self._pending_fix_outcome = "none"
            elif self._pending_fix_outcome == "partial":
                if self._current_issue_index + 1 < len(self._scenario.incident_chain):
                    self._current_issue_index += 1
                    self._append_unique(self._surface_errors, self._current_issue().ambiguous_error)
                    self._append_unique(self._findings, "Partial fix exposed a new failure mode.")
                else:
                    self._append_unique(self._findings, "Partial fix is insufficient; additional remediation required.")
                self._pending_fix_outcome = "none"
            elif self._pending_fix_outcome == "destructive":
                self._append_unique(self._findings, "Rerun confirms degradation from unsafe change.")
                self._pending_fix_outcome = "none"
            elif self._pending_fix_outcome == "wrong":
                self._append_unique(self._findings, "Rerun shows failure unchanged; refine diagnosis.")
                self._pending_fix_outcome = "none"
            else:
                self._append_unique(self._findings, "Rerun without remediation did not improve pipeline health.")

        elif operation == "finalize":
            if self._solved_issues < len(self._scenario.incident_chain):
                premature_finalize = True
            if self._incident_resolved:
                finalized_success = True
                self._append_unique(self._findings, self._scenario.final_success_message)
            else:
                finalized_failure = True
                self._append_unique(self._findings, "Finalization rejected: unresolved stages remain.")

        self._set_stage_progress_after_advance()

        stage_after = self._current_issue().stage if not self._incident_resolved else STAGE_ORDER[-1]
        stage_advanced = STAGE_ORDER.index(stage_after) > STAGE_ORDER.index(stage_before)
        issue_advanced = self._current_issue_index > issue_before

        reward = step_reward(
            operation=operation,
            was_redundant=was_redundant,
            revealed_new_evidence=revealed_new_evidence,
            hypothesis_correct_for_issue=hypothesis_correct_for_issue,
            fix_correct_for_issue=fix_correct_for_issue,
            fix_partial_for_issue=fix_partial_for_issue,
            is_destructive_fix=is_destructive_fix,
            stage_advanced=stage_advanced,
            issue_advanced=issue_advanced,
            finalized_success=finalized_success,
            finalized_failure=finalized_failure,
            blind_fix_attempt=blind_fix_attempt,
            premature_finalize=premature_finalize,
        )

        self._action_keys.add(key)

        done = False
        if operation == "finalize":
            done = True
        if self._state.step_count >= self._scenario.max_steps:
            done = True
        if was_done_before_step and operation != "finalize":
            done = True

        obs = self._base_observation(
            reward=reward,
            done=done,
            metadata={
                "task_key": (
                    self._task_order[(self._task_cursor - 1) % len(self._task_order)]
                    if self._task_key == "cycle"
                    else self._task_key
                ),
                "expected_fixes": SAFE_FIXES,
                "destructive_fixes": DESTRUCTIVE_FIXES,
                "max_steps": self._scenario.max_steps,
                "variant_id": self._current_variant().variant_id,
                "active_issue_stage": self._current_issue().stage,
                "supported_operations": SUPPORTED_OPERATIONS,
                "canonical_operations": CANONICAL_OPERATIONS,
            },
        )

        if done:
            final_score = grade_episode(
                issue_count=len(self._scenario.incident_chain),
                solved_issues=self._solved_issues,
                required_inspection_actions=self._compute_required_inspections(),
                used_inspection_actions=self._used_inspections,
                hypothesis_hits=self._hypothesis_hits,
                family_hits=self._family_hits,
                fix_hits=self._fix_hits,
                final_resolved=self._incident_resolved,
                action_count=len(self._history),
                max_steps=self._scenario.max_steps,
                redundant_actions=self._redundant_actions,
                destructive_actions=self._destructive_actions,
                pipeline_health=self._pipeline_health,
            )
            obs.final_score = final_score
            if obs.metadata is None:
                obs.metadata = {}
            obs.metadata["final_score"] = final_score

        return obs

    @property
    def state(self) -> State:
        """Get the current environment state."""
        return self._state

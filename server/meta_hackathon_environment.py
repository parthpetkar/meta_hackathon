# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Meta Hackathon CI/CD pipeline repair environment implementation."""

import os
from uuid import uuid4

from openenv.core.env_server.interfaces import Environment  # type: ignore
from openenv.core.env_server.types import State  # type: ignore

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional import safety
    load_dotenv = None

try:
    from .graders import action_key, grade_episode, matches_terms, step_reward
    from .graders import (
        classify_flaky_test_fix,
        classify_network_outage_fix,
        classify_security_fix,
        easy_finalize_ready,
        hard_modify_reward_for_issue,
    )
    from .rubric_judge import OpenEnvLLMJudgeAdapter, RubricJudgeResult
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
        sample_logs_for_issue_with_trace,
    )
    from ..models import MetaHackathonAction, MetaHackathonObservation
except ImportError:
    from server.graders import action_key, grade_episode, matches_terms, step_reward
    from server.graders import (
        classify_flaky_test_fix,
        classify_network_outage_fix,
        classify_security_fix,
        easy_finalize_ready,
        hard_modify_reward_for_issue,
    )
    from server.rubric_judge import OpenEnvLLMJudgeAdapter, RubricJudgeResult
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
        sample_logs_for_issue_with_trace,
    )
    from models import MetaHackathonAction, MetaHackathonObservation


class MetaHackathonCICDRepairEnvironment(Environment):
    """Extensible CI/CD repair simulator with staged debugging and pattern-grounded variability."""

    SUPPORTS_CONCURRENT_SESSIONS: bool = True

    def __init__(self, task_key: str = ""):
        if load_dotenv is not None:
            load_dotenv()

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
        self._hypothesis_attempts_by_issue: dict[int, int] = {}

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
        self._episode_seed = 0
        self._sampled_issue_logs: dict[int, list[str]] = {}
        self._sampled_issue_trace_events: dict[int, list[dict[str, object]]] = {}
        self._audit_trail_enabled = os.getenv("META_HACKATHON_AUDIT_TRAIL", "false").strip().lower() == "true"

        self._rubric_enabled = os.getenv("META_HACKATHON_RUBRIC_ENABLED", "true").strip().lower() == "true"
        self._rubric_blend_weight = max(
            0.0,
            min(1.0, float(os.getenv("META_HACKATHON_RUBRIC_WEIGHT", "0.30"))),
        )
        self._rubric_timeout_seconds = int(os.getenv("META_HACKATHON_RUBRIC_TIMEOUT_SECONDS", "10"))
        self._rubric_model_name = (
            os.getenv("META_HACKATHON_RUBRIC_MODEL")
            or os.getenv("MODEL_NAME")
        )
        self._rubric_judge = OpenEnvLLMJudgeAdapter(
            enabled=self._rubric_enabled,
            model_name=self._rubric_model_name,
            timeout_seconds=self._rubric_timeout_seconds,
        )

        self._easy_build_passing = False
        self._security_iam_fixed = False
        self._security_secret_fixed = False
        self._last_rerun_progressed = False
        self._verified_for_latest_rerun = False
        self._inspected_since_last_rerun = True

    def _rubric_payload(self) -> dict:
        issue_chain = [
            {
                "stage": issue.stage,
                "true_cause": issue.true_cause,
                "hypothesis_terms": list(issue.hypothesis_terms),
                "family_term_sets": [list(term_set) for term_set in issue.family_term_sets],
                "relevant_inspections": list(issue.relevant_inspections),
            }
            for issue in self._scenario.incident_chain
        ]
        evidence = {
            "hypothesis_history": list(self._hypothesis_history[-8:]),
            "findings": list(self._findings[-16:]),
            "action_history": list(self._history[-16:]),
            "active_issue_index": self._current_issue_index,
            "solved_issues": self._solved_issues,
            "issue_count": len(self._scenario.incident_chain),
            "incident_resolved": self._incident_resolved,
            "pipeline_health": round(self._pipeline_health, 3),
        }
        rubric = {
            "semantic_correctness": "Does hypothesis semantically identify the root cause?",
            "evidence_alignment": "Does hypothesis align with logs, clues, and surfaced errors?",
            "completeness": "For chained incidents, are all active causes covered over the episode?",
        }
        return {
            "task_id": self._scenario.task_id,
            "difficulty": self._scenario.difficulty,
            "incident_chain": issue_chain,
            "evidence": evidence,
            "rubric": rubric,
        }

    def _blend_terminal_scores(self, deterministic_score: float) -> tuple[float, float, RubricJudgeResult]:
        if not self._rubric_enabled:
            disabled_result = RubricJudgeResult(
                score=0.0,
                rationale="rubric disabled",
                source="disabled",
                used_fallback=True,
                error="rubric judging disabled",
            )
            return deterministic_score, 0.0, disabled_result

        judge_result = self._rubric_judge.evaluate_hypothesis_quality(self._rubric_payload())
        rubric_score = max(0.0, min(1.0, float(judge_result.score)))
        blended_score = ((1.0 - self._rubric_blend_weight) * deterministic_score) + (
            self._rubric_blend_weight * rubric_score
        )
        delayed_reward = blended_score - deterministic_score

        # Keep rubric contribution bounded by difficulty so blended scores preserve
        # the intended easy -> medium -> security -> hard score separation.
        delayed_cap_by_difficulty = {
            "easy": 0.12,
            "medium": 0.11,
            "security": 0.10,
            "hard": 0.08,
        }
        delayed_cap = delayed_cap_by_difficulty.get(self._scenario.difficulty, 0.10)
        delayed_reward = max(-delayed_cap, min(delayed_reward, delayed_cap))
        blended_score = deterministic_score + delayed_reward
        return round(blended_score, 3), round(delayed_reward, 3), judge_result

    def _current_issue(self) -> IncidentStep:
        index = min(self._current_issue_index, len(self._scenario.incident_chain) - 1)
        return self._scenario.incident_chain[index]

    def _compute_required_inspections(self) -> set[str]:
        required: set[str] = {"view_logs"}
        for issue in self._scenario.incident_chain:
            required.update(issue.relevant_inspections)
        return required

    def _can_finalize_now(self) -> bool:
        if self._scenario.difficulty == "easy":
            return easy_finalize_ready(self._history, self._pipeline_stages) and self._verified_for_latest_rerun

        if self._scenario.difficulty == "security":
            both_fixed = self._security_iam_fixed and self._security_secret_fixed
            return both_fixed and self._incident_resolved and self._verified_for_latest_rerun

        return self._incident_resolved and self._verified_for_latest_rerun

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
            deterministic_score=0.0,
            rubric_score=0.0,
            delayed_reward=0.0,
            rubric_blend_weight=self._rubric_blend_weight if self._rubric_enabled else 0.0,
            rubric_judge_used=False,
            rubric_judge_error="",
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

        if self._scenario.difficulty == "easy" and self._easy_build_passing:
            self._pipeline_status = "failed"
            self._pipeline_stages["build"] = "passed"
            self._pipeline_stages["test"] = "blocked"
            self._pipeline_stages["deploy"] = "blocked"
            return

        issue = self._current_issue()
        self._pipeline_status = "failed"
        self._set_stage_failure(issue.stage)

    def _variant_logs_for_issue(self, issue_index: int, issue: IncidentStep) -> list[str]:
        if issue_index not in self._sampled_issue_logs:
            issue_seed = self._episode_seed + issue_index
            sampled_logs, trace_events = sample_logs_for_issue_with_trace(issue, self._variant_selector, issue_seed)
            self._sampled_issue_logs[issue_index] = sampled_logs
            self._sampled_issue_trace_events[issue_index] = trace_events
        return self._sampled_issue_logs[issue_index]

    def _audit_metadata_payload(self) -> dict[str, object]:
        if not self._audit_trail_enabled:
            return {}

        issue_index = min(self._current_issue_index, len(self._scenario.incident_chain) - 1)
        issue = self._scenario.incident_chain[issue_index]
        trace_events = self._sampled_issue_trace_events.get(issue_index, [])
        pattern_events = [event for event in trace_events if event.get("source") == "pattern_library"]

        return {
            "audit_enabled": True,
            "episode_seed": self._episode_seed,
            "variant_id": self._current_variant().variant_id,
            "active_issue_index": issue_index,
            "active_issue_pattern_buckets": list(issue.pattern_buckets),
            "sampled_pattern_event_count": len(pattern_events),
            "sampled_pattern_events": pattern_events[-6:],
        }

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

        if self._scenario.task_id == "flaky_test" and operation == "modify_config":
            flaky_fix_detected, _ = classify_flaky_test_fix(value)
            if flaky_fix_detected:
                return "correct"

        if self._scenario.task_id == "network_outage" and operation == "modify_config":
            network_fix_detected, _ = classify_network_outage_fix(value)
            if network_fix_detected:
                return "correct"

        if operation == issue.correct_operation and matches_terms(value, issue.correct_fix_terms):
            return "correct"

        for partial_terms in issue.partial_fix_terms:
            if matches_terms(value, partial_terms):
                return "partial"

        return "wrong"

    def _is_red_herring(self, issue: IncidentStep, value: str) -> bool:
        if self._scenario.task_id == "flaky_test":
            _, red_herring_detected = classify_flaky_test_fix(value)
            if red_herring_detected:
                return True

        if self._scenario.task_id == "network_outage":
            _, red_herring_detected = classify_network_outage_fix(value)
            if red_herring_detected:
                return True

        for terms in issue.red_herring_terms:
            if matches_terms(value, terms):
                return True
        return False

    def _is_inspection_relevant(self, issue: IncidentStep, operation: str) -> bool:
        return operation in set(issue.relevant_inspections)

    def _redundancy_key(self, history_entry: dict[str, str]) -> str:
        """Track repeated low-value actions within the current issue phase only."""
        return f"{self._current_issue_index}:{action_key(history_entry)}"

    def _handle_view_logs(self) -> bool:
        issue_index = self._current_issue_index
        issue = self._current_issue()
        revealed = False
        for line in self._variant_logs_for_issue(issue_index, issue):
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
        episode_uuid = uuid4()
        self._state = State(episode_id=str(episode_uuid), step_count=0)
        self._episode_seed = episode_uuid.int & 0xFFFFFFFF

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
        self._used_inspections = {"view_logs"}
        self._hypothesis_hit_issues = set()
        self._family_hit_issues = set()
        self._fix_hit_issues = set()
        self._hypothesis_attempts_by_issue = {}

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
        self._sampled_issue_logs = {}
        self._sampled_issue_trace_events = {}

        self._easy_build_passing = False
        self._security_iam_fixed = False
        self._security_secret_fixed = False
        self._last_rerun_progressed = False
        self._verified_for_latest_rerun = False
        self._inspected_since_last_rerun = True

        self._set_stage_progress_after_advance()
        self._handle_view_logs()

        return self._base_observation(
            reward=0.0,
            done=False,
            metadata={
                "task_key": selected_key,
                "max_steps": self._scenario.max_steps,
                "variant_id": self._current_variant().variant_id,
                "ready_to_finalize": self._can_finalize_now(),
                "verification_required": bool(self._incident_resolved and not self._verified_for_latest_rerun),
                "verified_since_last_rerun": self._verified_for_latest_rerun,
                "supported_operations": SUPPORTED_OPERATIONS,
                "canonical_operations": CANONICAL_OPERATIONS,
                **self._audit_metadata_payload(),
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
        history_entry = {"operation": operation, "target": target, "value": value}
        self._history.append(history_entry)
        key = self._redundancy_key(history_entry)
        was_redundant = key in self._action_keys
        if was_redundant:
            self._redundant_actions += 1

        inspection_relevant = False
        hypothesis_correct_first_try = False
        hypothesis_correct_retry = False
        fix_correct_for_issue = False
        fix_partial_for_issue = False
        fix_wrong_for_issue = False
        is_destructive_fix = False
        red_herring_fix = False
        rerun_after_valid_fix = False
        verify_success = False
        verify_failed = False
        finalize_correct = False
        finalize_partial = False
        finalize_incorrect = False
        malformed_action_hint = False
        step_error_message: str | None = None
        modify_reward_override: float | None = None
        finalize_reward_override: float | None = None

        issue = self._current_issue()

        if operation == "view_logs":
            inspection_relevant = self._is_inspection_relevant(issue, operation)
            self._handle_view_logs()
            self._used_inspections.add(operation)
            self._inspected_since_last_rerun = True

        elif operation == "inspect_config":
            inspection_relevant = self._is_inspection_relevant(issue, operation)
            self._handle_inspect_config()
            self._used_inspections.add(operation)
            self._inspected_since_last_rerun = True

        elif operation == "inspect_dockerfile":
            inspection_relevant = self._is_inspection_relevant(issue, operation)
            self._handle_inspect_dockerfile()
            self._used_inspections.add(operation)
            self._inspected_since_last_rerun = True

        elif operation == "inspect_permissions":
            inspection_relevant = self._is_inspection_relevant(issue, operation)
            self._handle_inspect_permissions()
            self._used_inspections.add(operation)
            self._inspected_since_last_rerun = True

        elif operation == "set_hypothesis":
            self._current_hypothesis = value
            self._hypothesis_history.append(value)
            prior_attempts = self._hypothesis_attempts_by_issue.get(self._current_issue_index, 0)
            self._hypothesis_attempts_by_issue[self._current_issue_index] = prior_attempts + 1

            if self._scenario.difficulty == "hard" and self._current_issue_index >= 2 and not self._inspected_since_last_rerun:
                self._append_unique(
                    self._findings,
                    "Hard-task hypothesis rejected: inspect evidence after rerun before asserting timeout diagnosis.",
                )
                hypothesis_correct = False
            else:
                hypothesis_correct = matches_terms(value, issue.hypothesis_terms)

            if hypothesis_correct:
                if prior_attempts == 0:
                    hypothesis_correct_first_try = True
                else:
                    hypothesis_correct_retry = True

            if hypothesis_correct and self._current_issue_index not in self._hypothesis_hit_issues:
                self._hypothesis_hits += 1
                self._hypothesis_hit_issues.add(self._current_issue_index)
                self._append_unique(self._findings, "Hypothesis aligns with current failure evidence.")
            elif not hypothesis_correct:
                self._append_unique(self._findings, "Hypothesis does not explain all current clues yet.")

            if not hypothesis_correct and self._current_issue_index not in self._family_hit_issues:
                family_hit = any(matches_terms(value, term_set) for term_set in issue.family_term_sets)
                if family_hit:
                    self._family_hits += 1
                    self._family_hit_issues.add(self._current_issue_index)
                    self._append_unique(self._findings, "Hypothesis captures failure family but lacks full root-cause precision.")

        elif operation in {"modify_config", "add_dependency"}:
            self._attempted_fix = value
            self._verified_for_latest_rerun = False
            if not self._used_inspections:
                self._append_unique(self._findings, "Fix attempted before inspection evidence collection.")

            if operation == "add_dependency" and not value:
                fix_partial_for_issue = True
                malformed_action_hint = True
                self._pending_fix_outcome = "partial"
                step_error_message = "Malformed action: add_dependency requires a 'value' string specifying the target version pin."
                self._append_unique(self._findings, "add_dependency action received without a dependency value string to apply.")
                self._recovery_cost += 1
            elif self._scenario.difficulty == "hard" and operation == "modify_config":
                red_herring_fix = False
                hard_assessment, hard_reward = hard_modify_reward_for_issue(self._current_issue_index)
                modify_reward_override = hard_reward
                if hard_assessment == "correct":
                    fix_correct_for_issue = True
                    self._pending_fix_outcome = "correct"
                    self._append_unique(self._findings, "Hard-task state transition fix candidate accepted.")
                else:
                    fix_wrong_for_issue = True
                    self._pending_fix_outcome = "wrong"
                    self._wrong_fixes += 1
                    self._append_unique(self._findings, "Fix attempt did not resolve the active failure.")

            elif self._scenario.difficulty == "security" and operation == "modify_config":
                red_herring_fix = False
                iam_fix, secret_fix = classify_security_fix(value)
                self._security_iam_fixed = self._security_iam_fixed or iam_fix
                self._security_secret_fixed = self._security_secret_fixed or secret_fix
                if iam_fix or secret_fix:
                    fix_correct_for_issue = True
                    modify_reward_override = 0.35
                    self._pending_fix_outcome = "correct"
                    self._append_unique(self._findings, "Security remediation accepted by substring policy rules.")
                else:
                    fix_wrong_for_issue = True
                    self._pending_fix_outcome = "wrong"
                    self._wrong_fixes += 1
                    self._pipeline_health = max(0.0, self._pipeline_health - 0.10)
                    self._recovery_cost += 2
                    self._append_unique(self._findings, "Fix attempt did not resolve the active failure.")

            else:
                red_herring_fix = self._is_red_herring(issue, value)
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
                    fix_wrong_for_issue = True
                    self._pending_fix_outcome = "wrong"
                    self._wrong_fixes += 1
                    self._pipeline_health = max(0.0, self._pipeline_health - 0.10)
                    self._recovery_cost += 2
                    self._append_unique(self._findings, "Fix attempt did not resolve the active failure.")

            if red_herring_fix:
                self._append_unique(self._findings, "Action looked plausible but is a red herring for this incident.")

        elif operation == "rerun_pipeline":
            self._recovery_cost += 1
            rerun_after_valid_fix = self._pending_fix_outcome in {"correct", "partial"}
            self._last_rerun_progressed = rerun_after_valid_fix
            self._verified_for_latest_rerun = False
            self._inspected_since_last_rerun = False

            if self._scenario.difficulty == "easy":
                has_hypothesis = any(item.get("operation") == "set_hypothesis" for item in self._history)
                has_build_modify = any(
                    item.get("operation") == "modify_config" and (item.get("target") or "").strip().lower() == "build"
                    for item in self._history
                )
                if has_hypothesis and has_build_modify:
                    self._easy_build_passing = True
                    rerun_after_valid_fix = True
                    self._append_unique(self._findings, "Build stage now passing after rerun validation.")

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
                if issue.partial_advances and self._current_issue_index + 1 < len(self._scenario.incident_chain):
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

            if rerun_after_valid_fix or self._incident_resolved:
                self._append_unique(
                    self._findings,
                    "Run verify_fix before finalize to confirm the failure signature is gone.",
                )

        elif operation == "verify_fix":
            prior_actions = self._history[:-1]
            last_rerun_index = max(
                (index for index, item in enumerate(prior_actions) if item.get("operation") == "rerun_pipeline"),
                default=-1,
            )

            if last_rerun_index < 0:
                verify_failed = True
                self._append_unique(self._findings, "Verification requires a rerun_pipeline action first.")
            elif self._verified_for_latest_rerun:
                verify_failed = True
                self._append_unique(
                    self._findings,
                    "Latest rerun is already verified; apply a new fix before verifying again.",
                )
            elif any(
                item.get("operation") in {"modify_config", "add_dependency"}
                for item in prior_actions[last_rerun_index + 1 :]
            ):
                verify_failed = True
                self._append_unique(
                    self._findings,
                    "Verification must be performed after rerun and before introducing another fix.",
                )
            elif self._last_rerun_progressed or self._incident_resolved:
                verify_success = True
                self._verified_for_latest_rerun = True
                self._append_unique(
                    self._findings,
                    "Verification confirms the latest rerun addressed the active failure signal.",
                )
            else:
                verify_failed = True
                self._append_unique(
                    self._findings,
                    "Verification failed: rerun evidence still indicates unresolved failures.",
                )

        elif operation == "finalize":
            if self._scenario.difficulty == "easy":
                if easy_finalize_ready(self._history, self._pipeline_stages) and self._verified_for_latest_rerun:
                    self._incident_resolved = True
                    finalize_correct = True
                    self._append_unique(self._findings, self._scenario.final_success_message)
                elif easy_finalize_ready(self._history, self._pipeline_stages):
                    finalize_incorrect = True
                    self._append_unique(self._findings, "Finalization rejected: run verify_fix after rerun_pipeline.")
                else:
                    finalize_incorrect = True
                    self._append_unique(self._findings, "Finalization rejected: unresolved stages remain.")

            elif self._scenario.difficulty == "security":
                both_fixed = self._security_iam_fixed and self._security_secret_fixed
                one_fixed = self._security_iam_fixed or self._security_secret_fixed
                if both_fixed and self._incident_resolved and self._verified_for_latest_rerun:
                    self._incident_resolved = True
                    finalize_correct = True
                    self._append_unique(self._findings, self._scenario.final_success_message)
                elif both_fixed and not self._verified_for_latest_rerun:
                    finalize_incorrect = True
                    self._append_unique(self._findings, "Finalization rejected: run verify_fix after rerun_pipeline.")
                elif one_fixed:
                    finalize_partial = True
                    self._append_unique(
                        self._findings,
                        "Partial security remediation accepted: one of two critical issues remains unresolved.",
                    )
                else:
                    finalize_incorrect = True
                    self._append_unique(self._findings, "Finalization rejected: unresolved stages remain.")

            elif self._incident_resolved and self._verified_for_latest_rerun:
                finalize_correct = True
                self._append_unique(self._findings, self._scenario.final_success_message)
            elif self._incident_resolved and not self._verified_for_latest_rerun:
                finalize_incorrect = True
                self._append_unique(self._findings, "Finalization rejected: run verify_fix after rerun_pipeline.")
            else:
                finalize_incorrect = True
                self._append_unique(self._findings, "Finalization rejected: unresolved stages remain.")

        self._set_stage_progress_after_advance()

        reward = step_reward(
            operation=operation,
            was_redundant=was_redundant,
            inspection_relevant=inspection_relevant,
            hypothesis_correct_first_try=hypothesis_correct_first_try,
            hypothesis_correct_retry=hypothesis_correct_retry,
            fix_correct_for_issue=fix_correct_for_issue,
            fix_partial_for_issue=fix_partial_for_issue,
            fix_wrong_for_issue=fix_wrong_for_issue,
            is_destructive_fix=is_destructive_fix,
            red_herring_fix=red_herring_fix,
            rerun_after_valid_fix=rerun_after_valid_fix,
            verify_success=verify_success,
            verify_failed=verify_failed,
            finalize_correct=finalize_correct,
            finalize_partial=finalize_partial,
            finalize_incorrect=finalize_incorrect,
            malformed_action_hint=malformed_action_hint,
            modify_reward_override=modify_reward_override,
            finalize_reward_override=finalize_reward_override,
        )

        self._action_keys.add(key)

        done = False
        if operation == "finalize":
            done = True
        if self._state.step_count >= self._scenario.max_steps:
            done = True
        if was_done_before_step and operation not in {"verify_fix", "finalize"}:
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
                "active_issue_index": self._current_issue_index,
                "resolved_issue_count": self._solved_issues,
                "issue_count": len(self._scenario.incident_chain),
                "ready_to_finalize": self._can_finalize_now(),
                "verification_required": bool(self._incident_resolved and not self._verified_for_latest_rerun),
                "verified_since_last_rerun": self._verified_for_latest_rerun,
                "supported_operations": SUPPORTED_OPERATIONS,
                "canonical_operations": CANONICAL_OPERATIONS,
                **self._audit_metadata_payload(),
            },
        )
        
        if step_error_message:
            obs.metadata["error"] = step_error_message

        if done:
            deterministic_score = grade_episode(
                difficulty=self._scenario.difficulty,
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
                wrong_fixes=self._wrong_fixes,
            )
            final_score, delayed_reward, judge_result = self._blend_terminal_scores(deterministic_score)

            obs.deterministic_score = deterministic_score
            obs.rubric_score = judge_result.score if self._rubric_enabled else 0.0
            obs.rubric_blend_weight = self._rubric_blend_weight if self._rubric_enabled else 0.0
            obs.delayed_reward = delayed_reward
            obs.rubric_judge_used = self._rubric_enabled and (not judge_result.used_fallback)
            obs.rubric_judge_error = judge_result.error
            obs.final_score = final_score
            obs.reward = round(float(obs.reward or 0.0) + delayed_reward, 3)

            if obs.metadata is None:
                obs.metadata = {}
            obs.metadata["deterministic_score"] = deterministic_score
            obs.metadata["rubric_score"] = obs.rubric_score
            obs.metadata["rubric_blend_weight"] = obs.rubric_blend_weight
            obs.metadata["delayed_reward"] = delayed_reward
            obs.metadata["rubric_judge_used"] = obs.rubric_judge_used
            obs.metadata["rubric_judge_error"] = obs.rubric_judge_error
            obs.metadata["rubric_judge_source"] = judge_result.source
            obs.metadata["rubric_judge_rationale"] = judge_result.rationale
            obs.metadata["final_score"] = final_score

        return obs

    @property
    def state(self) -> State:
        """Get the current environment state."""
        return self._state


# Backward-compatible alias used by existing clients and docs.
MetaHackathonEnvironment = MetaHackathonCICDRepairEnvironment

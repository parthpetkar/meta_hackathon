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
    from .graders import action_key, grade_episode, matches_fix, matches_hypothesis, step_reward
    from .scenarios import (
        DESTRUCTIVE_FIXES,
        SAFE_FIXES,
        SUPPORTED_OPERATIONS,
        ScenarioCard,
        get_scenario,
        list_task_keys,
    )
    from ..models import MetaHackathonAction, MetaHackathonObservation
except ImportError:
    from server.graders import action_key, grade_episode, matches_fix, matches_hypothesis, step_reward
    from server.scenarios import (
        DESTRUCTIVE_FIXES,
        SAFE_FIXES,
        SUPPORTED_OPERATIONS,
        ScenarioCard,
        get_scenario,
        list_task_keys,
    )
    from models import MetaHackathonAction, MetaHackathonObservation


class MetaHackathonEnvironment(Environment):
    """Deterministic CI/CD repair simulator with step-level reward shaping."""

    # Enable concurrent WebSocket sessions.
    # Set to True if your environment isolates state between instances.
    # When True, multiple WebSocket clients can connect simultaneously, each
    # getting their own environment instance (when using factory mode in app.py).
    SUPPORTS_CONCURRENT_SESSIONS: bool = True

    def __init__(self, task_key: str = ""):
        """Initialize environment with a deterministic scenario card."""
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
        initial_key = self._task_order[0] if self._task_key == "cycle" else self._task_key
        self._scenario: ScenarioCard = get_scenario(initial_key)
        self._state = State(episode_id=str(uuid4()), step_count=0)
        self._history: list[dict[str, str]] = []
        self._action_keys: set[str] = set()
        self._discoveries: set[tuple[str, str]] = set()
        self._visible_logs: list[str] = []
        self._visible_metrics: list[str] = []
        self._findings: list[str] = []
        self._current_hypothesis = ""
        self._attempted_fix = ""
        self._incident_resolved = False
        self._pipeline_status = "failed"

    def _base_observation(self, *, reward: float, done: bool, metadata: dict | None = None) -> MetaHackathonObservation:
        return MetaHackathonObservation(
            task_id=self._scenario.task_id,
            task_title=self._scenario.task_title,
            difficulty=self._scenario.difficulty,
            pipeline_status=self._pipeline_status,
            current_stage=self._scenario.failing_stage,
            available_stages=list(self._scenario.stage_insights.keys()),
            available_tools=list(SUPPORTED_OPERATIONS),
            visible_alerts=[self._scenario.pipeline_alert],
            visible_logs=self._visible_logs[-10:],
            visible_metrics=self._visible_metrics[-10:],
            findings=self._findings[-12:],
            action_history=[
                f"{entry['operation']}:{entry.get('target', '')}:{entry.get('value', '')}".strip(":")
                for entry in self._history[-12:]
            ],
            current_hypothesis=self._current_hypothesis,
            attempted_fix=self._attempted_fix,
            incident_resolved=self._incident_resolved,
            final_score=0.0,
            reward=reward,
            done=done,
            metadata=metadata or {},
        )

    def _append_discovery(self, operation: str, payload: str) -> None:
        key = (operation, payload)
        if key not in self._discoveries:
            self._discoveries.add(key)
            self._findings.append(payload)

    def _handle_inspect_operation(self, operation: str, target: str) -> None:
        clues = self._scenario.clue_operations.get(operation, [])
        if target and operation == "inspect_stage":
            detail = self._scenario.stage_insights.get(target)
            if detail:
                clues = clues + [f"stage {target}: {detail}"]

        if not clues:
            self._findings.append(f"No additional evidence returned for {operation}.")
            return

        for clue in clues:
            self._append_discovery(operation, clue)
            if operation in {"inspect_logs", "inspect_git", "inspect_docker"}:
                self._visible_logs.append(clue)
            if operation in {
                "inspect_pipeline",
                "inspect_stage",
                "inspect_tests",
                "inspect_dependencies",
                "inspect_permissions",
            }:
                self._visible_metrics.append(clue)

    def reset(self) -> MetaHackathonObservation:
        """Reset episode state and return the initial task observation."""
        self._state = State(episode_id=str(uuid4()), step_count=0)
        if self._task_key == "cycle":
            selected_key = self._task_order[self._task_cursor % len(self._task_order)]
            self._task_cursor += 1
        else:
            selected_key = self._task_key
        self._scenario = get_scenario(selected_key)
        self._history = []
        self._action_keys = set()
        self._discoveries = set()
        self._visible_logs = []
        self._visible_metrics = list(self._scenario.initial_metrics)
        self._findings = [
            "Incident acknowledged. Start by inspecting pipeline state and failing stage.",
        ]
        self._current_hypothesis = ""
        self._attempted_fix = ""
        self._incident_resolved = False
        self._pipeline_status = "failed"

        return self._base_observation(
            reward=0.0,
            done=False,
            metadata={
                "task_key": selected_key,
                "max_steps": self._scenario.max_steps,
            },
        )

    def step(self, action: MetaHackathonAction) -> MetaHackathonObservation:  # type: ignore[override]
        """Apply action, transition deterministic scenario state, and return observation."""
        self._state.step_count += 1

        operation = (action.operation or "").strip()
        target = (action.target or "").strip()
        value = (action.value or "").strip()
        done = False

        if operation not in SUPPORTED_OPERATIONS:
            reward = -0.20
            return self._base_observation(
                reward=reward,
                done=False,
                metadata={
                    "error": f"unsupported operation '{operation}'",
                    "supported_operations": SUPPORTED_OPERATIONS,
                },
            )

        history_entry = {"operation": operation, "target": target, "value": value}
        self._history.append(history_entry)
        key = action_key(history_entry)

        if operation.startswith("inspect_"):
            self._handle_inspect_operation(operation, target)

        if operation == "set_hypothesis":
            self._current_hypothesis = value

        if operation == "apply_fix":
            self._attempted_fix = value
            if value.lower() == self._scenario.correct_fix.lower():
                self._findings.append("Fix candidate accepted by pipeline controller.")
            elif value.lower() in DESTRUCTIVE_FIXES:
                self._findings.append("Unsafe fix attempt detected and blocked by policy guard.")
            else:
                self._findings.append("Fix attempt did not resolve current failure conditions.")

        if operation == "verify_fix":
            has_correct_hypothesis = matches_hypothesis(
                self._scenario.root_cause,
                self._current_hypothesis,
            )
            has_correct_fix = matches_fix(
                self._scenario.correct_fix,
                self._attempted_fix,
            )
            self._incident_resolved = has_correct_hypothesis and has_correct_fix
            if self._incident_resolved:
                self._pipeline_status = "healthy"
                self._findings.append(self._scenario.terminal_success_message)
                done = True
            else:
                self._pipeline_status = "failed"
                self._findings.append("Verification failed. Continue investigation and apply a safe fix.")

        reward = step_reward(
            operation=operation,
            op_target=target,
            seen_action_keys=self._action_keys,
            expected_hypothesis=self._scenario.root_cause,
            hypothesis_value=self._current_hypothesis,
            expected_fix=self._scenario.correct_fix,
            fix_value=self._attempted_fix,
            incident_resolved=self._incident_resolved,
            is_destructive_fix=self._attempted_fix.lower() in DESTRUCTIVE_FIXES,
        )

        self._action_keys.add(key)

        if self._state.step_count >= self._scenario.max_steps:
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
            },
        )

        if done:
            final_score = grade_episode(
                action_history=[entry["operation"] for entry in self._history],
                discovered_clues=self._discoveries,
                expected_clue_ops=set(self._scenario.clue_operations.keys()),
                expected_hypothesis=self._scenario.root_cause,
                hypothesis=self._current_hypothesis,
                expected_fix=self._scenario.correct_fix,
                attempted_fix=self._attempted_fix,
                incident_resolved=self._incident_resolved,
                max_steps=self._scenario.max_steps,
                destructive_actions=sum(
                    1
                    for entry in self._history
                    if entry["operation"] == "apply_fix"
                    and entry["value"].lower() in DESTRUCTIVE_FIXES
                ),
            )
            obs.final_score = final_score
            if obs.metadata is None:
                obs.metadata = {}
            obs.metadata["final_score"] = final_score

        return obs

    @property
    def state(self) -> State:
        """
        Get the current environment state.

        Returns:
            Current State with episode_id and step_count
        """
        return self._state

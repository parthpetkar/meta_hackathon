# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Meta Hackathon incident response environment implementation."""

from copy import deepcopy
from uuid import uuid4

from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import State

try:
    from ..models import MetaHackathonAction, MetaHackathonObservation
    from .graders import GRADER_BY_TASK
    from .scenarios import SCENARIOS, IncidentScenario
except ImportError:
    from models import MetaHackathonAction, MetaHackathonObservation
    from server.graders import GRADER_BY_TASK
    from server.scenarios import SCENARIOS, IncidentScenario


class MetaHackathonEnvironment(Environment):
    """A deterministic production incident troubleshooting simulator."""

    # Enable concurrent WebSocket sessions.
    # Set to True if your environment isolates state between instances.
    # When True, multiple WebSocket clients can connect simultaneously, each
    # getting their own environment instance (when using factory mode in app.py).
    SUPPORTS_CONCURRENT_SESSIONS: bool = True

    MAX_STEPS: int = 10

    def __init__(self):
        """Initialize the incident response environment."""
        self._state = State(episode_id=str(uuid4()), step_count=0)
        self._reset_count = 0
        self._scenario: IncidentScenario = SCENARIOS[0]
        self._seen_alerts = False
        self._seen_metrics = False
        self._seen_services = set()
        self._seen_logs_services = set()
        self._revealed_logs = []
        self._current_hypothesis = ""
        self._hypothesis_correct = False
        self._fix_applied = ""
        self._fix_correct = False
        self._verified = False
        self._destructive_action = False
        self._repeated_actions = 0
        self._found_signals = 0
        self._action_counts = {}
        self._latest_finding = ""
        self._final_score = 0.0

    def reset(self) -> MetaHackathonObservation:
        """Reset environment to next deterministic task scenario."""
        scenario_index = self._reset_count % len(SCENARIOS)
        self._scenario = SCENARIOS[scenario_index]
        self._state = State(episode_id=str(uuid4()), step_count=0)
        self._reset_count += 1
        self._seen_alerts = False
        self._seen_metrics = False
        self._seen_services = set()
        self._seen_logs_services = set()
        self._revealed_logs = []
        self._current_hypothesis = ""
        self._hypothesis_correct = False
        self._fix_applied = ""
        self._fix_correct = False
        self._verified = False
        self._destructive_action = False
        self._repeated_actions = 0
        self._found_signals = 0
        self._action_counts = {}
        self._latest_finding = "Incident created. Start by inspecting alerts and metrics."
        self._final_score = 0.0

        return self._build_observation(done=False, reward=0.0, status="investigating")

    def step(self, action: MetaHackathonAction) -> MetaHackathonObservation:  # type: ignore[override]
        """Execute one troubleshooting step for the active incident."""
        self._state.step_count += 1

        action_key = f"{action.operation}:{action.target}:{action.value}"
        self._action_counts[action_key] = self._action_counts.get(action_key, 0) + 1

        reward = -0.02
        done = False
        status = "investigating"

        if self._action_counts[action_key] > 1:
            self._repeated_actions += 1
            reward -= 0.05

        if action.operation == "inspect_alerts":
            if not self._seen_alerts:
                self._seen_alerts = True
                self._found_signals += len(self._scenario.alert_clues)
                reward += 0.12
            else:
                reward -= 0.02
            self._latest_finding = "; ".join(self._scenario.alert_clues)

        elif action.operation == "inspect_metrics":
            if not self._seen_metrics:
                self._seen_metrics = True
                self._found_signals += len(self._scenario.metric_clues)
                reward += 0.14
            else:
                reward -= 0.02
            self._latest_finding = "Metrics reveal bottleneck patterns in dependent services."

        elif action.operation == "inspect_service":
            target = action.target.strip()
            if target in self._scenario.service_clues:
                if target not in self._seen_services:
                    self._seen_services.add(target)
                    self._found_signals += 1
                    reward += 0.10
                self._latest_finding = self._scenario.service_clues[target]
            else:
                reward -= 0.08
                self._latest_finding = "Unknown service target."

        elif action.operation == "inspect_logs":
            target = action.target.strip()
            if target in self._scenario.log_clues:
                if target not in self._seen_logs_services:
                    self._seen_logs_services.add(target)
                    self._found_signals += len(self._scenario.log_clues[target])
                    reward += 0.12
                    self._revealed_logs.extend(self._scenario.log_clues[target])
                else:
                    reward -= 0.02
                self._latest_finding = f"Collected logs from {target}."
            else:
                reward -= 0.08
                self._latest_finding = "No logs available for that target."

        elif action.operation == "set_hypothesis":
            hypothesis = action.value.strip().lower()
            self._current_hypothesis = action.value.strip()
            token_hits = sum(1 for token in self._scenario.expected_hypothesis_tokens if token in hypothesis)
            if token_hits >= 2:
                if not self._hypothesis_correct:
                    reward += 0.25
                self._hypothesis_correct = True
                self._latest_finding = "Hypothesis aligns with observed incident clues."
            else:
                self._hypothesis_correct = False
                reward -= 0.12
                self._latest_finding = "Hypothesis does not match key incident evidence."

        elif action.operation == "apply_fix":
            proposed_fix = action.value.strip()
            self._fix_applied = proposed_fix
            if proposed_fix in self._scenario.destructive_fixes:
                self._destructive_action = True
                reward -= 0.80
                done = True
                status = "failed"
                self._latest_finding = "Destructive fix caused incident escalation."
            elif proposed_fix == self._scenario.correct_fix:
                if self._hypothesis_correct:
                    reward += 0.35
                else:
                    reward += 0.20
                self._fix_correct = True
                self._latest_finding = "Fix applied successfully. Run verification to close incident."
            else:
                self._fix_correct = False
                reward -= 0.20
                self._latest_finding = "Fix did not address the root cause."

        elif action.operation == "verify_fix":
            if self._fix_correct:
                self._verified = True
                reward += 0.45
                done = True
                status = "resolved"
                self._latest_finding = "Verification passed. Incident resolved."
            else:
                reward -= 0.10
                self._latest_finding = "Verification failed. Root cause still active."

        else:
            reward -= 0.10
            self._latest_finding = "Unsupported operation."

        if not done and self._state.step_count >= self.MAX_STEPS:
            done = True
            status = "timed_out"
            reward -= 0.15
            self._latest_finding = "Step budget exhausted before incident resolution."

        if done:
            trace = self._build_trace()
            grader = GRADER_BY_TASK[self._scenario.task_id]
            self._final_score = grader(trace)

        return self._build_observation(done=done, reward=reward, status=status)

    def _build_trace(self) -> dict:
        expected_signals = (
            len(self._scenario.alert_clues)
            + len(self._scenario.metric_clues)
            + len(self._scenario.service_clues)
            + len(self._scenario.log_clues)
        )
        return {
            "found_signals": self._found_signals,
            "expected_signals": expected_signals,
            "unique_inspections": int(self._seen_alerts)
            + int(self._seen_metrics)
            + len(self._seen_services)
            + len(self._seen_logs_services),
            "hypothesis_correct": self._hypothesis_correct,
            "fix_correct": self._fix_correct,
            "verified": self._verified,
            "destructive_action": self._destructive_action,
            "repeated_actions": self._repeated_actions,
            "steps_taken": self._state.step_count,
            "max_steps": self.MAX_STEPS,
        }

    def _recommended_actions(self) -> list[str]:
        suggestions = []
        if not self._seen_alerts:
            suggestions.append("inspect_alerts")
        if not self._seen_metrics:
            suggestions.append("inspect_metrics")
        if not self._seen_services:
            suggestions.append("inspect_service:<service-name>")
        if not self._seen_logs_services:
            suggestions.append("inspect_logs:<service-name>")
        if not self._current_hypothesis:
            suggestions.append("set_hypothesis:<root-cause-theory>")
        if not self._fix_applied:
            suggestions.append("apply_fix:<fix-id>")
        if self._fix_correct and not self._verified:
            suggestions.append("verify_fix")
        return suggestions

    def _build_observation(self, done: bool, reward: float, status: str) -> MetaHackathonObservation:
        visible_alerts = deepcopy(self._scenario.alert_clues) if self._seen_alerts else []
        visible_metrics = deepcopy(self._scenario.metric_clues) if self._seen_metrics else {}

        return MetaHackathonObservation(
            task_id=self._scenario.task_id,
            task_title=self._scenario.title,
            difficulty=self._scenario.difficulty,
            status=status,
            available_services=deepcopy(self._scenario.services),
            visible_alerts=visible_alerts,
            visible_metrics=visible_metrics,
            visible_logs=deepcopy(self._revealed_logs),
            latest_finding=self._latest_finding,
            current_hypothesis=self._current_hypothesis,
            recommended_actions=self._recommended_actions(),
            incident_resolved=self._verified,
            final_score=self._final_score,
            done=done,
            reward=reward,
            metadata={
                "task_id": self._scenario.task_id,
                "step": self._state.step_count,
            },
        )

    @property
    def state(self) -> State:
        """
        Get the current environment state.

        Returns:
            Current State with episode_id and step_count
        """
        return self._state

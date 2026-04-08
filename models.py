# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Data models for the Meta Hackathon CI/CD repair environment."""

from typing import Dict, List

from openenv.core.env_server.types import Action, Observation
from pydantic import Field


class MetaHackathonAction(Action):
    """Action model for CI/CD pipeline investigation and repair."""

    operation: str = Field(
        ...,
        description=(
            "Operation name. Preferred values: view_logs, inspect_config, "
            "inspect_dockerfile, modify_config, add_dependency, rerun_pipeline, "
            "verify_fix, finalize, inspect_permissions, set_hypothesis. "
            "Legacy aliases are accepted for backward compatibility."
        ),
    )
    target: str = Field(
        default="",
        description="Optional target (stage/service/component) for inspect operations.",
    )
    value: str = Field(
        default="",
        description="Optional free-text payload for hypothesis/fix values.",
    )


class MetaHackathonObservation(Observation):
    """Observation model exposing CI/CD task state and visible evidence."""

    task_id: str = Field(default="", description="Task identifier.")
    task_title: str = Field(default="", description="Human-readable task title.")
    difficulty: str = Field(default="", description="Task difficulty: easy/medium/security/hard.")
    pipeline_status: str = Field(default="unknown", description="Current pipeline status.")
    current_stage: str = Field(default="", description="Current failing stage.")
    pipeline_stages: Dict[str, str] = Field(
        default_factory=dict,
        description="Per-stage status map (pending/running/failed/passed/blocked).",
    )
    available_stages: List[str] = Field(
        default_factory=list,
        description="Pipeline stages available for targeted inspection.",
    )
    available_tools: List[str] = Field(
        default_factory=list,
        description="High-level tools/actions an agent can invoke.",
    )
    visible_alerts: List[str] = Field(
        default_factory=list,
        description="Visible alert summaries.",
    )
    visible_logs: List[str] = Field(
        default_factory=list,
        description="Visible log lines discovered so far.",
    )
    logs_by_stage: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Visible logs grouped by pipeline stage.",
    )
    visible_metrics: List[str] = Field(
        default_factory=list,
        description="Visible metrics/clues discovered so far.",
    )
    config_files: Dict[str, str] = Field(
        default_factory=dict,
        description="Current snapshots of key configuration files.",
    )
    surfaced_errors: List[str] = Field(
        default_factory=list,
        description="Current error signatures visible to the agent.",
    )
    findings: List[str] = Field(
        default_factory=list,
        description="Structured findings accumulated by investigation.",
    )
    action_history: List[str] = Field(
        default_factory=list,
        description="Compact history of actions already taken.",
    )
    previous_actions: List[str] = Field(
        default_factory=list,
        description="Alias for action_history for clarity in prompts/agents.",
    )
    current_hypothesis: str = Field(
        default="",
        description="Current root-cause hypothesis set by the agent.",
    )
    attempted_fix: str = Field(
        default="",
        description="Most recent fix attempted by the agent.",
    )
    hypothesis_history: List[str] = Field(
        default_factory=list,
        description="Chronological hypothesis updates proposed by the agent.",
    )
    active_issue_index: int = Field(
        default=0,
        description="Current issue index in the scenario's deterministic incident chain.",
    )
    revealed_issue_count: int = Field(
        default=1,
        description="How many incident-chain issues have been revealed so far.",
    )
    pipeline_health: float = Field(
        default=1.0,
        description="Pipeline health in [0.0, 1.0]; bad fixes degrade this value.",
    )
    recovery_cost: int = Field(
        default=0,
        description="Accumulated recovery cost from retries and incorrect actions.",
    )
    redundant_actions: int = Field(
        default=0,
        description="Count of repeated low-value actions.",
    )
    destructive_actions: int = Field(
        default=0,
        description="Count of unsafe fix attempts.",
    )
    incident_resolved: bool = Field(
        default=False,
        description="Whether the CI/CD incident is resolved.",
    )
    final_score: float = Field(
        default=0.0,
        description="Episode score in [0.0, 1.0] once done=True.",
    )

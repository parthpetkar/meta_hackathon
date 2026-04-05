# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Data models for the Meta Hackathon CI/CD repair environment."""

from typing import List

from openenv.core.env_server.types import Action, Observation
from pydantic import Field


class MetaHackathonAction(Action):
    """Action model for CI/CD pipeline investigation and repair."""

    operation: str = Field(
        ...,
        description=(
            "Operation name. Supported values: inspect_pipeline, inspect_stage, "
            "inspect_logs, inspect_git, inspect_docker, inspect_tests, "
            "inspect_dependencies, inspect_permissions, set_hypothesis, apply_fix, "
            "verify_fix"
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
    difficulty: str = Field(default="", description="Task difficulty: easy/medium/hard.")
    pipeline_status: str = Field(default="unknown", description="Current pipeline status.")
    current_stage: str = Field(default="", description="Current failing stage.")
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
    visible_metrics: List[str] = Field(
        default_factory=list,
        description="Visible metrics/clues discovered so far.",
    )
    findings: List[str] = Field(
        default_factory=list,
        description="Structured findings accumulated by investigation.",
    )
    action_history: List[str] = Field(
        default_factory=list,
        description="Compact history of actions already taken.",
    )
    current_hypothesis: str = Field(
        default="",
        description="Current root-cause hypothesis set by the agent.",
    )
    attempted_fix: str = Field(
        default="",
        description="Most recent fix attempted by the agent.",
    )
    incident_resolved: bool = Field(
        default=False,
        description="Whether the CI/CD incident is resolved.",
    )
    final_score: float = Field(
        default=0.0,
        description="Episode score in [0.0, 1.0] once done=True.",
    )

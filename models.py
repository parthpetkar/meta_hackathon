# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Data models for the Meta Hackathon incident response environment."""

from typing import Dict, List, Literal

from openenv.core.env_server.types import Action, Observation
from pydantic import Field


class MetaHackathonAction(Action):
    """Structured troubleshooting action for a production incident."""

    operation: Literal[
        "inspect_alerts",
        "inspect_metrics",
        "inspect_service",
        "inspect_logs",
        "set_hypothesis",
        "apply_fix",
        "verify_fix",
    ] = Field(..., description="Troubleshooting operation to execute")
    target: str = Field(default="", description="Service or system target for the operation")
    value: str = Field(default="", description="Free-form value for hypothesis or fix identifier")


class MetaHackathonObservation(Observation):
    """Observation containing clues and current incident status."""

    task_id: str = Field(default="", description="Current task identifier")
    task_title: str = Field(default="", description="Current task title")
    difficulty: str = Field(default="", description="Difficulty level for the task")
    status: str = Field(default="investigating", description="High-level incident status")
    available_services: List[str] = Field(default_factory=list, description="Services available for inspection")
    visible_alerts: List[str] = Field(default_factory=list, description="Currently visible alert clues")
    visible_metrics: Dict[str, float] = Field(default_factory=dict, description="Currently visible metric clues")
    visible_logs: List[str] = Field(default_factory=list, description="Log lines revealed through inspection")
    latest_finding: str = Field(default="", description="Most recent finding from the agent action")
    current_hypothesis: str = Field(default="", description="Most recent agent hypothesis")
    recommended_actions: List[str] = Field(default_factory=list, description="Guidance on useful next actions")
    incident_resolved: bool = Field(default=False, description="True when incident has been resolved")
    final_score: float = Field(default=0.0, description="Task score in [0.0, 1.0] once episode ends")

"""Deterministic CI/CD scenario cards for the Meta Hackathon environment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


SUPPORTED_OPERATIONS: List[str] = [
    "inspect_pipeline",
    "inspect_stage",
    "inspect_logs",
    "inspect_git",
    "inspect_docker",
    "inspect_tests",
    "inspect_dependencies",
    "inspect_permissions",
    "set_hypothesis",
    "apply_fix",
    "verify_fix",
]


SAFE_FIXES: List[str] = [
    "resolve-merge-conflict",
    "pin-compatible-requests-version",
    "add-apt-update-before-install",
    "grant-registry-write-permission",
    "increase-ci-timeout-to-20m",
]


DESTRUCTIVE_FIXES: List[str] = [
    "disable-all-tests",
    "force-push-main",
    "delete-build-cache",
    "wipe-registry",
]


@dataclass(frozen=True)
class ScenarioCard:
    """Single deterministic CI/CD fault scenario."""

    task_id: str
    task_title: str
    difficulty: str
    benchmark: str
    failing_stage: str
    pipeline_alert: str
    initial_metrics: List[str]
    root_cause: str
    correct_fix: str
    terminal_success_message: str
    max_steps: int
    stage_insights: Dict[str, str]
    clue_operations: Dict[str, List[str]]


SCENARIOS: Dict[str, ScenarioCard] = {
    "easy": ScenarioCard(
        task_id="easy_merge_conflict",
        task_title="Resolve failing merge stage",
        difficulty="easy",
        benchmark="meta_hackathon",
        failing_stage="merge-check",
        pipeline_alert="Merge stage failed: unresolved conflict in services/api/routes.py",
        initial_metrics=[
            "pipeline.retry_count=2",
            "pipeline.blocked_prs=1",
            "merge.conflict_files=1",
        ],
        root_cause="feature branch is stale and contains unresolved merge conflict",
        correct_fix="resolve-merge-conflict",
        terminal_success_message="Merge stage recovered; pipeline now passes all checks.",
        max_steps=8,
        stage_insights={
            "merge-check": "Conflict markers found in services/api/routes.py",
            "test": "Tests were skipped because merge stage failed",
            "deploy": "Deploy not started due to upstream failure",
        },
        clue_operations={
            "inspect_pipeline": [
                "pipeline status: failed at merge-check",
                "recent change: PR #184 rebased 3 days ago",
            ],
            "inspect_stage": [
                "merge-check details: git merge --no-ff origin/main exited with code 1",
            ],
            "inspect_logs": [
                "CONFLICT (content): Merge conflict in services/api/routes.py",
                "Automatic merge failed; fix conflicts and commit",
            ],
            "inspect_git": [
                "git status: both modified services/api/routes.py",
                "git log --graph: feature branch behind main by 6 commits",
            ],
            "inspect_tests": [
                "tests not executed: blocked by merge-check stage",
            ],
        },
    ),
    "medium": ScenarioCard(
        task_id="medium_docker_dep_failure",
        task_title="Repair docker build dependency failure",
        difficulty="medium",
        benchmark="meta_hackathon",
        failing_stage="build-image",
        pipeline_alert="Docker build failed while installing Python dependencies",
        initial_metrics=[
            "build.duration_seconds=413",
            "build.layer_cache_hit_ratio=0.21",
            "python.install_failures=1",
        ],
        root_cause="requests 2.20.0 conflicts with urllib3 constraints required by the app",
        correct_fix="pin-compatible-requests-version",
        terminal_success_message="Docker image built successfully and tests now pass.",
        max_steps=9,
        stage_insights={
            "build-image": "Dependency resolution failed during pip install",
            "test": "Integration tests never started; image build failed",
            "deploy": "Deploy blocked waiting for a successful build artifact",
        },
        clue_operations={
            "inspect_pipeline": [
                "pipeline status: failed at build-image",
                "last successful run used a different requirements lock",
            ],
            "inspect_stage": [
                "build-image details: docker build exited with code 1",
            ],
            "inspect_logs": [
                "ERROR: requests==2.20.0 has requirement urllib3<1.25,>=1.21.1",
                "Current dependency graph resolved urllib3==2.1.0",
            ],
            "inspect_docker": [
                "Dockerfile step 11: RUN pip install -r requirements.txt",
                "No apt/package issue detected in build layers",
            ],
            "inspect_dependencies": [
                "requirements.txt pins requests==2.20.0",
                "pyproject transitive dependency expects urllib3>=2.0",
            ],
            "inspect_tests": [
                "tests skipped due to missing built image",
            ],
        },
    ),
    "hard": ScenarioCard(
        task_id="hard_permission_timeout_chain",
        task_title="Fix deploy permission and timeout chain",
        difficulty="hard",
        benchmark="meta_hackathon",
        failing_stage="deploy",
        pipeline_alert="Deploy stage failed: permission denied and rollout timeout",
        initial_metrics=[
            "deploy.timeout_seconds=900",
            "deploy.error_rate=0.43",
            "iam.denied_events=3",
        ],
        root_cause="ci service account lacks registry write permission causing delayed retries and timeout",
        correct_fix="grant-registry-write-permission",
        terminal_success_message="Deploy completed after permission fix and rollout verification.",
        max_steps=10,
        stage_insights={
            "build-image": "Build completes but publish step retries repeatedly",
            "deploy": "kubectl rollout status exceeded timeout after image publish failures",
            "post-deploy": "Health checks unavailable because rollout never completed",
        },
        clue_operations={
            "inspect_pipeline": [
                "pipeline status: failed at deploy",
                "deploy job retried image publish 5 times",
            ],
            "inspect_stage": [
                "deploy details: rollout status timeout after 900s",
            ],
            "inspect_logs": [
                "docker push denied: permission denied for repository team/api",
                "rollout blocked waiting for image tag 2026.04.05-rc1",
            ],
            "inspect_permissions": [
                "service account ci-runner has role: artifactregistry.reader",
                "missing role: artifactregistry.writer",
            ],
            "inspect_docker": [
                "publish command retried with exponential backoff",
            ],
            "inspect_tests": [
                "deploy smoke tests not executed: no running rollout",
            ],
        },
    ),
}


def get_scenario(task_key: str) -> ScenarioCard:
    """Return scenario by key and default to medium if unknown."""
    return SCENARIOS.get(task_key, SCENARIOS["medium"])


def list_task_keys() -> List[str]:
    """Return deterministic task order for baseline runs."""
    return ["easy", "medium", "hard"]

"""Deterministic staged CI/CD scenarios for the Meta Hackathon environment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


STAGE_ORDER: List[str] = ["build", "test", "deploy"]


CANONICAL_OPERATIONS: List[str] = [
    "view_logs",
    "inspect_config",
    "inspect_dockerfile",
    "modify_config",
    "add_dependency",
    "rerun_pipeline",
    "finalize",
    "inspect_permissions",
    "set_hypothesis",
]


LEGACY_OPERATION_ALIASES: Dict[str, str] = {
    "inspect_logs": "view_logs",
    "inspect_pipeline": "inspect_config",
    "inspect_stage": "inspect_config",
    "inspect_git": "inspect_config",
    "inspect_tests": "inspect_config",
    "inspect_dependencies": "inspect_config",
    "inspect_docker": "inspect_dockerfile",
    "apply_fix": "modify_config",
    "verify_fix": "finalize",
}


SUPPORTED_OPERATIONS: List[str] = CANONICAL_OPERATIONS + sorted(LEGACY_OPERATION_ALIASES.keys())


SAFE_FIXES: List[str] = [
    "sync-branch-and-resolve-conflict",
    "rebase-feature-branch",
    "pin-compatible-requests-urllib3",
    "reorder-docker-install-steps",
    "grant-artifactregistry-writer",
    "tune-rollout-timeout-after-auth-fix",
]


DESTRUCTIVE_FIXES: List[str] = [
    "disable-all-tests",
    "force-push-main",
    "wipe-registry",
    "skip-deploy-validations",
]


@dataclass(frozen=True)
class IncidentStep:
    """One deterministic failure node in a staged incident chain."""

    stage: str
    ambiguous_error: str
    possible_causes: List[str]
    family_term_sets: List[List[str]]
    true_cause: str
    hypothesis_terms: List[str]
    log_variants: List[List[str]]
    config_clues: List[str]
    docker_clues: List[str]
    permission_clues: List[str]
    correct_operation: str
    correct_fix_terms: List[str]
    partial_fix_terms: List[List[str]]
    partial_fix_reveal: str


@dataclass(frozen=True)
class ScenarioVariant:
    """Deterministic per-episode scenario variant for replayable diversity."""

    variant_id: str
    pipeline_alert_suffix: str
    extra_log_lines: List[str]
    extra_config_clues: List[str]


@dataclass(frozen=True)
class ScenarioCard:
    """Single deterministic CI/CD simulator scenario."""

    task_id: str
    task_title: str
    difficulty: str
    benchmark: str
    pipeline_alert: str
    initial_metrics: List[str]
    max_steps: int
    config_templates: Dict[str, str]
    final_success_message: str
    incident_chain: List[IncidentStep]
    variants: List[ScenarioVariant]


SCENARIOS: Dict[str, ScenarioCard] = {
    "easy": ScenarioCard(
        task_id="easy_merge_conflict",
        task_title="Resolve iterative merge and test breakage",
        difficulty="easy",
        benchmark="meta_hackathon",
        pipeline_alert="Build failed with merge conflict-like output.",
        initial_metrics=[
            "pipeline.retry_count=1",
            "pipeline.blocked_prs=1",
            "merge.conflict_files=1",
        ],
        max_steps=10,
        config_templates={
            "ci.yaml": "stages: [build, test, deploy]\nmerge_policy: strict\nbranch_sync: false",
            "services/api/routes.py": "<<<<<<< HEAD\nhandler_v1()\n=======\nhandler_v2()\n>>>>>>> feature",
        },
        final_success_message="Pipeline recovered after merge sync and post-merge test stabilization.",
        incident_chain=[
            IncidentStep(
                stage="build",
                ambiguous_error="merge-check failed: conflict detected in routes",
                possible_causes=[
                    "unresolved conflict markers",
                    "feature branch stale against main",
                ],
                family_term_sets=[
                    ["merge", "conflict"],
                    ["stale", "branch"],
                ],
                true_cause="feature branch stale and unresolved conflict markers",
                hypothesis_terms=["merge", "conflict", "stale"],
                log_variants=[
                    [
                        "CONFLICT (content): Merge conflict in services/api/routes.py",
                        "Automatic merge failed; fix conflicts and commit the result.",
                    ],
                    [
                        "merge-check exited code 1: competing changes in routes.py",
                        "hint: branch appears behind origin/main by multiple commits",
                    ],
                ],
                config_clues=[
                    "ci.yaml: branch_sync=false for feature pipeline",
                    "git metadata: feature branch behind main by 6 commits",
                ],
                docker_clues=["No docker anomaly surfaced for current failure."],
                permission_clues=["No IAM anomaly surfaced for current failure."],
                correct_operation="modify_config",
                correct_fix_terms=["sync", "resolve", "merge", "conflict"],
                partial_fix_terms=[["resolve", "conflict"]],
                partial_fix_reveal="Build passes but tests now fail due to stale API contract after incomplete merge sync.",
            ),
            IncidentStep(
                stage="test",
                ambiguous_error="contract-test failed after merge",
                possible_causes=[
                    "api contract drift",
                    "stale generated fixtures",
                ],
                family_term_sets=[
                    ["contract", "drift"],
                    ["stale", "schema"],
                ],
                true_cause="stale branch contract after partial merge resolution",
                hypothesis_terms=["contract", "stale", "branch"],
                log_variants=[
                    [
                        "contract_test.py::test_route_schema failed: expected v2 payload",
                        "artifact schema generated from outdated branch baseline",
                    ],
                ],
                config_clues=[
                    "schema.lock mismatch after merge resolution",
                    "branch sync marker missing from CI metadata",
                ],
                docker_clues=["Docker image not implicated in this stage failure."],
                permission_clues=["Permissions are valid for current stage."],
                correct_operation="modify_config",
                correct_fix_terms=["rebase", "feature", "branch"],
                partial_fix_terms=[],
                partial_fix_reveal="",
            ),
        ],
        variants=[
            ScenarioVariant(
                variant_id="easy_v1",
                pipeline_alert_suffix="Pipeline checks also reported schema drift warnings.",
                extra_log_lines=[
                    "hint: merge auto-resolution was attempted previously and aborted",
                ],
                extra_config_clues=[
                    "ci.yaml note: strict merge mode requires branch synchronization before final checks",
                ],
            ),
            ScenarioVariant(
                variant_id="easy_v2",
                pipeline_alert_suffix="PR metadata indicates stale base revision.",
                extra_log_lines=[
                    "warning: contract test fixtures may be stale after merge attempts",
                ],
                extra_config_clues=[
                    "preflight report: branch baseline hash differs from main verification snapshot",
                ],
            ),
        ],
    ),
    "medium": ScenarioCard(
        task_id="medium_docker_dep_failure",
        task_title="Repair dependency and Docker install-order chain",
        difficulty="medium",
        benchmark="meta_hackathon",
        pipeline_alert="Build failed while installing Python dependencies in Docker.",
        initial_metrics=[
            "build.duration_seconds=427",
            "build.layer_cache_hit_ratio=0.19",
            "python.install_failures=1",
        ],
        max_steps=11,
        config_templates={
            "requirements.txt": "requests==2.20.0\nurllib3==2.1.0\n",
            "Dockerfile": "FROM python:3.11-slim\nCOPY . /app\nRUN pip install -r requirements.txt\nRUN apt-get install -y build-essential\n",
        },
        final_success_message="Pipeline recovered after dependency compatibility and Docker order fix.",
        incident_chain=[
            IncidentStep(
                stage="build",
                ambiguous_error="pip install failed with dependency resolution error",
                possible_causes=[
                    "requests/urllib3 incompatibility",
                    "bad Docker layer order causing stale constraints",
                ],
                family_term_sets=[
                    ["requests", "urllib3"],
                    ["dependency", "incompatible"],
                ],
                true_cause="requests 2.20.0 incompatible with urllib3 2.x in current lock",
                hypothesis_terms=["requests", "urllib3", "incompatible"],
                log_variants=[
                    [
                        "ERROR: requests==2.20.0 requires urllib3<1.25,>=1.21.1",
                        "Resolved dependency graph selected urllib3==2.1.0",
                    ],
                    [
                        "pip resolver conflict: cannot satisfy requests and transitive urllib3 constraints",
                        "locked graph contains urllib3 2.x while legacy SDK expects <1.25",
                    ],
                ],
                config_clues=[
                    "requirements.txt pins requests==2.20.0 and urllib3==2.1.0",
                    "last green pipeline used a compatibility override",
                ],
                docker_clues=[
                    "Dockerfile installs Python deps before apt packages, reducing reproducibility",
                    "layer cache invalidation pattern changed after requirements update",
                ],
                permission_clues=["No IAM anomaly surfaced for current failure."],
                correct_operation="add_dependency",
                correct_fix_terms=["pin", "compatible", "requests", "urllib3"],
                partial_fix_terms=[["pin", "requests"]],
                partial_fix_reveal="Dependency conflict reduced, but build still flaky due to Docker install-order mismatch.",
            ),
            IncidentStep(
                stage="build",
                ambiguous_error="docker build still unstable after dependency update",
                possible_causes=[
                    "incorrect apt/pip ordering",
                    "stale cache invalidation strategy",
                ],
                family_term_sets=[
                    ["docker", "order"],
                    ["apt", "pip"],
                ],
                true_cause="Docker install order mismatch creates unstable build layers",
                hypothesis_terms=["docker", "order", "install"],
                log_variants=[
                    [
                        "build step warning: pip wheel compile intermittently fails before apt tooling install",
                        "recommend installing system packages before Python package compilation",
                    ],
                ],
                config_clues=[
                    "CI hints: deterministic build requires apt tooling before pip install",
                ],
                docker_clues=[
                    "Dockerfile order: pip install occurs before apt-get update/install",
                ],
                permission_clues=["No IAM anomaly surfaced for current failure."],
                correct_operation="modify_config",
                correct_fix_terms=["reorder", "docker", "install"],
                partial_fix_terms=[],
                partial_fix_reveal="",
            ),
        ],
        variants=[
            ScenarioVariant(
                variant_id="medium_v1",
                pipeline_alert_suffix="Build cache key changed after dependency lock update.",
                extra_log_lines=[
                    "resolver note: lockfile produced with legacy pip resolver mode",
                ],
                extra_config_clues=[
                    "build metadata: previous green run used compatibility constraints file",
                ],
            ),
            ScenarioVariant(
                variant_id="medium_v2",
                pipeline_alert_suffix="Intermittent wheel compile failures detected.",
                extra_log_lines=[
                    "pip output: dependency backtracking exceeded expected attempts",
                ],
                extra_config_clues=[
                    "Docker diagnostics: system build tooling appears after Python dependency installation",
                ],
            ),
        ],
    ),
    "hard": ScenarioCard(
        task_id="hard_permission_timeout_chain",
        task_title="Fix deploy permission + timeout chain",
        difficulty="hard",
        benchmark="meta_hackathon",
        pipeline_alert="Deploy failed with permission denied and rollout timeout.",
        initial_metrics=[
            "deploy.timeout_seconds=900",
            "deploy.error_rate=0.44",
            "iam.denied_events=3",
        ],
        max_steps=12,
        config_templates={
            "deploy.yaml": "rollout_timeout: 900\nregistry_repo: team/api\nservice_account: ci-runner\n",
            "iam.txt": "ci-runner: artifactregistry.reader\n",
        },
        final_success_message="Deploy completed after permission repair and timeout retuning.",
        incident_chain=[
            IncidentStep(
                stage="deploy",
                ambiguous_error="rollout exceeded timeout while image publish reported denied access",
                possible_causes=[
                    "registry write permission missing",
                    "network congestion causing push timeout",
                ],
                family_term_sets=[
                    ["registry", "permission"],
                    ["push", "denied"],
                    ["timeout", "deploy"],
                ],
                true_cause="ci service account missing artifactregistry.writer role",
                hypothesis_terms=["registry", "write", "permission"],
                log_variants=[
                    [
                        "docker push denied: permission denied for repository team/api",
                        "rollout waiting for image tag that never published",
                    ],
                    [
                        "publish retries exhausted with authz denied events",
                        "deployment timed out waiting for image availability",
                    ],
                ],
                config_clues=[
                    "deploy.yaml timeout is 900s with aggressive retry policy",
                    "auth scope uses service account ci-runner",
                ],
                docker_clues=[
                    "publish command retries with exponential backoff",
                ],
                permission_clues=[
                    "service account ci-runner has artifactregistry.reader",
                    "missing role artifactregistry.writer",
                ],
                correct_operation="modify_config",
                correct_fix_terms=["grant", "artifactregistry", "writer"],
                partial_fix_terms=[["increase", "timeout"], ["20m"]],
                partial_fix_reveal="Timeout change alone increases wait time but does not fix denied push permissions.",
            ),
            IncidentStep(
                stage="deploy",
                ambiguous_error="permission resolved but rollout still close to timeout threshold",
                possible_causes=[
                    "timeout still too low for current rollout profile",
                    "residual publish retry overhead",
                ],
                family_term_sets=[
                    ["timeout", "rollout"],
                    ["tune", "timeout"],
                ],
                true_cause="timeout needs retuning after auth recovery",
                hypothesis_terms=["timeout", "rollout", "tune"],
                log_variants=[
                    [
                        "image publish now succeeds, rollout reaches 92% before timeout",
                        "recommend extending rollout timeout to absorb warm start",
                    ],
                ],
                config_clues=[
                    "deploy.yaml rollout_timeout currently 900",
                ],
                docker_clues=["Docker publish step healthy after permission fix."],
                permission_clues=["artifactregistry.writer role now present for ci-runner."],
                correct_operation="modify_config",
                correct_fix_terms=["timeout", "20m"],
                partial_fix_terms=[],
                partial_fix_reveal="",
            ),
        ],
        variants=[
            ScenarioVariant(
                variant_id="hard_v1",
                pipeline_alert_suffix="Deploy retry storm observed in last 15 minutes.",
                extra_log_lines=[
                    "authz monitor: repeated denied writes detected before timeout escalation",
                ],
                extra_config_clues=[
                    "deploy policy: timeout extension allowed only after auth remediation",
                ],
            ),
            ScenarioVariant(
                variant_id="hard_v2",
                pipeline_alert_suffix="Rollout health gates stalled after publish retries.",
                extra_log_lines=[
                    "registry event stream: push attempts queued while insufficient permissions persist",
                ],
                extra_config_clues=[
                    "sre note: resolve write permissions before adjusting timeout budget",
                ],
            ),
        ],
    ),
}


def canonical_operation(operation: str) -> str:
    """Map legacy actions to canonical operation names."""
    normalized = (operation or "").strip().lower()
    if normalized in LEGACY_OPERATION_ALIASES:
        return LEGACY_OPERATION_ALIASES[normalized]
    return normalized


def get_scenario(task_key: str) -> ScenarioCard:
    """Return scenario by key and default to medium if unknown."""
    return SCENARIOS.get(task_key, SCENARIOS["medium"])


def list_task_keys() -> List[str]:
    """Return deterministic task order for baseline runs."""
    return ["easy", "medium", "hard"]

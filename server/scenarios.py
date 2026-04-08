"""Deterministic staged CI/CD scenarios for the Meta Hackathon environment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

try:
    from .failure_patterns import sample_failure_lines_with_trace
except ImportError:
    from server.failure_patterns import sample_failure_lines_with_trace


STAGE_ORDER: List[str] = ["build", "test", "deploy"]


CANONICAL_OPERATIONS: List[str] = [
    "view_logs",
    "inspect_config",
    "inspect_dockerfile",
    "modify_config",
    "add_dependency",
    "rerun_pipeline",
    "verify_fix",
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
    "verify_pipeline": "verify_fix",
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
    pattern_buckets: List[str]
    relevant_inspections: List[str]
    config_clues: List[str]
    docker_clues: List[str]
    permission_clues: List[str]
    correct_operation: str
    correct_fix_terms: List[str]
    partial_fix_terms: List[List[str]]
    partial_fix_reveal: str
    red_herring_terms: List[List[str]]
    partial_advances: bool


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
        task_title="Resolve single-file merge conflict",
        difficulty="easy",
        benchmark="meta_hackathon",
        pipeline_alert="Build failed with merge conflict markers.",
        initial_metrics=[
            "pipeline.retry_count=0",
            "merge.conflict_files=1",
        ],
        max_steps=8,
        config_templates={
            "ci.yaml": "stages: [build, test, deploy]\nmerge_policy: strict\nbranch_sync: false\n",
            "services/api/routes.py": "<<<<<<< HEAD\nhandler_v1()\n=======\nhandler_v2()\n>>>>>>> feature",
        },
        final_success_message="Pipeline recovered after resolving merge conflict in routes.py.",
        incident_chain=[
            IncidentStep(
                stage="build",
                ambiguous_error="merge-check failed: unresolved conflict markers in routes.py",
                possible_causes=[
                    "unresolved conflict markers",
                    "stale feature branch",
                ],
                family_term_sets=[
                    ["merge", "conflict"],
                    ["stale", "branch"],
                ],
                true_cause="unresolved merge markers in services/api/routes.py",
                hypothesis_terms=["merge", "conflict"],
                log_variants=[
                    [
                        "CONFLICT (content): Merge conflict in services/api/routes.py",
                        "Automatic merge failed; fix conflicts and commit the result.",
                    ],
                    [
                        "error: Your local changes would be overwritten by merge.",
                        "merge-check exited code 1: unresolved markers in routes.py",
                    ],
                ],
                pattern_buckets=["merge_conflict"],
                relevant_inspections=["view_logs", "inspect_config"],
                config_clues=[
                    "services/api/routes.py contains <<<<<<< and >>>>>>> markers",
                    "ci.yaml merge_policy is strict and rejects unresolved conflicts",
                ],
                docker_clues=[],
                permission_clues=[],
                correct_operation="modify_config",
                correct_fix_terms=["resolve", "merge", "conflict"],
                partial_fix_terms=[],
                partial_fix_reveal="",
                red_herring_terms=[
                    ["delete", "routes"],
                    ["skip", "merge", "checks"],
                ],
                partial_advances=False,
            ),
        ],
        variants=[
            ScenarioVariant(
                variant_id="easy_v1",
                pipeline_alert_suffix="Conflict surfaced in API routing module.",
                extra_log_lines=[
                    "hint: merge auto-resolution was attempted previously and aborted",
                ],
                extra_config_clues=[
                    "git metadata: merge conflict is isolated to services/api/routes.py",
                ],
            ),
            ScenarioVariant(
                variant_id="easy_v2",
                pipeline_alert_suffix="Build stopped at merge validation gate.",
                extra_log_lines=[
                    "warning: conflict marker scan failed for routes.py",
                ],
                extra_config_clues=[
                    "ci bot note: resolve markers before branch_sync can proceed",
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
                pattern_buckets=["dependency_conflict"],
                relevant_inspections=["view_logs", "inspect_config", "inspect_dockerfile"],
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
                red_herring_terms=[
                    ["upgrade", "latest", "requests"],
                    ["remove", "urllib3"],
                ],
                partial_advances=True,
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
                pattern_buckets=["docker_layer_cache"],
                relevant_inspections=["view_logs", "inspect_dockerfile", "inspect_config"],
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
                red_herring_terms=[
                    ["clear", "cache"],
                    ["retry", "without", "change"],
                ],
                partial_advances=False,
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
    "security": ScenarioCard(
        task_id="security_secrets_iam_misconfig",
        task_title="Fix IAM writer role and exposed CI secret",
        difficulty="security",
        benchmark="meta_hackathon",
        pipeline_alert="Deploy failed with IAM denial and secret scanning policy violations.",
        initial_metrics=[
            "deploy.error_rate=0.31",
            "iam.denied_events=2",
            "secrets.scan_findings=1",
        ],
        max_steps=13,
        config_templates={
            "deploy.yaml": "service_account: ci-deployer\nregistry_repo: team/secure-api\n",
            "iam.txt": "ci-deployer: artifactregistry.reader\n",
            "Dockerfile": "FROM python:3.11-slim\nENV API_KEY=plaintext_dev_key\nCOPY . /app\nRUN pip install -r requirements.txt\n",
        },
        final_success_message="Pipeline recovered after IAM binding repair and secure secret manager usage.",
        incident_chain=[
            IncidentStep(
                stage="deploy",
                ambiguous_error="artifact push denied for service account during deploy",
                possible_causes=[
                    "missing artifact registry writer role",
                    "incorrect active service account",
                ],
                family_term_sets=[
                    ["artifactregistry", "permission"],
                    ["service", "account"],
                ],
                true_cause="ci-deployer missing roles/artifactregistry.writer",
                hypothesis_terms=["artifactregistry", "writer", "permission"],
                log_variants=[
                    [
                        "ERROR: (gcloud.artifacts.docker.push) PERMISSION_DENIED",
                        "denied: Permission 'artifactregistry.repositories.uploadArtifacts' denied",
                    ],
                    [
                        "iam.serviceAccounts.actAs permission denied on service account ci-deployer",
                        "publish failed while writing to team/secure-api registry",
                    ],
                ],
                pattern_buckets=["permission_denied"],
                relevant_inspections=["view_logs", "inspect_permissions", "inspect_config"],
                config_clues=[
                    "deploy.yaml uses service_account=ci-deployer",
                    "registry repo team/secure-api requires writer on push",
                ],
                docker_clues=[],
                permission_clues=[
                    "iam: ci-deployer currently has artifactregistry.reader only",
                ],
                correct_operation="modify_config",
                correct_fix_terms=["grant", "artifactregistry", "writer", "ci-deployer"],
                partial_fix_terms=[],
                partial_fix_reveal="",
                red_herring_terms=[
                    ["grant", "reader"],
                    ["grant", "viewer"],
                ],
                partial_advances=False,
            ),
            IncidentStep(
                stage="deploy",
                ambiguous_error="security policy blocked deploy due plaintext secret in Dockerfile",
                possible_causes=[
                    "API_KEY exposed in ENV instruction",
                    "missing secret manager reference",
                ],
                family_term_sets=[
                    ["secret", "dockerfile"],
                    ["api_key", "plaintext"],
                ],
                true_cause="Dockerfile uses ENV API_KEY plaintext instead of secret manager binding",
                hypothesis_terms=["secret", "manager", "api_key"],
                log_variants=[
                    [
                        "WARNING: secrets detected in Dockerfile ENV instruction",
                        "policy gate: plaintext API_KEY is prohibited in deploy artifact",
                    ],
                    [
                        "security scanner blocked image promotion due exposed credential",
                        "build metadata: ENV API_KEY present in Dockerfile layer history",
                    ],
                ],
                pattern_buckets=["secrets_exposed"],
                relevant_inspections=["view_logs", "inspect_dockerfile", "inspect_config"],
                config_clues=[
                    "deploy policy requires secrets sourced from secret manager",
                ],
                docker_clues=[
                    "Dockerfile contains: ENV API_KEY=plaintext_dev_key",
                ],
                permission_clues=[],
                correct_operation="modify_config",
                correct_fix_terms=["secret", "manager", "api_key"],
                partial_fix_terms=[["remove", "api_key"]],
                partial_fix_reveal=(
                    "Plaintext key removed, but deployment still needs a managed secret reference for API_KEY."
                ),
                red_herring_terms=[
                    ["base64", "api_key"],
                    ["arg", "api_key"],
                ],
                partial_advances=False,
            ),
        ],
        variants=[
            ScenarioVariant(
                variant_id="security_v1",
                pipeline_alert_suffix="Security gate activated after IAM retry burst.",
                extra_log_lines=[
                    "audit: release blocked by combined IAM and secret policy failures",
                ],
                extra_config_clues=[
                    "release checklist: writer role and secret-manager references are mandatory",
                ],
            ),
            ScenarioVariant(
                variant_id="security_v2",
                pipeline_alert_suffix="Credential scanner and deploy auth checks both failing.",
                extra_log_lines=[
                    "policy engine: plaintext credentials found in image metadata",
                ],
                extra_config_clues=[
                    "sre note: avoid Dockerfile ENV secrets; use runtime secret injection",
                ],
            ),
        ],
    ),
    "hard": ScenarioCard(
        task_id="hard_multiservice_cascade",
        task_title="Resolve multi-service registry/deploy cascade with rollback",
        difficulty="hard",
        benchmark="meta_hackathon",
        pipeline_alert="Build and deploy failing in cascade across Service A and Service B.",
        initial_metrics=[
            "build.push_failures=3",
            "deploy.error_rate=0.44",
            "iam.denied_events=4",
        ],
        max_steps=14,
        config_templates={
            "deploy.yaml": (
                "services:\n"
                "  service-a:\n"
                "    image_repo: team/service-a\n"
                "    service_account: ci-service-a\n"
                "  service-b:\n"
                "    image: team/service-b:canary\n"
                "    rollout_timeout: 900\n"
            ),
            "iam.txt": "ci-service-a: artifactregistry.reader\nci-service-b: run.admin\n",
        },
        final_success_message="Cascade recovered after Service A permission fix, rollback, and timeout tuning.",
        incident_chain=[
            IncidentStep(
                stage="build",
                ambiguous_error="service-a image push denied, blocking downstream release",
                possible_causes=[
                    "service-a registry write permission missing",
                    "transient registry network issue",
                ],
                family_term_sets=[
                    ["service-a", "registry", "permission"],
                    ["push", "denied"],
                ],
                true_cause="ci-service-a missing artifactregistry.writer role for image publish",
                hypothesis_terms=["service-a", "artifactregistry", "writer"],
                log_variants=[
                    [
                        "Service A: denied: Permission 'artifactregistry.repositories.uploadArtifacts' denied",
                        "release orchestrator: Service B deploy blocked until Service A image exists",
                    ],
                    [
                        "Service A publish retries exhausted with authz denied events",
                        "manifest assembly stopped: missing digest for team/service-a",
                    ],
                ],
                pattern_buckets=["permission_denied", "image_push_failure"],
                relevant_inspections=["view_logs", "inspect_permissions", "inspect_config"],
                config_clues=[
                    "deploy.yaml service-a uses ci-service-a for artifact publish",
                    "release DAG: service-b depends on service-a image digest",
                ],
                docker_clues=[
                    "Service A publish command fails before manifest publication",
                ],
                permission_clues=[
                    "ci-service-a currently has artifactregistry.reader",
                    "missing role artifactregistry.writer for ci-service-a",
                ],
                correct_operation="modify_config",
                correct_fix_terms=["grant", "artifactregistry", "writer", "service-a"],
                partial_fix_terms=[["timeout", "service-b"], ["increase", "rollout", "timeout"]],
                partial_fix_reveal=(
                    "Service B tuning helped symptoms, but Service A publish permission still blocks the cascade."
                ),
                red_herring_terms=[
                    ["restart", "cloud", "run"],
                    ["clear", "deploy", "cache"],
                ],
                partial_advances=False,
            ),
            IncidentStep(
                stage="deploy",
                ambiguous_error="service-b rollout references unavailable image digest",
                possible_causes=[
                    "service-b pinned to broken canary image",
                    "deploy system needs rollback to stable digest",
                ],
                family_term_sets=[
                    ["service-b", "image", "unavailable"],
                    ["rollback", "stable"],
                ],
                true_cause="service-b must rollback to last known-good image before rollout",
                hypothesis_terms=["rollback", "service-b", "stable"],
                log_variants=[
                    [
                        "Service B deploy: image team/service-b:canary not found in registry",
                        "release gate suggests rollback to stable revision while new image warms",
                    ],
                ],
                pattern_buckets=["image_unavailable", "rollout_timeout"],
                relevant_inspections=["view_logs", "inspect_config"],
                config_clues=[
                    "deploy.yaml service-b image currently set to team/service-b:canary",
                ],
                docker_clues=[],
                permission_clues=[],
                correct_operation="modify_config",
                correct_fix_terms=["rollback", "service-b", "stable"],
                partial_fix_terms=[["timeout", "20m"], ["increase", "timeout"]],
                partial_fix_reveal="Timeout tuned, but rollout still targets unavailable canary image.",
                red_herring_terms=[
                    ["scale", "service-b", "0"],
                    ["ignore", "missing", "image"],
                ],
                partial_advances=False,
            ),
            IncidentStep(
                stage="deploy",
                ambiguous_error="rollback succeeded but rollout still times out near completion",
                possible_causes=[
                    "timeout budget too low after rollback warm-up",
                    "insufficient rollout deadline for service-b",
                ],
                family_term_sets=[
                    ["rollout", "timeout"],
                    ["service-b", "deadline"],
                ],
                true_cause="rollout timeout must be increased after rollback stabilization",
                hypothesis_terms=["timeout", "20m", "service-b"],
                log_variants=[
                    [
                        "Deployment 'service-b' exceeded progress deadline after 900s",
                        "recommend increasing rollout timeout to 20m for post-rollback warm-up",
                    ],
                ],
                pattern_buckets=["rollout_timeout"],
                relevant_inspections=["view_logs", "inspect_config"],
                config_clues=[
                    "deploy.yaml service-b rollout_timeout currently 900",
                ],
                docker_clues=[],
                permission_clues=[],
                correct_operation="modify_config",
                correct_fix_terms=["timeout", "20m"],
                partial_fix_terms=[],
                partial_fix_reveal="",
                red_herring_terms=[
                    ["set", "timeout", "5m"],
                    ["keep", "900"],
                ],
                partial_advances=False,
            ),
        ],
        variants=[
            ScenarioVariant(
                variant_id="hard_v1",
                pipeline_alert_suffix="Cross-service release DAG stalled on missing artifacts.",
                extra_log_lines=[
                    "orchestrator: service-a digest missing; service-b rollout remains blocked",
                ],
                extra_config_clues=[
                    "runbook: repair upstream publisher before downstream rollout tuning",
                ],
            ),
            ScenarioVariant(
                variant_id="hard_v2",
                pipeline_alert_suffix="Service B stuck in image-unavailable and timeout loop.",
                extra_log_lines=[
                    "release monitor: stale canary reference persists until rollback is applied",
                ],
                extra_config_clues=[
                    "sre note: apply rollback before extending timeout budget",
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
    return ["easy", "medium", "security", "hard"]


def sample_logs_for_issue(issue: IncidentStep, variant_selector: int, issue_seed: int) -> List[str]:
    """Build issue logs with deterministic variant selection and sampled real-world patterns."""
    logs, _ = sample_logs_for_issue_with_trace(issue, variant_selector, issue_seed)
    return logs


def sample_logs_for_issue_with_trace(
    issue: IncidentStep,
    variant_selector: int,
    issue_seed: int,
) -> tuple[List[str], List[Dict[str, object]]]:
    """Build deterministic issue logs and include provenance trace events."""
    if not issue.log_variants:
        logs: List[str] = []
        trace_events: List[Dict[str, object]] = []
    else:
        choice = variant_selector % len(issue.log_variants)
        logs = list(issue.log_variants[choice])
        trace_events = [
            {
                "source": "scenario_variant",
                "bucket": "scenario_log_variant",
                "variant_choice": choice,
                "line_index": index,
                "issue_seed": issue_seed,
                "line": line,
            }
            for index, line in enumerate(logs)
        ]

    for bucket in issue.pattern_buckets:
        # issue_seed stabilizes repeated view_logs calls within a single episode.
        sampled_lines, sampled_events = sample_failure_lines_with_trace(bucket, sample_size=1, issue_seed=issue_seed)
        logs.extend(sampled_lines)
        trace_events.extend(sampled_events)

    return logs, trace_events

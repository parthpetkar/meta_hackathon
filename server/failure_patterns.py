"""Real-world CI/CD failure pattern library and deterministic samplers."""

from __future__ import annotations

import random
from typing import Dict, List


FAILURE_PATTERNS: Dict[str, List[str]] = {
    "merge_conflict": [
        "CONFLICT (content): Merge conflict in routes.py",
        "Automatic merge failed; fix conflicts and then commit the result.",
        "error: Your local changes would be overwritten by merge.",
        "fatal: Exiting because of unresolved conflicts.",
    ],
    "dependency_conflict": [
        "ERROR: pip's dependency resolver does not currently take into account all the packages",
        "urllib3 2.0 is incompatible with requests<2.28",
        "Could not find a version that satisfies the requirement requests==2.20.0",
        "ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-resolution/",
    ],
    "permission_denied": [
        "denied: Permission 'artifactregistry.repositories.uploadArtifacts' denied",
        "ERROR: (gcloud.artifacts.docker.push) PERMISSION_DENIED",
        "iam.serviceAccounts.actAs permission denied on service account",
        "googleapi: Error 403: The caller does not have permission",
    ],
    "secrets_exposed": [
        "WARNING: secrets detected in Dockerfile ENV instruction",
        "DL3005 Do not use apt-get dist-upgrade with ENV credentials",
        "gitleaks: detected hardcoded secret in Dockerfile",
        "policy check failed: plaintext credential found in image layer",
    ],
    "rollout_timeout": [
        "Timeout waiting for rollout to complete after 600s",
        "Deployment 'api-server' exceeded progress deadline",
        "revision failed to become ready within deadline",
        "rollout monitor: progress deadline exceeded",
    ],
    "image_unavailable": [
        "Image pull failed: manifest for team/service-b:canary not found",
        "Cloud Run deploy failed: image digest unavailable",
        "artifact reference not found in registry for requested tag",
        "back-off pulling image due to unavailable digest",
    ],
    "image_push_failure": [
        "docker push failed after retries: unauthorized",
        "publish stage failed while uploading image manifest",
        "buildx: failed to push: denied",
        "registry write operation aborted by policy engine",
    ],
    "docker_layer_cache": [
        "cache miss on dependency layer causes repeated wheel compiles",
        "build step warning: apt tooling installed after pip compile",
        "non-deterministic Docker layer ordering detected",
        "cache invalidation storm due to early COPY . in Dockerfile",
    ],
    "network_dns": [
        "Temporary failure in name resolution while fetching dependencies",
        "TLS handshake timeout contacting package index",
        "dial tcp: lookup pypi.org: no such host",
        "connection reset by peer during artifact upload",
    ],
    "test_flaky": [
        "pytest rerun plugin recovered intermittent integration failure",
        "flake detected: test passed on retry #2",
        "timing-sensitive assertion failed under CI load",
        "race condition suspected in async integration test",
    ],
}


def sample_failure_lines(bucket: str, sample_size: int = 1, issue_seed: int = 0) -> List[str]:
    """Sample deterministic lines from a bucket using a per-issue seed."""
    sampled_lines, _ = sample_failure_lines_with_trace(bucket, sample_size=sample_size, issue_seed=issue_seed)
    return sampled_lines


def sample_failure_lines_with_trace(
    bucket: str,
    sample_size: int = 1,
    issue_seed: int = 0,
) -> tuple[List[str], List[Dict[str, object]]]:
    """Sample deterministic lines and include trace metadata for auditability."""
    lines = FAILURE_PATTERNS.get(bucket, [])
    if not lines or sample_size <= 0:
        return [], []

    rng = random.Random(f"{bucket}:{issue_seed}")
    k = min(sample_size, len(lines))
    sampled_indices = rng.sample(range(len(lines)), k=k)
    sampled_lines = [lines[index] for index in sampled_indices]
    trace_events: List[Dict[str, object]] = [
        {
            "source": "pattern_library",
            "bucket": bucket,
            "line_index": index,
            "issue_seed": issue_seed,
            "line": lines[index],
        }
        for index in sampled_indices
    ]
    return sampled_lines, trace_events

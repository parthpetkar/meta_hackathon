# Provenance Mapping

This document maps runtime failure-pattern buckets to public CI/CD incident archetypes.
The environment uses curated and normalized patterns, not raw production telemetry dumps.

## Curation policy

- Source type: public docs, issue threads, and postmortem-style references.
- Transformation: normalize wording and remove organization-specific identifiers.
- Goal: preserve operational failure shape while avoiding sensitive data leakage.
- Runtime behavior: pattern lines are sampled deterministically per `(bucket, issue_seed)`.

## Bucket-to-archetype mapping

### merge_conflict

- Archetype: unresolved git merge markers blocking CI checks.
- Public reference family:
  - https://git-scm.com/docs/git-merge
  - https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/addressing-merge-conflicts

### dependency_conflict

- Archetype: Python dependency resolver incompatibility during build.
- Public reference family:
  - https://pip.pypa.io/en/stable/topics/dependency-resolution/
  - https://pip.pypa.io/en/stable/topics/dependency-resolution/#dealing-with-dependency-conflicts

### docker_layer_cache

- Archetype: unstable builds from Docker layer ordering and cache invalidation.
- Public reference family:
  - https://docs.docker.com/build/cache/
  - https://docs.docker.com/build/building/best-practices/

### permission_denied

- Archetype: artifact push/deploy blocked by IAM role gaps.
- Public reference family:
  - https://cloud.google.com/artifact-registry/docs/access-control
  - https://cloud.google.com/iam/docs/roles-overview

### secrets_exposed

- Archetype: CI/security gate blocks images with plaintext secrets.
- Public reference family:
  - https://docs.docker.com/build/building/best-practices/
  - https://github.com/gitleaks/gitleaks

### image_push_failure

- Archetype: registry publish failures causing downstream release blockage.
- Public reference family:
  - https://docs.docker.com/reference/cli/docker/image/push/
  - https://cloud.google.com/artifact-registry/docs/docker/pushing-and-pulling

### image_unavailable

- Archetype: rollout failure from missing image tags or digests.
- Public reference family:
  - https://kubernetes.io/docs/concepts/containers/images/
  - https://cloud.google.com/run/docs/deploying

### rollout_timeout

- Archetype: deployment exceeds readiness/progress deadlines.
- Public reference family:
  - https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
  - https://cloud.google.com/run/docs/troubleshooting

### network_dns

- Archetype: transient DNS/TLS dependency-fetch failures.
- Public reference family:
  - https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/
  - https://docs.docker.com/engine/network/

### test_flaky

- Archetype: non-deterministic test instability in CI.
- Public reference family:
  - https://martinfowler.com/articles/nonDeterminism.html
  - https://docs.pytest.org/en/stable/explanation/flaky.html

## Runtime audit linkage

When `META_HACKATHON_AUDIT_TRAIL=true`, each observation includes audit metadata with:

- `active_issue_pattern_buckets`
- `sampled_pattern_events` (bucket, sampled line index, issue seed, line)

This enables external evaluators to verify that emitted evidence lines come from declared buckets.

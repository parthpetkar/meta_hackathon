"""Dynamic fault injection via real file mutation and git commits.

Each fault type mutates actual files in the git working tree, stages them,
and commits with a realistic message so the pipeline genuinely fails.
"""

from __future__ import annotations

import os
import random
import subprocess
import textwrap
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class FaultMetadata:
    fault_type: str
    affected_files: List[str]
    injected_at_commit_sha: str = ""
    expected_fail_stage: str = ""
    description: str = ""
    keywords: List[str] = field(default_factory=list)


FAULT_TYPES = [
    "merge_conflict",
    "dependency_conflict",
    "docker_order",
    "flaky_test",
    "missing_permission",
    "secret_exposure",
    "env_drift",
]

# Which pipeline stage each fault causes to fail
FAULT_STAGE_MAP: Dict[str, str] = {
    "merge_conflict": "test",       # SyntaxError surfaces when pytest imports routes.py
    "dependency_conflict": "build",
    "docker_order": "build",
    "flaky_test": "test",
    "missing_permission": "deploy",
    "secret_exposure": "build",
    "env_drift": "deploy",
}

# Keywords the agent's hypothesis should contain to score positively
FAULT_KEYWORDS: Dict[str, List[str]] = {
    "merge_conflict": ["merge", "conflict", "markers", "routes"],
    "dependency_conflict": ["dependency", "incompatible", "requests", "urllib3", "pip", "version"],
    "docker_order": ["docker", "order", "copy", "install", "layer", "dockerfile"],
    "flaky_test": ["flaky", "test", "intermittent", "timing", "random", "fail"],
    "missing_permission": ["permission", "network", "deploy", "compose", "missing"],
    "secret_exposure": ["secret", "credential", "api_key", "hardcoded", "exposed", "scan"],
    "env_drift": ["environment", "variable", "compose", "port", "invalid", "deploy"],
}


# ── Git helpers ────────────────────────────────────────────────────────────

def _git_cmd(workspace: str, args: List[str], env: Optional[Dict[str, str]] = None) -> tuple[int, str, str]:
    merged_env = dict(os.environ)
    merged_env.update({
        "GIT_AUTHOR_NAME": "Developer", "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Developer", "GIT_COMMITTER_EMAIL": "dev@example.com",
    })
    if env:
        merged_env.update(env)
    result = subprocess.run(
        ["git"] + args, cwd=workspace, env=merged_env,
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _head_sha(workspace: str) -> str:
    code, stdout, _ = _git_cmd(workspace, ["rev-parse", "HEAD"])
    return stdout if code == 0 else ""


def _commit(workspace: str, message: str, files: Optional[List[str]] = None) -> str:
    if files:
        for f in files:
            _git_cmd(workspace, ["add", f])
    else:
        _git_cmd(workspace, ["add", "."])
    _git_cmd(workspace, ["commit", "-m", message])
    return _head_sha(workspace)


# ── Public API ─────────────────────────────────────────────────────────────

def inject_fault(workspace: str, fault_type: str) -> FaultMetadata:
    """Inject a fault into the workspace and return metadata describing it."""
    injectors = {
        "merge_conflict": _inject_merge_conflict,
        "dependency_conflict": _inject_dependency_conflict,
        "docker_order": _inject_docker_order,
        "flaky_test": _inject_flaky_test,
        "missing_permission": _inject_missing_permission,
        "secret_exposure": _inject_secret_exposure,
        "env_drift": _inject_env_drift,
    }
    if fault_type not in injectors:
        raise ValueError(f"Unknown fault type: {fault_type!r}. Valid: {FAULT_TYPES}")

    metadata = injectors[fault_type](workspace)
    metadata.expected_fail_stage = FAULT_STAGE_MAP[fault_type]
    metadata.keywords = FAULT_KEYWORDS[fault_type]
    return metadata


def inject_random_fault(workspace: str) -> FaultMetadata:
    return inject_fault(workspace, random.choice(FAULT_TYPES))


def undo_fault(workspace: str, fault_metadata: FaultMetadata) -> bool:
    """Revert the injected fault commit. Returns True on success."""
    if not fault_metadata.injected_at_commit_sha:
        return False
    code, _, _ = _git_cmd(workspace, ["revert", "--no-commit", fault_metadata.injected_at_commit_sha])
    if code != 0:
        return False
    _commit(workspace, f"Revert: undo {fault_metadata.fault_type} injection")
    return True


# ── Individual fault injectors ─────────────────────────────────────────────

def _inject_merge_conflict(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "routes.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace(
        '    @app.route("/health", methods=["GET"])\n'
        '    def health():\n'
        '        _log.info("Health check", extra={"request_id": getattr(g, "request_id", "")})\n'
        '        return jsonify({"status": "healthy", "service": "api"})',
        '    @app.route("/health", methods=["GET"])\n'
        '    def health():\n'
        '<<<<<<< HEAD\n'
        '        _log.info("Health check", extra={"request_id": getattr(g, "request_id", "")})\n'
        '        return jsonify({"status": "healthy", "service": "api", "version": "2.0"})\n'
        '=======\n'
        '        _log.info("Health check", extra={"request_id": getattr(g, "request_id", "")})\n'
        '        return jsonify({"status": "healthy", "service": "api"})\n'
        '>>>>>>> feature/new-health-check',
    )
    if '<<<<<<< HEAD' not in content:
        raise RuntimeError(
            "_inject_merge_conflict: replacement string not found in routes.py — "
            "template may have drifted from expected content"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "Merge branch 'feature/new-health-check' into main", [path])
    return FaultMetadata(
        fault_type="merge_conflict",
        affected_files=["services/api/routes.py"],
        injected_at_commit_sha=sha,
        description="Unresolved merge conflict markers in routes.py causing SyntaxError",
    )


def _inject_dependency_conflict(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "requirements.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("flask>=3.0.0\nrequests==2.28.0\nurllib3==2.0.7\ngunicorn>=21.2.0\n")
    sha = _commit(workspace, "chore: pin requests and urllib3 versions for stability", [path])
    return FaultMetadata(
        fault_type="dependency_conflict",
        affected_files=["services/api/requirements.txt"],
        injected_at_commit_sha=sha,
        description="Incompatible pip dependency versions: requests==2.28.0 requires urllib3<2.0",
    )


def _inject_docker_order(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "Dockerfile")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
            FROM python:3.11-slim

            WORKDIR /app

            # Wrong order: install before copying requirements into image
            RUN uv pip install --system --no-cache -r services/api/requirements.txt

            # Copy application code too late
            COPY . /app/

            EXPOSE 5000

            CMD ["python", "-m", "services.api.app"]
        """))
    sha = _commit(workspace, "refactor: simplify Dockerfile build steps", [path])
    return FaultMetadata(
        fault_type="docker_order",
        affected_files=["Dockerfile"],
        injected_at_commit_sha=sha,
        description="Dockerfile installs services/api/requirements.txt before COPY, so file is unavailable at build time",
    )


def _inject_flaky_test(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "tests", "test_api.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    flaky = textwrap.dedent('''

    def test_response_time_health(client):
        """Timing-sensitive test that fails ~60% of CI runs."""
        import time, random
        random.seed(time.time_ns())
        start = time.time()
        time.sleep(0.1)
        response = client.get("/health")
        elapsed = time.time() - start
        assert response.status_code == 200
        threshold = 0.05 if random.random() < 0.6 else 0.5
        assert elapsed < threshold, (
            f"Health endpoint took {elapsed:.3f}s, expected < {threshold}s."
        )
    ''')
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + flaky)

    sha = _commit(workspace, "test: add response time check for health endpoint", [path])
    return FaultMetadata(
        fault_type="flaky_test",
        affected_files=["tests/test_api.py"],
        injected_at_commit_sha=sha,
        description="Timing-sensitive test with random threshold fails ~60% of runs",
    )


def _inject_missing_permission(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "docker-compose.yml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
            version: "3.8"

            services:
              api:
                build:
                  context: .
                  dockerfile: Dockerfile
                ports:
                  - "5000:5000"
                environment:
                  - FLASK_ENV=production
                networks:
                  - restricted_internal_net
                volumes:
                  - /opt/nonexistent-shared-data:/app/data:ro
                healthcheck:
                  test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
                  interval: 30s
                  timeout: 5s
                  retries: 3

            networks:
              restricted_internal_net:
                external: true
                name: corp-internal-network-v2
        """))
    sha = _commit(workspace, "infra: connect api service to internal corporate network", [path])
    return FaultMetadata(
        fault_type="missing_permission",
        affected_files=["docker-compose.yml"],
        injected_at_commit_sha=sha,
        description="docker-compose references non-existent external network 'corp-internal-network-v2'",
    )


def _inject_secret_exposure(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "app.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    secret_block = textwrap.dedent('''\
        # Third-party API integration credentials
        API_KEY = "sk-live-4f3c2a1b0e9d8c7f6a5b4e3d2c1a0f9e8d7c6b5a"
        DATABASE_PASSWORD = "super_secret_db_password_2024"
        WEBHOOK_SECRET = "whsec_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"

    ''')

    lines = content.split("\n")
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.startswith(("from ", "import ")):
            insert_idx = i + 1
        elif insert_idx > 0 and not line.strip():
            insert_idx = i + 1
            break
    lines.insert(insert_idx, secret_block)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    sha = _commit(workspace, "feat: add third-party API integration", [path])
    return FaultMetadata(
        fault_type="secret_exposure",
        affected_files=["services/api/app.py"],
        injected_at_commit_sha=sha,
        description="Hardcoded API keys and secrets exposed in source code",
    )


def _inject_env_drift(workspace: str) -> FaultMetadata:
        path = os.path.join(workspace, "docker-compose.yml")
        with open(path, "w", encoding="utf-8") as f:
                f.write(textwrap.dedent("""\
                        version: "3.8"

                        services:
                            api:
                                build:
                                    context: .
                                    dockerfile: Dockerfile
                                ports:
                                    - "not-a-number:5000"
                                environment:
                                    - FLASK_ENV=production
                                    - PORT=not-a-number
                                healthcheck:
                                    test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
                                    interval: 30s
                                    timeout: 5s
                                    retries: 3
                """))
        sha = _commit(workspace, "infra: externalize PORT via env for deploy standardization", [path])
        return FaultMetadata(
                fault_type="env_drift",
                affected_files=["docker-compose.yml"],
                injected_at_commit_sha=sha,
                description="Invalid runtime env var PORT=not-a-number breaks docker compose deploy port mapping",
        )

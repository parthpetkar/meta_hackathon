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
    affected_apps: List[str] = field(default_factory=list)
    cascade_faults: List[str] = field(default_factory=list)
    red_herring: str = ""


FAULT_TYPES = [
    "merge_conflict",
    "dependency_conflict",
    "docker_order",
    "flaky_test",
    "missing_permission",
    "secret_exposure",
    "env_drift",
    # Logging / observability faults
    "log_pii_leak",
    "log_disabled",
]

# Database-specific faults (used when scenario requests DB-targeted failures)
DB_FAULT_TYPES = [
    "bad_migration_sql",
    "schema_drift",
]

# Expose combined fault types for generators that want the full set
FAULT_TYPES = FAULT_TYPES + DB_FAULT_TYPES

# Which pipeline stage each fault causes to fail
FAULT_STAGE_MAP: Dict[str, str] = {
    # DB faults
    "bad_migration_sql": "build",
    "schema_drift": "build",
    "merge_conflict": "test",       # SyntaxError surfaces when pytest imports routes.py
    "dependency_conflict": "build",
    "docker_order": "build",
    "flaky_test": "test",
    "missing_permission": "deploy",
    "secret_exposure": "build",
    "env_drift": "deploy",
    # Logging faults
    "log_pii_leak":         "build",  # static PII pattern found in routes.py
    "log_disabled":         "build",  # CRITICAL level silences all output
}

# Keywords the agent's hypothesis should contain to score positively
FAULT_KEYWORDS: Dict[str, List[str]] = {
    "bad_migration_sql": ["sql", "syntax", "migration"],
    "schema_drift": ["schema", "mismatch", "column"],
    "merge_conflict": ["merge", "conflict", "markers", "routes"],
    "dependency_conflict": ["dependency", "incompatible", "requests", "urllib3", "pip", "version"],
    "docker_order": ["docker", "order", "copy", "install", "layer", "dockerfile"],
    "flaky_test": ["flaky", "test", "intermittent", "timing", "random", "fail"],
    "missing_permission": ["permission", "network", "deploy", "compose", "missing"],
    "secret_exposure": ["secret", "credential", "api_key", "hardcoded", "exposed", "scan"],
    "env_drift": ["environment", "variable", "compose", "port", "invalid", "deploy"],
    "log_pii_leak":         ["logging", "pii", "credential", "token", "secret", "leak", "routes"],
    "log_disabled":         ["logging", "level", "critical", "disabled", "silent", "log_level"],
}

# Apps affected by each multi-app fault (single-app faults leave this empty)
FAULT_AFFECTED_APPS: Dict[str, List[str]] = {}


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
        "log_pii_leak":      _inject_log_pii_leak,
        "log_disabled":      _inject_log_disabled,
        # DB faults
        "bad_migration_sql": _inject_bad_migration_sql,
        "schema_drift":      _inject_schema_drift,
    }
    if fault_type not in injectors:
        raise ValueError(f"Unknown fault type: {fault_type!r}. Valid: {FAULT_TYPES}")

    metadata = injectors[fault_type](workspace)
    metadata.expected_fail_stage = FAULT_STAGE_MAP[fault_type]
    metadata.keywords = FAULT_KEYWORDS[fault_type]
    metadata.affected_apps = FAULT_AFFECTED_APPS.get(fault_type, [])
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
        """Timing-sensitive test that fails intermittently due to tight threshold."""
        import time
        start = time.time()
        time.sleep(0.1)
        response = client.get("/health")
        elapsed = time.time() - start
        assert response.status_code == 200
        # Threshold is unrealistically tight — always fails after the sleep
        threshold = 0.001
        assert elapsed < threshold, (
            f"Health endpoint took {elapsed:.3f}s, expected < {threshold}s "
            f"(flaky: timing constraint too strict for this environment)."
        )
    ''')
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + flaky)

    sha = _commit(workspace, "test: add response time check for health endpoint", [path])
    return FaultMetadata(
        fault_type="flaky_test",
        affected_files=["tests/test_api.py"],
        injected_at_commit_sha=sha,
        description="Timing-sensitive test with impossibly tight threshold always fails — needs retry policy or relaxed threshold",
    )


def _inject_missing_permission(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "docker-compose.yml")
    # Read existing content to preserve environment variables and volumes
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()

    # Inject a deploy section that requires a named Docker network that does not exist.
    # Using 'cap_add: NET_ADMIN' with a missing network makes compose fail reliably at
    # container-start time with a permission/network error, regardless of Docker version.
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
                  - LOG_PATH=/app/logs/app.log
                  - LOG_LEVEL=INFO
                  - SERVICE_NAME=api
                volumes:
                  - ./logs:/app/logs
                networks:
                  - corp-internal-network-v2
                healthcheck:
                  test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
                  interval: 30s
                  timeout: 5s
                  retries: 3

            networks:
              corp-internal-network-v2:
                external: true
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


# ── Logging / observability fault injectors ────────────────────────────────


def _inject_log_pii_leak(workspace: str) -> FaultMetadata:
    """Add a log statement in routes.py that emits a plaintext API token."""
    path = os.path.join(workspace, "services", "api", "routes.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Insert after the module-level logger assignment
    leak_line = (
        '\n# FAULT(log_pii_leak): credential value logged directly\n'
        '_log.warning(\n'
        '    "Auth token received: sk-live-4f3c2a1b0e9d8c7f6a5b4e3d2c1a0f9e8d7c6b5a",\n'
        ')\n'
    )
    content = content.replace(
        '_log = logging.getLogger("api.routes")',
        '_log = logging.getLogger("api.routes")' + leak_line,
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "debug: add auth diagnostics for token validation", [path])
    return FaultMetadata(
        fault_type="log_pii_leak",
        affected_files=["services/api/routes.py"],
        injected_at_commit_sha=sha,
        description="routes.py logs a plaintext sk-live- API token — check_logs detects PII in static scan",
    )


def _inject_log_disabled(workspace: str) -> FaultMetadata:
    """Hardcode LOG_LEVEL to CRITICAL, silencing all INFO/WARN/ERROR output."""
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace(
        'LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()',
        'LOG_LEVEL: str = "CRITICAL"  # FAULT(log_disabled): hardcoded, overrides env var',
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "perf: suppress non-critical log output in production", [path])
    return FaultMetadata(
        fault_type="log_disabled",
        affected_files=["services/api/logging_config.py"],
        injected_at_commit_sha=sha,
        description="LOG_LEVEL hardcoded to CRITICAL — effective logging disabled, check_logs config check fails",
    )


# ── DB fault injectors ────────────────────────────────────────────────────


def _inject_bad_migration_sql(workspace: str) -> FaultMetadata:
    """Introduce a SQL syntax error in the migration file."""
    path = os.path.join(workspace, "db", "migrations", "001_init.sql")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    new_content = content.replace(
        "CREATE TABLE IF NOT EXISTS builds",
        "CREAT TABLE IF NOT EXISTS builds",
        1,
    )
    if new_content == content:
        raise RuntimeError(
            "_inject_bad_migration_sql: 'CREATE TABLE IF NOT EXISTS builds' not found — "
            "template may have drifted"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    sha = _commit(workspace, "db: add migration for builds table", [path])
    return FaultMetadata(
        fault_type="bad_migration_sql",
        affected_files=["db/migrations/001_init.sql"],
        injected_at_commit_sha=sha,
        description="SQL syntax error in 001_init.sql: 'CREAT TABLE' — database fails to initialise",
    )


def _inject_schema_drift(workspace: str) -> FaultMetadata:
    """Add a phantom column to CANONICAL_COLUMNS that the schema never defines."""
    path = os.path.join(workspace, "db", "database.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    old = '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail"'
    new = '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail", "artifact_url"'
    if old not in content:
        raise RuntimeError(
            "_inject_schema_drift: expected CANONICAL_COLUMNS string not found in db/database.py"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.replace(old, new, 1))

    sha = _commit(workspace, "feat: track artifact URL in builds table", [path])
    return FaultMetadata(
        fault_type="schema_drift",
        affected_files=["db/database.py"],
        injected_at_commit_sha=sha,
        description="CANONICAL_COLUMNS includes 'artifact_url' but no migration adds it — queries fail",
    )



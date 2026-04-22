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
    "log_bad_config",
    "log_path_unwritable",
    "log_volume_missing",
    "log_rotation_missing",
    "log_pii_leak",
    "log_disabled",
    # Multi-app cross-service faults
    "shared_secret_rotation",
    "infra_port_conflict",
    "dependency_version_drift",
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
    # Logging faults — all caught by check_logs.py running after docker build
    "log_bad_config":       "build",  # non-JSON output detected by check_logs
    "log_path_unwritable":  "build",  # LOG_PATH points to restricted dir
    "log_volume_missing":   "deploy", # log file unreachable from outside container
    "log_rotation_missing": "build",  # RotatingFileHandler absent — config check fails
    "log_pii_leak":         "build",  # static PII pattern found in routes.py
    "log_disabled":         "build",  # CRITICAL level silences all output
    # Multi-app cross-service faults
    "shared_secret_rotation":    "deploy",  # services reject auth after secret rotated in .env
    "infra_port_conflict":       "deploy",  # port binding fails; frontend can't reach api-service
    "dependency_version_drift":  "build",   # pip resolution impossible in api-service
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
    "log_bad_config":       ["logging", "config", "json", "formatter", "malformed", "structured"],
    "log_path_unwritable":  ["logging", "path", "permission", "writable", "log_path", "directory"],
    "log_volume_missing":   ["logging", "volume", "mount", "compose", "logs", "missing"],
    "log_rotation_missing": ["logging", "rotation", "rotating", "filehandler", "max_bytes", "unbounded"],
    "log_pii_leak":         ["logging", "pii", "credential", "token", "secret", "leak", "routes"],
    "log_disabled":         ["logging", "level", "critical", "disabled", "silent", "log_level"],
    # Multi-app cross-service faults
    "shared_secret_rotation":   ["secret", "auth", "rotation", "env", "shared", ".env", "authentication", "credential"],
    "infra_port_conflict":      ["port", "conflict", "binding", "docker-compose", "api-service", "frontend", "address"],
    "dependency_version_drift": ["dependency", "version", "drift", "incompatible", "fastapi", "pydantic", "requirements", "resolution"],
}

# Apps affected by each multi-app fault (single-app faults leave this empty)
FAULT_AFFECTED_APPS: Dict[str, List[str]] = {
    "shared_secret_rotation":   ["api-service", "worker"],
    "infra_port_conflict":      ["frontend", "api-service"],
    "dependency_version_drift": ["api-service", "worker"],
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
        "log_bad_config":       _inject_log_bad_config,
        "log_path_unwritable":  _inject_log_path_unwritable,
        "log_volume_missing":   _inject_log_volume_missing,
        "log_rotation_missing": _inject_log_rotation_missing,
        "log_pii_leak":         _inject_log_pii_leak,
        "log_disabled":         _inject_log_disabled,
        # Multi-app cross-service faults
        "shared_secret_rotation":   _inject_shared_secret_rotation,
        "infra_port_conflict":      _inject_infra_port_conflict,
        "dependency_version_drift": _inject_dependency_version_drift,
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


# ── Logging / observability fault injectors ────────────────────────────────
#
# All six modify files in the workspace and commit the change so the pipeline
# genuinely fails when check_logs.py (or the app itself) is exercised.


def _inject_log_bad_config(workspace: str) -> FaultMetadata:
    """Replace json.dumps with str() so the formatter emits Python dicts, not JSON."""
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Replace the JSON serialisation call — logs become non-JSON, breaking check_logs
    content = content.replace(
        "return json.dumps(payload, ensure_ascii=False)",
        "return str(payload)  # FAULT(log_bad_config): not valid JSON output",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "refactor: simplify log formatter for performance", [path])
    return FaultMetadata(
        fault_type="log_bad_config",
        affected_files=["services/api/logging_config.py"],
        injected_at_commit_sha=sha,
        description="Formatter returns Python str(dict) instead of JSON — check_logs detects malformed records",
    )


def _inject_log_path_unwritable(workspace: str) -> FaultMetadata:
    """Change LOG_PATH default to a root-owned directory the app process cannot write."""
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Hardcode LOG_PATH to bypass the env override and point to an unwritable system dir
    content = content.replace(
        'LOG_PATH: str = os.environ.get("LOG_PATH", "/app/logs/app.log")',
        'LOG_PATH: str = "/var/log/restricted/app.log"  # FAULT(log_path_unwritable)',
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "ops: centralise log output to system log directory", [path])
    return FaultMetadata(
        fault_type="log_path_unwritable",
        affected_files=["services/api/logging_config.py"],
        injected_at_commit_sha=sha,
        description="LOG_PATH default changed to /var/log/restricted/app.log — restricted directory, write fails",
    )


def _inject_log_volume_missing(workspace: str) -> FaultMetadata:
    """Comment out the logs volume mount so the log file is inaccessible from the host."""
    path = os.path.join(workspace, "docker-compose.yml")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace(
        "      - ./logs:/app/logs",
        "      # FAULT(log_volume_missing): log volume mount removed\n      # - ./logs:/app/logs",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "infra: remove ephemeral log volume (logs now in container only)", [path])
    return FaultMetadata(
        fault_type="log_volume_missing",
        affected_files=["docker-compose.yml"],
        injected_at_commit_sha=sha,
        description="Log volume mount commented out — check_logs cannot reach the log file from the host",
    )


def _inject_log_rotation_missing(workspace: str) -> FaultMetadata:
    """Replace RotatingFileHandler with plain FileHandler — logs grow unboundedly."""
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    import re as _re
    content = _re.sub(
        r"logging\.handlers\.RotatingFileHandler\(\s*\n?"
        r"\s*str\(path\), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding=\"utf-8\"\s*\n?"
        r"\s*\)",
        'logging.FileHandler(\n            str(path), encoding="utf-8"\n        )',
        content,
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "chore: simplify file handler — remove unused rotation config", [path])
    return FaultMetadata(
        fault_type="log_rotation_missing",
        affected_files=["services/api/logging_config.py"],
        injected_at_commit_sha=sha,
        description="RotatingFileHandler replaced with FileHandler — log rotation disabled, disk exhaustion risk",
    )


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


# ── Multi-app cross-service fault injectors ────────────────────────────────
#
# These faults mutate files under shared-infra/ or sibling app directories,
# causing failures that cascade across multiple services.


def _inject_shared_secret_rotation(workspace: str) -> FaultMetadata:
    """Rotate AUTH_SECRET in shared-infra/.env without updating dependent services."""
    path = os.path.join(workspace, "shared-infra", ".env")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace(
        "AUTH_SECRET=initial-shared-secret-value-abc123",
        "AUTH_SECRET=rotated-secret-xyz789-NEW  # FAULT(shared_secret_rotation)",
    )
    if "rotated-secret-xyz789-NEW" not in content:
        raise RuntimeError(
            "_inject_shared_secret_rotation: expected AUTH_SECRET line not found in shared-infra/.env"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "ops: rotate shared authentication secret for compliance", [path])
    return FaultMetadata(
        fault_type="shared_secret_rotation",
        affected_files=["shared-infra/.env"],
        injected_at_commit_sha=sha,
        description=(
            "AUTH_SECRET rotated in shared-infra/.env but running api-service and worker "
            "still hold the old value — all authenticated requests fail with 401"
        ),
        cascade_faults=["auth_token_mismatch", "worker_api_call_rejected"],
        red_herring=(
            "frontend shows HTTP 503 (downstream of api-service auth failure, not root cause)"
        ),
    )


def _inject_infra_port_conflict(workspace: str) -> FaultMetadata:
    """Remap api-service to port 5000, conflicting with the frontend binding."""
    path = os.path.join(workspace, "shared-infra", "docker-compose.yml")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace(
        '      - "8000:8000"',
        '      - "5000:8000"  # FAULT(infra_port_conflict): clashes with frontend port',
    )
    if "FAULT(infra_port_conflict)" not in content:
        raise RuntimeError(
            "_inject_infra_port_conflict: api-service port line not found in shared-infra/docker-compose.yml"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    sha = _commit(workspace, "infra: standardize all service ports to single range", [path])
    return FaultMetadata(
        fault_type="infra_port_conflict",
        affected_files=["shared-infra/docker-compose.yml"],
        injected_at_commit_sha=sha,
        description=(
            "api-service host port changed to 5000, conflicting with frontend — "
            "docker compose up fails to bind; frontend cannot reach api-service"
        ),
        cascade_faults=["port_bind_failure", "frontend_502_bad_gateway"],
        red_herring=(
            "api-service container logs show SIGTERM (killed due to port conflict, not a code bug)"
        ),
    )


def _inject_dependency_version_drift(workspace: str) -> FaultMetadata:
    """Pin fastapi==0.68.0 + pydantic>=2.0.0 in api-service — incompatible versions."""
    path = os.path.join(workspace, "api-service", "requirements.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "fastapi==0.68.0\n"
            "uvicorn>=0.23.0\n"
            "httpx>=0.24.0\n"
            "pydantic>=2.0.0\n"
            "# FAULT(dependency_version_drift): fastapi 0.68 requires pydantic<2\n"
        )

    sha = _commit(workspace, "deps: pin fastapi version for security audit compliance", [path])
    return FaultMetadata(
        fault_type="dependency_version_drift",
        affected_files=["api-service/requirements.txt"],
        injected_at_commit_sha=sha,
        description=(
            "fastapi==0.68.0 requires pydantic<2 but pydantic>=2.0.0 is pinned — "
            "pip resolution impossible; api-service build fails, worker loses its upstream"
        ),
        cascade_faults=["pip_resolution_impossible", "worker_cannot_reach_api_service"],
        red_herring=(
            "worker logs show ConnectionRefusedError (downstream of api-service build failure, "
            "not a worker bug)"
        ),
    )

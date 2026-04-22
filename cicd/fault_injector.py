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
    # Strings actually injected into files — used to derive keywords from
    # fault behavior rather than a separate hand-maintained list.
    injected_strings: List[str] = field(default_factory=list)
    # Regex patterns that will appear in pipeline output when this fault fires.
    # Hypothesis scoring can match against these instead of FAULT_KEYWORDS.
    expected_error_patterns: List[str] = field(default_factory=list)


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

# Database-specific faults (used when scenario requests DB-targeted failures)
DB_FAULT_TYPES = [
    "bad_migration_sql",
    "schema_drift",
    "wrong_db_url",
    "init_order_race",
    "missing_volume_mount",
]

# Expose combined fault types for generators that want the full set
FAULT_TYPES = FAULT_TYPES + DB_FAULT_TYPES

# Which pipeline stage each fault causes to fail
FAULT_STAGE_MAP: Dict[str, str] = {
    # DB faults
    "bad_migration_sql": "build",
    "schema_drift": "build",
    "wrong_db_url": "deploy",
    "init_order_race": "deploy",
    "missing_volume_mount": "deploy",
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
    "bad_migration_sql": ["sql", "syntax", "migration"],
    "schema_drift": ["schema", "mismatch", "column"],
    "wrong_db_url": ["database", "url", "connection"],
    "init_order_race": ["startup", "race", "dependency"],
    "missing_volume_mount": ["volume", "mount", "database"],
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

# Regex patterns that will appear in real pipeline output when each fault fires.
# These are derived from the actual mutations, not hand-maintained separately.
# Used by semantic/hybrid hypothesis scoring as ground-truth signal.
FAULT_ERROR_PATTERNS: Dict[str, List[str]] = {
    "merge_conflict":        [r"SyntaxError", r"<<<<<<", r"invalid syntax"],
    "dependency_conflict":   [r"ResolutionImpossible", r"urllib3", r"requests.*2\.28"],
    "docker_order":          [r"COPY.*requirements", r"no such file.*requirements"],
    "flaky_test":            [r"test_response_time", r"elapsed.*expected"],
    "missing_permission":    [r"network.*not found", r"corp-internal-network"],
    "secret_exposure":       [r"SECRET SCAN FAILED", r"sk-live-", r"hardcoded"],
    "env_drift":             [r"not-a-number", r"invalid.*port", r"PORT"],
    "log_bad_config":        [r"str\(payload\)", r"not valid JSON", r"malformed"],
    "log_path_unwritable":   [r"/var/log/restricted", r"Permission denied", r"LOG_PATH"],
    "log_volume_missing":    [r"log volume mount removed", r"logs:/app/logs", r"FAULT\(log_volume"],
    "log_rotation_missing":  [r"FileHandler", r"RotatingFileHandler", r"rotation"],
    "log_pii_leak":          [r"sk-live-", r"Auth token received", r"FAULT\(log_pii"],
    "log_disabled":          [r"CRITICAL", r"FAULT\(log_disabled\)", r"log_level"],
    "shared_secret_rotation": [r"AUTH_SECRET", r"rotated-secret", r"401"],
    "infra_port_conflict":   [r"5000:9000.*FAULT", r"port.*conflict", r"address already in use"],
    "dependency_version_drift": [r"fastapi", r"pydantic", r"ResolutionImpossible"],
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
        # DB faults
        "bad_migration_sql":    _inject_bad_migration_sql,
        "schema_drift":         _inject_schema_drift,
        "wrong_db_url":         _inject_wrong_db_url,
        "init_order_race":      _inject_init_order_race,
        "missing_volume_mount": _inject_missing_volume_mount,
    }
    if fault_type not in injectors:
        raise ValueError(f"Unknown fault type: {fault_type!r}. Valid: {FAULT_TYPES}")

    metadata = injectors[fault_type](workspace)
    metadata.expected_fail_stage = FAULT_STAGE_MAP[fault_type]
    metadata.keywords = FAULT_KEYWORDS[fault_type]
    metadata.affected_apps = FAULT_AFFECTED_APPS.get(fault_type, [])
    metadata.expected_error_patterns = FAULT_ERROR_PATTERNS.get(fault_type, [])
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

    old = "return json.dumps(payload, ensure_ascii=False)"
    new = "return str(payload)  # FAULT(log_bad_config): not valid JSON output"
    new_content = content.replace(old, new)
    if new_content == content:
        raise RuntimeError(
            "_inject_log_bad_config: target string not found in logging_config.py — "
            "template may have drifted from expected content"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

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

    old = 'LOG_PATH: str = os.environ.get("LOG_PATH", "/app/logs/app.log")'
    new = 'LOG_PATH: str = "/var/log/restricted/app.log"  # FAULT(log_path_unwritable)'
    new_content = content.replace(old, new)
    if new_content == content:
        raise RuntimeError(
            "_inject_log_path_unwritable: LOG_PATH line not found in logging_config.py — "
            "template may have drifted from expected content"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    sha = _commit(workspace, "ops: centralise log output to system log directory", [path])
    return FaultMetadata(
        fault_type="log_path_unwritable",
        affected_files=["services/api/logging_config.py"],
        injected_at_commit_sha=sha,
        description="LOG_PATH default changed to /var/log/restricted/app.log — restricted directory, write fails",
    )


def _inject_log_volume_missing(workspace: str) -> FaultMetadata:
    """Comment out the logs volume mount so the log file is inaccessible from the host."""
    # The volume mount lives in shared-infra/docker-compose.yml, not the root compose file
    path = os.path.join(workspace, "shared-infra", "docker-compose.yml")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    new_content = content.replace(
        "      - ../logs:/app/logs",
        "      # FAULT(log_volume_missing): log volume mount removed\n      # - ../logs:/app/logs",
    )
    if new_content == content:
        raise RuntimeError(
            "_inject_log_volume_missing: volume mount line not found in shared-infra/docker-compose.yml — "
            "template may have drifted from expected content"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    sha = _commit(workspace, "infra: remove ephemeral log volume (logs now in container only)", [path])
    return FaultMetadata(
        fault_type="log_volume_missing",
        affected_files=["shared-infra/docker-compose.yml"],
        injected_at_commit_sha=sha,
        description="Log volume mount commented out — check_logs cannot reach the log file from the host",
    )


def _inject_log_rotation_missing(workspace: str) -> FaultMetadata:
    """Replace RotatingFileHandler with plain FileHandler — logs grow unboundedly."""
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    import re as _re
    new_content = _re.sub(
        r"logging\.handlers\.RotatingFileHandler\(\s*\n?"
        r"\s*str\(path\), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding=\"utf-8\"\s*\n?"
        r"\s*\)",
        'logging.FileHandler(\n            str(path), encoding="utf-8"\n        )',
        content,
    )
    if new_content == content:
        raise RuntimeError(
            "_inject_log_rotation_missing: RotatingFileHandler pattern not found in logging_config.py — "
            "template may have drifted from expected content"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

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

    anchor = '_log = logging.getLogger("api.routes")'
    if anchor not in content:
        raise RuntimeError(
            "_inject_log_pii_leak: logger assignment not found in routes.py — "
            "template may have drifted from expected content"
        )
    leak_line = (
        '\n# FAULT(log_pii_leak): credential value logged directly\n'
        '_log.warning(\n'
        '    "Auth token received: sk-live-4f3c2a1b0e9d8c7f6a5b4e3d2c1a0f9e8d7c6b5a",\n'
        ')\n'
    )
    new_content = content.replace(anchor, anchor + leak_line)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

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

    old = 'LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()'
    new = 'LOG_LEVEL: str = "CRITICAL"  # FAULT(log_disabled): hardcoded, overrides env var'
    new_content = content.replace(old, new)
    if new_content == content:
        raise RuntimeError(
            "_inject_log_disabled: LOG_LEVEL line not found in logging_config.py — "
            "template may have drifted from expected content"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    sha = _commit(workspace, "perf: suppress non-critical log output in production", [path])
    return FaultMetadata(
        fault_type="log_disabled",
        affected_files=["services/api/logging_config.py"],
        injected_at_commit_sha=sha,
        description="LOG_LEVEL hardcoded to CRITICAL — effective logging disabled, check_logs config check fails",
    )


# ── Multi-app cross-service fault injectors ────────────────────────────────

def _inject_shared_secret_rotation(workspace: str) -> FaultMetadata:
    """Rotate AUTH_SECRET in shared-infra/.env for only one service, breaking auth."""
    env_path = os.path.join(workspace, "shared-infra", ".env")
    # Fall back to root .env if the multi-app layout isn't present
    if not os.path.exists(env_path):
        env_path = os.path.join(workspace, ".env")

    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Rotate the secret in the shared env file — worker still has the old value
    # baked into its image layer, so auth between services breaks at deploy time.
    old_line = "AUTH_SECRET=initial-shared-secret-value-abc123"
    new_line = "AUTH_SECRET=rotated-secret-xyz789  # FAULT(shared_secret_rotation)"
    new_content = content.replace(old_line, new_line)
    if new_content == content:
        # env file may not have the exact line; append a conflicting override
        new_content = content.rstrip("\n") + "\nAUTH_SECRET=rotated-secret-xyz789  # FAULT(shared_secret_rotation)\n"

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    affected = [os.path.relpath(env_path, workspace).replace("\\", "/")]
    sha = _commit(workspace, "ops: rotate shared AUTH_SECRET (partial rollout)", affected)
    return FaultMetadata(
        fault_type="shared_secret_rotation",
        affected_files=affected,
        injected_at_commit_sha=sha,
        description=(
            "AUTH_SECRET rotated in shared .env but worker image still carries the old value — "
            "inter-service authentication fails with 401 at deploy time"
        ),
        injected_strings=["rotated-secret-xyz789", "AUTH_SECRET"],
        expected_error_patterns=[r"AUTH_SECRET", r"rotated-secret", r"401"],
    )


def _inject_infra_port_conflict(workspace: str) -> FaultMetadata:
    """Introduce a port mapping conflict in shared-infra/docker-compose.yml."""
    compose_path = os.path.join(workspace, "shared-infra", "docker-compose.yml")
    if not os.path.exists(compose_path):
        compose_path = os.path.join(workspace, "docker-compose.yml")

    with open(compose_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Map the frontend to port 5000 on the host but forward to container port 9000
    # (same port the api-service already binds), causing a binding conflict.
    old_ports = '      - "5000:5000"'
    new_ports = '      - "5000:9000"  # FAULT(infra_port_conflict): wrong container port'
    new_content = content.replace(old_ports, new_ports, 1)
    if new_content == content:
        # Fallback: append a duplicate port binding for api-service
        new_content = content.replace(
            '      - "9000:9000"',
            '      - "9000:9000"\n      - "5000:9000"  # FAULT(infra_port_conflict)',
            1,
        )

    with open(compose_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    affected = [os.path.relpath(compose_path, workspace).replace("\\", "/")]
    sha = _commit(workspace, "infra: update port mappings for staging environment", affected)
    return FaultMetadata(
        fault_type="infra_port_conflict",
        affected_files=affected,
        injected_at_commit_sha=sha,
        description=(
            "docker-compose port mapping changed to 5000:9000 — conflicts with api-service binding, "
            "frontend cannot reach api-service at deploy time"
        ),
        injected_strings=["5000:9000", "FAULT(infra_port_conflict)"],
        expected_error_patterns=[r"5000:9000.*FAULT", r"port.*conflict", r"address already in use"],
    )


def _inject_dependency_version_drift(workspace: str) -> FaultMetadata:
    """Pin incompatible fastapi/pydantic versions in api-service/requirements.txt."""
    req_path = os.path.join(workspace, "api-service", "requirements.txt")
    if not os.path.exists(req_path):
        req_path = os.path.join(workspace, "services", "api", "requirements.txt")

    with open(req_path, "r", encoding="utf-8") as f:
        content = f.read()

    # pydantic v1 is incompatible with fastapi>=0.100 which requires pydantic v2
    old = "fastapi>=0.100.0"
    new = "fastapi>=0.100.0\npydantic==1.10.13  # FAULT(dependency_version_drift): incompatible with fastapi>=0.100"
    new_content = content.replace(old, new, 1)
    if new_content == content:
        # requirements.txt may not have the exact line; append the conflict
        new_content = content.rstrip("\n") + "\npydantic==1.10.13  # FAULT(dependency_version_drift)\n"

    with open(req_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    affected = [os.path.relpath(req_path, workspace).replace("\\", "/")]
    sha = _commit(workspace, "deps: pin pydantic to stable v1 release", affected)
    return FaultMetadata(
        fault_type="dependency_version_drift",
        affected_files=affected,
        injected_at_commit_sha=sha,
        description=(
            "pydantic==1.10.13 pinned in api-service/requirements.txt — "
            "incompatible with fastapi>=0.100.0 which requires pydantic v2; pip resolution fails at build"
        ),
        injected_strings=["pydantic==1.10.13", "FAULT(dependency_version_drift)"],
        expected_error_patterns=[r"fastapi", r"pydantic", r"ResolutionImpossible"],
    )


# ── DB fault injectors ─────────────────────────────────────────────────────

def _inject_bad_migration_sql(workspace: str) -> FaultMetadata:
    """Introduce a SQL syntax error in the init migration file."""
    migration_path = os.path.join(workspace, "db", "migrations", "001_init.sql")
    if not os.path.exists(migration_path):
        os.makedirs(os.path.dirname(migration_path), exist_ok=True)
        with open(migration_path, "w", encoding="utf-8") as f:
            f.write("CREATE TABLE users (id SERIAL PRIMARY KEY, name TEXT NOT NULL);\n")

    with open(migration_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Append a statement with a deliberate SQL syntax error
    fault_sql = "\n-- FAULT(bad_migration_sql): syntax error breaks migration runner\nCREATE TABLE orders (id SERIAL PRIMARYKEY, user_id INT REFERENCES users(id);\n"
    new_content = content + fault_sql

    with open(migration_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    affected = [os.path.relpath(migration_path, workspace).replace("\\", "/")]
    sha = _commit(workspace, "db: add orders table migration", affected)
    return FaultMetadata(
        fault_type="bad_migration_sql",
        affected_files=affected,
        injected_at_commit_sha=sha,
        description="SQL syntax error in 001_init.sql — migration runner fails at build/init time",
        injected_strings=["PRIMARYKEY", "FAULT(bad_migration_sql)"],
        expected_error_patterns=[r"syntax error", r"migration", r"SQL"],
    )


def _inject_schema_drift(workspace: str) -> FaultMetadata:
    """Add a column reference in app code that doesn't exist in the migration."""
    routes_path = os.path.join(workspace, "services", "api", "routes.py")
    if not os.path.exists(routes_path):
        routes_path = os.path.join(workspace, "api-service", "main.py")

    with open(routes_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Inject a reference to a non-existent column
    drift_snippet = (
        '\n# FAULT(schema_drift): references column "email" not present in migration\n'
        'def _get_user_email(user_id: int) -> str:\n'
        '    return db.query("SELECT email FROM users WHERE id = ?", user_id)\n'
    )
    new_content = content + drift_snippet

    with open(routes_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    affected = [os.path.relpath(routes_path, workspace).replace("\\", "/")]
    sha = _commit(workspace, "feat: expose user email in API response", affected)
    return FaultMetadata(
        fault_type="schema_drift",
        affected_files=affected,
        injected_at_commit_sha=sha,
        description="App code references column 'email' absent from migration schema — runtime query fails",
        injected_strings=["email", "FAULT(schema_drift)"],
        expected_error_patterns=[r"schema", r"column.*email", r"no such column"],
    )


def _inject_wrong_db_url(workspace: str) -> FaultMetadata:
    """Set DATABASE_URL to an unreachable host in the shared .env."""
    env_path = os.path.join(workspace, "shared-infra", ".env")
    if not os.path.exists(env_path):
        env_path = os.path.join(workspace, ".env")

    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()

    fault_line = "DATABASE_URL=postgresql://app:secret@nonexistent-db-host:5432/appdb  # FAULT(wrong_db_url)\n"
    if "DATABASE_URL" in content:
        import re as _re
        new_content = _re.sub(
            r"DATABASE_URL=.*\n",
            fault_line,
            content,
        )
    else:
        new_content = content.rstrip("\n") + "\n" + fault_line

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    affected = [os.path.relpath(env_path, workspace).replace("\\", "/")]
    sha = _commit(workspace, "config: update DATABASE_URL for new DB cluster", affected)
    return FaultMetadata(
        fault_type="wrong_db_url",
        affected_files=affected,
        injected_at_commit_sha=sha,
        description="DATABASE_URL points to nonexistent host — app cannot connect to DB at deploy time",
        injected_strings=["nonexistent-db-host", "FAULT(wrong_db_url)"],
        expected_error_patterns=[r"database", r"url", r"connection refused", r"could not connect"],
    )


def _inject_init_order_race(workspace: str) -> FaultMetadata:
    """Remove the depends_on / healthcheck that ensures DB is ready before the app starts."""
    compose_path = os.path.join(workspace, "shared-infra", "docker-compose.yml")
    if not os.path.exists(compose_path):
        compose_path = os.path.join(workspace, "docker-compose.yml")

    with open(compose_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Remove depends_on block so app starts before DB is ready
    import re as _re
    new_content = _re.sub(
        r"    depends_on:\s*\n(?:      - \S+\n)+",
        "    # FAULT(init_order_race): depends_on removed — app may start before DB is ready\n",
        content,
        count=1,
    )
    if new_content == content:
        # Append a comment marker so the fault is detectable even if no depends_on existed
        new_content = content.rstrip("\n") + "\n# FAULT(init_order_race): startup race condition introduced\n"

    with open(compose_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    affected = [os.path.relpath(compose_path, workspace).replace("\\", "/")]
    sha = _commit(workspace, "infra: simplify service startup order", affected)
    return FaultMetadata(
        fault_type="init_order_race",
        affected_files=affected,
        injected_at_commit_sha=sha,
        description="depends_on removed from docker-compose — app starts before DB is ready, causing startup race",
        injected_strings=["FAULT(init_order_race)", "startup race"],
        expected_error_patterns=[r"startup", r"race", r"connection refused", r"dependency"],
    )


def _inject_missing_volume_mount(workspace: str) -> FaultMetadata:
    """Remove the DB data volume mount so the database has no persistent storage."""
    compose_path = os.path.join(workspace, "shared-infra", "docker-compose.yml")
    if not os.path.exists(compose_path):
        compose_path = os.path.join(workspace, "docker-compose.yml")

    with open(compose_path, "r", encoding="utf-8") as f:
        content = f.read()

    import re as _re
    # Remove postgres volume mount lines
    new_content = _re.sub(
        r"      - \./data/db:/var/lib/postgresql/data\n",
        "      # FAULT(missing_volume_mount): DB volume mount removed — data lost on restart\n",
        content,
    )
    if new_content == content:
        # Generic fallback: strip any db-related volume line
        new_content = _re.sub(
            r"      - \./data/.*\n",
            "      # FAULT(missing_volume_mount): volume mount removed\n",
            content,
            count=1,
        )

    with open(compose_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    affected = [os.path.relpath(compose_path, workspace).replace("\\", "/")]
    sha = _commit(workspace, "infra: remove unused volume mounts to reduce disk usage", affected)
    return FaultMetadata(
        fault_type="missing_volume_mount",
        affected_files=affected,
        injected_at_commit_sha=sha,
        description="DB volume mount removed from docker-compose — database has no persistent storage, deploy fails",
        injected_strings=["FAULT(missing_volume_mount)", "volume mount removed"],
        expected_error_patterns=[r"volume", r"mount", r"database", r"missing"],
    )

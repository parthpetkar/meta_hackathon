"""Simulated fault injection — mutates workspace files without git operations.

Mirrors fault_injector.py's inject_fault() API exactly but:
- Writes real file mutations (so SimulatedPipelineRunner's file-read checks work)
- Skips all git add/commit operations (no git required)
- Returns the same FaultMetadata dataclass

This is used by SimulatedCICDRepairEnvironment when CICD_SIMULATE=true.
"""

from __future__ import annotations

import os
import random
import textwrap
from typing import Dict, List, Optional

from cicd.fault_injector import (
    FaultMetadata,
    FAULT_TYPES,
    FAULT_KEYWORDS,
    FAULT_STAGE_MAP,
    FAULT_AFFECTED_APPS,
)


# ── Public API ──────────────────────────────────────────────────────────────

def inject_fault_simulated(workspace: str, fault_type: str) -> FaultMetadata:
    """Inject a fault into workspace files (no git). Returns FaultMetadata."""
    injectors = {
        "merge_conflict":      _inject_merge_conflict,
        "dependency_conflict": _inject_dependency_conflict,
        "docker_order":        _inject_docker_order,
        "flaky_test":          _inject_flaky_test,
        "missing_permission":  _inject_missing_permission,
        "secret_exposure":     _inject_secret_exposure,
        "env_drift":           _inject_env_drift,
        "log_pii_leak":        _inject_log_pii_leak,
        "log_disabled":        _inject_log_disabled,
        "bad_migration_sql":   _inject_bad_migration_sql,
        "schema_drift":        _inject_schema_drift,
        # Extended fault types (present in simulated_runner but not in fault_injector)
        "log_bad_config":          _inject_log_bad_config,
        "log_path_unwritable":     _inject_log_path_unwritable,
        "log_rotation_missing":    _inject_log_rotation_missing,
        "log_volume_missing":      _inject_log_volume_missing,
        "shared_secret_rotation":  _inject_shared_secret_rotation,
        "infra_port_conflict":     _inject_infra_port_conflict,
        "dependency_version_drift": _inject_dependency_version_drift,
        "wrong_db_url":            _inject_wrong_db_url,
        "init_order_race":         _inject_init_order_race,
        "missing_volume_mount":    _inject_missing_volume_mount,
    }
    if fault_type not in injectors:
        raise ValueError(f"Unknown fault type: {fault_type!r}. Valid: {list(injectors)}")

    metadata = injectors[fault_type](workspace)
    metadata.expected_fail_stage = FAULT_STAGE_MAP.get(fault_type, "build")
    metadata.keywords = FAULT_KEYWORDS.get(fault_type, [])
    metadata.affected_apps = FAULT_AFFECTED_APPS.get(fault_type, [])
    return metadata


def inject_random_fault_simulated(workspace: str) -> FaultMetadata:
    return inject_fault_simulated(workspace, random.choice(FAULT_TYPES))


# ── File helpers ─────────────────────────────────────────────────────────────

def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, FileNotFoundError):
        return ""


# ── Core fault injectors (mirrors fault_injector.py, no git) ────────────────

def _inject_merge_conflict(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "routes.py")
    content = _read(path)
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
    _write(path, content)
    return FaultMetadata(
        fault_type="merge_conflict",
        affected_files=["services/api/routes.py"],
        description="Unresolved merge conflict markers in routes.py causing SyntaxError",
    )


def _inject_dependency_conflict(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "requirements.txt")
    _write(path, "flask>=3.0.0\nrequests==2.28.0\nurllib3==2.0.7\ngunicorn>=21.2.0\n")
    return FaultMetadata(
        fault_type="dependency_conflict",
        affected_files=["services/api/requirements.txt"],
        description="Incompatible pip dependency versions: requests==2.28.0 requires urllib3<2.0",
    )


def _inject_docker_order(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "Dockerfile")
    _write(path, textwrap.dedent("""\
        FROM python:3.11-slim

        WORKDIR /app

        # Wrong order: install before copying requirements into image
        RUN uv pip install --system --no-cache -r services/api/requirements.txt

        # Copy application code too late
        COPY . /app/

        EXPOSE 5000

        CMD ["python", "-m", "services.api.app"]
    """))
    return FaultMetadata(
        fault_type="docker_order",
        affected_files=["Dockerfile"],
        description="Dockerfile installs requirements before COPY, so file is unavailable at build time",
    )


def _inject_flaky_test(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "tests", "test_api.py")
    content = _read(path)
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
    _write(path, content + flaky)
    return FaultMetadata(
        fault_type="flaky_test",
        affected_files=["tests/test_api.py"],
        description="Timing-sensitive test with impossibly tight threshold always fails",
    )


def _inject_missing_permission(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "docker-compose.yml")
    _write(path, textwrap.dedent("""\
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
    return FaultMetadata(
        fault_type="missing_permission",
        affected_files=["docker-compose.yml"],
        description="docker-compose references non-existent external network 'corp-internal-network-v2'",
    )


def _inject_secret_exposure(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "app.py")
    content = _read(path)
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
    _write(path, "\n".join(lines))
    return FaultMetadata(
        fault_type="secret_exposure",
        affected_files=["services/api/app.py"],
        description="Hardcoded API keys and secrets exposed in source code",
    )


def _inject_env_drift(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "docker-compose.yml")
    _write(path, textwrap.dedent("""\
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
    return FaultMetadata(
        fault_type="env_drift",
        affected_files=["docker-compose.yml"],
        description="Invalid runtime env var PORT=not-a-number breaks docker compose deploy port mapping",
    )


def _inject_log_pii_leak(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "routes.py")
    content = _read(path)
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
    _write(path, content)
    return FaultMetadata(
        fault_type="log_pii_leak",
        affected_files=["services/api/routes.py"],
        description="routes.py logs a plaintext sk-live- API token — check_logs detects PII in static scan",
    )


def _inject_log_disabled(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    content = _read(path)
    content = content.replace(
        'LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()',
        'LOG_LEVEL: str = "CRITICAL"  # FAULT(log_disabled): hardcoded, overrides env var',
    )
    _write(path, content)
    return FaultMetadata(
        fault_type="log_disabled",
        affected_files=["services/api/logging_config.py"],
        description="LOG_LEVEL hardcoded to CRITICAL — effective logging disabled",
    )


def _inject_bad_migration_sql(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "db", "migrations", "001_init.sql")
    content = _read(path)
    new_content = content.replace(
        "CREATE TABLE IF NOT EXISTS builds",
        "CREAT TABLE IF NOT EXISTS builds",
        1,
    )
    _write(path, new_content)
    return FaultMetadata(
        fault_type="bad_migration_sql",
        affected_files=["db/migrations/001_init.sql"],
        description="SQL syntax error in 001_init.sql: 'CREAT TABLE'",
    )


def _inject_schema_drift(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "db", "database.py")
    content = _read(path)
    old = '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail"'
    new = '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail", "artifact_url"'
    _write(path, content.replace(old, new, 1))
    return FaultMetadata(
        fault_type="schema_drift",
        affected_files=["db/database.py"],
        description="CANONICAL_COLUMNS includes 'artifact_url' but no migration adds it",
    )


# ── Extended fault injectors (present in simulated_runner, not in fault_injector) ──

def _inject_log_bad_config(workspace: str) -> FaultMetadata:
    """Replace json.dumps with str() so log records are not valid JSON."""
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    content = _read(path)
    # Replace json.dumps with str() to break JSON formatting
    import re
    patched = re.sub(r'\bjson\.dumps\b', 'str', content)
    if patched == content:
        # Fallback: insert a broken formatter definition
        patched = content + textwrap.dedent("""

        # FAULT(log_bad_config): formatter uses str() instead of json.dumps
        def _bad_formatter(record):
            return str({"timestamp": record.created, "level": record.levelname,
                        "message": record.getMessage()})
        """)
    _write(path, patched)
    return FaultMetadata(
        fault_type="log_bad_config",
        affected_files=["services/api/logging_config.py"],
        description="Logging formatter uses str() instead of json.dumps — log records not valid JSON",
    )


def _inject_log_path_unwritable(workspace: str) -> FaultMetadata:
    """Set LOG_PATH to a restricted system directory."""
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    content = _read(path)
    import re
    patched = re.sub(
        r'LOG_PATH\s*(?::\s*str)?\s*=\s*["\'][^"\']*["\']',
        'LOG_PATH: str = "/var/log/restricted/app.log"  # FAULT(log_path_unwritable)',
        content,
    )
    if patched == content:
        patched = content + '\nLOG_PATH: str = "/var/log/restricted/app.log"  # FAULT(log_path_unwritable)\n'
    _write(path, patched)
    return FaultMetadata(
        fault_type="log_path_unwritable",
        affected_files=["services/api/logging_config.py"],
        description="LOG_PATH set to /var/log/restricted/app.log — root-only directory, app cannot write",
    )


def _inject_log_rotation_missing(workspace: str) -> FaultMetadata:
    """Replace RotatingFileHandler with plain FileHandler."""
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    content = _read(path)
    patched = content.replace("RotatingFileHandler", "FileHandler")
    if patched == content:
        # Fallback: comment out the RotatingFileHandler import/usage
        patched = content.replace(
            "from logging.handlers import RotatingFileHandler",
            "# FAULT(log_rotation_missing): RotatingFileHandler removed\nfrom logging import FileHandler",
        )
    _write(path, patched)
    return FaultMetadata(
        fault_type="log_rotation_missing",
        affected_files=["services/api/logging_config.py"],
        description="RotatingFileHandler replaced with FileHandler — logs may grow unboundedly",
    )


def _inject_log_volume_missing(workspace: str) -> FaultMetadata:
    """Remove the ./logs:/app/logs volume mount from docker-compose.yml."""
    path = os.path.join(workspace, "docker-compose.yml")
    content = _read(path)
    import re
    # Remove the volumes section line for app logs
    patched = re.sub(r'\s*-\s*\./logs:/app/logs\n?', '\n', content)
    if patched == content:
        # No volume to remove — write a compose without it
        _write(path, textwrap.dedent("""\
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
                healthcheck:
                  test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
                  interval: 30s
                  timeout: 5s
                  retries: 3
        """))
    else:
        _write(path, patched)
    return FaultMetadata(
        fault_type="log_volume_missing",
        affected_files=["docker-compose.yml"],
        description="docker-compose missing ./logs:/app/logs volume — app cannot write log files",
    )


def _inject_shared_secret_rotation(workspace: str) -> FaultMetadata:
    """Add stale secret literal and mismatched SECRET_VERSION to docker-compose."""
    path = os.path.join(workspace, "docker-compose.yml")
    content = _read(path)
    import re
    # Add old secret references to environment block
    patched = re.sub(
        r'(environment:\s*\n(?:\s+-[^\n]*\n)*)',
        lambda m: m.group(0) + '              - WEBHOOK_SECRET=whsec_old_a1b2c3d4e5f6\n'
                               '              - SECRET_VERSION=v1\n',
        content,
    )
    if patched == content:
        patched = content + textwrap.dedent("""
        # FAULT(shared_secret_rotation): stale secret version
        # SECRET_VERSION=v1 — peer expects v2
        """)
    _write(path, patched)
    # Also inject stale secret in app.py
    app_path = os.path.join(workspace, "services", "api", "app.py")
    app_content = _read(app_path)
    if "whsec_old_" not in app_content:
        _write(app_path, app_content + '\n# FAULT(shared_secret_rotation)\nWEBHOOK_SECRET = "whsec_old_a1b2c3d4e5f6"\n')
    return FaultMetadata(
        fault_type="shared_secret_rotation",
        affected_files=["docker-compose.yml", "services/api/app.py"],
        description="Secret rotation not propagated — stale whsec_old_ secret in app, SECRET_VERSION mismatch",
    )


def _inject_infra_port_conflict(workspace: str) -> FaultMetadata:
    """Change api-service port to 5000, clashing with another binding."""
    path = os.path.join(workspace, "docker-compose.yml")
    content = _read(path)
    import re
    # Change the host port to 5000:5000 and add a second service also on 5000
    patched = re.sub(r'ports:\s*\n\s*-\s*"5000:5000"', 'ports:\n              - "5000:5000"\n              - "5000:8001"', content)
    if patched == content:
        # Simpler: just duplicate the port mapping
        _write(path, textwrap.dedent("""\
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
              frontend:
                image: nginx:alpine
                ports:
                  - "5000:80"
        """))
    else:
        _write(path, patched)
    return FaultMetadata(
        fault_type="infra_port_conflict",
        affected_files=["docker-compose.yml"],
        description="Duplicate port 5000 binding across services causes deploy failure",
    )


def _inject_dependency_version_drift(workspace: str) -> FaultMetadata:
    """Pin fastapi/pydantic with known incompatible versions."""
    path = os.path.join(workspace, "services", "api", "requirements.txt")
    _write(path, textwrap.dedent("""\
        flask>=3.0.0
        requests>=2.31.0
        urllib3>=1.26,<2
        gunicorn>=21.2.0
        pytest>=8.0.0
        # FAULT(dependency_version_drift): conflicting urllib3 range with requests>=2.31
        # requests>=2.31 requires urllib3>=2, but this pins <2
    """))
    return FaultMetadata(
        fault_type="dependency_version_drift",
        affected_files=["services/api/requirements.txt"],
        description="urllib3 pinned <2 conflicts with requests>=2.31 which requires urllib3>=2",
    )


def _inject_wrong_db_url(workspace: str) -> FaultMetadata:
    """Inject a malformed DATABASE_URL with a double-slash hostname."""
    path = os.path.join(workspace, "docker-compose.yml")
    content = _read(path)
    import re
    patched = re.sub(
        r'(environment:\s*\n(?:\s+-[^\n]*\n)*)',
        lambda m: m.group(0) + '              - DATABASE_URL=postgresql://user:pass@db-host-missing/mydb\n',
        content,
    )
    if patched == content:
        _write(path, textwrap.dedent("""\
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
                  - DATABASE_URL=postgresql://user:pass@db-host-missing/mydb
        """))
    else:
        _write(path, patched)
    return FaultMetadata(
        fault_type="wrong_db_url",
        affected_files=["docker-compose.yml"],
        description="DATABASE_URL references unresolvable hostname 'db-host-missing'",
    )


def _inject_init_order_race(workspace: str) -> FaultMetadata:
    """Remove depends_on healthcheck so api starts before db is ready."""
    path = os.path.join(workspace, "docker-compose.yml")
    content = _read(path)
    import re
    # Remove depends_on block
    patched = re.sub(r'\s*depends_on:[^\n]*\n(?:\s+[^\n]*\n)*', '\n', content)
    if patched == content:
        # Write fresh compose without depends_on
        _write(path, textwrap.dedent("""\
            version: "3.8"

            services:
              db:
                image: postgres:15-alpine
                environment:
                  - POSTGRES_DB=mydb
                  - POSTGRES_USER=user
                  - POSTGRES_PASSWORD=pass
              api:
                build:
                  context: .
                  dockerfile: Dockerfile
                ports:
                  - "5000:5000"
                environment:
                  - FLASK_ENV=production
                  - DATABASE_URL=postgresql://user:pass@db/mydb
                # FAULT(init_order_race): depends_on removed — api may start before db
        """))
    else:
        _write(path, patched)
    return FaultMetadata(
        fault_type="init_order_race",
        affected_files=["docker-compose.yml"],
        description="depends_on healthcheck removed — api starts before db is ready, connection race",
    )


def _inject_missing_volume_mount(workspace: str) -> FaultMetadata:
    """Remove volume mount for /app/logs so log directory doesn't exist in container."""
    path = os.path.join(workspace, "docker-compose.yml")
    content = _read(path)
    import re
    patched = re.sub(r'\s*-\s*\./logs:/app/logs\n?', '\n', content)
    if patched == content:
        _write(path, textwrap.dedent("""\
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
                # FAULT(missing_volume_mount): ./logs:/app/logs volume not mounted
                # /app/logs directory will not exist in container
        """))
    else:
        _write(path, patched)
    return FaultMetadata(
        fault_type="missing_volume_mount",
        affected_files=["docker-compose.yml"],
        description="./logs:/app/logs volume mount missing — log directory absent in container",
    )

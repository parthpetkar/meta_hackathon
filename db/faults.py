"""Database fault registry, injection, and fix verification.

Each fault name maps to a spec that describes:
  - what it simulates
  - what observable failure it produces
  - the correct repair action
  - which backend it applies to

inject_fault(name, workspace)  → mutates real files to activate the fault
verify_fix(name, workspace)    → returns (passed: bool, message: str)

Both functions accept an optional workspace_path; they default to the repo root.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Tuple, TypedDict

# ---------------------------------------------------------------------------
# Paths (resolved relative to this file's location)
# ---------------------------------------------------------------------------

_REPO_ROOT   = Path(__file__).parent.parent
_COMPOSE     = _REPO_ROOT / "docker-compose.yml"
_MIGRATION   = _REPO_ROOT / "db" / "migrations" / "001_init.sql"
_DATABASE_PY = _REPO_ROOT / "db" / "database.py"


def _compose(wp: Path) -> Path:
    return wp / "docker-compose.yml"

def _migration(wp: Path) -> Path:
    return wp / "db" / "migrations" / "001_init.sql"

def _database_py(wp: Path) -> Path:
    return wp / "db" / "database.py"


# ---------------------------------------------------------------------------
# Fault registry
# ---------------------------------------------------------------------------

class FaultSpec(TypedDict):
    description: str
    breaks: str
    correct_fix: str
    affects: str   # "sqlite" | "postgres" | "both"


FAULT_REGISTRY: Dict[str, FaultSpec] = {
    "BAD_MIGRATION_SQL": {
        "description": (
            "Syntax error injected in 001_init.sql: 'CREATE TABLE' replaced with "
            "'CREAT TABLE', making the migration unparseable."
        ),
        "breaks": (
            "Database fails to initialise on startup. Every API endpoint returns 500 "
            "with 'OperationalError: near \"TABLE\": syntax error'."
        ),
        "correct_fix": (
            "Fix the typo in db/migrations/001_init.sql: change 'CREAT TABLE' back "
            "to 'CREATE TABLE'."
        ),
        "affects": "both",
    },
    "SCHEMA_DRIFT": {
        "description": (
            "db/database.py CANONICAL_COLUMNS includes 'artifact_url', a column the "
            "schema migration never added to the builds table."
        ),
        "breaks": (
            "Any query that selects or inserts artifact_url raises "
            "'OperationalError: table builds has no column named artifact_url'."
        ),
        "correct_fix": (
            "Either add a new migration that adds the artifact_url column to builds, "
            "or remove 'artifact_url' from CANONICAL_COLUMNS in db/database.py."
        ),
        "affects": "both",
    },
    "MISSING_VOLUME": {
        "description": (
            "The pgdata volume mount is commented out under the db service in "
            "docker-compose.yml."
        ),
        "breaks": (
            "Postgres starts successfully but all data is lost on every container restart. "
            "Stateful integration tests fail intermittently depending on restart order."
        ),
        "correct_fix": (
            "Restore the '- pgdata:/var/lib/postgresql/data' line under db.volumes "
            "in docker-compose.yml."
        ),
        "affects": "postgres",
    },
    "WRONG_DATABASE_URL": {
        "description": (
            "DATABASE_URL in docker-compose.yml contains a double-slash after the "
            "scheme, making the hostname unparseable: 'postgresql://app:secret@//db:5432/appdb'."
        ),
        "breaks": (
            "Connection attempt fails immediately with 'could not translate host name "
            "\"\" to address: Name or service not known'. API health check never passes."
        ),
        "correct_fix": (
            "Fix DATABASE_URL: remove the extra slash so it reads "
            "'postgresql://app:secret@db:5432/appdb'."
        ),
        "affects": "both",
    },
    "INIT_ORDER_RACE": {
        "description": (
            "The 'depends_on: db: condition: service_healthy' guard is removed from "
            "env-server in docker-compose.yml."
        ),
        "breaks": (
            "env-server starts before Postgres finishes initialising. The first "
            "init_db() call fails with 'Connection refused', leaving the schema "
            "uninitialised. Requests fail until the container is manually restarted."
        ),
        "correct_fix": (
            "Re-add 'depends_on: db: condition: service_healthy' (with required: false) "
            "under env-server in docker-compose.yml."
        ),
        "affects": "postgres",
    },
}


# ---------------------------------------------------------------------------
# Injection helpers
# ---------------------------------------------------------------------------

def inject_fault(fault_name: str, workspace_path: Path | None = None) -> None:
    """Mutate real project files to activate *fault_name*."""
    wp = workspace_path or _REPO_ROOT
    _require_known(fault_name)
    {
        "BAD_MIGRATION_SQL":  _inject_bad_migration,
        "SCHEMA_DRIFT":       _inject_schema_drift,
        "MISSING_VOLUME":     _inject_missing_volume,
        "WRONG_DATABASE_URL": _inject_wrong_database_url,
        "INIT_ORDER_RACE":    _inject_init_order_race,
    }[fault_name](wp)


def _inject_bad_migration(wp: Path) -> None:
    path = _migration(wp)
    text = path.read_text(encoding="utf-8")
    patched = text.replace(
        "CREATE TABLE IF NOT EXISTS builds",
        "CREAT TABLE IF NOT EXISTS builds",
        1,
    )
    path.write_text(patched, encoding="utf-8")


def _inject_schema_drift(wp: Path) -> None:
    path = _database_py(wp)
    text = path.read_text(encoding="utf-8")
    old = '    "id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail",'
    new = '    "id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail", "artifact_url",'
    if old not in text:
        raise RuntimeError("SCHEMA_DRIFT inject: expected CANONICAL_COLUMNS line not found in database.py")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _inject_missing_volume(wp: Path) -> None:
    path = _compose(wp)
    text = path.read_text(encoding="utf-8")
    target = "      - pgdata:/var/lib/postgresql/data"
    replacement = "      # FAULT(MISSING_VOLUME): volume mount removed — data will not persist\n      # - pgdata:/var/lib/postgresql/data"
    if target not in text:
        raise RuntimeError("MISSING_VOLUME inject: expected volume line not found in docker-compose.yml")
    path.write_text(text.replace(target, replacement, 1), encoding="utf-8")


def _inject_wrong_database_url(wp: Path) -> None:
    path = _compose(wp)
    text = path.read_text(encoding="utf-8")
    good = "postgresql://app:secret@db:5432/appdb"
    bad  = "postgresql://app:secret@//db:5432/appdb"
    if good not in text:
        raise RuntimeError("WRONG_DATABASE_URL inject: expected DATABASE_URL not found in docker-compose.yml")
    path.write_text(text.replace(good, bad), encoding="utf-8")


def _inject_init_order_race(wp: Path) -> None:
    path = _compose(wp)
    text = path.read_text(encoding="utf-8")
    # Remove the db entry from env-server's depends_on block
    patched = re.sub(
        r"(\s+depends_on:)(.*?)((\s+db:\n\s+condition: service_healthy\n\s+required: false\n))",
        lambda m: m.group(1) + m.group(2),
        text,
        count=1,
        flags=re.DOTALL,
    )
    if patched == text:
        raise RuntimeError(
            "INIT_ORDER_RACE inject: expected depends_on db block not found in docker-compose.yml"
        )
    path.write_text(patched, encoding="utf-8")


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def verify_fix(fault_name: str, workspace_path: Path | None = None) -> Tuple[bool, str]:
    """
    Check whether the agent has correctly resolved *fault_name*.
    Returns (passed, message).
    """
    wp = workspace_path or _REPO_ROOT
    _require_known(fault_name)
    return {
        "BAD_MIGRATION_SQL":  _verify_bad_migration,
        "SCHEMA_DRIFT":       _verify_schema_drift,
        "MISSING_VOLUME":     _verify_missing_volume,
        "WRONG_DATABASE_URL": _verify_wrong_database_url,
        "INIT_ORDER_RACE":    _verify_init_order_race,
    }[fault_name](wp)


def _verify_bad_migration(wp: Path) -> Tuple[bool, str]:
    sql = _migration(wp).read_text(encoding="utf-8")
    if "CREAT TABLE" in sql:
        return False, "Migration still contains the 'CREAT TABLE' syntax error."
    if "CREATE TABLE" not in sql:
        return False, "Migration is missing CREATE TABLE statement entirely."
    return True, "Migration SQL is syntactically correct."


def _verify_schema_drift(wp: Path) -> Tuple[bool, str]:
    src = _database_py(wp).read_text(encoding="utf-8")
    migration_text = _migration(wp).read_text(encoding="utf-8")
    # The fault injects "artifact_url" into the CANONICAL_COLUMNS list literal.
    # We detect it by looking for the injected line specifically, not just any mention
    # of the string (which also appears in the conditional EXPECTED_COLUMNS expression).
    canonical_has_phantom = bool(
        re.search(r'"log_tail",\s*"artifact_url"', src)
    )
    if canonical_has_phantom and "artifact_url" not in migration_text:
        return (
            False,
            "database.py CANONICAL_COLUMNS still includes 'artifact_url' but no migration adds it.",
        )
    return True, "Schema and application model are aligned."


def _verify_missing_volume(wp: Path) -> Tuple[bool, str]:
    text = _compose(wp).read_text(encoding="utf-8")
    # Volume mount must be present and not commented out
    if "# - pgdata:/var/lib/postgresql/data" in text:
        return False, "pgdata volume mount is still commented out."
    if "- pgdata:/var/lib/postgresql/data" not in text:
        return False, "pgdata volume mount is missing from docker-compose.yml entirely."
    return True, "Postgres volume mount is correctly configured."


def _verify_wrong_database_url(wp: Path) -> Tuple[bool, str]:
    text = _compose(wp).read_text(encoding="utf-8")
    if "postgresql://app:secret@//db" in text:
        return False, "DATABASE_URL still contains the double-slash typo in docker-compose.yml."
    if "sqlite:///./app_typo.dbb" in text:
        return False, "DATABASE_URL still contains the broken SQLite path."
    return True, "DATABASE_URL is well-formed."


def _verify_init_order_race(wp: Path) -> Tuple[bool, str]:
    text = _compose(wp).read_text(encoding="utf-8")
    has_db_dep   = re.search(r"depends_on:.*?db:\s*\n\s*condition:", text, re.DOTALL)
    has_healthy  = "condition: service_healthy" in text
    if not has_db_dep or not has_healthy:
        return (
            False,
            "env-server is missing 'depends_on: db: condition: service_healthy' in docker-compose.yml.",
        )
    return True, "Healthcheck dependency is correctly configured."


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _require_known(fault_name: str) -> None:
    if fault_name not in FAULT_REGISTRY:
        raise ValueError(
            f"Unknown fault: {fault_name!r}. Valid names: {sorted(FAULT_REGISTRY)}"
        )

"""Database service layer.

Default backend: SQLite (zero startup cost, local file).
Postgres backend: set DB_BACKEND=postgres (requires the postgres Compose profile).

Fault flags — each defaults to off; set to "true" to activate:
    WRONG_DATABASE_URL   Injects a double-slash typo into the connection string.
    BAD_MIGRATION_SQL    Replaces CREATE TABLE with CREAT TABLE in the migration SQL at load time.
    SCHEMA_DRIFT         Adds a phantom column to EXPECTED_COLUMNS that the table never received.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Generator, List

# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------

_SQLITE_DEFAULT   = "sqlite:///./app.db"
_POSTGRES_DEFAULT = "postgresql://app:secret@db:5432/appdb"
_POSTGRES_BROKEN  = "postgresql://app:secret@//db:5432/appdb"  # double-slash typo
_SQLITE_BROKEN    = "sqlite:///./app_typo.dbb"                 # wrong extension + name


def _resolve_url() -> str:
    explicit = os.environ.get("DATABASE_URL", "")
    if explicit:
        return explicit
    if os.environ.get("WRONG_DATABASE_URL") == "true":
        return _POSTGRES_BROKEN if os.environ.get("DB_BACKEND") == "postgres" else _SQLITE_BROKEN
    if os.environ.get("DB_BACKEND") == "postgres":
        return _POSTGRES_DEFAULT
    return _SQLITE_DEFAULT


DATABASE_URL: str = _resolve_url()

# ---------------------------------------------------------------------------
# Schema definition
# SCHEMA_DRIFT adds a phantom column that was never added to the real table.
# ---------------------------------------------------------------------------

CANONICAL_COLUMNS: List[str] = [
    "id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail",
]

_schema_drift_active = os.environ.get("SCHEMA_DRIFT") == "true"
EXPECTED_COLUMNS: List[str] = (
    CANONICAL_COLUMNS + ["artifact_url"] if _schema_drift_active else list(CANONICAL_COLUMNS)
)

# ---------------------------------------------------------------------------
# SQLite connection helper
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _sqlite_path() -> Path:
    return Path(DATABASE_URL.removeprefix("sqlite:///"))


@contextlib.contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Yield a committed-or-rolled-back SQLite connection."""
    path = _sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------

def _load_sql(filename: str) -> str:
    sql = (_MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
    if os.environ.get("BAD_MIGRATION_SQL") == "true":
        # Inject syntax error: CREAT TABLE instead of CREATE TABLE
        sql = sql.replace("CREATE TABLE IF NOT EXISTS builds", "CREAT TABLE IF NOT EXISTS builds", 1)
    return sql


def init_db() -> None:
    """Apply all pending SQL migrations. Idempotent — safe to call on every startup."""
    if DATABASE_URL.startswith("sqlite:"):
        _init_sqlite()
    elif DATABASE_URL.startswith("postgresql:"):
        _init_postgres()
    else:
        raise ValueError(f"Unsupported DATABASE_URL scheme: {DATABASE_URL!r}")


def _init_sqlite() -> None:
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations "
            "(name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        )
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            name = path.name
            if conn.execute("SELECT 1 FROM _migrations WHERE name=?", (name,)).fetchone():
                continue
            conn.executescript(_load_sql(name))
            conn.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))


def _init_postgres() -> None:
    try:
        import psycopg2  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 is required for DB_BACKEND=postgres — add it to server/requirements.txt"
        ) from exc

    import re

    m = re.match(r"postgresql://([^:]+):([^@]+)@([^:/]+):(\d+)/(\w+)", DATABASE_URL)
    if not m:
        raise ValueError(f"Unparseable DATABASE_URL: {DATABASE_URL!r}")
    user, password, host, port, dbname = m.groups()

    conn = psycopg2.connect(
        host=host, port=int(port), dbname=dbname, user=user, password=password
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS _migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
            )
            for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                name = path.name
                cur.execute("SELECT 1 FROM _migrations WHERE name=%s", (name,))
                if cur.fetchone():
                    continue
                sql = _load_sql(name)
                sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
                sql = sql.replace("datetime('now')", "now()")
                cur.execute(sql)
                cur.execute("INSERT INTO _migrations (name) VALUES (%s)", (name,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def insert_build(task_key: str) -> int:
    """Insert a pending build row and return its id."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO builds (task_key, status) VALUES (?, 'pending')", (task_key,)
        )
        return cur.lastrowid  # type: ignore[return-value]


def update_build(build_id: int, *, status: str, exit_code: int, log_tail: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE builds SET status=?, exit_code=?, log_tail=?, finished_at=datetime('now') "
            "WHERE id=?",
            (status, exit_code, log_tail, build_id),
        )


def get_recent_builds(task_key: str, limit: int = 10) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM builds WHERE task_key=? ORDER BY started_at DESC LIMIT ?",
            (task_key, limit),
        ).fetchall()

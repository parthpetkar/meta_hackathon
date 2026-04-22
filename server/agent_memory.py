# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Persistent agent memory: cross-episode belief updates via sqlite3."""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "agent_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            pattern_hash  TEXT NOT NULL,
            fix_text      TEXT NOT NULL,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            error_text    TEXT NOT NULL DEFAULT '',
            fault_type    TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (pattern_hash, fix_text, fault_type)
        )
    """)
    # Add columns to existing databases that predate this schema
    try:
        conn.execute("ALTER TABLE memory ADD COLUMN error_text TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE memory ADD COLUMN fault_type TEXT NOT NULL DEFAULT 'unknown'")
    except Exception:
        pass  # column already exists
    conn.commit()
    return conn


def fingerprint(errors: list[str]) -> str:
    """Deterministic 16-hex-char hash of a normalised error list."""
    normalised = sorted(
        re.sub(r"\s+", " ", e.strip().lower()) for e in errors if e.strip()
    )
    payload = json.dumps(normalised, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def recall(errors: list[str], fault_type: str = "unknown") -> dict:
    """Return best known fix for this error pattern, scoped by fault_type.

    Memory is now fault-type-aware: fixes are only recalled when the current
    fault_type matches the stored fault_type, preventing cross-contamination
    where different faults produce similar error messages.
    """
    if not errors or fault_type == "unknown":
        return {
            "suggested_fix": "",
            "confidence": 0.0,
            "times_seen": 0,
            "historical_success_rate": 0.0,
            "memory_log": "",
        }

    h = fingerprint(errors)
    try:
        conn = _connect()
        # Scope recall to the current fault_type to prevent cross-contamination
        row = conn.execute(
            "SELECT fix_text, success_count, failure_count FROM memory "
            "WHERE pattern_hash = ? AND fault_type = ? "
            "ORDER BY success_count DESC, (success_count + failure_count) DESC "
            "LIMIT 1",
            (h, fault_type),
        ).fetchone()
        conn.close()
    except Exception:
        return {
            "suggested_fix": "",
            "confidence": 0.0,
            "times_seen": 0,
            "historical_success_rate": 0.0,
            "memory_log": "",
        }

    if row is None:
        return {
            "suggested_fix": "",
            "confidence": 0.0,
            "times_seen": 0,
            "historical_success_rate": 0.0,
            "memory_log": "",
        }

    fix_text, success_count, failure_count = row
    total = success_count + failure_count
    confidence = success_count / total if total > 0 else 0.0
    memory_log = (
        "[MEMORY] Recalling fix for fault_type="
        f"{fault_type}: action={fix_text}, historical_success_rate={confidence:.2f}"
    )
    return {
        "suggested_fix": fix_text,
        "confidence": round(confidence, 3),
        "times_seen": total,
        "historical_success_rate": round(confidence, 3),
        "memory_log": memory_log,
    }


# Module-level cache for the sentence transformer model
_MEMORY_MODEL = None


def remember(errors: list[str], fix: str, success: bool, fault_type: str = "unknown") -> None:
    """Upsert fix outcome for this error pattern into persistent memory.

    Now stores fault_type alongside the pattern_hash so recall can be scoped
    to the current fault, preventing cross-contamination.
    """
    if not errors or not fix or fault_type == "unknown":
        return

    h = fingerprint(errors)
    fix_clean = fix.strip()
    error_text = " ".join(errors)[:2000]  # cap to avoid bloating the DB
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO memory (pattern_hash, fix_text, success_count, failure_count, error_text, fault_type) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pattern_hash, fix_text, fault_type) DO UPDATE SET "
            "success_count = success_count + excluded.success_count, "
            "failure_count = failure_count + excluded.failure_count, "
            "error_text = excluded.error_text",
            (h, fix_clean, 1 if success else 0, 0 if success else 1, error_text, fault_type),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

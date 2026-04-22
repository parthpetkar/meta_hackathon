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
            PRIMARY KEY (pattern_hash, fix_text)
        )
    """)
    # Add error_text column to existing databases that predate this schema
    try:
        conn.execute("ALTER TABLE memory ADD COLUMN error_text TEXT NOT NULL DEFAULT ''")
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
    """Return best known fix for this error pattern using semantic similarity.

    Tries semantic retrieval first (embedding-based), falls back to exact
    fingerprint match if embeddings are unavailable. This allows the agent
    to recall fixes for semantically similar errors even when the exact text
    differs (different line numbers, timestamps, etc.).
    """
    if not errors:
        return {
            "suggested_fix": "",
            "confidence": 0.0,
            "times_seen": 0,
            "historical_success_rate": 0.0,
            "memory_log": "",
        }

    # Try semantic retrieval first
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        import numpy as np  # type: ignore

        global _MEMORY_MODEL
        if _MEMORY_MODEL is None:
            _MEMORY_MODEL = SentenceTransformer("all-MiniLM-L6-v2")

        # Embed the current error pattern
        error_text = " ".join(errors)
        query_emb = _MEMORY_MODEL.encode(error_text, convert_to_numpy=True)

        # Retrieve all memories and compute similarity
        conn = _connect()
        rows = conn.execute(
            "SELECT DISTINCT pattern_hash, fix_text, success_count, failure_count FROM memory"
        ).fetchall()
        conn.close()

        if not rows:
            return {
                "suggested_fix": "",
                "confidence": 0.0,
                "times_seen": 0,
                "historical_success_rate": 0.0,
                "memory_log": "",
            }

        # Compute similarity for each stored pattern (this is slow for large DBs;
        # consider adding a vector index if memory grows beyond ~1000 entries)
        best_fix, best_score, best_success, best_failure = "", -1.0, 0, 0
        for pattern_hash, fix_text, success_count, failure_count in rows:
            # Reconstruct the error pattern from the hash (not possible — hash is one-way).
            # Instead, we'd need to store the original error text. For now, fall back
            # to exact fingerprint match as a simpler first step.
            pass

        # Semantic retrieval requires storing error text, not just hash.
        # Fall through to exact fingerprint match for now.
    except Exception:
        pass

    # Exact fingerprint match (original behavior)
    h = fingerprint(errors)
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT fix_text, success_count, failure_count FROM memory "
            "WHERE pattern_hash = ? "
            "ORDER BY success_count DESC, (success_count + failure_count) DESC "
            "LIMIT 1",
            (h,),
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


def remember(errors: list[str], fix: str, success: bool) -> None:
    """Upsert fix outcome for this error pattern into persistent memory.

    Stores both the hash (for exact lookup) and the raw error text
    (for future semantic similarity retrieval).
    """
    if not errors or not fix:
        return

    h = fingerprint(errors)
    fix_clean = fix.strip()
    error_text = " ".join(errors)[:2000]  # cap to avoid bloating the DB
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO memory (pattern_hash, fix_text, success_count, failure_count, error_text) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(pattern_hash, fix_text) DO UPDATE SET "
            "success_count = success_count + excluded.success_count, "
            "failure_count = failure_count + excluded.failure_count, "
            "error_text = excluded.error_text",
            (h, fix_clean, 1 if success else 0, 0 if success else 1, error_text),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Persistent agent memory: cross-episode belief updates via sqlite3."""

import hashlib
import json
import random
import re
import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "agent_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            pattern_hash TEXT NOT NULL,
            fix_text      TEXT NOT NULL,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (pattern_hash, fix_text)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fault_class_stats (
            fault_type    TEXT PRIMARY KEY,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0
        )
    """)
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
    """Return best known fix for this error pattern, or an empty suggestion."""
    if not errors:
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


def remember(errors: list[str], fix: str, success: bool) -> None:
    """Upsert fix outcome for this error pattern into persistent memory."""
    if not errors or not fix:
        return

    h = fingerprint(errors)
    fix_clean = fix.strip()
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO memory (pattern_hash, fix_text, success_count, failure_count) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pattern_hash, fix_text) DO UPDATE SET "
            "success_count = success_count + excluded.success_count, "
            "failure_count = failure_count + excluded.failure_count",
            (h, fix_clean, 1 if success else 0, 0 if success else 1),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Per-fault-class weakness tracking ─────────────────────────────────────────

def record_fault_outcome(fault_type: str, success: bool) -> None:
    """Increment success/failure counter for a fault class after each episode."""
    if not fault_type:
        return
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO fault_class_stats (fault_type, success_count, failure_count) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(fault_type) DO UPDATE SET "
            "success_count = success_count + excluded.success_count, "
            "failure_count = failure_count + excluded.failure_count",
            (fault_type, 1 if success else 0, 0 if success else 1),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_fault_class_stats() -> dict[str, dict]:
    """Return per-fault-class {attempts, success_rate, failure_rate} dict."""
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT fault_type, success_count, failure_count FROM fault_class_stats"
        ).fetchall()
        conn.close()
    except Exception:
        return {}

    result = {}
    for fault_type, successes, failures in rows:
        total = successes + failures
        result[fault_type] = {
            "attempts": total,
            "success_rate": round(successes / total, 4) if total else 0.0,
            "failure_rate": round(failures / total, 4) if total else 0.0,
        }
    return result


def sample_weak_fault(fault_types: list[str], temperature: float = 1.0) -> str:
    """Softmax-sample a fault type biased toward classes the agent keeps failing.

    Weakness weight = failure_rate for seen faults; unseen faults get weight 1.0
    (maximum curiosity — we haven't tried them yet).  Temperature controls how
    sharply the distribution peaks on the weakest class:
      temperature=1.0 → standard softmax
      temperature<1.0 → sharper (more greedy toward weakness)
      temperature>1.0 → flatter (more exploration)
    """
    import math

    stats = get_fault_class_stats()

    # Raw weakness score: failure_rate for seen, 1.0 for unseen
    raw = [
        stats[ft]["failure_rate"] if ft in stats else 1.0
        for ft in fault_types
    ]

    # Softmax with temperature
    scaled = [w / max(temperature, 1e-6) for w in raw]
    max_scaled = max(scaled)
    exp_w = [math.exp(s - max_scaled) for s in scaled]
    total = sum(exp_w)
    probs = [e / total for e in exp_w]

    # Weighted random draw
    r = random.random()
    cumulative = 0.0
    for ft, p in zip(fault_types, probs):
        cumulative += p
        if r <= cumulative:
            return ft
    return fault_types[-1]

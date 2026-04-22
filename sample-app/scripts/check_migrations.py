#!/usr/bin/env python3
"""Migration file validator for the CI/CD pipeline build stage.

Validates SQL migration files for syntax errors before the build completes.
Catches faults like bad_migration_sql that would otherwise pass silently.

Exit codes:
    0   All migrations are syntactically valid.
    1   One or more migrations have syntax errors.

Usage:
    python scripts/check_migrations.py [--migrations-dir DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def validate_sql_syntax(sql: str, filename: str) -> list[str]:
    """Basic SQL syntax validation. Returns list of error strings."""
    failures: list[str] = []

    # Check for common SQL syntax errors that would break postgres/sqlite
    patterns = [
        (r"\bPRIMARYKEY\b", "Missing space in PRIMARY KEY"),
        (r"\bFOREIGNKEY\b", "Missing space in FOREIGN KEY"),
        (r"\bNOTNULL\b", "Missing space in NOT NULL"),
        (r"CREATE\s+TABLE\s+\w+\s*\([^)]*\bPRIMARY\s+KEY[^)]*$", "Unclosed parenthesis in CREATE TABLE"),
        (r"CREATE\s+TABLE\s+\w+\s*\([^)]*;", "Missing closing parenthesis before semicolon"),
    ]

    for pattern, msg in patterns:
        if re.search(pattern, sql, re.IGNORECASE | re.MULTILINE):
            failures.append(f"{filename}: {msg}")

    # Check for unbalanced parentheses
    open_count = sql.count("(")
    close_count = sql.count(")")
    if open_count != close_count:
        failures.append(
            f"{filename}: Unbalanced parentheses "
            f"({open_count} open, {close_count} close)"
        )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate SQL migration files.")
    parser.add_argument(
        "--migrations-dir",
        default="db/migrations",
        help="Path to migrations directory (default: db/migrations)",
    )
    args = parser.parse_args()

    migrations_dir = Path(args.migrations_dir)
    if not migrations_dir.exists():
        print(f"Migrations directory not found: {migrations_dir} — skipping validation.", file=sys.stderr)
        return 0

    all_failures: list[str] = []
    sql_files = sorted(migrations_dir.glob("*.sql"))

    if not sql_files:
        print(f"No .sql files found in {migrations_dir} — skipping validation.", file=sys.stderr)
        return 0

    for sql_file in sql_files:
        try:
            sql = sql_file.read_text(encoding="utf-8")
            failures = validate_sql_syntax(sql, sql_file.name)
            all_failures.extend(failures)
        except Exception as exc:
            all_failures.append(f"{sql_file.name}: Failed to read file — {exc}")

    if all_failures:
        print("MIGRATION VALIDATION FAILED:", file=sys.stderr)
        for f in all_failures:
            print(f"  FAIL: {f}", file=sys.stderr)
        print(f"\ncheck_migrations: {len(all_failures)} issue(s) found.", file=sys.stderr)
        return 1

    print(f"check_migrations: all {len(sql_files)} migration(s) passed validation.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

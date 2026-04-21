"""Clear all rows from a SQLite database while keeping schema intact.

Default target is server/agent_memory.db.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "server" / "agent_memory.db"


def clear_all_rows(db_path: Path) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"DB file not found: {db_path}")

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()

        cur.execute("PRAGMA foreign_keys = OFF")
        cur.execute("BEGIN")

        tables = [
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]

        if not tables:
            print(f"No user tables found in {db_path}")
            conn.commit()
            return

        for table in tables:
            cur.execute(f'DELETE FROM "{table}"')

        conn.commit()
        cur.execute("VACUUM")

    print(f"Cleared all rows in {len(tables)} table(s) from: {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete all rows from all user tables in a SQLite DB."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to SQLite DB file (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    clear_all_rows(Path(args.db).resolve())


if __name__ == "__main__":
    main()

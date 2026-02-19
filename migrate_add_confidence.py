"""
Migration: add avg_logprob and no_speech_prob to transcript_segments.

Run once from the project root:
    python migrate_add_confidence.py

Safe to run multiple times — checks if columns exist before adding them.
Existing rows will have NULL for both columns, which the review UI handles
gracefully (segments transcribed before this migration show no confidence badge).
"""

import sqlite3
import sys
from pathlib import Path

# Adjust this path if your database file is in a different location
DB_PATH = Path(__file__).parent / "civic_media.db"


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def migrate(db_path: Path) -> None:
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        print("Check DB_PATH at the top of this script.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)

    try:
        added = []

        if not column_exists(conn, "transcript_segments", "avg_logprob"):
            conn.execute(
                "ALTER TABLE transcript_segments ADD COLUMN avg_logprob REAL"
            )
            added.append("avg_logprob")

        if not column_exists(conn, "transcript_segments", "no_speech_prob"):
            conn.execute(
                "ALTER TABLE transcript_segments ADD COLUMN no_speech_prob REAL"
            )
            added.append("no_speech_prob")

        conn.commit()

        if added:
            print(f"Migration complete. Added columns: {', '.join(added)}")
            print("Existing segments will show NULL for these fields (expected).")
        else:
            print("Nothing to do — columns already exist.")

    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        DB_PATH = Path(sys.argv[1])
    print(f"Migrating: {DB_PATH}")
    migrate(DB_PATH)

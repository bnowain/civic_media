"""
Migration: add source_segment_id to voiceprints.

Run once from the project root:
    python migrate_add_voiceprint_tracking.py

Safe to run multiple times — checks if column exists before adding it.
Existing voiceprints will have NULL for source_segment_id (expected —
the re-confirm logic falls back to byte-equality matching for these).
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "database" / "civic_media.db"


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

        if not column_exists(conn, "voiceprints", "source_segment_id"):
            conn.execute(
                "ALTER TABLE voiceprints ADD COLUMN source_segment_id TEXT"
            )
            added.append("source_segment_id")

        conn.commit()

        if added:
            print(f"Migration complete. Added columns: {', '.join(added)}")
            print("Existing voiceprints will have NULL for source_segment_id (expected).")
        else:
            print("Nothing to do — columns already exist.")

    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        DB_PATH = Path(sys.argv[1])
    print(f"Migrating: {DB_PATH}")
    migrate(DB_PATH)

"""
Migration: Add processed_at column to meetings table.

Run once from the project root:
    python migrate_add_processed_at.py

Safe to run multiple times — checks if column exists before adding.

Backfill: Sets processed_at = created_at for meetings that have transcript
segments with embeddings (i.e., fully processed meetings).
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
        sys.exit(1)

    conn = sqlite3.connect(db_path)

    try:
        if column_exists(conn, "meetings", "processed_at"):
            print("Nothing to do — processed_at column already exists.")
            return

        conn.execute(
            "ALTER TABLE meetings ADD COLUMN processed_at DATETIME"
        )

        # Backfill: set processed_at = created_at for meetings with embedded segments
        conn.execute("""
            UPDATE meetings
            SET processed_at = created_at
            WHERE meeting_id IN (
                SELECT DISTINCT ts.meeting_id
                FROM transcript_segments ts
                WHERE ts.embedding IS NOT NULL
            )
        """)
        updated = conn.execute("SELECT changes()").fetchone()[0]

        conn.commit()
        print(f"Migration complete: added processed_at column to meetings.")
        print(f"  Backfilled {updated} meetings with processed_at = created_at.")

    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        DB_PATH = Path(sys.argv[1])
    print(f"Migrating: {DB_PATH}")
    migrate(DB_PATH)

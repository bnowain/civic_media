"""
Idempotent migration: add source_duration column to voiceprints and
backfill from source segments.

Usage:
    python migrate_voiceprint_duration.py

Safe to re-run — checks for column existence before ALTER.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "database" / "civic_media.db"


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Check if column already exists
    cur.execute("PRAGMA table_info(voiceprints)")
    columns = {row[1] for row in cur.fetchall()}

    if "source_duration" not in columns:
        cur.execute("ALTER TABLE voiceprints ADD COLUMN source_duration REAL")
        conn.commit()
        print("Added source_duration column to voiceprints")
    else:
        print("source_duration column already exists — skipping ALTER.")

    # Backfill from source segments (only where source_segment_id is set
    # and source_duration is still NULL)
    cur.execute("""
        UPDATE voiceprints
        SET source_duration = (
            SELECT ts.end_time - ts.start_time
            FROM transcript_segments ts
            WHERE ts.segment_id = voiceprints.source_segment_id
        )
        WHERE source_segment_id IS NOT NULL
          AND source_duration IS NULL
          AND EXISTS (
              SELECT 1 FROM transcript_segments ts
              WHERE ts.segment_id = voiceprints.source_segment_id
          )
    """)
    backfilled = cur.rowcount
    conn.commit()
    print(f"Backfilled source_duration for {backfilled} voiceprints from source segments.")

    conn.close()


if __name__ == "__main__":
    migrate()

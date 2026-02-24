"""
Idempotent migration: add overlap_ratio column to transcript_segments.

Usage:
    python migrate_overlap_ratio.py

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
    cur.execute("PRAGMA table_info(transcript_segments)")
    columns = {row[1] for row in cur.fetchall()}

    if "overlap_ratio" not in columns:
        cur.execute(
            "ALTER TABLE transcript_segments ADD COLUMN overlap_ratio REAL DEFAULT 0.0"
        )
        conn.commit()
        print("Added overlap_ratio column to transcript_segments (default 0.0)")
    else:
        print("overlap_ratio column already exists — nothing to do.")

    conn.close()


if __name__ == "__main__":
    migrate()

"""
Migration: Create clips table.

Run once from the project root:
    python migrate_clips_table.py

Safe to run multiple times — checks for existing table before creating.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "database" / "civic_media.db"


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cursor.fetchone() is not None


def migrate(db_path: Path) -> None:
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        changes = []

        if not table_exists(conn, "clips"):
            conn.execute("""
                CREATE TABLE clips (
                    clip_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_media_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL NOT NULL,
                    duration REAL NOT NULL,
                    title TEXT NOT NULL DEFAULT 'Untitled clip',
                    notes TEXT,
                    thumbnail_path TEXT,
                    export_path TEXT,
                    export_status TEXT,
                    export_error TEXT,
                    cover_image_path TEXT,
                    downloaded_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            changes.append("Created clips table")
        else:
            print("clips table already exists — skipping.")

        conn.commit()

        if changes:
            print("Migration complete:")
            for c in changes:
                print(f"  - {c}")
        else:
            print("Nothing to do — table already exists.")

    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        DB_PATH = Path(sys.argv[1])
    print(f"Migrating: {DB_PATH}")
    migrate(DB_PATH)

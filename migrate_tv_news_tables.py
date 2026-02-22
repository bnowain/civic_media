"""
Migration: Create TV news tables (tv_newscasts, tv_news_segments, tv_news_transcription_chunks).

Run once from the project root:
    python migrate_tv_news_tables.py

Safe to run multiple times — checks for existing tables before creating.
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

        if not table_exists(conn, "tv_newscasts"):
            conn.execute("""
                CREATE TABLE tv_newscasts (
                    newscast_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    station TEXT NOT NULL DEFAULT '',
                    air_date TEXT,
                    air_time TEXT,
                    source_file TEXT,
                    cleaned_file TEXT,
                    duration_seconds REAL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    error_detail TEXT,
                    processed_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            changes.append("Created tv_newscasts table")
        else:
            print("tv_newscasts table already exists — skipping.")

        if not table_exists(conn, "tv_news_segments"):
            conn.execute("""
                CREATE TABLE tv_news_segments (
                    segment_id TEXT PRIMARY KEY,
                    newscast_id TEXT NOT NULL REFERENCES tv_newscasts(newscast_id) ON DELETE CASCADE,
                    title TEXT,
                    summary TEXT,
                    start_time REAL,
                    end_time REAL,
                    story_type TEXT,
                    transcript TEXT,
                    last_tagged_at DATETIME,
                    tagging_version TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            changes.append("Created tv_news_segments table")
        else:
            print("tv_news_segments table already exists — skipping.")

        if not table_exists(conn, "tv_news_transcription_chunks"):
            conn.execute("""
                CREATE TABLE tv_news_transcription_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    newscast_id TEXT NOT NULL REFERENCES tv_newscasts(newscast_id) ON DELETE CASCADE,
                    segment_id TEXT REFERENCES tv_news_segments(segment_id) ON DELETE SET NULL,
                    start_time REAL,
                    end_time REAL,
                    text TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            changes.append("Created tv_news_transcription_chunks table")
        else:
            print("tv_news_transcription_chunks table already exists — skipping.")

        conn.commit()

        if changes:
            print("Migration complete:")
            for c in changes:
                print(f"  - {c}")
        else:
            print("Nothing to do — all tables already exist.")

    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        DB_PATH = Path(sys.argv[1])
    print(f"Migrating: {DB_PATH}")
    migrate(DB_PATH)

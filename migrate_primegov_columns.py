"""
Migration: Add PrimeGov columns to meetings table + transcode_status to media_files.

Safe to re-run — checks for existing columns before altering.

New columns on `meetings`:
  - primegov_id  INTEGER UNIQUE  (PrimeGov meeting ID for deduplication)
  - video_url    TEXT             (Swagit video page URL)
  - agenda_url   TEXT             (PrimeGov compiled agenda PDF URL)
  - minutes_url  TEXT             (PrimeGov compiled minutes PDF URL)

New columns on `media_files`:
  - transcode_status TEXT  (null | "pending" | "transcoding" | "transcoded")
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.config import DATABASE_PATH

DB_PATH = str(DATABASE_PATH)


def get_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def get_tables(cursor):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in cursor.fetchall()}


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    tables = get_tables(cursor)

    if "meetings" not in tables:
        print("  ! meetings table not found — run the app once first to create tables")
        conn.close()
        return

    cols = get_columns(cursor, "meetings")

    if "primegov_id" not in cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN primegov_id INTEGER")
        print("  + meetings.primegov_id")
        # Create unique index
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_meetings_primegov_id "
            "ON meetings(primegov_id) WHERE primegov_id IS NOT NULL"
        )
        print("  + ix_meetings_primegov_id (unique, partial)")
    else:
        print("  - meetings.primegov_id already exists")

    if "video_url" not in cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN video_url TEXT")
        print("  + meetings.video_url")
    else:
        print("  - meetings.video_url already exists")

    if "agenda_url" not in cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN agenda_url TEXT")
        print("  + meetings.agenda_url")
    else:
        print("  - meetings.agenda_url already exists")

    if "minutes_url" not in cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN minutes_url TEXT")
        print("  + meetings.minutes_url")
    else:
        print("  - meetings.minutes_url already exists")

    # ── media_files: add transcode_status ───────────────────────────────
    if "media_files" in tables:
        mf_cols = get_columns(cursor, "media_files")
        if "transcode_status" not in mf_cols:
            cursor.execute("ALTER TABLE media_files ADD COLUMN transcode_status TEXT")
            print("  + media_files.transcode_status")
        else:
            print("  - media_files.transcode_status already exists")
    else:
        print("  ! media_files table not found")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

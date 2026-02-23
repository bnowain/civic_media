"""
Migration: Add ingest system tables and columns.

Safe to re-run — checks for existing columns/tables before altering.

New columns on `meetings`:
  - description TEXT
  - source_url TEXT
  - thumbnail_url TEXT

New table: `ingest_sources`

Seeds 3 default ingest sources (KCNR, KQMS/SecureNet, Freedom in Action).
"""

import json
import sqlite3
import sys
from pathlib import Path

# Add project root to path
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

    # ── meetings: add new columns ───────────────────────────────────────
    if "meetings" in tables:
        cols = get_columns(cursor, "meetings")

        if "description" not in cols:
            cursor.execute("ALTER TABLE meetings ADD COLUMN description TEXT")
            print("  + meetings.description")

        if "source_url" not in cols:
            cursor.execute("ALTER TABLE meetings ADD COLUMN source_url TEXT")
            print("  + meetings.source_url")

        if "thumbnail_url" not in cols:
            cursor.execute("ALTER TABLE meetings ADD COLUMN thumbnail_url TEXT")
            print("  + meetings.thumbnail_url")
    else:
        print("  ! meetings table not found — run the app once first to create tables")

    # ── ingest_sources ──────────────────────────────────────────────────
    if "ingest_sources" not in tables:
        cursor.execute("""
            CREATE TABLE ingest_sources (
                source_id          TEXT PRIMARY KEY,
                name               TEXT NOT NULL UNIQUE,
                source_type        TEXT NOT NULL,
                config_json        TEXT,
                last_scraped_at    DATETIME,
                last_scraped_count REAL,
                enabled            BOOLEAN DEFAULT 1,
                created_at         DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("  + created ingest_sources table")

        # Seed default sources
        import uuid

        sources = [
            {
                "source_id": str(uuid.uuid4()),
                "name": "KCNR - Kevin Crye Show",
                "source_type": "kcnr",
                "config_json": json.dumps({
                    "base_url": "https://apps.kcnr1460.com",
                    "archive_path": "/kevin-crye-show",
                    "max_pages": 19,
                    "cutoff_date": "2026-01-25",
                }),
            },
            {
                "source_id": str(uuid.uuid4()),
                "name": "KQMS - SecureNet Shows",
                "source_type": "securenet",
                "config_json": json.dumps({
                    "station_url": "https://radio.securenetsystems.net/v5/KQMS",
                    "station_id": "KQMS",
                    "shows": [
                        "Kevin Crye",
                        "Poke the Hornets Nest",
                        "Freedom in Action",
                    ],
                }),
            },
            {
                "source_id": str(uuid.uuid4()),
                "name": "Freedom in Action - Kelly Frost",
                "source_type": "freedominaction",
                "config_json": json.dumps({
                    "blog_url": "https://www.freedominactionradio.com/blog",
                }),
            },
        ]

        for src in sources:
            cursor.execute(
                "INSERT INTO ingest_sources (source_id, name, source_type, config_json) "
                "VALUES (?, ?, ?, ?)",
                (src["source_id"], src["name"], src["source_type"], src["config_json"]),
            )
        print(f"  + seeded {len(sources)} default ingest sources")
    else:
        print("  - ingest_sources table already exists")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

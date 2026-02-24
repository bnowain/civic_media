"""
Migration: Add Podbean ingest source for Kevin Crye Show.

Safe to re-run — checks if the source already exists before inserting.
"""

import json
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.config import DATABASE_PATH

DB_PATH = str(DATABASE_PATH)


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if ingest_sources table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ingest_sources'")
    if not cursor.fetchone():
        print("  ! ingest_sources table not found — run migrate_ingest_system.py first")
        conn.close()
        return

    # Check if podbean source already exists
    cursor.execute(
        "SELECT source_id FROM ingest_sources WHERE source_type = 'podbean'"
    )
    if cursor.fetchone():
        print("  - Podbean source already exists, skipping")
        conn.close()
        return

    source = {
        "source_id": str(uuid.uuid4()),
        "name": "Kevin Crye Show (Podbean)",
        "source_type": "podbean",
        "config_json": json.dumps({
            "feed_url": "https://feed.podbean.com/admind4/feed.xml",
            "show_name": "Kevin Crye Show",
        }),
    }

    cursor.execute(
        "INSERT INTO ingest_sources (source_id, name, source_type, config_json) "
        "VALUES (?, ?, ?, ?)",
        (source["source_id"], source["name"], source["source_type"], source["config_json"]),
    )
    print(f"  + seeded Podbean source: {source['name']} ({source['source_id']})")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

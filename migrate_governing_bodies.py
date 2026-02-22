"""
Migration: Create governing_bodies lookup table and backfill from existing meetings.

Run once from the project root:
    python migrate_governing_bodies.py

Safe to run multiple times — checks for existing table/column before modifying.

Steps:
  1. Create governing_bodies table (governing_body_id TEXT PK, name, display_name, created_at)
  2. SELECT DISTINCT governing_body from meetings -> insert each as a row
  3. Add governing_body_id column to meetings (nullable FK)
  4. Backfill governing_body_id by matching name
"""

import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "database" / "civic_media.db"


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cursor.fetchone() is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def migrate(db_path: Path) -> None:
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        changes = []

        # 1. Create governing_bodies table
        if not table_exists(conn, "governing_bodies"):
            conn.execute("""
                CREATE TABLE governing_bodies (
                    governing_body_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            changes.append("Created governing_bodies table")

            # 2. Backfill from existing distinct governing_body values
            cursor = conn.execute(
                "SELECT DISTINCT governing_body FROM meetings "
                "WHERE governing_body IS NOT NULL AND governing_body != ''"
            )
            rows = cursor.fetchall()
            now = datetime.utcnow().isoformat()
            for (name,) in rows:
                gb_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO governing_bodies (governing_body_id, name, display_name, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (gb_id, name, name, now),
                )
            changes.append(f"Inserted {len(rows)} governing bodies from existing data")
        else:
            print("governing_bodies table already exists — skipping creation.")

        # 3. Add governing_body_id column to meetings
        if not column_exists(conn, "meetings", "governing_body_id"):
            conn.execute(
                "ALTER TABLE meetings ADD COLUMN governing_body_id TEXT "
                "REFERENCES governing_bodies(governing_body_id)"
            )
            changes.append("Added governing_body_id column to meetings")

            # 4. Backfill governing_body_id by matching name
            conn.execute("""
                UPDATE meetings
                SET governing_body_id = (
                    SELECT gb.governing_body_id
                    FROM governing_bodies gb
                    WHERE gb.name = meetings.governing_body
                )
                WHERE governing_body IS NOT NULL AND governing_body != ''
            """)
            updated = conn.execute(
                "SELECT changes()"
            ).fetchone()[0]
            changes.append(f"Backfilled governing_body_id on {updated} meetings")
        else:
            print("governing_body_id column already exists on meetings — skipping.")

        conn.commit()

        if changes:
            print("Migration complete:")
            for c in changes:
                print(f"  - {c}")
        else:
            print("Nothing to do — all changes already applied.")

    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        DB_PATH = Path(sys.argv[1])
    print(f"Migrating: {DB_PATH}")
    migrate(DB_PATH)

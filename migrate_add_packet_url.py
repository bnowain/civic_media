"""
Migration: Add packet_url column to meetings table.

Safe to re-run — checks for existing column before altering.

New column on `meetings`:
  - packet_url  TEXT  (PrimeGov compiled meeting packet PDF URL)
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

    if "packet_url" not in cols:
        cursor.execute("ALTER TABLE meetings ADD COLUMN packet_url TEXT")
        print("  + meetings.packet_url")
    else:
        print("  - meetings.packet_url already exists")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

"""
Migration: add 'category' column to the meetings table.
Safe to run multiple times — checks if column already exists.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("database/civic_media.db")


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH} — skipping migration.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(meetings)")
    columns = [row[1] for row in cursor.fetchall()]

    if "category" in columns:
        print("Column 'category' already exists — nothing to do.")
        conn.close()
        return

    cursor.execute(
        "ALTER TABLE meetings ADD COLUMN category TEXT NOT NULL DEFAULT 'meeting'"
    )
    conn.commit()
    print("Added 'category' column to meetings table (default='meeting').")

    conn.close()


if __name__ == "__main__":
    migrate()

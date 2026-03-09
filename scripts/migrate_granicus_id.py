"""
Migration: add granicus_id column to meetings table.

Additive-only — safe to run on existing databases.
Run with Windows Python from project root:

    powershell.exe -Command "cd 'E:\0-Automated-Apps\civic_media'; .\venv\Scripts\python.exe scripts/migrate_granicus_id.py"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
from app.config import DATABASE_PATH

DB_PATH = str(DATABASE_PATH)


def main():
    print(f"Database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    cur = conn.execute("PRAGMA table_info(meetings)")
    columns = {row[1] for row in cur.fetchall()}

    if "granicus_id" in columns:
        print("granicus_id column already exists — nothing to do.")
        conn.close()
        return

    print("Adding granicus_id column to meetings table…")
    conn.execute("ALTER TABLE meetings ADD COLUMN granicus_id INTEGER NULL")

    print("Creating index ix_meetings_granicus_id…")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_meetings_granicus_id "
        "ON meetings (granicus_id) WHERE granicus_id IS NOT NULL"
    )
    conn.commit()
    conn.close()

    print("Migration complete: granicus_id added to meetings.")


if __name__ == "__main__":
    main()

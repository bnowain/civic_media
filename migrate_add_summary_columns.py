"""
Migration: Add summary columns to meetings and documents tables.

Safe to re-run — checks for existing columns before altering.

New columns on `meetings`:
  - summary_short      TEXT   (short LLM-generated summary)
  - summary_long       TEXT   (detailed LLM-generated summary)
  - summary_updated_at TIMESTAMP (when summary was last generated)

New columns on `documents`:
  - summary_short      TEXT
  - summary_long       TEXT
  - summary_updated_at TIMESTAMP
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


COLUMNS_TO_ADD = {
    "meetings": [
        ("summary_short", "TEXT"),
        ("summary_long", "TEXT"),
        ("summary_updated_at", "TIMESTAMP"),
    ],
    "documents": [
        ("summary_short", "TEXT"),
        ("summary_long", "TEXT"),
        ("summary_updated_at", "TIMESTAMP"),
    ],
}


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    tables = get_tables(cursor)

    for table, columns in COLUMNS_TO_ADD.items():
        if table not in tables:
            print(f"  ! {table} table not found — run the app once first to create tables")
            continue

        existing = get_columns(cursor, table)
        for col_name, col_type in columns:
            if col_name not in existing:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                print(f"  + {table}.{col_name}")
            else:
                print(f"  - {table}.{col_name} already exists")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

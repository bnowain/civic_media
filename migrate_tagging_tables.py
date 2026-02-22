"""
Migration: Create tagging tables (tags, tag_assignments, tag_denials,
people_mentions, people_mention_denials).

Run once from the project root:
    python migrate_tagging_tables.py

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

        if not table_exists(conn, "tags"):
            conn.execute("""
                CREATE TABLE tags (
                    tag_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    parent_id TEXT REFERENCES tags(tag_id),
                    tag_type TEXT,
                    description TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            changes.append("Created tags table")
        else:
            print("tags table already exists — skipping.")

        if not table_exists(conn, "tag_assignments"):
            conn.execute("""
                CREATE TABLE tag_assignments (
                    assignment_id TEXT PRIMARY KEY,
                    tag_id TEXT NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
                    content_type TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    source TEXT,
                    confidence REAL,
                    llm_context TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    tagged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tag_id, content_type, content_id)
                )
            """)
            changes.append("Created tag_assignments table")
        else:
            print("tag_assignments table already exists — skipping.")

        if not table_exists(conn, "tag_denials"):
            conn.execute("""
                CREATE TABLE tag_denials (
                    denial_id TEXT PRIMARY KEY,
                    tag_id TEXT NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
                    content_type TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    denied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tag_id, content_type, content_id)
                )
            """)
            changes.append("Created tag_denials table")
        else:
            print("tag_denials table already exists — skipping.")

        if not table_exists(conn, "people_mentions"):
            conn.execute("""
                CREATE TABLE people_mentions (
                    mention_id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
                    content_type TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    source TEXT,
                    confidence REAL,
                    context TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(person_id, content_type, content_id)
                )
            """)
            changes.append("Created people_mentions table")
        else:
            print("people_mentions table already exists — skipping.")

        if not table_exists(conn, "people_mention_denials"):
            conn.execute("""
                CREATE TABLE people_mention_denials (
                    denial_id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
                    content_type TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    denied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(person_id, content_type, content_id)
                )
            """)
            changes.append("Created people_mention_denials table")
        else:
            print("people_mention_denials table already exists — skipping.")

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

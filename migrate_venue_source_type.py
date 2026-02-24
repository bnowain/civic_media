"""
Migration: Add venues table, source_type columns, and venue FK columns.

Safe to re-run — checks for existing tables/columns before altering.

New table:
  - venues (venue_id, name, acoustic_type, notes, created_at, updated_at)

New columns:
  - governing_bodies.default_venue_id  TEXT FK → venues
  - meetings.venue_id                  TEXT FK → venues
  - transcript_segments.source_type    TEXT NOT NULL DEFAULT 'in_person'
  - voiceprints.venue_id               TEXT FK → venues
  - voiceprints.source_type            TEXT NOT NULL DEFAULT 'in_person'

Seed data: 4 initial venues (BOS Chamber, Redding Council, KCNR Studio, Telephone).
Backfill: sets BOS governing body default_venue_id if both exist.
"""

import sqlite3
import sys
import uuid
from datetime import datetime
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
    now = datetime.utcnow().isoformat()

    # ── Step 1: Create venues table ──────────────────────────────────────
    if "venues" not in tables:
        cursor.execute("""
            CREATE TABLE venues (
                venue_id      TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                acoustic_type TEXT NOT NULL DEFAULT 'indoor_dry',
                notes         TEXT,
                created_at    DATETIME,
                updated_at    DATETIME
            )
        """)
        print("  + created venues table")
    else:
        print("  - venues table already exists")

    # ── Step 2: Seed initial venues ──────────────────────────────────────
    seeds = [
        ("Shasta County Board of Supervisors Chamber", "indoor_dry"),
        ("Redding City Council Chambers", "indoor_dry"),
        ("KCNR Studio", "radio_studio"),
        ("Telephone", "phone"),
    ]
    for name, acoustic_type in seeds:
        cursor.execute("SELECT venue_id FROM venues WHERE name = ?", (name,))
        if cursor.fetchone() is None:
            venue_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO venues (venue_id, name, acoustic_type, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (venue_id, name, acoustic_type, now, now),
            )
            print(f"  + seeded venue: {name}")
        else:
            print(f"  - venue already exists: {name}")

    # ── Step 3: Add columns ──────────────────────────────────────────────

    # governing_bodies.default_venue_id
    if "governing_bodies" in tables:
        cols = get_columns(cursor, "governing_bodies")
        if "default_venue_id" not in cols:
            cursor.execute(
                "ALTER TABLE governing_bodies ADD COLUMN default_venue_id TEXT "
                "REFERENCES venues(venue_id)"
            )
            print("  + governing_bodies.default_venue_id")
        else:
            print("  - governing_bodies.default_venue_id already exists")
    else:
        print("  ! governing_bodies table not found")

    # meetings.venue_id
    if "meetings" in tables:
        cols = get_columns(cursor, "meetings")
        if "venue_id" not in cols:
            cursor.execute(
                "ALTER TABLE meetings ADD COLUMN venue_id TEXT "
                "REFERENCES venues(venue_id)"
            )
            print("  + meetings.venue_id")
        else:
            print("  - meetings.venue_id already exists")
    else:
        print("  ! meetings table not found")

    # transcript_segments.source_type
    if "transcript_segments" in tables:
        cols = get_columns(cursor, "transcript_segments")
        if "source_type" not in cols:
            cursor.execute(
                "ALTER TABLE transcript_segments ADD COLUMN source_type TEXT "
                "NOT NULL DEFAULT 'in_person'"
            )
            print("  + transcript_segments.source_type")
        else:
            print("  - transcript_segments.source_type already exists")
    else:
        print("  ! transcript_segments table not found")

    # voiceprints.venue_id
    if "voiceprints" in tables:
        vp_cols = get_columns(cursor, "voiceprints")
        if "venue_id" not in vp_cols:
            cursor.execute(
                "ALTER TABLE voiceprints ADD COLUMN venue_id TEXT "
                "REFERENCES venues(venue_id)"
            )
            print("  + voiceprints.venue_id")
        else:
            print("  - voiceprints.venue_id already exists")

        if "source_type" not in vp_cols:
            cursor.execute(
                "ALTER TABLE voiceprints ADD COLUMN source_type TEXT "
                "NOT NULL DEFAULT 'in_person'"
            )
            print("  + voiceprints.source_type")
        else:
            print("  - voiceprints.source_type already exists")
    else:
        print("  ! voiceprints table not found")

    # ── Step 4: Backfill governing body defaults ─────────────────────────
    cursor.execute(
        "SELECT venue_id FROM venues WHERE name = ?",
        ("Shasta County Board of Supervisors Chamber",),
    )
    bos_venue = cursor.fetchone()

    cursor.execute(
        "SELECT governing_body_id, default_venue_id FROM governing_bodies "
        "WHERE name LIKE '%Board of Supervisors%'"
    )
    bos_gb = cursor.fetchone()

    if bos_venue and bos_gb and bos_gb[1] is None:
        cursor.execute(
            "UPDATE governing_bodies SET default_venue_id = ? "
            "WHERE governing_body_id = ?",
            (bos_venue[0], bos_gb[0]),
        )
        print("  + backfilled BOS default_venue_id")
    elif bos_venue and bos_gb and bos_gb[1] is not None:
        print("  - BOS default_venue_id already set")
    else:
        print("  - BOS governing body or venue not found (skipping backfill)")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

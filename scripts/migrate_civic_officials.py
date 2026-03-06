"""
Migration: Create civic_officials, civic_chair_history, and civic_frequent_speakers tables.
Seeds data from Mission Control's knowledge.db (where it was stored temporarily).

Run: python scripts/migrate_civic_officials.py
"""

import json
import sqlite3
import sys
from pathlib import Path

CIVIC_DB = Path(__file__).parent.parent / "database" / "civic_media.db"
MC_DB = Path(__file__).parent.parent.parent / "Mission_Control" / "database" / "knowledge.db"


def run():
    conn = sqlite3.connect(str(CIVIC_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # ── Create tables ─────────────────────────────────────────────────────────

    conn.execute("""
        CREATE TABLE IF NOT EXISTS civic_officials (
            official_id           TEXT PRIMARY KEY,
            official_jurisdiction TEXT NOT NULL,
            official_body_type    TEXT NOT NULL,
            official_body_name    TEXT,
            official_person_name  TEXT NOT NULL,
            official_role         TEXT NOT NULL,
            official_district     TEXT,
            official_term_start   TEXT,
            official_term_end     TEXT,
            official_is_current   INTEGER DEFAULT 1,
            official_aliases      TEXT DEFAULT '[]',
            official_notes        TEXT,
            person_id             TEXT REFERENCES people(person_id),
            created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_civic_officials_lookup
        ON civic_officials(official_jurisdiction, official_body_type, official_is_current)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS civic_chair_history (
            chair_id     TEXT PRIMARY KEY,
            jurisdiction TEXT NOT NULL,
            body_type    TEXT NOT NULL,
            chair_name   TEXT NOT NULL,
            chair_start  TEXT NOT NULL,
            chair_end    TEXT,
            chair_role   TEXT NOT NULL DEFAULT 'chair',
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS civic_frequent_speakers (
            speaker_id           TEXT PRIMARY KEY,
            speaker_jurisdiction TEXT NOT NULL,
            speaker_body_type    TEXT,
            canonical_name       TEXT NOT NULL,
            speaker_aliases      TEXT DEFAULT '[]',
            appearance_count     INTEGER DEFAULT 1,
            last_seen            TEXT,
            speaker_notes        TEXT,
            person_id            TEXT REFERENCES people(person_id),
            created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_frequent_speakers_lookup
        ON civic_frequent_speakers(speaker_jurisdiction, speaker_body_type)
    """)

    conn.commit()
    print("Tables created.")

    # ── Seed from Mission Control if available ────────────────────────────────

    if not MC_DB.exists():
        print(f"Mission Control DB not found at {MC_DB} — tables created but not seeded.")
        conn.close()
        return

    mc = sqlite3.connect(str(MC_DB))
    mc.row_factory = sqlite3.Row

    # Check if MC has the tables
    mc_tables = {r[0] for r in mc.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    seeded = 0

    if "civic_officials" in mc_tables:
        existing = conn.execute("SELECT COUNT(*) FROM civic_officials").fetchone()[0]
        if existing == 0:
            rows = mc.execute("SELECT * FROM civic_officials").fetchall()
            for r in rows:
                # Try to link to existing person in civic_media
                person_row = conn.execute(
                    "SELECT person_id FROM people WHERE canonical_name = ?",
                    (r["official_person_name"],)
                ).fetchone()
                person_id = person_row[0] if person_row else None

                conn.execute("""
                    INSERT INTO civic_officials (
                        official_id, official_jurisdiction, official_body_type,
                        official_body_name, official_person_name, official_role,
                        official_district, official_term_start, official_term_end,
                        official_is_current, official_aliases, official_notes,
                        person_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r["official_id"], r["official_jurisdiction"], r["official_body_type"],
                    r["official_body_name"], r["official_person_name"], r["official_role"],
                    r["official_district"], r["official_term_start"], r["official_term_end"],
                    r["official_is_current"], r["official_aliases"], r["official_notes"],
                    person_id, r["created_at"], r["updated_at"],
                ))
            seeded += len(rows)
            print(f"  Seeded {len(rows)} civic_officials from Mission Control")
        else:
            print(f"  civic_officials already has {existing} rows — skipping seed")

    if "civic_chair_history" in mc_tables:
        existing = conn.execute("SELECT COUNT(*) FROM civic_chair_history").fetchone()[0]
        if existing == 0:
            rows = mc.execute("SELECT * FROM civic_chair_history").fetchall()
            for r in rows:
                conn.execute("""
                    INSERT INTO civic_chair_history (
                        chair_id, jurisdiction, body_type, chair_name,
                        chair_start, chair_end, chair_role, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r["chair_id"], r["jurisdiction"], r["body_type"], r["chair_name"],
                    r["chair_start"], r["chair_end"], r["chair_role"], r["created_at"],
                ))
            seeded += len(rows)
            print(f"  Seeded {len(rows)} civic_chair_history from Mission Control")
        else:
            print(f"  civic_chair_history already has {existing} rows — skipping seed")

    if "civic_frequent_speakers" in mc_tables:
        existing = conn.execute("SELECT COUNT(*) FROM civic_frequent_speakers").fetchone()[0]
        if existing == 0:
            rows = mc.execute("SELECT * FROM civic_frequent_speakers").fetchall()
            for r in rows:
                person_row = conn.execute(
                    "SELECT person_id FROM people WHERE canonical_name = ?",
                    (r["canonical_name"],)
                ).fetchone()
                person_id = person_row[0] if person_row else None

                conn.execute("""
                    INSERT INTO civic_frequent_speakers (
                        speaker_id, speaker_jurisdiction, speaker_body_type,
                        canonical_name, speaker_aliases, appearance_count,
                        last_seen, speaker_notes, person_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r["speaker_id"], r["speaker_jurisdiction"], r["speaker_body_type"],
                    r["canonical_name"], r["speaker_aliases"], r["appearance_count"],
                    r["last_seen"], r["speaker_notes"], person_id,
                    r["created_at"], r["updated_at"],
                ))
            seeded += len(rows)
            print(f"  Seeded {len(rows)} civic_frequent_speakers from Mission Control")
        else:
            print(f"  civic_frequent_speakers already has {existing} rows — skipping seed")

    conn.commit()
    mc.close()
    conn.close()
    print(f"Migration complete. {seeded} total rows seeded.")


if __name__ == "__main__":
    run()

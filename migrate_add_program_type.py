"""
Migration: add 'program_type' column to the meetings table.

Introduces a semantic content classification that is distinct from the
existing 'category' field (which mixes format and purpose) and from the
'content_type' field already used on tag_assignments/people_mentions
(which is a polymorphic FK, not a meeting classification).

program_type values:
  'governing_meeting' — official government body convened in its legal
                        capacity (Board of Supervisors, City Council,
                        Planning Commission, etc.)
  'media_broadcast'   — radio shows, podcasts, AM/FM talk programs
                        (Kevin Crye Show, Free Fire Radio, Poke the
                        Hornets Nest, Freedom in Action, etc.)
  'news_broadcast'    — TV or radio news segments / newscasts
  'web_show'          — online/streamed web shows (auto-ingest or one-off)

Populated from existing 'category' values:
  category='meeting'    → program_type='governing_meeting'
  category='audio'      → program_type='media_broadcast'
  category='news'       → program_type='news_broadcast'
  category='web_series' → program_type='web_show'

The 'category' column is NOT removed or changed — existing code continues
to work. New code (ingest filters, Atlas search, mc meeting ingest) should
use 'program_type'. Migrate existing callsites when convenient, not required
to do it all at once.

Safe to run multiple times — idempotent.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("database/civic_media.db")

_CATEGORY_MAP = {
    "meeting":    "governing_meeting",
    "audio":      "media_broadcast",
    "news":       "news_broadcast",
    "web_series": "web_show",
}


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH} — skipping migration.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    cursor = conn.cursor()

    # ── Step 1: Add column if missing ────────────────────────────────────────
    cursor.execute("PRAGMA table_info(meetings)")
    existing = [row[1] for row in cursor.fetchall()]

    if "program_type" not in existing:
        cursor.execute("ALTER TABLE meetings ADD COLUMN program_type TEXT")
        conn.commit()
        print("Added 'program_type' column to meetings table.")
    else:
        print("Column 'program_type' already exists — skipping ALTER TABLE.")

    # ── Step 2: Populate from category ───────────────────────────────────────
    for category_val, program_type_val in _CATEGORY_MAP.items():
        result = cursor.execute(
            """
            UPDATE meetings
            SET    program_type = ?
            WHERE  category     = ?
              AND  (program_type IS NULL OR program_type != ?)
            """,
            (program_type_val, category_val, program_type_val),
        )
        if result.rowcount:
            print(
                f"  Set program_type='{program_type_val}' on "
                f"{result.rowcount} rows where category='{category_val}'."
            )

    conn.commit()

    # ── Step 3: Catch any rows with an unrecognised category ─────────────────
    unrecognised = cursor.execute(
        "SELECT category, COUNT(*) FROM meetings WHERE program_type IS NULL GROUP BY category"
    ).fetchall()
    if unrecognised:
        print("\nWARNING — rows with unknown category (program_type left NULL):")
        for cat, cnt in unrecognised:
            print(f"  category={repr(cat)!s:20} {cnt} rows")
        print("  Add these to _CATEGORY_MAP and re-run, or set program_type manually.")
    else:
        print("\nAll rows have a program_type — migration complete.")

    # ── Step 4: Summary ───────────────────────────────────────────────────────
    print()
    rows = cursor.execute(
        "SELECT program_type, COUNT(*) as cnt FROM meetings GROUP BY program_type ORDER BY cnt DESC"
    ).fetchall()
    print("program_type distribution:")
    for pt, cnt in rows:
        print(f"  {str(pt):25} {cnt}")

    conn.close()


if __name__ == "__main__":
    migrate()

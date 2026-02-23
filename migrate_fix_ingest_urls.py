"""
Migration: Fix ingest source URLs and delete test episodes.

1. Updates SecureNet config_json → correct station_url
2. Updates KCNR config_json → correct base_url
3. Deletes all meetings created by test_ingest.py (the 6 test episodes)

Safe to re-run — updates are idempotent, deletes only affect matching rows.
"""

import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.config import DATABASE_PATH, MEDIA_DIR

DB_PATH = str(DATABASE_PATH)


def migrate():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()

    # ── 1. Fix SecureNet station_url ────────────────────────────────────
    cursor.execute(
        "SELECT source_id, config_json FROM ingest_sources WHERE source_type = 'securenet'"
    )
    for source_id, config_str in cursor.fetchall():
        config = json.loads(config_str) if config_str else {}
        old_url = config.get("station_url", "")
        if old_url != "https://radio.securenetsystems.net/v5/KQMS":
            config["station_url"] = "https://radio.securenetsystems.net/v5/KQMS"
            cursor.execute(
                "UPDATE ingest_sources SET config_json = ? WHERE source_id = ?",
                (json.dumps(config), source_id),
            )
            print(f"  Fixed SecureNet station_url: {old_url} -> {config['station_url']}")

    # ── 2. Fix KCNR base_url ───────────────────────────────────────────
    cursor.execute(
        "SELECT source_id, config_json FROM ingest_sources WHERE source_type = 'kcnr'"
    )
    for source_id, config_str in cursor.fetchall():
        config = json.loads(config_str) if config_str else {}
        old_url = config.get("base_url", "")
        if old_url != "https://apps.kcnr1460.com":
            config["base_url"] = "https://apps.kcnr1460.com"
            cursor.execute(
                "UPDATE ingest_sources SET config_json = ? WHERE source_id = ?",
                (json.dumps(config), source_id),
            )
            print(f"  Fixed KCNR base_url: {old_url} -> {config['base_url']}")

    # ── 3. Delete test episodes ─────────────────────────────────────────
    # Find meetings that were created by the test run.
    # These include "Global Alert News" and "Retirement Lifestyles" (unwanted)
    # plus any other test_ingest.py episodes.
    # We identify them by source_url pointing to securenetsystems.net or
    # kcnr1460.com AND category='audio' AND no processed_at (never processed).
    # Also match titles containing known bad shows.
    cursor.execute("""
        SELECT meeting_id, title FROM meetings
        WHERE category = 'audio'
          AND processed_at IS NULL
          AND (
              title LIKE '%Global Alert News%'
              OR title LIKE '%Retirement Lifestyles%'
          )
    """)
    bad_meetings = cursor.fetchall()

    # Also find ALL unprocessed audio meetings from the test run
    # (those created very recently with source_url from securenet or kcnr)
    cursor.execute("""
        SELECT meeting_id, title FROM meetings
        WHERE category = 'audio'
          AND processed_at IS NULL
          AND source_url IS NOT NULL
          AND (
              source_url LIKE '%securenetsystems.net%'
              OR source_url LIKE '%kcnr1460.com%'
          )
    """)
    test_meetings = cursor.fetchall()

    # Combine and deduplicate
    all_ids = {row[0] for row in bad_meetings + test_meetings}

    if all_ids:
        print(f"\n  Deleting {len(all_ids)} test episodes:")
        for mid in all_ids:
            cursor.execute("SELECT title FROM meetings WHERE meeting_id = ?", (mid,))
            row = cursor.fetchone()
            title = row[0] if row else "?"
            print(f"    - {mid[:8]}... {title}")

            # Delete media_files rows
            cursor.execute("DELETE FROM media_files WHERE meeting_id = ?", (mid,))
            # Delete meeting row (cascades handled by FK or manual)
            cursor.execute("DELETE FROM meetings WHERE meeting_id = ?", (mid,))

            # Delete files on disk
            meeting_dir = MEDIA_DIR / mid
            if meeting_dir.exists():
                shutil.rmtree(meeting_dir)
                print(f"      Deleted directory: {meeting_dir}")
    else:
        print("  No test episodes to delete.")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

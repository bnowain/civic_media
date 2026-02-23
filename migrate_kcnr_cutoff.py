"""
Migration: Add KCNR cutoff date and remove post-cutoff KCNR episodes.

Kevin Crye and Poke the Hornets Nest moved from KCNR to KQMS (SecureNet)
on 2026-01-25. Episodes after that date on KCNR are duplicates.

1. Adds cutoff_date to KCNR ingest source config
2. Deletes any downloaded KCNR episodes dated >= 2026-01-25

Safe to re-run.
"""

import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.config import DATABASE_PATH, MEDIA_DIR

DB_PATH = str(DATABASE_PATH)
CUTOFF = "2026-01-25"


def migrate():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()

    # ── 1. Add cutoff_date to KCNR config ──────────────────────────────
    cursor.execute(
        "SELECT source_id, config_json FROM ingest_sources WHERE source_type = 'kcnr'"
    )
    for source_id, config_str in cursor.fetchall():
        config = json.loads(config_str) if config_str else {}
        if config.get("cutoff_date") != CUTOFF:
            config["cutoff_date"] = CUTOFF
            cursor.execute(
                "UPDATE ingest_sources SET config_json = ? WHERE source_id = ?",
                (json.dumps(config), source_id),
            )
            print(f"  Set KCNR cutoff_date = {CUTOFF}")

    # ── 2. Delete KCNR episodes >= cutoff ───────────────────────────────
    cursor.execute("""
        SELECT meeting_id, title, meeting_date FROM meetings
        WHERE category = 'audio'
          AND source_url LIKE '%kcnr1460.com%'
          AND meeting_date >= ?
    """, (CUTOFF,))
    rows = cursor.fetchall()

    if rows:
        print(f"\n  Deleting {len(rows)} KCNR episodes >= {CUTOFF}:")
        for mid, title, date in rows:
            print(f"    - {date} | {mid[:8]}... {title}")
            cursor.execute("DELETE FROM media_files WHERE meeting_id = ?", (mid,))
            cursor.execute("DELETE FROM meetings WHERE meeting_id = ?", (mid,))
            meeting_dir = MEDIA_DIR / mid
            if meeting_dir.exists():
                try:
                    shutil.rmtree(meeting_dir)
                except PermissionError:
                    # File may be locked by another process; try individual files
                    import gc, time
                    gc.collect()
                    time.sleep(0.5)
                    try:
                        shutil.rmtree(meeting_dir)
                    except PermissionError as e:
                        print(f"      WARNING: Could not delete dir (locked): {e}")
                        print(f"      Delete manually: {meeting_dir}")
    else:
        print("  No KCNR episodes to delete.")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

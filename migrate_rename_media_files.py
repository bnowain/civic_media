"""
Migration: Rename all legacy-named media files to intelligent names.

Renames files on disk and updates file_path in the database:
  - audio_source.* → {date}_{title}.{ext}
  - video.*        → {date}_{title}.{ext}
  - audio.wav      → {date}_{title}_extracted.wav

Also updates clips.source_media_path for stale references.

Safe to re-run — skips files that already use the new naming convention.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.config import DATABASE_PATH
from app.utils import generate_media_filename

DB_PATH = str(DATABASE_PATH)

# Legacy filename prefixes that indicate old naming convention
_LEGACY_PREFIXES = ("audio_source", "video.", "audio.wav")


def _is_legacy_name(filename: str) -> bool:
    """Check if a filename uses the old naming convention."""
    lower = filename.lower()
    return (
        lower.startswith("audio_source")
        or lower.startswith("video.")
        or lower == "audio.wav"
    )


def _is_extracted(filename: str) -> bool:
    """Check if a file is the pipeline-extracted audio (not a source file)."""
    return filename.lower() == "audio.wav"


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    renamed = 0
    skipped = 0
    errors = 0

    # ── Rename media_files ──────────────────────────────────────────────
    cursor.execute("""
        SELECT mf.media_id, mf.file_path, mf.file_type,
               m.meeting_date, m.title, m.meeting_id
        FROM media_files mf
        JOIN meetings m ON mf.meeting_id = m.meeting_id
        WHERE mf.file_type IN ('video', 'audio')
    """)

    rows = cursor.fetchall()
    print(f"  Checking {len(rows)} media files...")

    for media_id, file_path, file_type, meeting_date, title, meeting_id in rows:
        old_path = Path(file_path)
        old_name = old_path.name

        if not _is_legacy_name(old_name):
            skipped += 1
            continue

        if not old_path.exists():
            print(f"    SKIP (missing): {old_path}")
            skipped += 1
            continue

        # Determine if this is extracted audio or a source file
        ext = old_path.suffix
        suffix = "_extracted" if _is_extracted(old_name) else ""

        new_name = generate_media_filename(meeting_date, title, ext, suffix=suffix)
        new_path = old_path.parent / new_name

        # Avoid collisions (shouldn't happen, but be safe)
        if new_path.exists() and new_path != old_path:
            # Add media_id fragment to disambiguate
            stem = new_path.stem
            new_name = f"{stem}_{media_id[:8]}{ext}"
            new_path = old_path.parent / new_name

        try:
            old_path.rename(new_path)
            cursor.execute(
                "UPDATE media_files SET file_path = ? WHERE media_id = ?",
                (str(new_path), media_id),
            )
            print(f"    {old_name} -> {new_name}")
            renamed += 1

            # Update any clips referencing the old path
            cursor.execute(
                "UPDATE clips SET source_media_path = ? WHERE source_media_path = ?",
                (str(new_path), str(old_path)),
            )
        except Exception as exc:
            print(f"    ERROR renaming {old_path}: {exc}")
            errors += 1

    conn.commit()
    conn.close()
    print(f"\nMigration complete: {renamed} renamed, {skipped} skipped, {errors} errors.")


if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}")
    migrate()

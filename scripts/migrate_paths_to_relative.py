#!/usr/bin/env python3
"""
Migrate civic_media.db path columns from absolute to relative.

Strips the STORAGE_ROOT prefix from all 10 path columns so the file store
can be moved to a different machine without breaking the app.

Usage:
    cd /path/to/civic_media
    python scripts/migrate_paths_to_relative.py           # live run
    python scripts/migrate_paths_to_relative.py --dry-run # preview only

A backup is created automatically at civic_media.db.bak-premigration
before any changes are committed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# --Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
DATABASE_PATH = BASE_DIR / "database" / "civic_media.db"
STORAGE_ROOT = Path(os.environ.get("CIVIC_MEDIA_STORAGE_ROOT", str(BASE_DIR))).resolve()

# Table → column pairs (all 10 path columns)
PATH_COLUMNS = [
    ("meetings",            "media_directory"),
    ("media_files",         "file_path"),
    ("documents",           "file_path"),
    ("tv_newscasts",        "source_file"),
    ("tv_newscasts",        "cleaned_file"),
    ("clips",               "source_media_path"),
    ("clips",               "thumbnail_path"),
    ("clips",               "export_path"),
    ("clips",               "cover_image_path"),
    ("reference_documents", "source_file"),
]


def _strip_prefix(original: str, prefix: str) -> str | None:
    """Return the relative form of *original* if it starts with *prefix*.

    Returns None if no stripping was needed (path already relative or
    outside STORAGE_ROOT).
    """
    # Normalise separators for Windows/WSL paths stored with backslashes
    norm = original.replace("\\", "/")
    pfx = prefix.replace("\\", "/").rstrip("/")

    if norm.startswith(pfx + "/") or norm == pfx:
        rel = norm[len(pfx):].lstrip("/")
        return rel if rel else None  # skip if stripping yields empty string
    return None


def migrate(dry_run: bool = False) -> None:
    if not DATABASE_PATH.exists():
        print(f"ERROR: Database not found at {DATABASE_PATH}", file=sys.stderr)
        sys.exit(1)

    prefix = str(STORAGE_ROOT)

    print(f"STORAGE_ROOT : {STORAGE_ROOT}")
    print(f"Database     : {DATABASE_PATH}")
    print(f"Dry run      : {dry_run}")
    print()

    # --Backup ────────────────────────────────────────────────────────────────
    backup_path = DATABASE_PATH.with_suffix(".db.bak-premigration")
    if not dry_run:
        shutil.copy2(DATABASE_PATH, backup_path)
        print(f"Backup created : {backup_path}")
    else:
        print(f"[DRY RUN] Would create backup : {backup_path}")
    print()

    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row

    try:
        total_updated = 0

        for table, column in PATH_COLUMNS:
            # Verify table exists
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not cur.fetchone():
                print(f"  SKIP  {table}.{column} — table not found")
                continue

            # Fetch all non-null values
            rows = conn.execute(
                f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()

            updated = 0
            for row in rows:
                original = row[column]
                rel = _strip_prefix(original, prefix)
                if rel is not None and rel != original:
                    if not dry_run:
                        conn.execute(
                            f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                            (rel, row["rowid"]),
                        )
                    updated += 1

            if updated:
                verb = "[DRY RUN] Would update" if dry_run else "Updated"
                print(f"  {verb:20s} {table}.{column}: {updated} rows")
            else:
                print(f"  {'OK (no change)':20s} {table}.{column}")

            total_updated += updated

        if not dry_run:
            conn.commit()
            print(f"\nCommitted. Total rows updated: {total_updated}")
        else:
            print(f"\n[DRY RUN] Would update {total_updated} rows total. No changes made.")

        # --Verification ──────────────────────────────────────────────────────
        print("\n-- Verification: rows still containing absolute '/' prefix --")
        checks = [
            ("meetings",            "media_directory",   "meetings"),
            ("media_files",         "file_path",         "media_files"),
            ("documents",           "file_path",         "documents"),
            ("tv_newscasts [src]",  "source_file",       "tv_newscasts"),
            ("tv_newscasts [cln]",  "cleaned_file",      "tv_newscasts"),
            ("clips [src]",         "source_media_path", "clips"),
            ("clips [thumb]",       "thumbnail_path",    "clips"),
            ("clips [export]",      "export_path",       "clips"),
            ("clips [cover]",       "cover_image_path",  "clips"),
        ]
        for label, col, tbl in checks:
            try:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE {col} LIKE '/%' OR {col} LIKE '_:\\%'"
                ).fetchone()[0]
                status = "OK (0 absolute)" if count == 0 else f"WARNING: {count} absolute paths remain"
                print(f"  {label}.{col}: {status}")
            except Exception as exc:
                print(f"  {label}.{col}: ERROR ({exc})")

        # Sample values
        print("\n-- Sample values (should look like media/{id}/file.mp4) --")
        for tbl, col in [("media_files", "file_path"), ("meetings", "media_directory")]:
            rows = conn.execute(
                f"SELECT {col} FROM {tbl} WHERE {col} IS NOT NULL LIMIT 3"
            ).fetchall()
            for r in rows:
                print(f"  {tbl}.{col}: {r[0]}")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate civic_media DB paths to relative (strip STORAGE_ROOT prefix)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying the DB",
    )
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)

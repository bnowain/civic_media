"""
Portable path helpers for civic_media.

All file paths stored in the DB are relative to STORAGE_ROOT so the file
store can be moved to a NAS, cloud mount, or separate server without
breaking the app.

  to_relative(path)  — strip STORAGE_ROOT prefix before DB writes
  to_absolute(path)  — prepend STORAGE_ROOT before file I/O reads

Both functions are idempotent: already-relative paths pass through
to_relative unchanged; already-absolute paths pass through to_absolute
unchanged.
"""

from __future__ import annotations

from pathlib import Path

from app.config import STORAGE_ROOT


def to_relative(path: str | None) -> str | None:
    """Strip STORAGE_ROOT prefix for DB writes.

    Idempotent — if path is already relative, returns it unchanged.
    If path is absolute but outside STORAGE_ROOT, returns it unchanged
    (edge case: reference documents from arbitrary locations).
    """
    if not path:
        return path
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.relative_to(STORAGE_ROOT))
        except ValueError:
            return path  # outside STORAGE_ROOT — store as-is
    return path


def to_absolute(path: str | None) -> str | None:
    """Prepend STORAGE_ROOT for file I/O reads.

    Idempotent — if path is already absolute, returns it unchanged.
    """
    if not path:
        return path
    p = Path(path)
    if p.is_absolute():
        return path
    return str(STORAGE_ROOT / p)

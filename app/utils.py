"""
Shared utility functions for Civic Media.
"""

from __future__ import annotations

import re


def generate_media_filename(
    date: str | None,
    title: str | None,
    ext: str,
    suffix: str = "",
) -> str:
    """
    Generate a human-readable media filename.

    Args:
        date:   YYYY-MM-DD date string (falls back to "unknown-date").
        title:  Meeting/episode title (falls back to "media").
        ext:    File extension including dot (e.g. ".mp3", ".wav").
        suffix: Optional suffix before extension (e.g. "_extracted").

    Returns:
        Filename like ``2026-02-22_Kevin-Crye-Show_extracted.wav``
    """
    # Validate / fallback date
    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        date = "unknown-date"

    # Sanitize title
    if not title or not title.strip():
        safe_title = "media"
    else:
        # Replace non-alphanumeric (except hyphens) with hyphens
        safe_title = re.sub(r"[^a-zA-Z0-9-]+", "-", title)
        # Collapse multiple hyphens
        safe_title = re.sub(r"-{2,}", "-", safe_title)
        # Strip leading/trailing hyphens
        safe_title = safe_title.strip("-")
        # Truncate to ~80 chars
        if len(safe_title) > 80:
            safe_title = safe_title[:80].rstrip("-")
        # Final fallback
        if not safe_title:
            safe_title = "media"

    # Ensure ext starts with a dot
    if ext and not ext.startswith("."):
        ext = f".{ext}"

    return f"{date}_{safe_title}{suffix}{ext}"

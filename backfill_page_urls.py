#!/usr/bin/env python3
"""
Backfill page_url for existing audio meetings.

Handles three source types:
  - KCNR (source_url LIKE '%kcnr1460%') ->archive page by show name
  - SecureNet (source_url LIKE '%securenetsystems%') ->station player URL
  - Podbean (source_url LIKE '%podbean%') ->re-fetch RSS, match by source_url

Idempotent — skips rows that already have page_url set.
"""

from __future__ import annotations

import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).resolve().parent / "database" / "civic_media.db"

KCNR_BASE = "https://apps.kcnr1460.com"
KCNR_SHOW_MAP = {
    "Kevin Crye Show": "/Show/archive/kevin_crye",
    "Poke the Hornets Nest": "/Show/archive/poke",
}
# Fallback: if governing_body doesn't match, use the kevin_crye archive
KCNR_DEFAULT_PATH = "/Show/archive/kevin_crye"

SECURENET_PLAYER_URL = "https://radio.securenetsystems.net/v5/KQMS"

PODBEAN_FEED_URL = "https://feed.podbean.com/admind4/feed.xml"


def backfill_kcnr(conn: sqlite3.Connection) -> int:
    """Backfill KCNR episodes with archive page URLs."""
    cur = conn.execute(
        "SELECT meeting_id, governing_body, source_url FROM meetings "
        "WHERE source_url LIKE '%kcnr1460%' AND (page_url IS NULL OR page_url = '')"
    )
    rows = cur.fetchall()
    if not rows:
        print("KCNR: nothing to backfill")
        return 0

    updated = 0
    for meeting_id, gov_body, source_url in rows:
        path = KCNR_SHOW_MAP.get(gov_body, KCNR_DEFAULT_PATH)
        page_url = f"{KCNR_BASE}{path}"
        conn.execute(
            "UPDATE meetings SET page_url = ? WHERE meeting_id = ?",
            (page_url, meeting_id),
        )
        updated += 1

    conn.commit()
    # Show a few examples
    print(f"KCNR: updated {updated} rows")
    for meeting_id, gov_body, source_url in rows[:3]:
        path = KCNR_SHOW_MAP.get(gov_body, KCNR_DEFAULT_PATH)
        print(f"  {gov_body} ->{KCNR_BASE}{path}")
    return updated


def backfill_securenet(conn: sqlite3.Connection) -> int:
    """Backfill SecureNet episodes with station player URL."""
    cur = conn.execute(
        "SELECT meeting_id FROM meetings "
        "WHERE source_url LIKE '%securenetsystems%' AND (page_url IS NULL OR page_url = '')"
    )
    rows = cur.fetchall()
    if not rows:
        print("SecureNet: nothing to backfill")
        return 0

    ids = [r[0] for r in rows]
    conn.executemany(
        "UPDATE meetings SET page_url = ? WHERE meeting_id = ?",
        [(SECURENET_PLAYER_URL, mid) for mid in ids],
    )
    conn.commit()
    print(f"SecureNet: updated {len(ids)} rows ->{SECURENET_PLAYER_URL}")
    return len(ids)


def backfill_podbean(conn: sqlite3.Connection) -> int:
    """Backfill Podbean episodes by re-fetching RSS and matching audio URLs."""
    cur = conn.execute(
        "SELECT meeting_id, source_url FROM meetings "
        "WHERE source_url LIKE '%podbean%' AND (page_url IS NULL OR page_url = '')"
    )
    rows = cur.fetchall()
    if not rows:
        print("Podbean: nothing to backfill")
        return 0

    # Build lookup from RSS: audio_url ->episode page link
    print(f"Podbean: fetching RSS feed {PODBEAN_FEED_URL}...")
    try:
        resp = requests.get(PODBEAN_FEED_URL, timeout=(15, 60))
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"Podbean: failed to fetch/parse RSS — {e}")
        return 0

    channel = root.find("channel")
    if channel is None:
        print("Podbean: no <channel> in feed")
        return 0

    audio_to_link: dict[str, str] = {}
    for item in channel.findall("item"):
        link = (item.findtext("link") or "").strip()
        enclosure = item.find("enclosure")
        if enclosure is not None and link:
            audio_url = enclosure.get("url", "")
            if audio_url:
                audio_to_link[audio_url] = link

    print(f"Podbean: {len(audio_to_link)} episodes in RSS feed")

    updated = 0
    for meeting_id, source_url in rows:
        page_url = audio_to_link.get(source_url)
        if page_url:
            conn.execute(
                "UPDATE meetings SET page_url = ? WHERE meeting_id = ?",
                (page_url, meeting_id),
            )
            updated += 1

    conn.commit()
    unmatched = len(rows) - updated
    print(f"Podbean: updated {updated} rows, {unmatched} unmatched")
    if updated:
        # Show a few examples
        for meeting_id, source_url in rows[:3]:
            link = audio_to_link.get(source_url, "(no match)")
            print(f"  {source_url[:60]}... ->{link}")
    return updated


def main():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))

    # Check if page_url column exists
    cols = [r[1] for r in conn.execute("PRAGMA table_info(meetings)").fetchall()]
    if "page_url" not in cols:
        print("Adding page_url column to meetings table...")
        conn.execute("ALTER TABLE meetings ADD COLUMN page_url TEXT")
        conn.commit()

    total = 0
    total += backfill_kcnr(conn)
    total += backfill_securenet(conn)
    total += backfill_podbean(conn)

    # Summary
    count = conn.execute("SELECT COUNT(*) FROM meetings WHERE page_url IS NOT NULL").fetchone()[0]
    print(f"\nDone. Total updated this run: {total}")
    print(f"Meetings with page_url: {count}")

    conn.close()


if __name__ == "__main__":
    main()

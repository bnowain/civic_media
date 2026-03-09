"""
Granicus Backfill Script — Redding City Council meetings (Jan 1, 2022 → present)

For each discovered meeting:
  1. Creates a Meeting record (idempotent — skips if granicus_id already exists)
  2. Downloads agenda PDF  →  Document(type="agenda")
  3. Downloads minutes PDF →  Document(type="minutes")   [if available]
  4. Downloads video (direct MP4 from archive-video.granicus.com)
  5. Transcodes to 540p inline (produces {name}_540p.mp4, deletes original)

After this script completes, meetings are ready for the normal processing
pipeline (process_video_task) via the backfill queue or review UI.

Usage (run from Windows, not WSL):
    powershell.exe -Command "cd 'E:\\0-Automated-Apps\\civic_media'; .\\venv\\Scripts\\python.exe scripts\\granicus_backfill.py"

Flags:
    --discover-only     Register meetings in DB without downloading any files
    --no-video          Skip video download/transcode
    --no-docs           Skip PDF downloads
    --min-date YYYY-MM-DD   Override start date (default: 2022-01-01)
    --dry-run           Print what would be done without touching the DB
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path (works when run from project root)
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import DATABASE_PATH, MEDIA_DIR
from app.database import SessionLocal
from app.services.granicus.scraper import GranicusScraper
from app.services.granicus.discovery import clip_to_meeting
from app.services.granicus.downloader import (
    download_video,
    transcode_video,
    download_document,
)
from app import models

# ── Colours for terminal output ────────────────────────────────────────────────

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"


def header(text: str) -> None:
    print(f"\n{CYAN}{'─' * 70}{RESET}")
    print(f"{CYAN}{text}{RESET}")
    print(f"{CYAN}{'─' * 70}{RESET}")


def ok(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def skip(text: str) -> None:
    print(f"  {YELLOW}↷{RESET} {text}")


def err(text: str) -> None:
    print(f"  {RED}✗{RESET} {text}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Redding City Council meetings from Granicus")
    parser.add_argument("--discover-only", action="store_true",
                        help="Register meetings in DB only — no file downloads")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip video download and transcode")
    parser.add_argument("--no-docs", action="store_true",
                        help="Skip agenda and minutes PDF downloads")
    parser.add_argument("--min-date", default="2022-01-01",
                        help="Earliest meeting date to process (YYYY-MM-DD, default: 2022-01-01)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print discovery results without writing to DB")
    args = parser.parse_args()

    print(f"\n{CYAN}Redding City Council Granicus Backfill{RESET}")
    print(f"Database:   {DATABASE_PATH}")
    print(f"Media root: {MEDIA_DIR}")
    print(f"Min date:   {args.min_date}")
    if args.dry_run:
        print(f"{YELLOW}DRY RUN — no DB writes or downloads{RESET}")

    # ── Step 1: Discover meetings from Granicus ────────────────────────────────
    header("Phase 1: Discovering meetings from Granicus…")
    scraper = GranicusScraper()

    try:
        clips = scraper.discover(min_date=args.min_date)
    except Exception as exc:
        err(f"Discovery failed: {exc}")
        traceback.print_exc()
        sys.exit(1)

    print(f"\nFound {len(clips)} City Council meetings since {args.min_date}")

    if args.dry_run:
        for clip in clips:
            print(
                f"  {clip.meeting_date}  {clip.meeting_type:<25}  "
                f"clip={clip.clip_id}  "
                f"agenda={'✓' if clip.agenda_url else '✗'}  "
                f"minutes={'✓' if clip.minutes_url else '✗'}  "
                f"mp4={'✓' if clip.mp4_url else '✗'}"
            )
        print(f"\n{YELLOW}Dry run complete — nothing written.{RESET}")
        return

    # ── Step 2: Register meetings in DB ───────────────────────────────────────
    header("Phase 2: Registering meetings in database…")

    db = SessionLocal()
    try:
        meeting_ids: list[str] = []
        for clip in clips:
            meeting, created = clip_to_meeting(db, clip)
            meeting_ids.append(meeting.meeting_id)
            status = "created" if created else "exists"
            print(f"  {clip.meeting_date}  {clip.meeting_type:<25}  [{status}]  id={meeting.meeting_id[:8]}…")
        db.commit()
        ok(f"Registered {len(clips)} meetings ({sum(1 for _ in meeting_ids)} total)")
    except Exception as exc:
        db.rollback()
        err(f"DB registration failed: {exc}")
        traceback.print_exc()
        db.close()
        sys.exit(1)

    if args.discover_only:
        db.close()
        print(f"\n{GREEN}Discover-only complete.{RESET}")
        return

    # ── Step 3: Download and transcode each meeting ────────────────────────────
    header("Phase 3: Downloading files…")

    stats = {"video_ok": 0, "video_skip": 0, "video_err": 0,
             "agenda_ok": 0, "minutes_ok": 0, "doc_err": 0}

    for idx, clip in enumerate(clips, 1):
        db.expire_all()
        meeting = db.query(models.Meeting).filter(
            models.Meeting.granicus_id == clip.clip_id
        ).first()
        if not meeting:
            err(f"clip {clip.clip_id}: meeting not found in DB after registration?")
            continue

        print(f"\n[{idx}/{len(clips)}] {clip.meeting_date}  {clip.meeting_type}  (clip_id={clip.clip_id})")

        # Documents
        if not args.no_docs:
            for doc_type in ("agenda", "minutes"):
                try:
                    r = download_document(db, meeting, doc_type, print_progress=True)
                    if r["status"] == "complete":
                        stats[f"{doc_type}_ok"] += 1
                    elif r["status"] == "skipped":
                        skip(f"{doc_type}: {r.get('detail', 'skipped')}")
                    else:
                        err(f"{doc_type}: {r.get('error', 'unknown error')}")
                        stats["doc_err"] += 1
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    err(f"{doc_type}: exception — {exc}")
                    stats["doc_err"] += 1

        # Video
        if not args.no_video:
            if not clip.mp4_url:
                skip("video: no MP4 URL for this clip (video may not be posted)")
                stats["video_skip"] += 1
                continue

            try:
                output_dir = MEDIA_DIR / meeting.meeting_id
                r = download_video(db, meeting, output_dir, print_progress=True)
                db.commit()

                if r["status"] == "complete":
                    # Transcode to 540p
                    db.expire_all()
                    media = db.query(models.MediaFile).filter_by(
                        meeting_id=meeting.meeting_id, file_type="video"
                    ).first()
                    if media:
                        tr = transcode_video(db, media, output_dir, print_progress=True)
                        db.commit()
                        if tr["status"] == "complete":
                            ok(f"video: transcoded to 540p")
                            stats["video_ok"] += 1
                        elif tr["status"] == "already_transcoded":
                            skip("video: already 540p")
                            stats["video_skip"] += 1
                        else:
                            err(f"video: transcode failed — {tr.get('error')}")
                            stats["video_err"] += 1
                    else:
                        err("video: MediaFile record missing after download")
                        stats["video_err"] += 1

                elif r["status"] == "skipped":
                    skip(f"video: {r.get('detail')}")
                    stats["video_skip"] += 1
                else:
                    err(f"video: {r.get('error')}")
                    stats["video_err"] += 1

            except Exception as exc:
                db.rollback()
                err(f"video: exception — {exc}")
                traceback.print_exc()
                stats["video_err"] += 1

    db.close()

    # ── Summary ────────────────────────────────────────────────────────────────
    header("Summary")
    print(f"  Meetings processed:  {len(clips)}")
    print(f"  Videos downloaded:   {stats['video_ok']}  "
          f"(skipped: {stats['video_skip']}, errors: {stats['video_err']})")
    print(f"  Agendas downloaded:  {stats['agenda_ok']}  "
          f"(errors: {stats['doc_err']})")
    print(f"  Minutes downloaded:  {stats['minutes_ok']}")
    print()
    print("Next step: process meetings through the normal pipeline via:")
    print("  → Review UI → Process button")
    print("  → OR: POST /api/backfill/next-process (auto-advance will handle it)")


if __name__ == "__main__":
    main()

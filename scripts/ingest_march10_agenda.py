"""
One-off script: find the March 10, 2026 Redding City Council meeting in Granicus,
register it in the DB, download its agenda PDF(s), and queue OCR.

Run via Windows Python (not WSL — needs WAL write access):
  powershell.exe -Command "cd 'E:/0-Automated-Apps/civic_media'; .\\venv\\Scripts\\python.exe scripts/ingest_march10_agenda.py"

Or to target a specific clip_id (skip discovery):
  .\\venv\\Scripts\\python.exe scripts/ingest_march10_agenda.py --clip-id 1234
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to sys.path so app.* imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ingest_march10")

TARGET_DATE = "2026-03-10"
MIN_DISCOVERY_DATE = "2026-03-01"


def main(clip_id: int | None = None) -> None:
    from app.database import SessionLocal, engine
    from app import models
    from app.services.granicus.scraper import GranicusScraper, GranicusClip
    from app.services.granicus.discovery import clip_to_meeting
    from app.services.granicus.downloader import download_document
    from app.config import DOCUMENTS_DIR

    # Create tables if they don't exist
    models.Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        scraper = GranicusScraper()

        if clip_id is not None:
            # Fast path: user provided the clip_id directly
            logger.info("Using provided clip_id=%d", clip_id)
            clip_ids_map = scraper.list_clip_ids()
            clip = scraper.get_clip_details(clip_id)
            if clip is None:
                logger.error("Could not retrieve clip %d from Granicus", clip_id)
                sys.exit(1)
            urls = clip_ids_map.get(clip_id, {})
            if urls.get("agenda_viewer_url"):
                clip.agenda_url = scraper.resolve_document_url(urls["agenda_viewer_url"])
            if urls.get("minutes_viewer_url"):
                clip.minutes_url = scraper.resolve_document_url(urls["minutes_viewer_url"])
            clips = [clip]
        else:
            # Discover all meetings since March 1st and find March 10th
            logger.info("Discovering Granicus meetings from %s…", MIN_DISCOVERY_DATE)
            clips = scraper.discover(min_date=MIN_DISCOVERY_DATE)
            clips = [c for c in clips if c.meeting_date == TARGET_DATE]
            if not clips:
                logger.warning(
                    "No meeting found for %s in Granicus. "
                    "The meeting may not yet be posted, or the date may differ. "
                    "Run with --clip-id <id> to target a specific clip directly.",
                    TARGET_DATE,
                )
                # Show what we did find so user can pick the right clip
                all_clips = scraper.discover(min_date=MIN_DISCOVERY_DATE)
                if all_clips:
                    logger.info("Meetings found in range %s–present:", MIN_DISCOVERY_DATE)
                    for c in all_clips:
                        logger.info("  clip_id=%-6d  date=%s  type=%s",
                                    c.clip_id, c.meeting_date, c.meeting_type)
                sys.exit(0)

        for clip in clips:
            logger.info("Processing clip %d: %s %s", clip.clip_id, clip.meeting_date, clip.meeting_type)

            # Register (or update) the meeting record
            meeting, created = clip_to_meeting(db, clip)
            db.commit()
            action = "Created" if created else "Found existing"
            logger.info("%s meeting_id=%s", action, meeting.meeting_id)

            if not clip.agenda_url:
                logger.warning("No agenda URL resolved — cannot download agenda PDF")
            else:
                logger.info("Downloading agenda PDF…")
                result = download_document(db, meeting, doc_type="agenda", print_progress=True)
                db.commit()
                logger.info("Agenda download: %s", result)

            if clip.minutes_url:
                logger.info("Downloading minutes PDF…")
                result = download_document(db, meeting, doc_type="minutes", print_progress=True)
                db.commit()
                logger.info("Minutes download: %s", result)
            else:
                logger.info("No minutes URL (expected — meeting is upcoming or recent)")

            logger.info(
                "Done. meeting_id=%s  agenda_url=%s",
                meeting.meeting_id,
                meeting.agenda_url or "(none)",
            )
            logger.info(
                "OCR task queued (if workers are running). "
                "Check logs/huey.log or http://localhost:8000/api/meetings/%s",
                meeting.meeting_id,
            )

    except Exception:
        db.rollback()
        logger.exception("Script failed")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest March 10 Redding CC meeting agenda")
    parser.add_argument(
        "--clip-id", type=int, default=None,
        help="Granicus clip_id to use directly (skips full discovery scan)",
    )
    args = parser.parse_args()
    main(clip_id=args.clip_id)

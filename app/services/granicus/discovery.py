"""
Granicus discovery orchestrator — registers Redding City Council meetings into the DB.

Idempotent: deduplicates by granicus_id.  Updates agenda_url, minutes_url, and
video_url on existing records when they change (e.g., minutes become available).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Meeting
from app.services.group_helper import ensure_group
from app.services.granicus.scraper import GranicusScraper, GranicusClip, CITY_COUNCIL_NAME

logger = logging.getLogger(__name__)

# Canonical group name used for all Granicus-sourced Redding City Council meetings
GRANICUS_GROUP_NAME = CITY_COUNCIL_NAME
GRANICUS_MIN_DATE = "2022-01-01"


def clip_to_meeting(db: Session, clip: GranicusClip) -> tuple[Meeting, bool]:
    """
    Create or update a Meeting record from a GranicusClip.

    Returns (meeting, created).  If the meeting already exists, only updates
    fields that were previously None (never overwrites existing data).
    """
    existing = db.query(Meeting).filter(Meeting.granicus_id == clip.clip_id).first()
    group_id = ensure_group(db, GRANICUS_GROUP_NAME, group_type="government",
                             display_name="Redding City Council")

    if existing:
        changed = False
        if clip.agenda_url and not existing.agenda_url:
            existing.agenda_url = clip.agenda_url
            changed = True
        if clip.minutes_url and not existing.minutes_url:
            existing.minutes_url = clip.minutes_url
            changed = True
        if clip.mp4_url and not existing.video_url:
            existing.video_url = clip.mp4_url
            changed = True
        if changed:
            db.flush()
            logger.info(
                "Updated meeting granicus_id=%d (%s)", clip.clip_id, clip.meeting_date
            )
        return existing, False

    meeting = Meeting(
        group_name=GRANICUS_GROUP_NAME,
        group_id=group_id,
        meeting_type=clip.meeting_type,
        meeting_date=clip.meeting_date,
        title=GRANICUS_GROUP_NAME,  # "Redding City Council" — mirrors PrimeGov BOS pattern
        category="meeting",
        program_type="governing_meeting",
        granicus_id=clip.clip_id,
        video_url=clip.mp4_url,
        agenda_url=clip.agenda_url,
        minutes_url=clip.minutes_url,
        page_url=clip.page_url,
    )
    db.add(meeting)
    db.flush()
    logger.info(
        "Created meeting: %s %s (granicus_id=%d)", clip.meeting_date, clip.meeting_type, clip.clip_id
    )
    return meeting, True


def run_discovery(
    db: Session,
    min_date: str = GRANICUS_MIN_DATE,
) -> dict:
    """
    Discover and register all Redding City Council meetings from Granicus.

    Makes one HTTP request to ViewPublisher + two requests per in-range clip
    (MediaPlayer + AgendaViewer redirect).  Typically ~200 requests for 2022-present.
    Idempotent — safe to re-run.

    Returns summary dict.
    """
    scraper = GranicusScraper()
    clips = scraper.discover(min_date=min_date)

    created = updated = 0
    for clip in clips:
        _, is_new = clip_to_meeting(db, clip)
        if is_new:
            created += 1
        else:
            updated += 1

    db.commit()
    logger.info("Granicus discovery: %d created, %d updated", created, updated)
    return {
        "source": "granicus",
        "total": len(clips),
        "created": created,
        "updated": updated,
        "min_date": min_date,
    }

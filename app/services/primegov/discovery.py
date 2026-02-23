"""
PrimeGov discovery orchestrator.

Fetches meetings from PrimeGov API, deduplicates against existing Meeting
records by primegov_id, and creates/updates records as needed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import MEDIA_DIR
from app.models import Meeting, GoverningBody
from app.services.primegov.scraper import PrimeGovScraper, PrimeGovMeeting, COMMITTEES

logger = logging.getLogger(__name__)

PROGRESS_FILE = MEDIA_DIR / "primegov_progress.json"


def _write_progress(
    stage: str,
    pct: int = 0,
    detail: str = "",
    error: bool = False,
) -> None:
    """Write discovery progress for UI polling."""
    try:
        PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS_FILE.write_text(json.dumps({
            "stage": stage,
            "pct": pct,
            "detail": detail,
            "error": error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as exc:
        logger.warning("Could not write primegov progress: %s", exc)


def _ensure_governing_body(db: Session, committee_name: str) -> Optional[str]:
    """Get or create a GoverningBody for the committee. Returns its ID."""
    existing = db.query(GoverningBody).filter_by(name=committee_name).first()
    if existing:
        return existing.governing_body_id

    gb = GoverningBody(name=committee_name, display_name=committee_name)
    db.add(gb)
    db.flush()
    logger.info("Created governing body: %s", committee_name)
    return gb.governing_body_id


def run_discovery(
    db: Session,
    committee_ids: list[int] | None = None,
    years: list[int] | None = None,
) -> dict:
    """
    Run PrimeGov discovery: scrape → deduplicate → create/update Meeting records.

    Returns summary dict with counts.
    """
    _write_progress("Fetching meetings from PrimeGov...", 10)

    scraper = PrimeGovScraper()
    meetings = scraper.discover(
        committee_ids=committee_ids,
        years=years,
        include_upcoming=True,
    )

    _write_progress("Processing meetings...", 30, f"{len(meetings)} found from PrimeGov")

    # Load existing primegov_id set for dedup
    existing_ids: dict[int, Meeting] = {}
    existing_records = (
        db.query(Meeting)
        .filter(Meeting.primegov_id.isnot(None))
        .all()
    )
    for m in existing_records:
        existing_ids[m.primegov_id] = m

    # Cache governing body lookups
    gb_cache: dict[str, str] = {}  # committee_name -> governing_body_id

    created = 0
    updated = 0
    skipped = 0
    total = len(meetings)

    for i, pm in enumerate(meetings):
        if i % 20 == 0:
            pct = 30 + int(60 * i / max(total, 1))
            _write_progress(
                "Syncing meetings...", pct,
                f"{created} new, {updated} updated, {skipped} unchanged ({i}/{total})",
            )

        if pm.primegov_id in existing_ids:
            # Update existing record — only fill NULL fields
            existing = existing_ids[pm.primegov_id]
            changed = False

            if existing.video_url is None and pm.video_url:
                existing.video_url = pm.video_url
                changed = True
            if existing.agenda_url is None and pm.agenda_url:
                existing.agenda_url = pm.agenda_url
                changed = True
            if existing.minutes_url is None and pm.minutes_url:
                existing.minutes_url = pm.minutes_url
                changed = True

            if changed:
                updated += 1
            else:
                skipped += 1
        else:
            # Create new Meeting record
            if pm.committee_name not in gb_cache:
                gb_cache[pm.committee_name] = _ensure_governing_body(
                    db, pm.committee_name
                )

            meeting = Meeting(
                governing_body=pm.committee_name,
                meeting_type="Regular",
                meeting_date=pm.meeting_date_iso,
                title=pm.title,
                category="meeting",
                governing_body_id=gb_cache[pm.committee_name],
                primegov_id=pm.primegov_id,
                video_url=pm.video_url,
                agenda_url=pm.agenda_url,
                minutes_url=pm.minutes_url,
            )
            db.add(meeting)
            created += 1

    db.commit()

    summary = {
        "total_from_api": total,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "committees": [
            COMMITTEES.get(cid, f"Committee {cid}")
            for cid in (committee_ids or [3])
        ],
    }

    _write_progress(
        "Discovery complete", 100,
        f"{created} new, {updated} updated, {skipped} unchanged",
    )

    logger.info(
        "PrimeGov discovery: %d from API, %d created, %d updated, %d skipped",
        total, created, updated, skipped,
    )
    return summary

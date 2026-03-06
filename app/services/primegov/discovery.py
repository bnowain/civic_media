"""
PrimeGov discovery orchestrator.

Fetches meetings from PrimeGov API, deduplicates against existing Meeting
records by primegov_id, and creates/updates records as needed.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import MEDIA_DIR
from app.models import Meeting, Group
from app.services.group_helper import ensure_group
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


def _ensure_group(db: Session, committee_name: str) -> Optional[str]:
    """Get or create a Group for the committee. Returns its group_id."""
    return ensure_group(db, committee_name, group_type="government")


def run_discovery(
    db: Session,
    committee_ids: list[int] | None = None,
    years: list[int] | None = None,
    mode: str = "update",
) -> dict:
    """
    Run PrimeGov discovery: scrape → deduplicate → create/update Meeting records.

    mode="update"  (default) — only fetch meetings from the last 90 days before
        the newest meeting in the DB.  Fast for regular sync runs.
    mode="full" — fetch all history (original behavior).

    Returns summary dict with counts.
    """
    # Compute min_date for update mode
    min_date: str | None = None
    if mode == "update" and years is None:
        newest = (
            db.query(func.max(Meeting.meeting_date))
            .filter(Meeting.program_type == "governing_meeting")
            .scalar()
        )
        if newest:
            newest_dt = date.fromisoformat(newest)
            min_date = (newest_dt - timedelta(days=90)).isoformat()
            logger.info(
                "Update mode: fetching meetings from %s onward (newest in DB: %s)",
                min_date, newest,
            )
        else:
            logger.info("Update mode: no meetings in DB yet — doing full discovery")

    _write_progress(
        "Fetching meetings from PrimeGov...", 10,
        f"Mode: {'update (last 90 days)' if min_date else 'full history'}",
    )

    scraper = PrimeGovScraper()
    meetings = scraper.discover(
        committee_ids=committee_ids,
        years=years,
        include_upcoming=True,
        min_date=min_date,
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
    group_cache: dict[str, str] = {}  # committee_name -> group_id

    created = 0
    updated = 0
    skipped = 0
    total = len(meetings)
    # Track meetings that gained new document URLs so we can auto-queue downloads
    newly_doc_updated: list[Meeting] = []

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
            doc_changed = False

            if existing.video_url is None and pm.video_url:
                existing.video_url = pm.video_url
                changed = True
            if existing.agenda_url is None and pm.agenda_url:
                existing.agenda_url = pm.agenda_url
                changed = True
                doc_changed = True
            if existing.minutes_url is None and pm.minutes_url:
                existing.minutes_url = pm.minutes_url
                changed = True
                doc_changed = True
            if existing.packet_url is None and pm.packet_url:
                existing.packet_url = pm.packet_url
                changed = True
                doc_changed = True

            if changed:
                updated += 1
                if doc_changed:
                    newly_doc_updated.append(existing)
            else:
                skipped += 1
        else:
            # Create new Meeting record
            if pm.committee_name not in group_cache:
                group_cache[pm.committee_name] = _ensure_group(
                    db, pm.committee_name
                )

            meeting = Meeting(
                group_name=pm.committee_name,
                meeting_type="Regular",
                meeting_date=pm.meeting_date_iso,
                title=pm.title,
                category="meeting",
                program_type="governing_meeting",
                group_id=group_cache[pm.committee_name],
                primegov_id=pm.primegov_id,
                video_url=pm.video_url,
                agenda_url=pm.agenda_url,
                minutes_url=pm.minutes_url,
                packet_url=pm.packet_url,
            )
            db.add(meeting)
            created += 1

    db.commit()

    # Auto-queue document downloads for meetings that just gained new doc URLs.
    # download_document() is idempotent — it skips files already on disk.
    docs_queued = 0
    if newly_doc_updated:
        try:
            from app.services.task_dispatch import send_task
            for meeting in newly_doc_updated:
                send_task("tasks.primegov_download", kwargs={
                    "meeting_id": meeting.meeting_id,
                    "download_video": False,
                    "download_agenda": meeting.agenda_url is not None,
                    "download_minutes": meeting.minutes_url is not None,
                    "download_packet": meeting.packet_url is not None,
                    "auto_process": False,
                })
                docs_queued += 1
            logger.info(
                "Auto-queued document downloads for %d meetings that gained new doc URLs",
                docs_queued,
            )
        except Exception:
            logger.warning("Could not queue doc downloads (worker may not be running)", exc_info=True)

    summary = {
        "total_from_api": total,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "docs_queued": docs_queued,
        "mode": mode,
        "committees": [
            COMMITTEES.get(cid, f"Committee {cid}")
            for cid in (committee_ids or [3])
        ],
    }

    _write_progress(
        "Discovery complete", 100,
        f"{created} new, {updated} updated, {skipped} unchanged"
        + (f", {docs_queued} doc downloads queued" if docs_queued else ""),
    )

    logger.info(
        "PrimeGov discovery (%s): %d from API, %d created, %d updated, %d skipped, %d docs queued",
        mode, total, created, updated, skipped, docs_queued,
    )
    return summary

"""
PrimeGov integration endpoints — meeting discovery and asset download.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import MEDIA_DIR
from app.models import Meeting, MediaFile, Document
from app.services.primegov.scraper import COMMITTEES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/primegov", tags=["primegov"])

PROGRESS_FILE = MEDIA_DIR / "primegov_progress.json"


# ── Discovery ────────────────────────────────────────────────────────────────


@router.post("/discover")
def discover_meetings(
    committee_ids: Optional[list[int]] = Query(default=None),
    years: Optional[list[int]] = Query(default=None),
    background: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """
    Run PrimeGov meeting discovery.

    - Fetches all meetings from the PrimeGov API
    - Creates new Meeting records for undiscovered meetings
    - Updates existing records with newly available URLs (agenda/minutes/video)
    - Idempotent: safe to re-run

    Set `background=false` to run synchronously (useful for testing).
    """
    if background:
        from app.tasks import primegov_discover_task

        task = primegov_discover_task.delay(
            committee_ids=committee_ids or [3],
            years=years,
        )
        return {
            "task_id": task.id,
            "status": "queued",
            "message": "Discovery started in background",
        }
    else:
        from app.services.primegov.discovery import run_discovery

        result = run_discovery(db, committee_ids or [3], years)
        return {"status": "complete", **result}


@router.get("/discover/status")
def discover_status():
    """Poll discovery progress."""
    if not PROGRESS_FILE.exists():
        return {"stage": "idle", "pct": 0, "detail": "", "error": False}

    try:
        data = json.loads(PROGRESS_FILE.read_text())
        return data
    except Exception:
        return {"stage": "idle", "pct": 0, "detail": "", "error": False}


# ── Download ─────────────────────────────────────────────────────────────────


@router.post("/download/{meeting_id}")
def download_meeting_assets(
    meeting_id: str,
    download_video: bool = Query(default=True),
    download_agenda: bool = Query(default=True),
    download_minutes: bool = Query(default=True),
    download_packet: bool = Query(default=True),
    auto_process: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """
    Download assets (video, agenda, minutes) for a single meeting.

    Runs as a background Celery task. Returns task_id for polling.
    """
    meeting = db.query(Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    from app.tasks import primegov_download_task

    task = primegov_download_task.delay(
        meeting_id=meeting_id,
        download_video=download_video,
        download_agenda=download_agenda,
        download_minutes=download_minutes,
        download_packet=download_packet,
        auto_process=auto_process,
    )
    return {
        "task_id": task.id,
        "meeting_id": meeting_id,
        "status": "queued",
    }


@router.post("/download-all")
def download_all_undownloaded(
    auto_process: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """
    Queue downloads for all meetings that have a video_url but no MediaFile.
    """
    # Find meetings with video_url but no downloaded media
    downloadable = (
        db.query(Meeting)
        .filter(
            Meeting.video_url.isnot(None),
            Meeting.primegov_id.isnot(None),
        )
        .all()
    )

    # Filter to those without existing media files
    queued = 0
    from app.tasks import primegov_download_task

    for meeting in downloadable:
        has_media = (
            db.query(MediaFile)
            .filter_by(meeting_id=meeting.meeting_id)
            .filter(MediaFile.file_type.in_(["video", "audio"]))
            .first()
        )
        if not has_media:
            primegov_download_task.delay(
                meeting_id=meeting.meeting_id,
                download_video=True,
                download_agenda=True,
                download_minutes=True,
                download_packet=True,
                auto_process=auto_process,
            )
            queued += 1

    return {
        "status": "queued",
        "meetings_queued": queued,
    }


# ── Committees ───────────────────────────────────────────────────────────────


@router.get("/committees")
def list_committees():
    """List available PrimeGov committees."""
    return [
        {"id": cid, "name": name}
        for cid, name in sorted(COMMITTEES.items())
    ]


# ── Meeting Assets Status ────────────────────────────────────────────────────


@router.get("/assets/{meeting_id}")
def meeting_assets_status(meeting_id: str, db: Session = Depends(get_db)):
    """
    Get download status for a meeting's PrimeGov assets.

    Returns which URLs are available and which have been downloaded.
    """
    meeting = db.query(Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    has_video = (
        db.query(MediaFile)
        .filter_by(meeting_id=meeting_id)
        .filter(MediaFile.file_type.in_(["video", "audio"]))
        .first()
    ) is not None

    has_agenda = (
        db.query(Document)
        .filter_by(meeting_id=meeting_id, document_type="agenda")
        .first()
    ) is not None

    has_minutes = (
        db.query(Document)
        .filter_by(meeting_id=meeting_id, document_type="minutes")
        .first()
    ) is not None

    has_packet = (
        db.query(Document)
        .filter_by(meeting_id=meeting_id, document_type="packet")
        .first()
    ) is not None

    return {
        "meeting_id": meeting_id,
        "primegov_id": meeting.primegov_id,
        "video_url_available": meeting.video_url is not None,
        "video_downloaded": has_video,
        "agenda_url_available": meeting.agenda_url is not None,
        "agenda_downloaded": has_agenda,
        "minutes_url_available": meeting.minutes_url is not None,
        "minutes_downloaded": has_minutes,
        "packet_url_available": meeting.packet_url is not None,
        "packet_downloaded": has_packet,
    }

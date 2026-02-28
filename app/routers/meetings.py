"""Meeting CRUD endpoints."""

import shutil
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.config import MEDIA_DIR, DOCUMENTS_DIR, OCR_TEXT_DIR

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


@router.get("/", response_model=list[schemas.MeetingOut])
def list_meetings(
    category: Optional[str] = Query(None),
    group_id: Optional[str] = Query(None),
    meeting_type: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),   # YYYY-MM-DD inclusive
    date_to: Optional[str] = Query(None),     # YYYY-MM-DD inclusive
    limit: Optional[int] = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(models.Meeting)

    if category:
        q = q.filter(models.Meeting.category == category)
    if group_id:
        q = q.filter(models.Meeting.group_id == group_id)
    if meeting_type:
        q = q.filter(models.Meeting.meeting_type == meeting_type)
    if date_from:
        q = q.filter(models.Meeting.meeting_date >= date_from)
    if date_to:
        q = q.filter(models.Meeting.meeting_date <= date_to)

    q = q.order_by(models.Meeting.meeting_date.desc(), models.Meeting.created_at.desc())
    q = q.offset(offset)

    if limit:
        q = q.limit(limit)

    return q.all()


@router.post("/", response_model=schemas.MeetingOut, status_code=201)
def create_meeting(payload: schemas.MeetingCreate, db: Session = Depends(get_db)):
    import uuid
    from app.services.group_helper import ensure_group

    meeting_id = str(uuid.uuid4())
    data = payload.model_dump()

    # Auto-create a Group for audio/web_series when group_name text is
    # provided but no group_id was supplied.
    if data.get("category") in ("audio", "web_series") and data.get("group_name") and not data.get("group_id"):
        data["group_id"] = ensure_group(db, data["group_name"], group_type="show")

    meeting = models.Meeting(meeting_id=meeting_id, **data)
    meeting.media_directory = str(MEDIA_DIR / meeting_id)
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting


@router.get("/{meeting_id}", response_model=schemas.MeetingOut)
def get_meeting(meeting_id: str, db: Session = Depends(get_db)):
    m = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not m:
        raise HTTPException(404, "Meeting not found")
    return m


@router.patch("/{meeting_id}", response_model=schemas.MeetingOut)
def update_meeting(meeting_id: str, payload: schemas.MeetingUpdate, db: Session = Depends(get_db)):
    m = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not m:
        raise HTTPException(404, "Meeting not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(m, field, value)
    db.commit()
    db.refresh(m)
    return m


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: str, db: Session = Depends(get_db)):
    """
    Delete a meeting, all associated database records, and all files on disk.
    Cascades to: media_files, documents, transcript_segments, segment_assignments, voiceprints.
    """
    m = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not m:
        raise HTTPException(404, "Meeting not found")

    # Delete database records — cascade handles child tables
    db.delete(m)
    db.commit()

    # Delete all files on disk for this meeting
    for directory in [MEDIA_DIR, DOCUMENTS_DIR, OCR_TEXT_DIR]:
        meeting_dir = directory / meeting_id
        if meeting_dir.exists():
            shutil.rmtree(meeting_dir)


@router.patch("/{meeting_id}/summary")
def update_meeting_summary(meeting_id: str, body: schemas.SummaryUpload, db: Session = Depends(get_db)):
    """Upload short and/or long summary for a meeting."""
    m = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not m:
        raise HTTPException(404, "Meeting not found")

    if body.summary_short is not None:
        m.summary_short = body.summary_short
    if body.summary_long is not None:
        m.summary_long = body.summary_long
    m.summary_updated_at = datetime.utcnow()

    db.commit()
    db.refresh(m)
    return {"meeting_id": meeting_id, "summary_short": m.summary_short, "summary_long": m.summary_long}


@router.post("/{meeting_id}/summary/upload")
async def upload_summary_markdown(
    meeting_id: str,
    summary_type: str = Query(..., regex="^(short|long)$"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a markdown file as summary. summary_type = 'short' | 'long'"""
    m = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not m:
        raise HTTPException(404, "Meeting not found")

    content = (await file.read()).decode("utf-8", errors="replace")

    if summary_type == "short":
        m.summary_short = content
    else:
        m.summary_long = content
    m.summary_updated_at = datetime.utcnow()

    db.commit()
    return {"meeting_id": meeting_id, "summary_type": summary_type, "length": len(content)}

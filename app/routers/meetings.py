"""Meeting CRUD endpoints."""

import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.config import MEDIA_DIR, DOCUMENTS_DIR, OCR_TEXT_DIR

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


@router.get("/", response_model=list[schemas.MeetingOut])
def list_meetings(
    category: Optional[str] = Query(None),
    governing_body_id: Optional[str] = Query(None),
    meeting_type: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(models.Meeting)

    if category:
        q = q.filter(models.Meeting.category == category)
    if governing_body_id:
        q = q.filter(models.Meeting.governing_body_id == governing_body_id)
    if meeting_type:
        q = q.filter(models.Meeting.meeting_type == meeting_type)

    q = q.order_by(models.Meeting.meeting_date.desc(), models.Meeting.created_at.desc())
    q = q.offset(offset)

    if limit:
        q = q.limit(limit)

    return q.all()


@router.post("/", response_model=schemas.MeetingOut, status_code=201)
def create_meeting(payload: schemas.MeetingCreate, db: Session = Depends(get_db)):
    import uuid
    meeting_id = str(uuid.uuid4())
    meeting = models.Meeting(meeting_id=meeting_id, **payload.model_dump())
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
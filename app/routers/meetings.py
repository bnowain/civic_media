"""Meeting CRUD endpoints."""

import shutil

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.config import MEDIA_DIR, DOCUMENTS_DIR, OCR_TEXT_DIR

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


@router.get("/", response_model=list[schemas.MeetingOut])
def list_meetings(db: Session = Depends(get_db)):
    return (
        db.query(models.Meeting)
        .order_by(models.Meeting.meeting_date.desc(), models.Meeting.created_at.desc())
        .all()
    )


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
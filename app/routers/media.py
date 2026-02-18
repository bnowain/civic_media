"""
Media upload endpoints.

POST /api/media/{meeting_id}/upload  — accepts a video file, saves it,
                                       enqueues the processing pipeline.
GET  /api/media/{meeting_id}/status  — poll pipeline readiness.
GET  /api/media/{meeting_id}         — list media files for a meeting.
"""

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import MEDIA_DIR
from app.database import get_db
from app.tasks import process_video_task

router = APIRouter(prefix="/api/media", tags=["media"])

_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}


@router.post(
    "/{meeting_id}/upload",
    response_model=schemas.MediaFileOut,
    status_code=202,
)
async def upload_video(
    meeting_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload a video file for a meeting and trigger the ingestion pipeline.
    Returns immediately (202) — pipeline runs asynchronously.
    """
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in _ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(
            400,
            f"Unsupported video format '{suffix}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_VIDEO_SUFFIXES))}",
        )

    # Save to media/{meeting_id}/video{suffix}
    dest_dir = MEDIA_DIR / meeting_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"video{suffix}"

    with dest_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    media = models.MediaFile(
        meeting_id=meeting_id,
        file_type="video",
        file_path=str(dest_path),
        duration=None,
    )
    db.add(media)
    db.commit()
    db.refresh(media)

    # Enqueue pipeline
    process_video_task.delay(meeting_id, media.media_id)

    return media


@router.get("/{meeting_id}/status", response_model=schemas.PipelineStatus)
def pipeline_status(meeting_id: str, db: Session = Depends(get_db)):
    """Poll how many segments have been processed so far."""
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    segment_count = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .count()
    )

    has_video = (
        db.query(models.MediaFile)
        .filter_by(meeting_id=meeting_id, file_type="video")
        .first()
    )

    if not has_video:
        status = "pending"
    elif segment_count == 0:
        status = "processing"
    else:
        status = "complete"

    return schemas.PipelineStatus(
        meeting_id=meeting_id,
        segment_count=segment_count,
        task_id=None,
        status=status,
    )


@router.get("/{meeting_id}", response_model=list[schemas.MediaFileOut])
def list_media(meeting_id: str, db: Session = Depends(get_db)):
    return (
        db.query(models.MediaFile)
        .filter_by(meeting_id=meeting_id)
        .order_by(models.MediaFile.file_type)
        .all()
    )

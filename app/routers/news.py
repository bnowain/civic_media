"""TV News endpoints — CRUD, upload, status, segments, chunks."""

import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.config import TV_NEWS_DIR

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("/", response_model=list[schemas.TVNewscastOut])
def list_newscasts(
    station: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(models.TVNewscast)
    if station:
        q = q.filter(models.TVNewscast.station == station)
    q = q.order_by(models.TVNewscast.created_at.desc())
    q = q.offset(offset)
    if limit:
        q = q.limit(limit)
    return q.all()


@router.post("/", response_model=schemas.TVNewscastOut, status_code=201)
def create_newscast(payload: schemas.TVNewscastCreate, db: Session = Depends(get_db)):
    newscast_id = str(uuid.uuid4())
    newscast = models.TVNewscast(
        newscast_id=newscast_id,
        title=payload.title,
        station=payload.station,
        air_date=payload.air_date,
        air_time=payload.air_time,
    )
    db.add(newscast)
    db.commit()
    db.refresh(newscast)
    return newscast


@router.get("/{newscast_id}", response_model=schemas.TVNewscastOut)
def get_newscast(newscast_id: str, db: Session = Depends(get_db)):
    n = db.query(models.TVNewscast).filter_by(newscast_id=newscast_id).first()
    if not n:
        raise HTTPException(404, "Newscast not found")
    return n


@router.post("/{newscast_id}/upload")
async def upload_newscast_video(
    newscast_id: str,
    file: UploadFile = File(...),
    skip_commercial_strip: bool = Query(False),
    db: Session = Depends(get_db),
):
    newscast = db.query(models.TVNewscast).filter_by(newscast_id=newscast_id).first()
    if not newscast:
        raise HTTPException(404, "Newscast not found")

    # Save uploaded file
    newscast_dir = TV_NEWS_DIR / newscast_id
    newscast_dir.mkdir(parents=True, exist_ok=True)
    dest = newscast_dir / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    newscast.source_file = str(dest)
    newscast.status = "queued"
    db.commit()

    # Dispatch Celery task
    from app.tasks import process_newscast_task
    from app.services.worker_manager import ensure_worker
    ensure_worker()
    task = process_newscast_task.delay(newscast_id, skip_commercial_strip)

    return {"newscast_id": newscast_id, "task_id": task.id, "status": "queued"}


@router.get("/{newscast_id}/status")
def newscast_status(newscast_id: str, db: Session = Depends(get_db)):
    newscast = db.query(models.TVNewscast).filter_by(newscast_id=newscast_id).first()
    if not newscast:
        raise HTTPException(404, "Newscast not found")

    # Try to read progress.json
    import json
    progress_file = TV_NEWS_DIR / newscast_id / "progress.json"
    progress = {}
    if progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text())
        except Exception:
            pass

    segment_count = (
        db.query(models.TVNewsSegment)
        .filter_by(newscast_id=newscast_id)
        .count()
    )

    return {
        "newscast_id": newscast_id,
        "status": newscast.status,
        "stage": progress.get("stage"),
        "progress_pct": progress.get("pct"),
        "detail": progress.get("detail"),
        "error_detail": newscast.error_detail,
        "segment_count": segment_count,
    }


@router.get("/{newscast_id}/segments", response_model=list[schemas.TVNewsSegmentOut])
def list_segments(newscast_id: str, db: Session = Depends(get_db)):
    return (
        db.query(models.TVNewsSegment)
        .filter_by(newscast_id=newscast_id)
        .order_by(models.TVNewsSegment.start_time)
        .all()
    )


@router.get("/{newscast_id}/chunks", response_model=list[schemas.TVNewsChunkOut])
def list_chunks(newscast_id: str, db: Session = Depends(get_db)):
    return (
        db.query(models.TVNewsTranscriptionChunk)
        .filter_by(newscast_id=newscast_id)
        .order_by(models.TVNewsTranscriptionChunk.start_time)
        .all()
    )


@router.get("/{newscast_id}/transcript")
def get_transcript(newscast_id: str, db: Session = Depends(get_db)):
    """Return the full transcript text assembled from chunks."""
    chunks = (
        db.query(models.TVNewsTranscriptionChunk)
        .filter_by(newscast_id=newscast_id)
        .order_by(models.TVNewsTranscriptionChunk.start_time)
        .all()
    )
    text = " ".join(c.text for c in chunks if c.text)
    return {"newscast_id": newscast_id, "transcript": text, "chunk_count": len(chunks)}


@router.delete("/{newscast_id}", status_code=204)
def delete_newscast(newscast_id: str, db: Session = Depends(get_db)):
    n = db.query(models.TVNewscast).filter_by(newscast_id=newscast_id).first()
    if not n:
        raise HTTPException(404, "Newscast not found")

    db.delete(n)
    db.commit()

    # Delete files on disk
    newscast_dir = TV_NEWS_DIR / newscast_id
    if newscast_dir.exists():
        shutil.rmtree(newscast_dir)

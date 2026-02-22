"""
Clip endpoints.

POST   /api/clips/                    — Create clip + thumbnail + dispatch export.
GET    /api/clips/                    — List clips (optional source_type, source_id filters).
GET    /api/clips/{clip_id}           — Get single clip.
PATCH  /api/clips/{clip_id}           — Update title/notes.
DELETE /api/clips/{clip_id}           — Delete clip + files.
GET    /api/clips/{clip_id}/download  — Serve export file, set downloaded_at.
GET    /api/clips/{clip_id}/thumbnail — Serve thumbnail image.
POST   /api/clips/{clip_id}/re-export — Re-dispatch export task.
POST   /api/clips/{clip_id}/cover-image — Upload cover image for audio-to-MP4.
POST   /api/clips/cleanup             — Delete old export files, keep metadata.
"""

import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import (
    CLIP_CLEANUP_HOURS, CLIP_MAX_DURATION, CLIP_MIN_DURATION,
    CLIPS_DIR, MEDIA_DIR,
)
from app.database import get_db

router = APIRouter(prefix="/api/clips", tags=["clips"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_source_media(
    db: Session, source_type: str, source_id: str
) -> tuple[str, str]:
    """
    Resolve source media path and media_type from source_type + source_id.
    Returns (absolute_path, media_type).
    """
    if source_type == "meeting":
        media = (
            db.query(models.MediaFile)
            .filter(
                models.MediaFile.meeting_id == source_id,
                models.MediaFile.file_type.in_(["video", "audio"]),
            )
            .first()
        )
        if not media:
            raise HTTPException(404, "No media file found for this meeting")
        return media.file_path, media.file_type

    elif source_type == "newscast":
        newscast = (
            db.query(models.TVNewscast)
            .filter_by(newscast_id=source_id)
            .first()
        )
        if not newscast:
            raise HTTPException(404, "Newscast not found")
        path = newscast.cleaned_file or newscast.source_file
        if not path:
            raise HTTPException(400, "Newscast has no media file")
        return path, "video"

    else:
        raise HTTPException(400, f"Invalid source_type: {source_type}")


def _generate_thumbnail(
    source_path: str, media_type: str, start_time: float, end_time: float,
    clip_dir: Path,
) -> Optional[str]:
    """Generate thumbnail image synchronously. Returns path or None."""
    thumb_path = clip_dir / "thumb.jpg"

    try:
        if media_type == "video":
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(start_time),
                    "-i", source_path,
                    "-vframes", "1",
                    "-q:v", "3",
                    "-vf", "scale=320:-1",
                    str(thumb_path),
                ],
                capture_output=True, timeout=15,
            )
        else:
            # Audio: waveform thumbnail
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(start_time),
                    "-to", str(end_time),
                    "-i", source_path,
                    "-filter_complex",
                    "showwavespic=s=320x180:colors=#4a7cf7",
                    "-frames:v", "1",
                    str(thumb_path),
                ],
                capture_output=True, timeout=15,
            )
    except Exception:
        return None

    return str(thumb_path) if thumb_path.exists() else None


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.post("/", response_model=schemas.ClipOut, status_code=201)
def create_clip(payload: schemas.ClipCreate, db: Session = Depends(get_db)):
    """Create a clip, generate thumbnail, and dispatch export task."""
    duration = payload.end_time - payload.start_time
    if duration < CLIP_MIN_DURATION:
        raise HTTPException(400, f"Clip too short (min {CLIP_MIN_DURATION}s)")
    if duration > CLIP_MAX_DURATION:
        raise HTTPException(400, f"Clip too long (max {CLIP_MAX_DURATION}s)")
    if payload.start_time < 0:
        raise HTTPException(400, "start_time cannot be negative")
    if payload.end_time <= payload.start_time:
        raise HTTPException(400, "end_time must be greater than start_time")

    source_path, media_type = _resolve_source_media(
        db, payload.source_type, payload.source_id
    )

    if not Path(source_path).exists():
        raise HTTPException(400, "Source media file not found on disk")

    clip = models.Clip(
        source_type=payload.source_type,
        source_id=payload.source_id,
        source_media_path=source_path,
        media_type=media_type,
        start_time=payload.start_time,
        end_time=payload.end_time,
        duration=duration,
        title=payload.title,
        notes=payload.notes,
        export_status="pending",
    )
    db.add(clip)
    db.flush()  # get clip_id

    # Create clip directory and generate thumbnail
    clip_dir = CLIPS_DIR / clip.clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)

    thumb = _generate_thumbnail(
        source_path, media_type, payload.start_time, payload.end_time, clip_dir
    )
    if thumb:
        clip.thumbnail_path = thumb

    db.commit()
    db.refresh(clip)

    # Dispatch export task
    from app.tasks import export_clip_task
    export_clip_task.delay(clip.clip_id)

    return clip


@router.get("/", response_model=list[schemas.ClipOut])
def list_clips(
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Clip)
    if source_type:
        q = q.filter(models.Clip.source_type == source_type)
    if source_id:
        q = q.filter(models.Clip.source_id == source_id)
    return q.order_by(models.Clip.created_at.desc()).all()


@router.get("/{clip_id}", response_model=schemas.ClipOut)
def get_clip(clip_id: str, db: Session = Depends(get_db)):
    clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip


@router.patch("/{clip_id}", response_model=schemas.ClipOut)
def update_clip(
    clip_id: str, payload: schemas.ClipUpdate, db: Session = Depends(get_db)
):
    clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
    if not clip:
        raise HTTPException(404, "Clip not found")
    if payload.title is not None:
        clip.title = payload.title
    if payload.notes is not None:
        clip.notes = payload.notes
    db.commit()
    db.refresh(clip)
    return clip


@router.delete("/{clip_id}", status_code=204)
def delete_clip(clip_id: str, db: Session = Depends(get_db)):
    clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
    if not clip:
        raise HTTPException(404, "Clip not found")

    # Delete files on disk
    clip_dir = CLIPS_DIR / clip_id
    if clip_dir.exists():
        shutil.rmtree(clip_dir, ignore_errors=True)

    db.delete(clip)
    db.commit()


# ── Download & Thumbnail ─────────────────────────────────────────────────────

@router.get("/{clip_id}/download")
def download_clip(clip_id: str, db: Session = Depends(get_db)):
    clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
    if not clip:
        raise HTTPException(404, "Clip not found")
    if clip.export_status != "ready" or not clip.export_path:
        raise HTTPException(400, f"Clip not ready (status: {clip.export_status})")

    export = Path(clip.export_path)
    if not export.exists():
        raise HTTPException(404, "Export file not found on disk")

    clip.downloaded_at = datetime.utcnow()
    db.commit()

    # Build a descriptive filename
    safe_title = "".join(
        c if c.isalnum() or c in " _-" else "_" for c in clip.title
    ).strip()[:60]
    filename = f"{safe_title}{export.suffix}"

    return FileResponse(
        path=str(export),
        filename=filename,
        media_type="application/octet-stream",
    )


@router.get("/{clip_id}/thumbnail")
def get_thumbnail(clip_id: str, db: Session = Depends(get_db)):
    clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
    if not clip:
        raise HTTPException(404, "Clip not found")
    if not clip.thumbnail_path or not Path(clip.thumbnail_path).exists():
        raise HTTPException(404, "Thumbnail not available")

    return FileResponse(
        path=clip.thumbnail_path,
        media_type="image/jpeg",
    )


# ── Re-export & Cover Image ─────────────────────────────────────────────────

@router.post("/{clip_id}/re-export", response_model=schemas.ClipOut)
def re_export_clip(clip_id: str, db: Session = Depends(get_db)):
    clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
    if not clip:
        raise HTTPException(404, "Clip not found")
    if clip.export_status == "exporting":
        raise HTTPException(409, "Export already in progress")

    if not Path(clip.source_media_path).exists():
        raise HTTPException(400, "Source media file no longer exists")

    clip.export_status = "pending"
    clip.export_error = None
    clip.export_path = None
    db.commit()
    db.refresh(clip)

    from app.tasks import export_clip_task
    export_clip_task.delay(clip.clip_id)

    return clip


@router.post("/{clip_id}/cover-image", response_model=schemas.ClipOut)
async def upload_cover_image(
    clip_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
    if not clip:
        raise HTTPException(404, "Clip not found")

    clip_dir = CLIPS_DIR / clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename or "cover.jpg").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "Cover image must be JPG, PNG, or WebP")

    cover_path = clip_dir / f"cover{suffix}"
    with cover_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    clip.cover_image_path = str(cover_path)
    db.commit()
    db.refresh(clip)
    return clip


# ── Cleanup ──────────────────────────────────────────────────────────────────

@router.post("/cleanup")
def cleanup_clips(db: Session = Depends(get_db)):
    """Delete export files for downloaded or expired clips. Keeps metadata + thumbnails."""
    cutoff = datetime.utcnow() - timedelta(hours=CLIP_CLEANUP_HOURS)
    cleaned = 0

    clips = (
        db.query(models.Clip)
        .filter(
            models.Clip.export_status == "ready",
            models.Clip.export_path.isnot(None),
        )
        .all()
    )

    for clip in clips:
        should_clean = (
            clip.downloaded_at is not None
            or clip.created_at < cutoff
        )
        if not should_clean:
            continue

        if clip.export_path:
            p = Path(clip.export_path)
            if p.exists():
                p.unlink()

        clip.export_status = "cleaned"
        clip.export_path = None
        cleaned += 1

    db.commit()
    return {"cleaned": cleaned}

"""
Media upload endpoints.

POST /api/media/{meeting_id}/upload  — accepts a video file, saves it,
                                       enqueues the processing pipeline.
GET  /api/media/{meeting_id}/status  — poll pipeline readiness + progress.
GET  /api/media/{meeting_id}         — list media files for a meeting.
GET  /media/{meeting_id}/video       — stream video with range request support.
"""

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import MEDIA_DIR
from app.database import get_db
from app.tasks import process_video_task

router = APIRouter(tags=["media"])

_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

_MIME_TYPES = {
    ".mp4":  "video/mp4",
    ".mkv":  "video/x-matroska",
    ".mov":  "video/quicktime",
    ".avi":  "video/x-msvideo",
    ".webm": "video/webm",
    ".m4v":  "video/mp4",
}


def _find_video_file(meeting_id: str) -> Path | None:
    """Return the video file path regardless of extension, or None."""
    d = MEDIA_DIR / meeting_id
    for suffix in _ALLOWED_VIDEO_SUFFIXES:
        p = d / f"video{suffix}"
        if p.exists():
            return p
    return None


# ── Video streaming ───────────────────────────────────────────────────────────

@router.get("/media/{meeting_id}/video")
async def stream_video(meeting_id: str, request: Request):
    """
    Stream a meeting video with HTTP 206 range request support.
    Browsers require range responses to seek in large video files.
    """
    video_path = _find_video_file(meeting_id)
    if not video_path:
        raise HTTPException(404, "Video file not found")

    file_size = video_path.stat().st_size
    media_type = _MIME_TYPES.get(video_path.suffix.lower(), "video/mp4")

    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            path=str(video_path),
            media_type=media_type,
            headers={"Accept-Ranges": "bytes"},
        )

    # Parse "bytes=start-end"
    try:
        range_val = range_header.strip().replace("bytes=", "")
        start_str, _, end_str = range_val.partition("-")
        start = int(start_str) if start_str else 0
        end   = int(end_str)   if end_str   else file_size - 1
    except ValueError:
        raise HTTPException(400, "Invalid Range header")

    if start > end or end >= file_size:
        raise HTTPException(
            416,
            "Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = end - start + 1

    def iterfile():
        with video_path.open("rb") as f:
            f.seek(start)
            remaining = chunk_size
            buf = 1024 * 256  # 256 KB chunks
            while remaining > 0:
                data = f.read(min(buf, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        iterfile(),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range":  f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges":  "bytes",
            "Content-Length": str(chunk_size),
        },
    )


# ── Upload ────────────────────────────────────────────────────────────────────

@router.post(
    "/api/media/{meeting_id}/upload",
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

    process_video_task.delay(meeting_id, media.media_id)

    return media


# ── Reprocess ─────────────────────────────────────────────────────────────────

@router.post("/api/media/{meeting_id}/reprocess", status_code=202)
def reprocess_video(meeting_id: str, db: Session = Depends(get_db)):
    """
    Re-queue the video pipeline for a meeting that already has a video uploaded.
    Safe to call at any time — the pipeline skips steps already completed.
    Use this to resume after a crash or to re-run the embedding step.
    """
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    media = (
        db.query(models.MediaFile)
        .filter_by(meeting_id=meeting_id, file_type="video")
        .first()
    )
    if not media:
        raise HTTPException(404, "No video uploaded for this meeting — upload first.")

    process_video_task.delay(meeting_id, media.media_id)
    return {"meeting_id": meeting_id, "status": "queued"}


# ── Status / list ─────────────────────────────────────────────────────────────

def _read_progress(meeting_id: str) -> dict:
    """Read progress.json written by the pipeline. Returns empty dict if absent."""
    p = MEDIA_DIR / meeting_id / "progress.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


@router.get("/api/media/{meeting_id}/status", response_model=schemas.PipelineStatus)
def pipeline_status(meeting_id: str, db: Session = Depends(get_db)):
    """Poll pipeline status and progress for a meeting."""
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

    progress = _read_progress(meeting_id)
    stage = progress.get("stage")
    pct = progress.get("pct")
    detail = progress.get("detail", "")

    if not has_video:
        status = "pending"
        stage = stage or "Waiting for upload"
        pct = pct or 0
    elif stage == "Complete" or pct == 100:
        status = "complete"
        pct = 100
    elif segment_count > 0 or stage:
        status = "processing"
        pct = pct or 10
    else:
        status = "processing"
        stage = stage or "Starting..."
        pct = pct or 0

    return schemas.PipelineStatus(
        meeting_id=meeting_id,
        segment_count=segment_count,
        task_id=None,
        status=status,
        stage=stage,
        progress_pct=pct,
        detail=detail,
    )


@router.get("/api/media/{meeting_id}", response_model=list[schemas.MediaFileOut])
def list_media(meeting_id: str, db: Session = Depends(get_db)):
    return (
        db.query(models.MediaFile)
        .filter_by(meeting_id=meeting_id)
        .order_by(models.MediaFile.file_type)
        .all()
    )
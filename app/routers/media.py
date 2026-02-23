"""
Media upload endpoints.

POST /api/media/{meeting_id}/upload  — accepts a video file, saves it,
                                       enqueues the processing pipeline.
POST /api/media/{meeting_id}/rerun   — wipe segments/cache and reprocess.
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

_VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
_AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".wma"}
_ALLOWED_SUFFIXES = _VIDEO_SUFFIXES | _AUDIO_SUFFIXES

_MIME_TYPES = {
    ".mp4":  "video/mp4",
    ".mkv":  "video/x-matroska",
    ".mov":  "video/quicktime",
    ".avi":  "video/x-msvideo",
    ".webm": "video/webm",
    ".m4v":  "video/mp4",
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".wav":  "audio/wav",
    ".aac":  "audio/aac",
    ".flac": "audio/flac",
    ".ogg":  "audio/ogg",
    ".wma":  "audio/x-ms-wma",
}


def _find_media_file(meeting_id: str) -> Path | None:
    """Return the uploaded media file (video or audio) regardless of extension, or None."""
    d = MEDIA_DIR / meeting_id
    # Check video files first, then audio source files
    for prefix, suffixes in [("video", _VIDEO_SUFFIXES), ("audio_source", _AUDIO_SUFFIXES)]:
        for suffix in suffixes:
            p = d / f"{prefix}{suffix}"
            if p.exists():
                return p
    return None


# ── Video streaming ───────────────────────────────────────────────────────────

@router.get("/media/{meeting_id}/video")
async def stream_media(meeting_id: str, request: Request):
    """
    Stream a meeting's media file (video or audio) with HTTP 206 range request support.
    Browsers require range responses to seek in large media files.
    """
    video_path = _find_media_file(meeting_id)
    if not video_path:
        raise HTTPException(404, "Media file not found")

    file_size  = video_path.stat().st_size
    media_type = _MIME_TYPES.get(video_path.suffix.lower(), "video/mp4")
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            path=str(video_path),
            media_type=media_type,
            headers={"Accept-Ranges": "bytes"},
        )

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
            buf = 1024 * 256
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
    Upload a video or audio file for a meeting and trigger the ingestion pipeline.
    Returns immediately (202) — pipeline runs asynchronously.
    """
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            400,
            f"Unsupported format '{suffix}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_SUFFIXES))}",
        )

    is_audio = suffix in _AUDIO_SUFFIXES
    file_prefix = "audio_source" if is_audio else "video"
    file_type = "audio" if is_audio else "video"

    dest_dir  = MEDIA_DIR / meeting_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{file_prefix}{suffix}"

    with dest_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    media = models.MediaFile(
        meeting_id=meeting_id,
        file_type=file_type,
        file_path=str(dest_path),
        duration=None,
    )
    db.add(media)
    db.commit()
    db.refresh(media)

    process_video_task.delay(meeting_id, media.media_id)
    return media


# ── Process unprocessed meeting ────────────────────────────────────────────────

@router.post("/api/media/{meeting_id}/process", status_code=202)
def process_meeting(meeting_id: str, db: Session = Depends(get_db)):
    """
    Trigger the processing pipeline for a meeting that has a media file
    but hasn't been processed yet (e.g. ingested radio episodes).
    """
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
        )
        .first()
    )
    if not media:
        raise HTTPException(400, "No media file found for this meeting")

    segment_count = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .count()
    )
    if segment_count > 0:
        raise HTTPException(400, "Meeting already has transcript segments. Use /rerun instead.")

    process_video_task.delay(meeting_id, media.media_id)
    return {"meeting_id": meeting_id, "status": "queued"}


# ── Rerun pipeline ────────────────────────────────────────────────────────────

@router.post("/api/media/{meeting_id}/rerun", status_code=202)
def rerun_pipeline(meeting_id: str, db: Session = Depends(get_db)):
    """
    Wipe all transcript segments, voiceprints, and cached pipeline files
    for a meeting, then requeue the full ingestion pipeline.

    This preserves:
      - The meeting record and metadata
      - The uploaded video file
      - People records (speaker names)

    This deletes:
      - All TranscriptSegment rows (and their assignments via cascade)
      - All Voiceprint rows for this meeting's speakers (embeddings only —
        voiceprints from other meetings are unaffected)
      - diarization.json, progress.json cache files
      - audio.wav (will be re-extracted so transcription uses a clean slate)
      - The audio MediaFile record
    """
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    # Find the source media file (video or audio upload).
    # Uploaded audio files are saved as "audio_source.*", whereas the
    # pipeline-extracted audio lives at "audio.wav".
    source_media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%audio.wav"),
        )
        .first()
    )
    if not source_media:
        raise HTTPException(400, "No media found for this meeting — upload a file first")

    # Delete assignments first (bulk delete bypasses ORM cascade)
    segment_ids = [
        row.segment_id for row in
        db.query(models.TranscriptSegment.segment_id)
        .filter_by(meeting_id=meeting_id)
        .all()
    ]
    if segment_ids:
        db.query(models.SegmentAssignment).filter(
            models.SegmentAssignment.segment_id.in_(segment_ids)
        ).delete(synchronize_session=False)

    # Now safe to delete segments
    deleted = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .delete(synchronize_session=False)
    )

    # Delete extracted audio.wav MediaFile record (will be re-extracted).
    # Keep uploaded audio source files intact.
    db.query(models.MediaFile).filter(
        models.MediaFile.meeting_id == meeting_id,
        models.MediaFile.file_path.like("%audio.wav"),
    ).delete(synchronize_session=False)

    db.commit()

    # Delete cache files — audio.wav, diarization.json, progress.json
    meeting_dir = MEDIA_DIR / meeting_id
    for fname in ("audio.wav", "diarization.json", "progress.json"):
        p = meeting_dir / fname
        if p.exists():
            p.unlink()

    # Requeue pipeline
    process_video_task.delay(meeting_id, source_media.media_id)

    return {
        "meeting_id": meeting_id,
        "segments_deleted": deleted,
        "status": "queued",
    }


# ── Status / list ─────────────────────────────────────────────────────────────

def _read_progress(meeting_id: str) -> dict:
    p = MEDIA_DIR / meeting_id / "progress.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


@router.get("/api/media/{meeting_id}/status", response_model=schemas.PipelineStatus)
def pipeline_status(meeting_id: str, db: Session = Depends(get_db)):
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    segment_count = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .count()
    )

    has_media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
        )
        .first()
    )

    progress = _read_progress(meeting_id)
    stage  = progress.get("stage")
    pct    = progress.get("pct")
    detail = progress.get("detail", "")

    if not has_media:
        status = "pending"
        stage  = stage or "Waiting for upload"
        pct    = pct or 0
    elif stage == "Complete" or pct == 100:
        status = "complete"
        pct    = 100
    elif segment_count > 0 or stage:
        status = "processing"
        pct    = pct or 10
    else:
        status = "processing"
        stage  = stage or "Starting..."
        pct    = pct or 0

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

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
from app.paths import to_relative, to_absolute
from app.utils import generate_media_filename

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


def _find_media_file(meeting_id: str, db: Session) -> Path | None:
    """Return the source media file (upload or download) via DB query, or None.

    Scans all candidate records and returns the first whose file exists on disk,
    so stale duplicate records (e.g. from retried downloads) are skipped silently.
    """
    candidates = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
        )
        .all()
    )
    for source in candidates:
        p = Path(to_absolute(source.file_path))
        if p.exists():
            return p
    return None


# ── Video streaming ───────────────────────────────────────────────────────────

@router.get("/media/{meeting_id}/video")
async def stream_media(meeting_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Stream a meeting's media file (video or audio) with HTTP 206 range request support.
    Browsers require range responses to seek in large media files.
    """
    video_path = _find_media_file(meeting_id, db)
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
    file_type = "audio" if is_audio else "video"

    # Reject if a primary source of this type already exists for the meeting.
    existing = (
        db.query(models.MediaFile)
        .filter_by(meeting_id=meeting_id, file_type=file_type)
        .filter(~models.MediaFile.file_path.like("%_extracted.wav"))
        .first()
    )
    if existing:
        raise HTTPException(
            409,
            f"Meeting already has a {file_type} file "
            f"({Path(existing.file_path).name}). "
            "Delete it first before uploading a replacement.",
        )

    dest_dir  = MEDIA_DIR / meeting_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_filename = generate_media_filename(meeting.meeting_date, meeting.title, suffix)
    dest_path = dest_dir / dest_filename

    with dest_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    media = models.MediaFile(
        meeting_id=meeting_id,
        file_type=file_type,
        file_path=to_relative(str(dest_path)),
        duration=None,
    )
    db.add(media)
    db.commit()
    db.refresh(media)

    from app.services.task_dispatch import send_task
    send_task("tasks.process_video", args=[meeting_id, media.media_id])
    return media


# ── Transcode to 540p ─────────────────────────────────────────────────────────

@router.post("/api/media/{meeting_id}/transcode", status_code=202)
def transcode_meeting_video(meeting_id: str, db: Session = Depends(get_db)):
    """
    Transcode a meeting's video to 540p (960x540).
    Deletes the original file on success.
    Must be done before processing the pipeline for large downloaded files.
    Runs in a background thread.
    """
    import threading

    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    candidates = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
        )
        .all()
    )
    if not candidates:
        raise HTTPException(400, "No media file found for this meeting")

    # Prefer a record whose file exists on disk; fall back to first record so
    # run_transcode's recovery logic can still handle the missing-source case.
    media = next((c for c in candidates if Path(to_absolute(c.file_path)).exists()), candidates[0])

    if media.transcode_status == "transcoding":
        raise HTTPException(400, "Transcode already in progress")

    if media.transcode_status == "transcoded":
        raise HTTPException(400, "Already transcoded")

    media_id = media.media_id
    media.transcode_status = "transcoding"
    db.commit()

    # Run in background thread
    from app.services.transcoder import run_transcode
    t = threading.Thread(target=run_transcode, args=(meeting_id, media_id), daemon=True)
    t.start()

    return {"meeting_id": meeting_id, "media_id": media_id, "status": "started"}


# ── Process unprocessed meeting ────────────────────────────────────────────────

@router.post("/api/media/{meeting_id}/process", status_code=202)
def process_meeting(meeting_id: str, db: Session = Depends(get_db)):
    """
    Trigger the processing pipeline for a meeting that has a media file
    but hasn't been processed yet (e.g. ingested radio episodes).
    Dispatches via Huey task queue; falls back to a background thread if unavailable.
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

    from app.services.task_dispatch import send_task
    send_task("tasks.process_video", args=[meeting_id, media.media_id])
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

    # Find the source media file (video or audio upload/download).
    # Pipeline-extracted audio filenames end with "_extracted.wav".
    source_media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%\\_extracted.wav", escape="\\"),
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

    # Delete extracted audio MediaFile record (will be re-extracted).
    # Keep uploaded/downloaded source files intact.
    db.query(models.MediaFile).filter(
        models.MediaFile.meeting_id == meeting_id,
        models.MediaFile.file_path.like("%\\_extracted.wav", escape="\\"),
    ).delete(synchronize_session=False)

    db.commit()

    # Delete cache files — *_extracted.wav, diarization.json, progress.json
    meeting_dir = MEDIA_DIR / meeting_id
    if meeting_dir.exists():
        for p in meeting_dir.glob("*_extracted.wav"):
            p.unlink()
    for fname in ("diarization.json", "progress.json"):
        p = meeting_dir / fname
        if p.exists():
            p.unlink()

    # Requeue pipeline
    from app.services.task_dispatch import send_task
    send_task("tasks.process_video", args=[meeting_id, source_media.media_id])

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
    from datetime import datetime, timezone
    from app.config import PROGRESS_STALE_SECONDS

    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    segment_count = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .count()
    )

    source_media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
        )
        .first()
    )
    has_media = source_media is not None
    transcode_status = source_media.transcode_status if source_media else None

    progress = _read_progress(meeting_id)
    stage  = progress.get("stage")
    pct    = progress.get("pct")
    detail = progress.get("detail", "")
    is_error = progress.get("error", False)

    # Check for explicit error state in progress.json.
    # Exception: if the error stage was a download/extract step but the meeting
    # now has media, the error is stale (a subsequent attempt succeeded).
    # Ignore it and fall through to the normal DB-based status logic.
    if is_error:
        _stage_lower = (stage or "").lower()
        _is_stale_download_error = has_media and any(
            k in _stage_lower for k in (
                "download", "extract", "video downloaded",
                "agenda", "minutes", "packet", "ocr",
            )
        )
        if not _is_stale_download_error:
            return schemas.PipelineStatus(
                meeting_id=meeting_id,
                segment_count=segment_count,
                task_id=None,
                status="error",
                stage=stage or "Error",
                progress_pct=pct or 0,
                detail=detail,
                error=True,
                transcode_status=transcode_status,
            )
        # Stale download error — clear it so stale-detection below doesn't fire either
        stage = None
        pct = None
        is_error = False

    # Check for stale progress (worker crashed without writing error).
    # Two zones:
    #   PROGRESS_STALE_SECONDS (10 min): "recently stalled" → show error badge so user
    #     knows the active worker crashed and needs restart.
    #   PROGRESS_ABANDONED_SECONDS (2 hours): "old abandoned job" → the progress.json is
    #     a leftover from a previous session that was never cleaned up.  Treat it as
    #     if there is no progress file and fall through to the normal status logic.
    PROGRESS_ABANDONED_SECONDS = 7200  # 2 hours
    updated_at_str = progress.get("updated_at")
    if (updated_at_str
        and stage not in (None, "Complete", "")
        and pct != 100):
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if PROGRESS_STALE_SECONDS < age <= PROGRESS_ABANDONED_SECONDS:
                # Recently stalled — worker crashed mid-job. Show error.
                return schemas.PipelineStatus(
                    meeting_id=meeting_id,
                    segment_count=segment_count,
                    task_id=None,
                    status="error",
                    stage=stage,
                    progress_pct=pct,
                    detail=f"Worker appears stalled (no update for {int(age)}s). "
                           f"Kill and restart the worker, or click Process to retry.",
                    error=True,
                    transcode_status=transcode_status,
                )
            elif age > PROGRESS_ABANDONED_SECONDS:
                # Very old leftover — ignore stale progress, fall through to DB-based status.
                stage = None
                pct = None
        except (ValueError, TypeError):
            pass

    # "Transcode complete" at pct=100 is NOT pipeline complete
    is_transcode_stage = stage and "transcode" in stage.lower()
    is_download_stage = stage and any(
        k in stage.lower() for k in (
            "download", "extract", "video downloaded",
            "agenda", "minutes", "packet", "ocr",
        )
    )

    if not has_media:
        status = "pending"
        stage  = stage or "Waiting for upload"
        pct    = pct or 0
    elif transcode_status == "transcoding":
        # Active transcode in progress — report progress from progress.json
        status = "processing"
        stage  = stage or "Transcoding to 540p"
        pct    = pct or 5
    elif segment_count > 0 and (meeting and meeting.processed_at is not None):
        # Meeting has segments and processed_at is set — it's done.
        # This catches meetings where progress.json was cleared or stage is stale.
        status = "complete"
        pct    = 100
    elif (stage == "Complete" or pct == 100) and not is_transcode_stage and not is_download_stage and segment_count > 0:
        status = "complete"
        pct    = 100
    elif is_transcode_stage and pct == 100:
        # Transcode finished, but pipeline hasn't run yet
        status = "pending"
        pct    = 0
    elif has_media and transcode_status == "pending" and segment_count == 0 and not is_transcode_stage:
        # Media downloaded but needs transcode
        status = "pending"
        stage  = "Ready to process"
        pct    = 0
    elif has_media and transcode_status == "transcoded" and segment_count == 0 and meeting and meeting.processed_at is None:
        # Transcoded but pipeline hasn't run yet
        status = "pending"
        stage  = "Ready to process"
        pct    = 0
    elif segment_count > 0 or (stage and stage not in ("", "Complete")):
        # Active processing — has a real stage label or segments being generated
        status = "processing"
        pct    = pct or 10
    elif meeting and meeting.processed_at is None and segment_count == 0:
        # Media exists but never processed — ready to process
        status = "pending"
        stage  = "Ready to process"
        pct    = 0
    else:
        status = "complete"
        pct    = 100

    # Vote ingest status
    vote_count = (
        db.query(models.MeetingVote)
        .filter_by(meeting_id=meeting_id)
        .count()
    )
    minutes_doc = (
        db.query(models.Document)
        .filter_by(meeting_id=meeting_id, document_type="minutes")
        .first()
    )
    minutes_parse_status = minutes_doc.minutes_parse_status if minutes_doc else None

    return schemas.PipelineStatus(
        meeting_id=meeting_id,
        segment_count=segment_count,
        task_id=None,
        status=status,
        stage=stage,
        progress_pct=pct,
        detail=detail,
        transcode_status=transcode_status,
        vote_count=vote_count,
        minutes_parse_status=minutes_parse_status,
    )


@router.get("/api/media/{meeting_id}", response_model=list[schemas.MediaFileOut])
def list_media(meeting_id: str, db: Session = Depends(get_db)):
    return (
        db.query(models.MediaFile)
        .filter_by(meeting_id=meeting_id)
        .order_by(models.MediaFile.file_type)
        .all()
    )

"""
Backfill API — controlled one-at-a-time stage progression.

Each next-* endpoint finds ONE meeting, queues it, and returns immediately.
The UI loops: queue one → wait for worker idle → pause 3s → queue next.
Nothing runs concurrently — the Huey thread worker (workers=1) enforces that
at the worker level, and the UI enforces it at the trigger level.

Each next-* endpoint also auto-resets stuck meetings before selecting a
candidate, so reset-stuck never needs to be called manually.

Endpoints
─────────
GET  /api/backfill/status               Counts: download/transcode/process/diarize pending + stuck
GET  /api/backfill/queue                Paginated list of process-pending meetings
GET  /api/backfill/active               Currently running meeting (from processing_jobs DB)
GET  /api/backfill/progress/{id}        Latest job for a specific meeting
GET  /api/backfill/worker-idle          True if Huey queues are empty and no DB jobs running
GET  /api/backfill/events               SSE stream: real-time progress updates
POST /api/backfill/next-download        Queue one primegov_download_task
POST /api/backfill/next-transcode       Queue one transcode_video_task
POST /api/backfill/next-process         Queue one process_video_task (oldest first)
POST /api/backfill/next-process-newest  Queue one process_video_task (newest first, for dual-worker)
POST /api/backfill/next-diarize         Queue one process_video_task for transcribed-but-not-diarized meetings
POST /api/backfill/process-now/{id}     Queue process_video_task for a specific meeting
POST /api/backfill/process-priority/{id} Queue and jump to front of queue (priority=100 default)
POST /api/backfill/full/{id}            Queue full_ingest_task for a specific meeting
POST /api/backfill/skip/{id}            Add meeting to skip set (excluded from auto-queue)
POST /api/backfill/unskip/{id}          Remove meeting from skip set
GET  /api/backfill/stuck                List stale in-progress meetings (>10 min)
POST /api/backfill/reset-stuck          Reset stuck meetings manually
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.config import MEDIA_DIR, BASE_DIR
from app.paths import to_absolute

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backfill", tags=["backfill"])

_STUCK_SECONDS = 7200      # 2 hr: default for GPU-heavy "process" stage
                           # Long meetings (10 hr) can diarize for 60-90 min
                           # with no progress callback from pyannote.

# Per-stage stuck thresholds (seconds without a progress update).
# Download/transcode emit progress ticks during ffmpeg — if no tick arrives
# within these windows, the task is dead.
_STUCK_SECONDS_BY_STAGE = {
    "download":  600,      # 10 min — ffmpeg download ticks every ~30s
    "transcode": 900,      # 15 min — ffmpeg transcode ticks every ~10s
    "process":   7200,     # 2 hr  — pyannote diarization has no progress callback
}

# Minimum process timeout (even for short meetings)
_MIN_PROCESS_TIMEOUT = 7200  # 2 hr


def _get_process_timeout(db: "Session", meeting_id: str) -> int:
    """Return a dynamic process-stage timeout based on media duration.

    For long meetings (4-6 hr), the fixed 2 hr timeout is too short.
    Scale to 1.5x the media duration (in seconds), with a floor of 2 hr.
    """
    from app import models

    media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            models.MediaFile.duration.isnot(None),
        )
        .first()
    )
    if media and media.duration and media.duration > 0:
        return max(_MIN_PROCESS_TIMEOUT, int(media.duration * 1.5))
    return _MIN_PROCESS_TIMEOUT


def _stage_timeout(db: "Session", job) -> int:
    """Get the stuck timeout for a job, using dynamic duration for process stage."""
    if job.stage == "process":
        return _get_process_timeout(db, job.meeting_id)
    return _STUCK_SECONDS_BY_STAGE.get(job.stage, _STUCK_SECONDS)

# Self-healing loop interval (seconds). A background thread runs _do_reset_stuck
# this often, so stuck jobs are recovered even if no next-* endpoints are called.
_SELF_HEAL_INTERVAL = 300  # 5 minutes: stuck-job reset cadence
_AUTO_ADVANCE_INTERVAL = 30  # 30 s: idle-queue refill cadence

_SKIP_FILE = BASE_DIR / "database" / "backfill_skipped.json"


# ── DB dependency ─────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Skip-set helpers (file-backed, no Redis) ────────────────────────────────

def _get_skipped() -> set[str]:
    try:
        if _SKIP_FILE.exists():
            return set(json.loads(_SKIP_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def _save_skipped(skipped: set[str]) -> None:
    try:
        _SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SKIP_FILE.write_text(json.dumps(sorted(skipped)), encoding="utf-8")
    except Exception as exc:
        logger.warning("skip save failed: %s", exc)


def _skip(meeting_id: str) -> None:
    skipped = _get_skipped()
    skipped.add(meeting_id)
    _save_skipped(skipped)


def _unskip(meeting_id: str) -> None:
    skipped = _get_skipped()
    skipped.discard(meeting_id)
    _save_skipped(skipped)


# ── Active-job helpers ────────────────────────────────────────────────────────

def _running_job(db: Session):
    """Return the most-recently-updated running ProcessingJob, or None."""
    from app import models
    return (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status == "running")
        .order_by(models.ProcessingJob.updated_at.desc())
        .first()
    )


def _is_active(db: Session, meeting_id: str) -> bool:
    """True if this meeting has a queued or running (non-stale) processing job.

    Uses per-stage timeouts: a download job that's silent for 10 min is stale,
    but a process job gets 2 hours before being considered stale.
    """
    from app import models
    now = datetime.utcnow()
    jobs = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.meeting_id == meeting_id,
            models.ProcessingJob.status.in_(["queued", "running"]),
        )
        .all()
    )
    for job in jobs:
        timeout = _stage_timeout(db, job)
        age = (now - job.updated_at).total_seconds() if job.updated_at else float("inf")
        if age < timeout:
            return True
    return False


def _active_meeting_ids(db: Session) -> set[str]:
    """Return set of meeting_ids with a queued or running (non-stale) processing job.

    Uses per-stage timeouts for staleness check.
    """
    from app import models
    now = datetime.utcnow()
    jobs = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status.in_(["queued", "running"]))
        .all()
    )
    active = set()
    for job in jobs:
        timeout = _stage_timeout(db, job)
        age = (now - job.updated_at).total_seconds() if job.updated_at else float("inf")
        if age < timeout:
            active.add(job.meeting_id)
    return active


def _count_stuck(db: Session) -> int:
    """Count running/queued jobs that exceed their per-stage stuck threshold."""
    from app import models
    now = datetime.utcnow()
    active_jobs = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status.in_(["running", "queued"]))
        .all()
    )
    count = 0
    for job in active_jobs:
        timeout = _stage_timeout(db, job)
        age = (now - job.updated_at).total_seconds() if job.updated_at else float("inf")
        if age >= timeout:
            count += 1
    return count


# ── Shared reset logic ────────────────────────────────────────────────────────

def _do_reset_stuck(db: Session) -> dict:
    """
    Three-phase cleanup, runs automatically every 5 min and on every next-* call:

    1. Reset stale jobs — per-stage timeout (download=10m, transcode=15m, process=2h)
    2. Kill duplicate active jobs — only one queued/running job per meeting allowed
    3. Reset stuck transcodes — MediaFile.transcode_status='transcoding' → 'pending'

    Uses per-stage thresholds: download/transcode have shorter leashes than
    GPU-heavy process tasks because they emit frequent progress ticks.
    """
    from app import models

    now = datetime.utcnow()

    active_jobs = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status.in_(["running", "queued"]))
        .order_by(models.ProcessingJob.updated_at.desc())
        .all()
    )

    # Phase 1: Reset stale jobs (per-stage timeout)
    reset_ids = []
    for job in active_jobs:
        stage_timeout = _stage_timeout(db, job)
        age = (now - job.updated_at).total_seconds() if job.updated_at else float("inf")
        if age < stage_timeout:
            continue
        job.status = "error"
        job.error_msg = (
            f"Self-healed: no progress for {int(age)}s "
            f"(limit {stage_timeout}s for stage '{job.stage}')"
        )
        job.completed_at = now
        job.updated_at = now
        reset_ids.append(job.meeting_id)
        try:
            pf = MEDIA_DIR / job.meeting_id / "progress.json"
            if pf.exists():
                pf.write_text(json.dumps({
                    "stage": "Error",
                    "pct": 0,
                    "detail": job.error_msg,
                    "error": True,
                    "updated_at": now.isoformat(),
                }), encoding="utf-8")
        except Exception:
            pass
    if reset_ids:
        db.commit()

    # Phase 2: Kill duplicate active jobs (only newest per meeting survives)
    # Re-query after phase 1 to get the surviving active jobs
    surviving = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status.in_(["running", "queued"]))
        .order_by(models.ProcessingJob.updated_at.desc())
        .all()
    )
    seen_meetings: set[str] = set()
    dupes_killed = 0
    for job in surviving:
        if job.meeting_id in seen_meetings:
            job.status = "error"
            job.error_msg = "Duplicate active job — killed by self-healer"
            job.completed_at = now
            job.updated_at = now
            dupes_killed += 1
        else:
            seen_meetings.add(job.meeting_id)
    if dupes_killed:
        db.commit()
        logger.info("reset-stuck: killed %d duplicate active job(s)", dupes_killed)

    # Reset stuck transcodes (MediaFile rows stuck at 'transcoding')
    stuck_transcodes = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.transcode_status == "transcoding")
        .all()
    )
    transcode_resets = 0
    for media in stuck_transcodes:
        media.transcode_status = "pending"
        transcode_resets += 1
    if transcode_resets:
        db.commit()

    if reset_ids or transcode_resets:
        logger.info("reset-stuck: %d job(s) reset, %d transcode reset",
                    len(reset_ids), transcode_resets)

    return {"reset_ids": reset_ids, "transcode_resets": transcode_resets}


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def backfill_status(db: Session = Depends(get_db)):
    from app import models

    meeting_ids_with_url = {
        r.meeting_id for r in
        db.query(models.Meeting.meeting_id).filter(models.Meeting.video_url.isnot(None)).all()
    }
    meeting_ids_with_media = {
        r.meeting_id for r in
        db.query(models.MediaFile.meeting_id)
        .filter(models.MediaFile.file_type.in_(["video", "audio"]))
        .distinct().all()
    }
    download_pending = len(meeting_ids_with_url - meeting_ids_with_media)

    # All meetings with any source media (including transcode-pending)
    all_media_meeting_ids = {
        r.meeting_id for r in
        db.query(models.MediaFile.meeting_id)
        .filter(
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
        ).distinct().all()
    }
    meetings_with_segments_ids = {
        r.meeting_id for r in
        db.query(models.TranscriptSegment.meeting_id).distinct().all()
    }
    # Process pending = has media (any transcode state), no segments, not processed
    process_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            models.Meeting.meeting_id.in_(all_media_meeting_ids),
            ~models.Meeting.meeting_id.in_(meetings_with_segments_ids),
        ).count()
    )

    # Diarize pending: transcription done (has segments) but pipeline not complete.
    # The pipeline is resumable — re-queuing these skips transcription and runs
    # diarization → alignment → embedding → voiceprint.
    diarize_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            models.Meeting.meeting_id.in_(meetings_with_segments_ids),
        ).count()
    )

    # Stuck count: running/queued jobs that exceed their per-stage timeout
    stuck_count = _count_stuck(db)

    return {
        "download_pending": download_pending,
        "process_pending": process_pending,
        "diarize_pending": diarize_pending,
        "stuck": stuck_count,
    }


# ── Queue (for UI table) ──────────────────────────────────────────────────────

@router.get("/queue")
def backfill_queue(
    offset: int = 0,
    limit: int = 1000,
    db: Session = Depends(get_db),
):
    """
    Return paginated list of process-pending meetings, annotated with skip
    status, active state, and whether the file is on disk.

    Active state is queried from processing_jobs DB (no filesystem scan).
    """
    from app import models

    skipped = _get_skipped()

    # ── Single DB query for all running jobs (per-stage stuck filtering) ────
    now = datetime.utcnow()
    all_running = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status == "running")
        .all()
    )
    # Map: meeting_id → job (only non-stuck jobs)
    active_by_meeting: dict[str, models.ProcessingJob] = {}
    for j in all_running:
        timeout = _stage_timeout(db, j)
        age = (now - j.updated_at).total_seconds() if j.updated_at else float("inf")
        if age < timeout:
            active_by_meeting[j.meeting_id] = j

    # ── DB queries ─────────────────────────────────────────────────────────
    meetings_with_segments_ids = {
        r.meeting_id for r in
        db.query(models.TranscriptSegment.meeting_id).distinct().all()
    }

    media_rows = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
        )
        .all()
    )
    media_by_meeting: dict[str, models.MediaFile] = {}
    for row in media_rows:
        if row.meeting_id not in media_by_meeting:
            media_by_meeting[row.meeting_id] = row

    # Process-pending: have media but no transcript segments
    process_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            ~models.Meeting.meeting_id.in_(meetings_with_segments_ids),
            models.Meeting.meeting_id.in_(media_by_meeting.keys()),
        )
        .order_by(models.Meeting.meeting_date)
        .all()
    )

    # Download-pending: have video_url but no media file at all
    download_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.video_url.isnot(None),
            ~models.Meeting.meeting_id.in_(media_by_meeting.keys()),
            ~models.Meeting.meeting_id.in_(meetings_with_segments_ids),
        )
        .order_by(models.Meeting.meeting_date)
        .all()
    )

    all_pending = process_pending + download_pending
    all_pending.sort(key=lambda m: m.meeting_date or "")

    total = len(all_pending)
    page_items = all_pending[offset: offset + limit]

    items = []
    for meeting in page_items:
        media = media_by_meeting.get(meeting.meeting_id)
        job = active_by_meeting.get(meeting.meeting_id)
        needs_download = meeting.meeting_id not in media_by_meeting

        items.append({
            "meeting_id": meeting.meeting_id,
            "meeting_date": meeting.meeting_date,
            "group_name": meeting.group_name,
            "title": meeting.title,
            "category": meeting.category or "meeting",
            "media_id": media.media_id if media else None,
            "file_type": media.file_type if media else None,
            "transcode_status": media.transcode_status if media else None,
            "file_exists": bool(media and Path(to_absolute(media.file_path)).exists()),
            "needs_download": needs_download,
            "skipped": meeting.meeting_id in skipped,
            "active": meeting.meeting_id in active_by_meeting,
            "stage": job.stage_label if job else "",
            "pct": job.pct if job else 0,
        })

    return {"items": items, "total": total, "offset": offset, "limit": limit}


# ── Active task ───────────────────────────────────────────────────────────────

@router.get("/active")
def backfill_active(db: Session = Depends(get_db)):
    """Return the meeting currently being processed (DB query — no filesystem scan)."""
    from app import models

    threshold = datetime.utcnow() - timedelta(seconds=_STUCK_SECONDS)
    job = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.status == "running",
            models.ProcessingJob.updated_at >= threshold,
        )
        .order_by(models.ProcessingJob.updated_at.desc())
        .first()
    )

    if not job:
        return {"active": None}

    meeting = db.query(models.Meeting).filter_by(meeting_id=job.meeting_id).first()
    return {
        "active": {
            "meeting_id": job.meeting_id,
            "stage": job.stage_label or job.stage,
            "pct": job.pct or 0,
            "detail": job.detail or "",
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "title": meeting.title if meeting else job.meeting_id,
            "group_name": meeting.group_name if meeting else "",
        }
    }


# Stage → worker type mapping
_LIGHT_STAGES = {"download", "transcode"}
_GPU_STAGES = {"process"}


def _worker_type_for_stage(stage: str) -> str:
    """Return 'light' or 'gpu' based on the processing stage."""
    return "light" if stage in _LIGHT_STAGES else "gpu"


def _job_to_dict(job, meeting) -> dict:
    """Convert a ProcessingJob + Meeting to a JSON-friendly dict."""
    return {
        "job_id": job.job_id,
        "meeting_id": job.meeting_id,
        "stage": job.stage,
        "stage_label": job.stage_label or job.stage,
        "status": job.status,
        "pct": job.pct or 0,
        "detail": job.detail or "",
        "error_message": job.error_msg or "",
        "worker_type": _worker_type_for_stage(job.stage),
        "queued_at": job.queued_at.isoformat() if job.queued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "meeting_date": meeting.meeting_date if meeting else None,
        "title": meeting.title if meeting else job.meeting_id,
        "group_name": meeting.group_name if meeting else "",
    }


@router.get("/workers")
def backfill_workers(db: Session = Depends(get_db)):
    """Return the active job for each worker type (GPU and light), plus their queues."""
    from app import models

    now = datetime.utcnow()

    # Get all running jobs (non-stuck)
    running = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status == "running")
        .order_by(models.ProcessingJob.updated_at.desc())
        .all()
    )

    gpu_active = None
    light_active = None
    for job in running:
        timeout = _stage_timeout(db, job)
        age = (now - job.updated_at).total_seconds() if job.updated_at else float("inf")
        if age >= timeout:
            continue
        wt = _worker_type_for_stage(job.stage)
        meeting = db.query(models.Meeting).filter_by(meeting_id=job.meeting_id).first()
        job_dict = _job_to_dict(job, meeting)
        if wt == "gpu" and gpu_active is None:
            gpu_active = job_dict
        elif wt == "light" and light_active is None:
            light_active = job_dict

    # Get all queued jobs
    queued = (
        db.query(models.ProcessingJob, models.Meeting)
        .join(models.Meeting, models.ProcessingJob.meeting_id == models.Meeting.meeting_id)
        .filter(models.ProcessingJob.status == "queued")
        .order_by(models.ProcessingJob.queued_at.asc())
        .all()
    )

    queued_items = [_job_to_dict(j, m) for j, m in queued]

    return {
        "gpu": {
            "active": gpu_active,
            "queued": [q for q in queued_items if q["worker_type"] == "gpu"],
        },
        "light": {
            "active": light_active,
            "queued": [q for q in queued_items if q["worker_type"] == "light"],
        },
    }


@router.get("/queued")
def backfill_queued(db: Session = Depends(get_db)):
    """Return all queued processing jobs, ordered by queue time."""
    from app import models

    queued = (
        db.query(models.ProcessingJob, models.Meeting)
        .join(models.Meeting, models.ProcessingJob.meeting_id == models.Meeting.meeting_id)
        .filter(models.ProcessingJob.status == "queued")
        .order_by(models.ProcessingJob.queued_at.asc())
        .all()
    )

    return {
        "items": [_job_to_dict(j, m) for j, m in queued],
        "total": len(queued),
    }


@router.delete("/queued/{job_id}")
def backfill_cancel_queued(job_id: str, db: Session = Depends(get_db)):
    """Cancel a queued processing job. Only works for status='queued'."""
    from app import models

    job = db.query(models.ProcessingJob).filter_by(job_id=job_id).first()
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")
    if job.status != "queued":
        from fastapi import HTTPException
        raise HTTPException(400, f"Cannot cancel job with status '{job.status}' — only queued jobs can be cancelled")

    meeting_id = job.meeting_id
    db.delete(job)
    db.commit()

    # Also clear progress.json if it exists
    try:
        pf = MEDIA_DIR / meeting_id / "progress.json"
        if pf.exists():
            pf.unlink()
    except Exception:
        pass

    logger.info("Cancelled queued job %s for meeting %s", job_id, meeting_id)
    return {"cancelled": True, "job_id": job_id, "meeting_id": meeting_id}


@router.post("/queued/{job_id}/prioritize")
def backfill_prioritize(job_id: str, db: Session = Depends(get_db)):
    """Move a queued job to the front of the queue by setting its queued_at to earliest."""
    from app import models

    job = db.query(models.ProcessingJob).filter_by(job_id=job_id).first()
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")
    if job.status != "queued":
        from fastapi import HTTPException
        raise HTTPException(400, f"Cannot prioritize job with status '{job.status}'")

    # Set queued_at to 1 second before the earliest queued job
    earliest = (
        db.query(func.min(models.ProcessingJob.queued_at))
        .filter(models.ProcessingJob.status == "queued")
        .scalar()
    )
    if earliest:
        job.queued_at = earliest - timedelta(seconds=1)
    else:
        job.queued_at = datetime.utcnow()
    db.commit()

    meeting = db.query(models.Meeting).filter_by(meeting_id=job.meeting_id).first()
    logger.info("Prioritized job %s for meeting %s", job_id, job.meeting_id)
    return {"prioritized": True, "job_id": job_id, "title": meeting.title if meeting else job.meeting_id}


# ── Per-meeting progress ──────────────────────────────────────────────────────

@router.get("/progress/{meeting_id}")
def backfill_progress(meeting_id: str, db: Session = Depends(get_db)):
    """Return the latest processing job for a specific meeting (DB query)."""
    from app import models

    job = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.meeting_id == meeting_id)
        .order_by(models.ProcessingJob.queued_at.desc())
        .first()
    )

    if not job:
        # Fallback: read progress.json for meetings that predate this feature
        p = MEDIA_DIR / meeting_id / "progress.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    return {
        "stage": job.stage_label or job.stage,
        "pct": job.pct or 0,
        "detail": job.detail or "",
        "status": job.status,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "error": job.status == "error",
    }


# ── Worker idle check ─────────────────────────────────────────────────────────

@router.get("/worker-idle")
def backfill_worker_idle(db: Session = Depends(get_db)):
    """
    Return True if the Huey queues have no pending tasks and no
    processing_jobs are running in the DB.
    """
    from app import models

    # Check Huey queues for pending tasks
    huey_idle = True
    try:
        from app.worker import huey, huey_light
        pending = huey.pending_count() + huey_light.pending_count()
        huey_idle = pending == 0
    except Exception as exc:
        logger.warning("worker-idle Huey check failed: %s", exc)

    # Also check DB for any running jobs (belt-and-suspenders)
    threshold = datetime.utcnow() - timedelta(seconds=_STUCK_SECONDS)
    db_running = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.status == "running",
            models.ProcessingJob.updated_at >= threshold,
        )
        .count()
    )

    return {"idle": huey_idle and db_running == 0}


# ── SSE events stream ─────────────────────────────────────────────────────────

@router.get("/events")
async def backfill_events():
    """
    Server-Sent Events stream. Polls the processing_jobs DB every 3 seconds
    for changes and pushes updates to connected clients.

    On connect, sends an 'init' event with current status + active job.
    """
    async def generate():
        import asyncio

        # ── Send initial state on connect ──────────────────────────────────
        db = SessionLocal()
        try:
            init_data = _build_init_state(db)
        finally:
            db.close()
        yield f"data: {json.dumps({'type': 'init', **init_data})}\n\n"

        # ── Poll for changes ───────────────────────────────────────────────
        prev_state: dict | None = None
        keepalive_counter = 0

        while True:
            await asyncio.sleep(3)
            keepalive_counter += 1

            try:
                db = SessionLocal()
                try:
                    state = _build_init_state(db)
                finally:
                    db.close()

                # Only send update if something changed
                if state != prev_state:
                    prev_state = state
                    yield f"data: {json.dumps({'type': 'update', **state})}\n\n"
                elif keepalive_counter >= 10:
                    # Send keepalive every ~30s even if no changes
                    keepalive_counter = 0
                    yield ": keepalive\n\n"
            except Exception as exc:
                logger.debug("SSE poll error (non-fatal): %s", exc)
                if keepalive_counter >= 10:
                    keepalive_counter = 0
                    yield ": keepalive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _build_init_state(db: Session) -> dict:
    """Build the initial state payload for SSE connect."""
    from app import models

    # Status counts
    meeting_ids_with_url = {
        r.meeting_id for r in
        db.query(models.Meeting.meeting_id).filter(models.Meeting.video_url.isnot(None)).all()
    }
    meeting_ids_with_media = {
        r.meeting_id for r in
        db.query(models.MediaFile.meeting_id)
        .filter(models.MediaFile.file_type.in_(["video", "audio"]))
        .distinct().all()
    }
    meetings_with_segments_ids = {
        r.meeting_id for r in
        db.query(models.TranscriptSegment.meeting_id).distinct().all()
    }
    all_media_ids = {
        r.meeting_id for r in
        db.query(models.MediaFile.meeting_id)
        .filter(
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
        ).distinct().all()
    }
    process_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            models.Meeting.meeting_id.in_(all_media_ids),
            ~models.Meeting.meeting_id.in_(meetings_with_segments_ids),
        ).count()
    )
    diarize_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            models.Meeting.meeting_id.in_(meetings_with_segments_ids),
        ).count()
    )
    stuck = _count_stuck(db)

    # Active jobs: find one per worker type (GPU and light)
    now = datetime.utcnow()
    running_jobs = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status == "running")
        .order_by(models.ProcessingJob.updated_at.desc())
        .all()
    )
    gpu_active = None
    light_active = None
    active = None  # legacy: first non-stuck job regardless of type
    for candidate in running_jobs:
        timeout = _stage_timeout(db, candidate)
        age = (now - candidate.updated_at).total_seconds() if candidate.updated_at else float("inf")
        if age >= timeout:
            continue
        meeting = db.query(models.Meeting).filter_by(meeting_id=candidate.meeting_id).first()
        job_info = {
            "meeting_id": candidate.meeting_id,
            "stage": candidate.stage_label or candidate.stage,
            "pct": candidate.pct or 0,
            "detail": candidate.detail or "",
            "title": meeting.title if meeting else candidate.meeting_id,
            "group_name": meeting.group_name if meeting else "",
            "meeting_date": meeting.meeting_date if meeting else None,
        }
        if active is None:
            active = job_info  # legacy compat
        wt = _worker_type_for_stage(candidate.stage)
        if wt == "gpu" and gpu_active is None:
            gpu_active = job_info
        elif wt == "light" and light_active is None:
            light_active = job_info

    # Queued jobs count per worker
    queued_jobs = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status == "queued")
        .all()
    )
    gpu_queued = sum(1 for j in queued_jobs if _worker_type_for_stage(j.stage) == "gpu")
    light_queued = sum(1 for j in queued_jobs if _worker_type_for_stage(j.stage) == "light")

    return {
        "status": {
            "download_pending": len(meeting_ids_with_url - meeting_ids_with_media),
            "process_pending": process_pending,
            "diarize_pending": diarize_pending,
            "stuck": stuck,
        },
        "active": active,
        "workers": {
            "gpu": {"active": gpu_active, "queued_count": gpu_queued},
            "light": {"active": light_active, "queued_count": light_queued},
        },
    }


# ── Next Download ─────────────────────────────────────────────────────────────

@router.post("/next-download")
def backfill_next_download(
    group_name: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    """
    Auto-reset stuck meetings, then queue one primegov_download_task for the
    oldest meeting that has a video_url but no downloaded MediaFile.
    Skipped meetings are excluded. Optional group_name/category filters
    let the UI scope "Download All" to a specific show or content type.
    """
    from app import models
    from app.services.task_dispatch import send_task

    reset_summary = _do_reset_stuck(db)
    skipped = _get_skipped()

    existing_media_ids = {
        r.meeting_id for r in
        db.query(models.MediaFile.meeting_id)
        .filter(models.MediaFile.file_type.in_(["video", "audio"]))
        .distinct().all()
    }

    candidates_query = (
        db.query(models.Meeting)
        .filter(models.Meeting.video_url.isnot(None))
    )
    if category:
        candidates_query = candidates_query.filter(models.Meeting.category == category)
    if group_name:
        candidates_query = candidates_query.filter(models.Meeting.group_name == group_name)
    candidates = candidates_query.order_by(models.Meeting.meeting_date).all()

    active_ids = _active_meeting_ids(db)

    for meeting in candidates:
        if meeting.meeting_id in existing_media_ids:
            continue
        if meeting.meeting_id in skipped:
            continue
        if meeting.meeting_id in active_ids:
            continue

        send_task("tasks.primegov_download", kwargs={
            "meeting_id": meeting.meeting_id,
            "download_video": True,
            "download_agenda": False,
            "download_minutes": False,
            "download_packet": False,
            "auto_process": False,
        })
        logger.info("next-download: queued %s (%s)", meeting.meeting_id, meeting.meeting_date)
        return {
            "queued": True,
            "meeting_id": meeting.meeting_id,
            "meeting_date": meeting.meeting_date,
            "title": meeting.title,
            "auto_reset": reset_summary,
        }

    return {"queued": False, "nothing_to_do": True, "auto_reset": reset_summary}


# ── Next Transcode ────────────────────────────────────────────────────────────

@router.post("/next-transcode")
def backfill_next_transcode(db: Session = Depends(get_db)):
    """
    Auto-reset stuck meetings, then queue one transcode_video_task for the
    oldest MediaFile with transcode_status='pending'.
    """
    from app import models
    from app.services.task_dispatch import send_task

    reset_summary = _do_reset_stuck(db)

    result = (
        db.query(models.MediaFile, models.Meeting)
        .join(models.Meeting, models.Meeting.meeting_id == models.MediaFile.meeting_id)
        .filter(models.MediaFile.transcode_status == "pending")
        .order_by(models.Meeting.meeting_date)
        .first()
    )

    if not result:
        return {"queued": False, "nothing_to_do": True, "auto_reset": reset_summary}

    media, meeting = result
    if _is_active(db, media.meeting_id):
        return {
            "queued": False, "skipped": True, "reason": "active",
            "meeting_id": media.meeting_id, "auto_reset": reset_summary,
        }

    send_task("tasks.transcode_video", args=[media.meeting_id, media.media_id])
    logger.info("next-transcode: queued %s / %s", media.meeting_id, media.media_id)
    return {
        "queued": True,
        "meeting_id": media.meeting_id,
        "media_id": media.media_id,
        "meeting_date": meeting.meeting_date,
        "title": meeting.title,
        "auto_reset": reset_summary,
    }


# ── Next Process ──────────────────────────────────────────────────────────────

@router.post("/next-process")
def backfill_next_process(
    group_name: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    """
    Auto-reset stuck meetings, then queue one task for the oldest meeting
    with source media, 0 segments, and processed_at=null.

    Smart routing:
      - Already transcoded / no transcode needed → process_video
      - Needs transcode (status='pending')       → full_ingest (transcode + process)

    Skips: meetings in the skip set, meetings whose file is missing on disk.
    Optional group_name/category filters let the UI scope "Process All"
    to a specific show or content type.
    """
    from app import models
    from app.services.task_dispatch import send_task

    reset_summary = _do_reset_stuck(db)
    skipped = _get_skipped()

    meetings_with_segments_ids = {
        r.meeting_id for r in
        db.query(models.TranscriptSegment.meeting_id).distinct().all()
    }

    candidates_query = db.query(models.Meeting).filter(
        models.Meeting.processed_at.is_(None),
        ~models.Meeting.meeting_id.in_(meetings_with_segments_ids),
    )
    if category:
        candidates_query = candidates_query.filter(models.Meeting.category == category)
    if group_name:
        candidates_query = candidates_query.filter(models.Meeting.group_name == group_name)
    candidates = candidates_query.order_by(models.Meeting.meeting_date).all()

    if candidates:
        candidate_ids = [m.meeting_id for m in candidates]
        # Fetch ALL media (including transcode_status='pending')
        media_rows = (
            db.query(models.MediaFile)
            .filter(
                models.MediaFile.meeting_id.in_(candidate_ids),
                models.MediaFile.file_type.in_(["video", "audio"]),
                ~models.MediaFile.file_path.like("%_extracted.wav"),
            )
            .all()
        )
        media_by_meeting: dict[str, models.MediaFile] = {}
        for row in media_rows:
            if row.meeting_id not in media_by_meeting:
                media_by_meeting[row.meeting_id] = row
    else:
        media_by_meeting = {}

    # Meetings with 3+ errors (any stage) — auto-skip without dispatching
    error_counts = dict(
        db.query(models.ProcessingJob.meeting_id, func.count(models.ProcessingJob.job_id))
        .filter(models.ProcessingJob.status == "error")
        .group_by(models.ProcessingJob.meeting_id)
        .all()
    )
    auto_skip_ids = {mid for mid, cnt in error_counts.items() if cnt >= 3}

    active_ids = _active_meeting_ids(db)

    for meeting in candidates:
        if meeting.meeting_id in skipped:
            continue
        if meeting.meeting_id in active_ids:
            continue
        if meeting.meeting_id in auto_skip_ids:
            continue
        media = media_by_meeting.get(meeting.meeting_id)
        if not media:
            continue
        if not Path(to_absolute(media.file_path)).exists():
            logger.warning("next-process: skipping %s — file missing: %s", meeting.meeting_id, media.file_path)
            continue

        # Route: needs transcode → full_ingest, otherwise → process_video
        if media.transcode_status == "pending":
            send_task("tasks.full_ingest", args=[meeting.meeting_id])
            action = "full_ingest"
        else:
            send_task("tasks.process_video", args=[meeting.meeting_id, media.media_id])
            action = "process"
        logger.info("next-process: queued %s [%s] (%s)", meeting.meeting_id, action, meeting.meeting_date)
        return {
            "queued": True,
            "meeting_id": meeting.meeting_id,
            "media_id": media.media_id,
            "meeting_date": meeting.meeting_date,
            "title": meeting.title,
            "action": action,
            "auto_reset": reset_summary,
        }

    return {"queued": False, "nothing_to_do": True, "auto_reset": reset_summary}


# ── Next Process Newest (dual-worker opposite-end) ───────────────────────────

@router.post("/next-process-newest")
def backfill_next_process_newest(
    group_name: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    """
    Same as next-process but selects the NEWEST unprocessed meeting first.
    Used by Worker 2 in the opposite-end dual-worker strategy: Worker 1
    processes oldest-first, Worker 2 processes newest-first. They converge
    toward the middle without ever colliding.

    Smart routing: needs transcode → full_ingest, otherwise → process_video.
    """
    from app import models
    from app.services.task_dispatch import send_task

    reset_summary = _do_reset_stuck(db)
    skipped = _get_skipped()

    meetings_with_segments_ids = {
        r.meeting_id for r in
        db.query(models.TranscriptSegment.meeting_id).distinct().all()
    }

    candidates_query = db.query(models.Meeting).filter(
        models.Meeting.processed_at.is_(None),
        ~models.Meeting.meeting_id.in_(meetings_with_segments_ids),
    )
    if category:
        candidates_query = candidates_query.filter(models.Meeting.category == category)
    if group_name:
        candidates_query = candidates_query.filter(models.Meeting.group_name == group_name)
    candidates = candidates_query.order_by(models.Meeting.meeting_date.desc()).all()

    if candidates:
        candidate_ids = [m.meeting_id for m in candidates]
        media_rows = (
            db.query(models.MediaFile)
            .filter(
                models.MediaFile.meeting_id.in_(candidate_ids),
                models.MediaFile.file_type.in_(["video", "audio"]),
                ~models.MediaFile.file_path.like("%_extracted.wav"),
            )
            .all()
        )
        media_by_meeting: dict[str, models.MediaFile] = {}
        for row in media_rows:
            if row.meeting_id not in media_by_meeting:
                media_by_meeting[row.meeting_id] = row
    else:
        media_by_meeting = {}

    # Meetings with 3+ errors (any stage) — auto-skip without dispatching
    error_counts = dict(
        db.query(models.ProcessingJob.meeting_id, func.count(models.ProcessingJob.job_id))
        .filter(models.ProcessingJob.status == "error")
        .group_by(models.ProcessingJob.meeting_id)
        .all()
    )
    auto_skip_ids = {mid for mid, cnt in error_counts.items() if cnt >= 3}

    active_ids = _active_meeting_ids(db)

    for meeting in candidates:
        if meeting.meeting_id in skipped:
            continue
        if meeting.meeting_id in active_ids:
            continue
        if meeting.meeting_id in auto_skip_ids:
            continue
        media = media_by_meeting.get(meeting.meeting_id)
        if not media:
            continue
        if not Path(to_absolute(media.file_path)).exists():
            logger.warning("next-process-newest: skipping %s — file missing: %s", meeting.meeting_id, media.file_path)
            continue

        if media.transcode_status == "pending":
            send_task("tasks.full_ingest", args=[meeting.meeting_id])
            action = "full_ingest"
        else:
            send_task("tasks.process_video", args=[meeting.meeting_id, media.media_id])
            action = "process"
        logger.info("next-process-newest: queued %s [%s] (%s)", meeting.meeting_id, action, meeting.meeting_date)
        return {
            "queued": True,
            "meeting_id": meeting.meeting_id,
            "media_id": media.media_id,
            "meeting_date": meeting.meeting_date,
            "title": meeting.title,
            "action": action,
            "auto_reset": reset_summary,
        }

    return {"queued": False, "nothing_to_do": True, "auto_reset": reset_summary}


@router.post("/next-diarize")
def backfill_next_diarize(db: Session = Depends(get_db)):
    """
    Queue one process_video_task for the oldest meeting that has been fully
    transcribed (has transcript segments) but the pipeline hasn't completed
    (processed_at is NULL).

    The pipeline is resumable: since segments already exist it skips
    transcription and continues from diarization → alignment → embedding →
    voiceprint matching.

    Safety checks:
    - Requires the extracted WAV to exist on disk (transcription was done on
      the full audio, so the WAV is by definition complete — no partial audio).
    - Skips meetings that are currently active (already being processed).
    - Skips meetings whose WAV is suspiciously short vs. the source media
      duration (truncated WAV guard).
    """
    from app import models
    from app.services.task_dispatch import send_task
    from app.services.audio_extractor import get_wav_duration, _probe_duration

    reset_summary = _do_reset_stuck(db)
    skipped = _get_skipped()

    # Meetings with segments but not yet complete
    candidates = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            models.Meeting.meeting_id.in_(
                db.query(models.TranscriptSegment.meeting_id).distinct()
            ),
        )
        .order_by(models.Meeting.meeting_date)
        .all()
    )

    active_ids = _active_meeting_ids(db)

    for meeting in candidates:
        if meeting.meeting_id in skipped:
            continue
        if meeting.meeting_id in active_ids:
            continue

        # Find the extracted WAV for this meeting
        wav_record = (
            db.query(models.MediaFile)
            .filter(
                models.MediaFile.meeting_id == meeting.meeting_id,
                models.MediaFile.file_type == "audio",
                models.MediaFile.file_path.like("%_extracted.wav"),
            )
            .first()
        )
        if not wav_record or not Path(to_absolute(wav_record.file_path)).exists():
            logger.warning(
                "next-diarize: skipping %s — extracted WAV missing", meeting.meeting_id
            )
            continue

        # Truncated WAV guard: WAV must be >= 80% of source media duration.
        # If the source media duration is unknown, skip the check.
        source_media = (
            db.query(models.MediaFile)
            .filter(
                models.MediaFile.meeting_id == meeting.meeting_id,
                models.MediaFile.file_type.in_(["video", "audio"]),
                ~models.MediaFile.file_path.like("%_extracted.wav"),
            )
            .first()
        )
        if source_media and Path(to_absolute(source_media.file_path)).exists():
            src_dur = _probe_duration(to_absolute(source_media.file_path))
            wav_dur = get_wav_duration(to_absolute(wav_record.file_path))
            if src_dur > 60 and wav_dur < src_dur * 0.80:
                logger.warning(
                    "next-diarize: skipping %s — WAV %.0fs is only %.0f%% of source %.0fs "
                    "(truncated audio; re-extract before diarizing)",
                    meeting.meeting_id, wav_dur, wav_dur / src_dur * 100, src_dur,
                )
                continue

        # Find the source media ID to pass to the task
        if not source_media:
            logger.warning("next-diarize: skipping %s — no source media record", meeting.meeting_id)
            continue

        send_task("tasks.process_video", args=[meeting.meeting_id, source_media.media_id])
        logger.info("next-diarize: queued %s (%s)", meeting.meeting_id, meeting.meeting_date)
        return {
            "queued": True,
            "meeting_id": meeting.meeting_id,
            "media_id": source_media.media_id,
            "meeting_date": meeting.meeting_date,
            "title": meeting.title,
            "auto_reset": reset_summary,
        }

    return {"queued": False, "nothing_to_do": True, "auto_reset": reset_summary}


# ── Process specific meeting ──────────────────────────────────────────────────

@router.post("/process-now/{meeting_id}")
def backfill_process_now(meeting_id: str, db: Session = Depends(get_db)):
    """Queue processing for a specific meeting immediately.

    Smart routing:
      - If media needs transcode → queue full_ingest (transcode + process)
      - If already transcoded   → queue process_video directly
    """
    from app import models
    from app.services.task_dispatch import send_task
    from fastapi import HTTPException

    # First try: already transcoded or no-transcode-needed media
    # transcode_status can be NULL, empty string, or "transcoded" for ready media
    media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
            models.MediaFile.transcode_status.in_(["transcoded", None, ""]),
        )
        .first()
    )
    if media:
        if not Path(to_absolute(media.file_path)).exists():
            raise HTTPException(status_code=409, detail="Media file not found on disk")
        send_task("tasks.process_video", args=[meeting_id, media.media_id])
        logger.info("process-now: queued process_video %s / %s", meeting_id, media.media_id)
        return {"queued": True, "meeting_id": meeting_id, "media_id": media.media_id,
                "action": "process"}

    # Second try: needs transcode — queue full_ingest (transcode → process)
    media_pending = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
            models.MediaFile.transcode_status == "pending",
        )
        .first()
    )
    if media_pending:
        if not Path(to_absolute(media_pending.file_path)).exists():
            raise HTTPException(status_code=409, detail="Media file not found on disk")
        send_task("tasks.full_ingest", args=[meeting_id])
        logger.info("process-now: queued full_ingest (needs transcode) %s / %s",
                     meeting_id, media_pending.media_id)
        return {"queued": True, "meeting_id": meeting_id, "media_id": media_pending.media_id,
                "action": "full_ingest"}

    raise HTTPException(status_code=404, detail="No eligible media found for this meeting")


# ── Priority processing ───────────────────────────────────────────────────────

def _bump_huey_priority(queue_name: str, priority: float = 100.0) -> bool:
    """Set the most-recently-inserted task in a Huey queue to a higher priority.

    Called immediately after send_task() — the last-inserted row (MAX id) for
    that queue is our task. Race window is negligible (single-threaded uvicorn,
    self-heal queues at most one task per 30 s).

    Returns True if a row was updated.
    """
    import sqlite3
    from app.config import HUEY_DB, HUEY_LIGHT_DB

    db_path = str(HUEY_DB if queue_name == "civic_media" else HUEY_LIGHT_DB)
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        cur = conn.execute(
            "UPDATE task SET priority=? WHERE queue=? AND id=("
            "  SELECT MAX(id) FROM task WHERE queue=?)",
            (priority, queue_name, queue_name),
        )
        conn.commit()
        updated = cur.rowcount > 0
        conn.close()
        return updated
    except Exception as exc:
        logger.warning("_bump_huey_priority failed: %s", exc)
        return False


@router.post("/process-priority/{meeting_id}")
def backfill_process_priority(
    meeting_id: str,
    priority: float = 100.0,
    db: Session = Depends(get_db),
):
    """Queue processing for a specific meeting and jump it to the front of the queue.

    Same routing logic as process-now, but immediately bumps the task's priority
    in the Huey DB so the worker picks it up before all default-priority (0.0) tasks.

    priority param: defaults to 100.0. Higher = picked up sooner. All normal
    backfill tasks have priority 0.0, so anything > 0 jumps the queue.
    """
    from app import models
    from app.services.task_dispatch import send_task
    from fastapi import HTTPException

    media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
            models.MediaFile.transcode_status.in_(["transcoded", None, ""]),
        )
        .first()
    )
    if media:
        if not Path(to_absolute(media.file_path)).exists():
            raise HTTPException(status_code=409, detail="Media file not found on disk")
        send_task("tasks.process_video", args=[meeting_id, media.media_id])
        bumped = _bump_huey_priority("civic_media", priority)
        logger.info("process-priority: queued process_video %s (priority=%.1f, bumped=%s)",
                    meeting_id, priority, bumped)
        return {"queued": True, "meeting_id": meeting_id, "media_id": media.media_id,
                "action": "process", "priority": priority, "jumped_queue": bumped}

    media_pending = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
            models.MediaFile.transcode_status == "pending",
        )
        .first()
    )
    if media_pending:
        if not Path(to_absolute(media_pending.file_path)).exists():
            raise HTTPException(status_code=409, detail="Media file not found on disk")
        send_task("tasks.full_ingest", args=[meeting_id])
        bumped = _bump_huey_priority("civic_media_light", priority)
        logger.info("process-priority: queued full_ingest %s (priority=%.1f, bumped=%s)",
                    meeting_id, priority, bumped)
        return {"queued": True, "meeting_id": meeting_id, "media_id": media_pending.media_id,
                "action": "full_ingest", "priority": priority, "jumped_queue": bumped}

    raise HTTPException(status_code=404, detail="No eligible media found for this meeting")


# ── Full ingest ───────────────────────────────────────────────────────────────

@router.post("/full/{meeting_id}")
def backfill_full(meeting_id: str, db: Session = Depends(get_db)):
    """Queue full_ingest_task (download → transcode → process) for a specific meeting."""
    from app import models
    from app.services.task_dispatch import send_task
    from fastapi import HTTPException

    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    send_task("tasks.full_ingest", args=[meeting_id])
    logger.info("full: queued full_ingest_task for %s", meeting_id)
    return {
        "queued": True,
        "meeting_id": meeting_id,
        "meeting_date": meeting.meeting_date,
        "title": meeting.title,
    }


# ── Skip / Unskip ─────────────────────────────────────────────────────────────

@router.post("/skip/{meeting_id}")
def backfill_skip(meeting_id: str):
    """Add a meeting to the skip set — excluded from next-process auto-queue."""
    _skip(meeting_id)
    return {"skipped": True, "meeting_id": meeting_id}


@router.post("/unskip/{meeting_id}")
def backfill_unskip(meeting_id: str):
    """Remove a meeting from the skip set."""
    _unskip(meeting_id)
    return {"skipped": False, "meeting_id": meeting_id}


# ── Stuck ─────────────────────────────────────────────────────────────────────

@router.get("/stuck")
def backfill_stuck(db: Session = Depends(get_db)):
    """List meetings with stale running/queued jobs, using per-stage timeouts."""
    from app import models

    now = datetime.utcnow()
    active_jobs = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status.in_(["running", "queued"]))
        .all()
    )

    stuck = []
    for job in active_jobs:
        timeout = _stage_timeout(db, job)
        age = (now - job.updated_at).total_seconds() if job.updated_at else float("inf")
        if age < timeout:
            continue
        meeting = db.query(models.Meeting).filter_by(meeting_id=job.meeting_id).first()
        stuck.append({
            "meeting_id": job.meeting_id,
            "stage": job.stage_label or job.stage,
            "pct": job.pct or 0,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "age_minutes": round(age / 60, 1),
            "timeout_minutes": round(timeout / 60, 1),
            "title": meeting.title if meeting else None,
            "meeting_date": meeting.meeting_date if meeting else None,
        })

    stuck.sort(key=lambda x: x["age_minutes"], reverse=True)
    return {"stuck": stuck, "count": len(stuck)}


# ── Reset stuck (manual) ──────────────────────────────────────────────────────

@router.post("/reset-stuck")
def backfill_reset_stuck(db: Session = Depends(get_db)):
    """Manually reset all stuck meetings. Also runs automatically inside every next-* endpoint."""
    result = _do_reset_stuck(db)
    return {
        "reset": result["reset_ids"],
        "count": len(result["reset_ids"]),
        "transcode_resets": result["transcode_resets"],
    }


# ── Clear error history for a specific meeting ────────────────────────────────

@router.post("/clear-errors/{meeting_id}")
def backfill_clear_errors(meeting_id: str, db: Session = Depends(get_db)):
    """
    Delete all error-status jobs (any stage) for a meeting, allowing it to be
    retried.  Also resets running jobs for the meeting to allow immediate retry.
    """
    from app import models
    deleted = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.meeting_id == meeting_id,
            models.ProcessingJob.status.in_(["error", "running"]),
        )
        .all()
    )
    count = len(deleted)
    for job in deleted:
        db.delete(job)
    db.commit()

    # Also clear progress.json so the UI stops showing the stale error
    try:
        pf = MEDIA_DIR / meeting_id / "progress.json"
        if pf.exists():
            pf.unlink()
            logger.info("clear-errors: removed progress.json for %s", meeting_id)
    except Exception:
        pass

    logger.info("clear-errors: deleted %d job(s) for meeting %s", count, meeting_id)
    return {"meeting_id": meeting_id, "cleared": count}


@router.get("/errors")
def backfill_list_errors(db: Session = Depends(get_db)):
    """List all meetings with error-status processing jobs, newest first."""
    from app import models
    jobs = (
        db.query(models.ProcessingJob, models.Meeting)
        .join(models.Meeting, models.ProcessingJob.meeting_id == models.Meeting.meeting_id)
        .filter(models.ProcessingJob.status == "error")
        .order_by(models.Meeting.meeting_date.desc())
        .all()
    )
    items = []
    for job, meeting in jobs:
        items.append({
            "meeting_id": meeting.meeting_id,
            "meeting_date": meeting.meeting_date,
            "title": meeting.title,
            "group_name": meeting.group_name,
            "stage": job.stage,
            "error_message": job.error_msg or job.detail or "",
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "video_url": meeting.video_url,
        })
    return {"items": items, "total": len(items)}


@router.post("/clear-all-errors")
def backfill_clear_all_errors(db: Session = Depends(get_db)):
    """Clear error jobs for ALL meetings at once. Returns count cleared."""
    from app import models
    error_jobs = (
        db.query(models.ProcessingJob)
        .filter(models.ProcessingJob.status == "error")
        .all()
    )
    meeting_ids = set()
    for job in error_jobs:
        meeting_ids.add(job.meeting_id)
        db.delete(job)
    db.commit()

    # Clear progress.json files
    cleared_progress = 0
    for mid in meeting_ids:
        try:
            pf = MEDIA_DIR / mid / "progress.json"
            if pf.exists():
                pf.unlink()
                cleared_progress += 1
        except Exception:
            pass

    logger.info(
        "clear-all-errors: deleted %d jobs across %d meetings, cleared %d progress files",
        len(error_jobs), len(meeting_ids), cleared_progress,
    )
    return {
        "cleared_jobs": len(error_jobs),
        "meetings_affected": len(meeting_ids),
        "progress_files_cleared": cleared_progress,
    }


# ── Self-healing background thread ────────────────────────────────────────────

_self_heal_started = False


def _auto_advance(db: Session) -> None:
    """Refill idle Huey queues from the DB backlog.

    Checks the pending count on each queue AND the DB for running jobs before
    queuing more work. pending_count() drops to 0 when a worker picks up a
    task (before it finishes), so we must check DB state as well to avoid
    queueing multiple tasks while one is already executing.
    """
    from app import models

    try:
        from app.worker import huey, huey_light
        gpu_pending = huey.pending_count()
        light_pending = huey_light.pending_count()
    except Exception as exc:
        logger.warning("auto-advance: Huey check failed: %s", exc)
        return

    now = datetime.utcnow()

    # GPU worker: only queue a process task if the queue is empty AND no
    # process job is queued/running in the DB (belt-and-suspenders).
    if gpu_pending == 0:
        gpu_busy = (
            db.query(models.ProcessingJob)
            .filter(
                models.ProcessingJob.status.in_(["queued", "running"]),
                models.ProcessingJob.updated_at >= now - timedelta(seconds=_STUCK_SECONDS_BY_STAGE["process"]),
            )
            .count()
        ) > 0
        if not gpu_busy:
            result = backfill_next_process(db=db)
            if result.get("queued"):
                logger.info(
                    "auto-advance: queued process for %s (%s)",
                    result.get("meeting_id"), result.get("meeting_date"),
                )

    # Light worker: only queue a download task if the queue is empty AND no
    # download job is queued/running in the DB.
    if light_pending == 0:
        light_busy = (
            db.query(models.ProcessingJob)
            .filter(
                models.ProcessingJob.status.in_(["queued", "running"]),
                models.ProcessingJob.stage == "download",
                models.ProcessingJob.updated_at >= now - timedelta(seconds=_STUCK_SECONDS_BY_STAGE["download"]),
            )
            .count()
        ) > 0
        if not light_busy:
            result = backfill_next_download(db=db)
            if result.get("queued"):
                logger.info(
                    "auto-advance: queued download for %s (%s)",
                    result.get("meeting_id"), result.get("meeting_date"),
                )


def _self_heal_loop():
    """Periodically reset stuck jobs and refill idle queues from the DB backlog.

    Runs in a daemon thread — dies with the process. Uses its own DB session
    (never shares with request handlers). Errors are swallowed and logged.

    Cadence:
      - Every 30 s: check Huey queue depth and auto-advance if idle
      - Every 5 min: reset stuck jobs
    """
    import time
    last_reset = 0.0
    while True:
        time.sleep(_AUTO_ADVANCE_INTERVAL)
        now = time.monotonic()
        try:
            db = SessionLocal()
            try:
                # Reset stuck jobs every 5 minutes
                if now - last_reset >= _SELF_HEAL_INTERVAL:
                    result = _do_reset_stuck(db)
                    reset_ids = result.get("reset_ids", [])
                    tc_resets = result.get("transcode_resets", 0)
                    if reset_ids or tc_resets:
                        logger.info(
                            "Self-heal: reset %d stuck job(s), %d transcode(s): %s",
                            len(reset_ids), tc_resets, reset_ids,
                        )
                    last_reset = now

                # Auto-advance: refill idle queues every tick
                _auto_advance(db)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Self-heal loop error (non-fatal): %s", exc)


def start_self_heal():
    """Start the self-healing background thread (idempotent)."""
    global _self_heal_started
    if _self_heal_started:
        return
    _self_heal_started = True
    t = threading.Thread(target=_self_heal_loop, name="backfill-self-heal", daemon=True)
    t.start()
    logger.info("Self-healing background thread started (interval=%ds)", _SELF_HEAL_INTERVAL)


# Auto-start on module import (happens once when FastAPI loads the router)
start_self_heal()


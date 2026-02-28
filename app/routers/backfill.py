"""
Backfill API — controlled one-at-a-time stage progression.

Each next-* endpoint finds ONE meeting, queues it, and returns immediately.
The UI loops: queue one → wait for worker idle → pause 3s → queue next.
Nothing runs concurrently — the Celery solo pool enforces that at the worker
level, and the UI enforces it at the trigger level.

Each next-* endpoint also auto-resets stuck meetings before selecting a
candidate, so reset-stuck never needs to be called manually.

Endpoints
─────────
GET  /api/backfill/status               Counts: download/transcode/process/diarize pending + stuck
GET  /api/backfill/queue                Paginated list of process-pending meetings
GET  /api/backfill/active               Currently running meeting (from processing_jobs DB)
GET  /api/backfill/progress/{id}        Latest job for a specific meeting
GET  /api/backfill/worker-idle          True if Celery queue + unacked are both empty
GET  /api/backfill/events               SSE stream: real-time progress updates
POST /api/backfill/next-download        Queue one primegov_download_task
POST /api/backfill/next-transcode       Queue one transcode_video_task
POST /api/backfill/next-process         Queue one process_video_task
POST /api/backfill/next-diarize         Queue one process_video_task for transcribed-but-not-diarized meetings
POST /api/backfill/process-now/{id}     Queue process_video_task for a specific meeting
POST /api/backfill/full/{id}            Queue full_ingest_task for a specific meeting
POST /api/backfill/skip/{id}            Add meeting to skip set (excluded from auto-queue)
POST /api/backfill/unskip/{id}          Remove meeting from skip set
GET  /api/backfill/stuck                List stale in-progress meetings (>10 min)
POST /api/backfill/reset-stuck          Reset stuck meetings manually
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.config import MEDIA_DIR, CELERY_BROKER

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backfill", tags=["backfill"])

_STUCK_SECONDS = 600       # 10 min: job updated_at age to be considered stuck
_SKIP_KEY = "backfill:skipped"


# ── DB dependency ─────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Redis skip-set helpers ────────────────────────────────────────────────────

def _redis():
    import redis
    # Force 127.0.0.1 — on Windows, 'localhost' resolves IPv6 first which
    # can hang for 20+ seconds before falling back to IPv4.
    url = CELERY_BROKER.replace("localhost", "127.0.0.1")
    return redis.from_url(url, decode_responses=True,
                          socket_connect_timeout=2, socket_timeout=2)


def _get_skipped() -> set[str]:
    try:
        return _redis().smembers(_SKIP_KEY)
    except Exception:
        return set()


def _skip(meeting_id: str) -> None:
    try:
        _redis().sadd(_SKIP_KEY, meeting_id)
    except Exception as exc:
        logger.warning("skip add failed for %s: %s", meeting_id, exc)


def _unskip(meeting_id: str) -> None:
    try:
        _redis().srem(_SKIP_KEY, meeting_id)
    except Exception as exc:
        logger.warning("skip remove failed for %s: %s", meeting_id, exc)


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
    """True if this meeting has a running (non-stale) processing job."""
    from app import models
    threshold = datetime.utcnow() - timedelta(seconds=_STUCK_SECONDS)
    job = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.meeting_id == meeting_id,
            models.ProcessingJob.status == "running",
            models.ProcessingJob.updated_at >= threshold,
        )
        .first()
    )
    return job is not None


# ── Shared reset logic ────────────────────────────────────────────────────────

def _do_reset_stuck(db: Session) -> dict:
    """
    Reset all stale processing_jobs (running but updated_at too old) and
    reset any MediaFiles stuck at transcode_status='transcoding' → 'pending'.
    Called automatically by every next-* endpoint.
    """
    from app import models

    threshold = datetime.utcnow() - timedelta(seconds=_STUCK_SECONDS)

    # Reset stale running OR queued jobs in DB
    # "queued" jobs that never transitioned are orphaned worker-crash artifacts
    stale_jobs = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.status.in_(["running", "queued"]),
            models.ProcessingJob.updated_at < threshold,
        )
        .all()
    )
    reset_ids = []
    for job in stale_jobs:
        job.status = "error"
        job.error_msg = "Reset by auto-reset-stuck (no progress update in 10+ min)"
        reset_ids.append(job.meeting_id)
        # Also clear progress.json for backward compat
        try:
            pf = MEDIA_DIR / job.meeting_id / "progress.json"
            if pf.exists():
                pf.write_text(json.dumps({"stage": "", "pct": 0}))
        except Exception:
            pass
    if stale_jobs:
        db.commit()

    # Reset stuck transcodes
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

    transcode_pending = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.transcode_status == "pending")
        .count()
    )

    ready_meeting_ids = {
        r.meeting_id for r in
        db.query(models.MediaFile.meeting_id)
        .filter(
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
            models.MediaFile.transcode_status.in_(["transcoded", None]),
        ).distinct().all()
    }
    meetings_with_segments_ids = {
        r.meeting_id for r in
        db.query(models.TranscriptSegment.meeting_id).distinct().all()
    }
    process_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            models.Meeting.meeting_id.in_(ready_meeting_ids),
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

    # Stuck count: running jobs with no update in >10 min (DB query, not filesystem)
    threshold = datetime.utcnow() - timedelta(seconds=_STUCK_SECONDS)
    stuck_count = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.status == "running",
            models.ProcessingJob.updated_at < threshold,
        )
        .count()
    )

    return {
        "download_pending": download_pending,
        "transcode_pending": transcode_pending,
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

    # ── Single DB query for all running jobs ───────────────────────────────
    threshold = datetime.utcnow() - timedelta(seconds=_STUCK_SECONDS)
    running_jobs = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.status == "running",
            models.ProcessingJob.updated_at >= threshold,
        )
        .all()
    )
    # Map: meeting_id → job (for quick lookup)
    active_by_meeting: dict[str, models.ProcessingJob] = {
        j.meeting_id: j for j in running_jobs
    }

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

    all_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            ~models.Meeting.meeting_id.in_(meetings_with_segments_ids),
            models.Meeting.meeting_id.in_(media_by_meeting.keys()),
        )
        .order_by(models.Meeting.meeting_date)
        .all()
    )

    total = len(all_pending)
    page_items = all_pending[offset: offset + limit]

    items = []
    for meeting in page_items:
        media = media_by_meeting.get(meeting.meeting_id)
        job = active_by_meeting.get(meeting.meeting_id)

        items.append({
            "meeting_id": meeting.meeting_id,
            "meeting_date": meeting.meeting_date,
            "group_name": meeting.group_name,
            "title": meeting.title,
            "category": meeting.category or "meeting",
            "media_id": media.media_id if media else None,
            "file_type": media.file_type if media else None,
            "transcode_status": media.transcode_status if media else None,
            "file_exists": bool(media and Path(media.file_path).exists()),
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
    Return True if the Celery worker has no queued or in-flight tasks.
    Also checks processing_jobs for any running jobs as extra safety.
    """
    from app import models

    try:
        import redis as _redis_lib
        url = CELERY_BROKER.replace("localhost", "127.0.0.1")
        r = _redis_lib.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        queue_len = r.llen("celery")
        unacked_len = r.hlen("unacked")
        redis_idle = queue_len == 0 and unacked_len == 0
    except Exception as exc:
        logger.warning("worker-idle Redis check failed: %s", exc)
        redis_idle = True

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

    return {"idle": redis_idle and db_running == 0}


# ── SSE events stream ─────────────────────────────────────────────────────────

@router.get("/events")
async def backfill_events():
    """
    Server-Sent Events stream. Pushes progress updates, job completions,
    and status changes in real time.

    On connect, sends an 'init' event with current status + active job.
    Subsequent events are published via Redis pub/sub by the progress helper.
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

        # ── Subscribe to Redis pub/sub ─────────────────────────────────────
        try:
            import redis.asyncio as aioredis
        except ImportError:
            # Fallback: yield a keepalive every 30s if async Redis isn't available
            logger.warning("redis.asyncio not available — SSE will send keepalives only")
            while True:
                await asyncio.sleep(30)
                yield ": keepalive\n\n"
            return

        url = CELERY_BROKER.replace("localhost", "127.0.0.1")
        try:
            # socket_timeout=None — never time out on idle pub/sub reads.
            # Long jobs (transcription, diarization) can go 10+ minutes without
            # a progress message; a short socket_timeout would kill the stream.
            r = aioredis.from_url(url, socket_connect_timeout=5, socket_timeout=None)
            pubsub = r.pubsub()
            await pubsub.subscribe("civic_media:progress")
        except Exception as exc:
            logger.warning("SSE Redis subscribe failed: %s", exc)
            # Can't subscribe — send keepalives only
            while True:
                await asyncio.sleep(30)
                yield ": keepalive\n\n"
            return

        try:
            # Use get_message() with asyncio.wait_for so we can send SSE keepalives
            # during long silent phases without closing the Redis connection.
            while True:
                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True),
                        timeout=25,
                    )
                except asyncio.TimeoutError:
                    # No progress message for 25s — send SSE keepalive comment
                    yield ": keepalive\n\n"
                    continue
                if message is None:
                    # Subscription acknowledged but no data yet
                    await asyncio.sleep(0.05)
                    continue
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                yield f"data: {data}\n\n"
        finally:
            try:
                await pubsub.unsubscribe("civic_media:progress")
                await r.aclose()
            except Exception:
                pass

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
    transcode_pending = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.transcode_status == "pending")
        .count()
    )
    meetings_with_segments_ids = {
        r.meeting_id for r in
        db.query(models.TranscriptSegment.meeting_id).distinct().all()
    }
    ready_ids = {
        r.meeting_id for r in
        db.query(models.MediaFile.meeting_id)
        .filter(
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
            models.MediaFile.transcode_status.in_(["transcoded", None]),
        ).distinct().all()
    }
    process_pending = (
        db.query(models.Meeting)
        .filter(
            models.Meeting.processed_at.is_(None),
            models.Meeting.meeting_id.in_(ready_ids),
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
    threshold = datetime.utcnow() - timedelta(seconds=_STUCK_SECONDS)
    stuck = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.status == "running",
            models.ProcessingJob.updated_at < threshold,
        ).count()
    )

    # Active job
    job = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.status == "running",
            models.ProcessingJob.updated_at >= threshold,
        )
        .order_by(models.ProcessingJob.updated_at.desc())
        .first()
    )
    active = None
    if job:
        meeting = db.query(models.Meeting).filter_by(meeting_id=job.meeting_id).first()
        active = {
            "meeting_id": job.meeting_id,
            "stage": job.stage_label or job.stage,
            "pct": job.pct or 0,
            "detail": job.detail or "",
            "title": meeting.title if meeting else job.meeting_id,
            "group_name": meeting.group_name if meeting else "",
        }

    return {
        "status": {
            "download_pending": len(meeting_ids_with_url - meeting_ids_with_media),
            "transcode_pending": transcode_pending,
            "process_pending": process_pending,
            "diarize_pending": diarize_pending,
            "stuck": stuck,
        },
        "active": active,
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
    from app.tasks import primegov_download_task

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

    for meeting in candidates:
        if meeting.meeting_id in existing_media_ids:
            continue
        if meeting.meeting_id in skipped:
            continue
        if _is_active(db, meeting.meeting_id):
            continue

        primegov_download_task.delay(
            meeting.meeting_id,
            download_video=True,
            download_agenda=False,
            download_minutes=False,
            download_packet=False,
            auto_process=False,
        )
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
    from app.tasks import transcode_video_task

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

    transcode_video_task.delay(media.meeting_id, media.media_id)
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
    Auto-reset stuck meetings, then queue one process_video_task for the
    oldest meeting with ready source media, 0 segments, and processed_at=null.

    Skips: meetings in the skip set, videos still needing transcode,
           meetings whose file no longer exists on disk.
    Optional group_name/category filters let the UI scope "Process All"
    to a specific show or content type.
    """
    from app import models
    from app.tasks import process_video_task

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
        media_rows = (
            db.query(models.MediaFile)
            .filter(
                models.MediaFile.meeting_id.in_(candidate_ids),
                models.MediaFile.file_type.in_(["video", "audio"]),
                ~models.MediaFile.file_path.like("%_extracted.wav"),
                models.MediaFile.transcode_status.in_(["transcoded", None]),
            )
            .all()
        )
        media_by_meeting: dict[str, models.MediaFile] = {}
        for row in media_rows:
            if row.meeting_id not in media_by_meeting:
                media_by_meeting[row.meeting_id] = row
    else:
        media_by_meeting = {}

    for meeting in candidates:
        if meeting.meeting_id in skipped:
            continue
        media = media_by_meeting.get(meeting.meeting_id)
        if not media:
            continue
        if not Path(media.file_path).exists():
            logger.warning("next-process: skipping %s — file missing: %s", meeting.meeting_id, media.file_path)
            continue
        if _is_active(db, meeting.meeting_id):
            continue

        process_video_task.delay(meeting.meeting_id, media.media_id)
        logger.info("next-process: queued %s (%s)", meeting.meeting_id, meeting.meeting_date)
        return {
            "queued": True,
            "meeting_id": meeting.meeting_id,
            "media_id": media.media_id,
            "meeting_date": meeting.meeting_date,
            "title": meeting.title,
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
    from app.tasks import process_video_task
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

    for meeting in candidates:
        if meeting.meeting_id in skipped:
            continue
        if _is_active(db, meeting.meeting_id):
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
        if not wav_record or not Path(wav_record.file_path).exists():
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
        if source_media and Path(source_media.file_path).exists():
            src_dur = _probe_duration(source_media.file_path)
            wav_dur = get_wav_duration(wav_record.file_path)
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

        process_video_task.delay(meeting.meeting_id, source_media.media_id)
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
    """Queue process_video_task for a specific meeting immediately."""
    from app import models
    from app.tasks import process_video_task
    from fastapi import HTTPException

    media = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_type.in_(["video", "audio"]),
            ~models.MediaFile.file_path.like("%_extracted.wav"),
            models.MediaFile.transcode_status.in_(["transcoded", None]),
        )
        .first()
    )
    if not media:
        raise HTTPException(status_code=404, detail="No eligible media found for this meeting")
    if not Path(media.file_path).exists():
        raise HTTPException(status_code=409, detail="Media file not found on disk")

    process_video_task.delay(meeting_id, media.media_id)
    logger.info("process-now: queued %s / %s", meeting_id, media.media_id)
    return {"queued": True, "meeting_id": meeting_id, "media_id": media.media_id}


# ── Full ingest ───────────────────────────────────────────────────────────────

@router.post("/full/{meeting_id}")
def backfill_full(meeting_id: str, db: Session = Depends(get_db)):
    """Queue full_ingest_task (download → transcode → process) for a specific meeting."""
    from app import models
    from app.tasks import full_ingest_task
    from fastapi import HTTPException

    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    full_ingest_task.delay(meeting_id)
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
    """List meetings with stale running jobs (DB query — no filesystem scan)."""
    from app import models

    threshold = datetime.utcnow() - timedelta(seconds=_STUCK_SECONDS)
    stale_jobs = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.status == "running",
            models.ProcessingJob.updated_at < threshold,
        )
        .all()
    )

    now = datetime.utcnow()
    stuck = []
    for job in stale_jobs:
        age = (now - job.updated_at).total_seconds() if job.updated_at else 0
        meeting = db.query(models.Meeting).filter_by(meeting_id=job.meeting_id).first()
        stuck.append({
            "meeting_id": job.meeting_id,
            "stage": job.stage_label or job.stage,
            "pct": job.pct or 0,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "age_minutes": round(age / 60, 1),
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
    Delete all error-status process jobs for a meeting, allowing it to be
    retried after the crash counter (3 failures) has blocked it.
    Also resets running jobs for the meeting to allow immediate retry.
    """
    from app import models
    deleted = (
        db.query(models.ProcessingJob)
        .filter(
            models.ProcessingJob.meeting_id == meeting_id,
            models.ProcessingJob.stage == "process",
            models.ProcessingJob.status.in_(["error", "running"]),
        )
        .all()
    )
    count = len(deleted)
    for job in deleted:
        db.delete(job)
    db.commit()
    logger.info("clear-errors: deleted %d process job(s) for meeting %s", count, meeting_id)
    return {"meeting_id": meeting_id, "cleared": count}

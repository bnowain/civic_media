"""
Celery worker entry point.
Import this module to get a configured Celery application instance.
"""

import logging
from datetime import datetime, timedelta

from celery import Celery
from celery.signals import celeryd_init, worker_ready

from app.config import CELERY_BACKEND, CELERY_BROKER, ORPHAN_RECOVERY_SECONDS

logger = logging.getLogger(__name__)

celery_app = Celery(
    "civic_media",
    broker=CELERY_BROKER,
    backend=CELERY_BACKEND,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # One task at a time per worker — ML models are memory-intensive
    worker_concurrency=1,
    # Acknowledge tasks only after completion (safer for long jobs)
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Keep results for 24 hours
    result_expires=86400,
    # Disable cluster protocols — we run a single solo worker.
    # mingle broadcasts "who's there?" and waits for replies from ghost
    # worker registrations left in Redis by previous killed workers,
    # adding 3+ minutes to every startup. gossip is the continuous
    # version of the same protocol. Neither serves any purpose here.
    worker_enable_mingle=False,
    worker_enable_gossip=False,
    # Hard time limit: kill tasks that hang for more than 2 hours.
    # Soft limit at 110 min gives the task a chance to write an error
    # to progress.json before the hard kill fires.
    # (Solo pool uses thread-based soft limits and process signals for hard.)
    task_time_limit=7200,        # 2 hours — hard kill
    task_soft_time_limit=6600,   # 110 minutes — raises SoftTimeLimitExceeded
    # Cancel running tasks if the broker connection drops.
    # Without this, a task keeps running but can never ack/report back,
    # creating a zombie that blocks the worker indefinitely.
    worker_cancel_long_running_tasks_on_connection_loss=True,
    # Auto-reconnect to broker after connection loss (Celery 5.3+)
    broker_connection_retry_on_startup=True,
    # ── Queue routing ────────────────────────────────────────────────────
    # Light tasks (I/O-bound, no GPU) go to the "light" queue.
    # Heavy tasks (GPU-bound) stay on the default "celery" queue.
    # GPU workers consume from "celery" only; the light worker consumes
    # from "light" only.  This prevents ingest/download/transcode from
    # waiting behind multi-hour transcription jobs.
    task_routes={
        "tasks.ingest_radio":       {"queue": "light"},
        "tasks.primegov_discover":  {"queue": "light"},
        "tasks.primegov_download":  {"queue": "light"},
        "tasks.transcode_video":    {"queue": "light"},
        "tasks.retag_content":      {"queue": "light"},
        "tasks.export_clip":        {"queue": "light"},
        "tasks.cleanup_clips":      {"queue": "light"},
        "tasks.full_ingest":        {"queue": "light"},
    },
)


@celeryd_init.connect
def flush_ghost_bindings(sender=None, **kwargs):
    """Flush stale Kombu binding keys and orphaned unacked tasks on startup.

    Two housekeeping operations run before the worker accepts any new work:

    1. Kombu binding keys — every killed worker leaves routing-key registrations
       in Redis without deregistering.  We wipe them so they never accumulate.

    2. Orphaned unacked tasks — if the worker was killed while tasks were
       in-flight, those tasks stay in the Redis `unacked` hash forever.  On the
       next startup the worker will immediately start re-processing them.  For
       GPU-heavy tasks (Whisper + pyannote + SpeechBrain) this causes system-wide
       lag as multiple multi-GB models load at once.  We detect orphans by
       cross-checking `unacked` against the processing_jobs DB: if unacked > 0
       but no job has a recent 'running' status, the tasks are orphaned and safe
       to discard.  The backfill UI's next-* endpoints will re-queue legitimate
       work deliberately.
    """
    try:
        import redis
        # Short timeout — this must never delay startup or count as a crash.
        url = CELERY_BROKER.replace("localhost", "127.0.0.1")
        r = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)

        # ── 1. Kombu ghost bindings ────────────────────────────────────────
        keys = [
            "_kombu.binding.celeryev",
            "_kombu.binding.celery.pidbox",
            "_kombu.binding.reply.celery.pidbox",
        ]
        deleted = r.delete(*keys)
        if deleted:
            logger.info("Flushed %d stale Kombu binding key(s) from Redis", deleted)

        # ── 2. Orphaned unacked tasks ──────────────────────────────────────
        unacked_count = r.hlen("unacked")
        if unacked_count > 0:
            # Check if any job in the DB has a recent 'running' status.
            # If none do, the unacked entries are orphans from a previous session.
            has_active = False
            try:
                from app.database import SessionLocal
                from app.models import ProcessingJob
                threshold = datetime.utcnow() - timedelta(seconds=ORPHAN_RECOVERY_SECONDS)
                db = SessionLocal()
                try:
                    has_active = (
                        db.query(ProcessingJob)
                        .filter(
                            ProcessingJob.status == "running",
                            ProcessingJob.updated_at >= threshold,
                        )
                        .count()
                    ) > 0
                finally:
                    db.close()
            except Exception as dbe:
                logger.warning("flush_ghost_bindings DB check failed: %s", dbe)

            if not has_active:
                r.delete("unacked", "unacked_index")
                logger.info(
                    "Flushed %d orphaned unacked task(s) from Redis "
                    "(no active processing_jobs found — safe to discard).",
                    unacked_count,
                )
            else:
                logger.info(
                    "Found %d unacked task(s) in Redis with active DB jobs — "
                    "leaving for Celery to redeliver.",
                    unacked_count,
                )

        r.close()
    except Exception as exc:
        logger.warning("Could not flush ghost bindings (non-fatal): %s", exc)


def _requeue_meeting(meeting_id: str, stage: str, reason: str) -> bool:
    """Look up source media for meeting_id and dispatch process_video_task.
    Returns True if successfully queued."""
    try:
        from app.database import SessionLocal
        from app import models

        db = SessionLocal()
        try:
            source_media = (
                db.query(models.MediaFile)
                .filter(
                    models.MediaFile.meeting_id == meeting_id,
                    models.MediaFile.file_type.in_(["video", "audio"]),
                    ~models.MediaFile.file_path.like("%_extracted.wav"),
                )
                .first()
            )
            if source_media:
                from app.services.task_dispatch import send_task
                send_task("tasks.process_video", args=[meeting_id, source_media.media_id])
                logger.info("Self-heal [%s]: re-queued %s (%s)", reason, meeting_id, stage)
                return True
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Self-heal requeue failed for %s: %s", meeting_id, exc)
    return False


@worker_ready.connect
def recover_orphaned_tasks(sender=None, **kwargs):
    """
    On worker startup, mark ALL active (running/queued) processing_jobs as error.

    Rationale: if the worker just started, NO task should be running or queued
    in the DB — any such jobs are orphans from a previous crash. The backfill
    system's next-* endpoints and self-healing loop will re-discover and re-queue
    legitimate work automatically.

    This is the nuclear option that guarantees zero stuck jobs survive a restart.
    """
    try:
        from app.database import SessionLocal
        from app.models import ProcessingJob

        db = SessionLocal()
        try:
            orphans = (
                db.query(ProcessingJob)
                .filter(ProcessingJob.status.in_(["running", "queued"]))
                .all()
            )

            if not orphans:
                return

            now = datetime.utcnow()
            for job in orphans:
                job.status = "error"
                job.error_msg = "Worker restart — all in-flight jobs reset"
                job.completed_at = now
                job.updated_at = now
            db.commit()

            logger.info(
                "Self-heal on startup: reset %d orphaned job(s) "
                "(backfill system will re-queue as needed)",
                len(orphans),
            )
        finally:
            db.close()

    except Exception as exc:
        logger.warning("recover_orphaned_tasks failed (non-fatal): %s", exc)

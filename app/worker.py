"""
Celery worker entry point.
Import this module to get a configured Celery application instance.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from celery import Celery
from celery.signals import celeryd_init, worker_ready

from app.config import CELERY_BACKEND, CELERY_BROKER, MEDIA_DIR, ORPHAN_RECOVERY_SECONDS

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
)


@celeryd_init.connect
def flush_ghost_bindings(sender=None, **kwargs):
    """Flush stale Kombu binding keys left by previously killed workers.

    Every killed worker leaves its routing-key registrations in Redis without
    deregistering. These accumulate over time but are harmless with mingle/gossip
    disabled. We wipe them on every startup so they never build up. The new
    worker re-registers itself immediately after this runs.
    """
    try:
        import redis
        # Short timeout — this must never delay startup or count as a crash.
        # Any failure is non-fatal: the flush is housekeeping, not required.
        r = redis.from_url(CELERY_BROKER, socket_connect_timeout=2, socket_timeout=2)
        keys = [
            "_kombu.binding.celeryev",
            "_kombu.binding.celery.pidbox",
            "_kombu.binding.reply.celery.pidbox",
        ]
        deleted = r.delete(*keys)
        if deleted:
            logger.info("Flushed %d stale Kombu binding key(s) from Redis", deleted)
        r.close()
    except Exception as exc:
        logger.warning("Could not flush ghost bindings (non-fatal): %s", exc)


@worker_ready.connect
def recover_orphaned_tasks(sender=None, **kwargs):
    """
    On worker startup, scan for meetings whose progress.json indicates
    they were mid-pipeline when the worker died. Re-queue them.
    """
    if not MEDIA_DIR.exists():
        return

    now = datetime.now(timezone.utc)
    recovered = 0

    for progress_file in MEDIA_DIR.glob("*/progress.json"):
        try:
            data = json.loads(progress_file.read_text())
        except Exception:
            continue

        stage = data.get("stage", "")
        pct = data.get("pct", 0)
        is_error = data.get("error", False)

        # Skip completed, error, or not-yet-started tasks
        if stage in ("Complete", "Error", "") or pct == 100 or is_error:
            continue

        # Check staleness via updated_at
        updated_at_str = data.get("updated_at")
        if not updated_at_str:
            continue

        try:
            updated_at = datetime.fromisoformat(updated_at_str)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        age_seconds = (now - updated_at).total_seconds()
        if age_seconds < ORPHAN_RECOVERY_SECONDS:
            continue

        # This task is stale — find the meeting and re-queue
        meeting_id = progress_file.parent.name

        try:
            from app.database import SessionLocal
            from app import models

            db = SessionLocal()
            try:
                # Find source media (not extracted audio)
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
                    from app.tasks import process_video_task
                    process_video_task.delay(meeting_id, source_media.media_id)
                    recovered += 1
                    logger.info(
                        "Orphan recovery: re-queued meeting %s (was at '%s' %d%%, stale %.0fs)",
                        meeting_id, stage, pct, age_seconds,
                    )
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Orphan recovery failed for meeting %s: %s", meeting_id, exc)

    if recovered:
        logger.info("Orphan recovery: re-queued %d stale tasks", recovered)

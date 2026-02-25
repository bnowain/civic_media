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


# How many days after a meeting's date before a missing file is an error (vs "not available yet")
_NOT_AVAILABLE_GRACE_DAYS = 5


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
                from app.tasks import process_video_task
                process_video_task.delay(meeting_id, source_media.media_id)
                logger.info("Self-heal [%s]: re-queued %s (%s)", reason, meeting_id, stage)
                return True
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Self-heal requeue failed for %s: %s", meeting_id, exc)
    return False


def _mark_not_available(progress_file: Path, meeting_id: str) -> None:
    """Write 'Not available yet' status to progress.json."""
    try:
        data = {
            "stage": "Not available yet",
            "pct": 0,
            "detail": "Recording not yet posted — will retry automatically",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        progress_file.write_text(json.dumps(data))
        logger.info("Self-heal [not_available]: marked %s as not available yet", meeting_id)
    except Exception as exc:
        logger.warning("Could not write not-available status for %s: %s", meeting_id, exc)


@worker_ready.connect
def recover_orphaned_tasks(sender=None, **kwargs):
    """
    On worker startup, scan all meeting progress.json files and self-heal:

    1. Stale mid-pipeline (existing behaviour):
       Stage is in-progress, updated_at is stale → re-queue.

    2. Transcode complete but never transcribed:
       Stage = "Transcode complete", pct = 100, file exists, stale → re-queue.
       (Pipeline was killed right after FFmpeg finished.)

    3. Error with no file — missing download:
       Stage = "Error", file missing on disk:
         - Meeting date within _NOT_AVAILABLE_GRACE_DAYS days → mark "Not available yet"
         - Older → re-queue (attempt re-download via process_video_task)

    4. "Not available yet" that has aged out:
       Stage = "Not available yet", meeting date > grace period → re-queue.
    """
    if not MEDIA_DIR.exists():
        return

    now = datetime.now(timezone.utc)
    recovered = 0
    marked_unavailable = 0

    for progress_file in MEDIA_DIR.glob("*/progress.json"):
        try:
            data = json.loads(progress_file.read_text())
        except Exception:
            continue

        stage = data.get("stage", "")
        pct = data.get("pct", 0)
        meeting_id = progress_file.parent.name

        # ── Case 1 & 2: in-progress or transcode-complete ─────────────────────
        is_mid_pipeline = stage not in ("Complete", "Error", "Not available yet", "") and not (
            stage == "Transcode complete" and pct == 100
        )
        is_transcode_done = stage == "Transcode complete" and pct == 100

        if is_mid_pipeline or is_transcode_done:
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
                continue  # still active

            if _requeue_meeting(meeting_id, stage, "stale" if is_mid_pipeline else "transcode_done"):
                recovered += 1
            continue

        # ── Case 3: Error with no file ─────────────────────────────────────────
        if stage == "Error":
            try:
                from app.database import SessionLocal
                from app import models

                db = SessionLocal()
                try:
                    row = (
                        db.query(models.Meeting.meeting_date, models.MediaFile.file_path, models.MediaFile.media_id)
                        .join(models.MediaFile, models.MediaFile.meeting_id == models.Meeting.meeting_id)
                        .filter(
                            models.Meeting.meeting_id == meeting_id,
                            models.MediaFile.file_type.in_(["video", "audio"]),
                            ~models.MediaFile.file_path.like("%_extracted.wav"),
                        )
                        .first()
                    )
                    if not row:
                        continue
                    meeting_date_str, file_path, media_id = row

                    # If the file already exists on disk, just re-queue (error was spurious)
                    if file_path and Path(file_path).exists():
                        if _requeue_meeting(meeting_id, stage, "error_file_exists"):
                            recovered += 1
                        continue

                    # File missing — check grace period
                    try:
                        from datetime import date
                        mdate = date.fromisoformat(str(meeting_date_str)[:10])
                        age_days = (now.date() - mdate).days
                    except Exception:
                        age_days = 999

                    if age_days <= _NOT_AVAILABLE_GRACE_DAYS:
                        _mark_not_available(progress_file, meeting_id)
                        marked_unavailable += 1
                    else:
                        # Old enough — attempt re-download
                        if _requeue_meeting(meeting_id, stage, "error_retry"):
                            recovered += 1
                finally:
                    db.close()
            except Exception as exc:
                logger.warning("Self-heal error-check failed for %s: %s", meeting_id, exc)
            continue

        # ── Case 4: "Not available yet" that has aged out ─────────────────────
        if stage == "Not available yet":
            try:
                from app.database import SessionLocal
                from app import models

                db = SessionLocal()
                try:
                    meeting = (
                        db.query(models.Meeting)
                        .filter(models.Meeting.meeting_id == meeting_id)
                        .first()
                    )
                    if not meeting:
                        continue
                    try:
                        from datetime import date
                        mdate = date.fromisoformat(str(meeting.meeting_date)[:10])
                        age_days = (now.date() - mdate).days
                    except Exception:
                        age_days = 0

                    if age_days > _NOT_AVAILABLE_GRACE_DAYS:
                        if _requeue_meeting(meeting_id, stage, "not_available_aged"):
                            recovered += 1
                finally:
                    db.close()
            except Exception as exc:
                logger.warning("Self-heal not-available check failed for %s: %s", meeting_id, exc)

    if recovered:
        logger.info("Self-heal: re-queued %d meeting(s)", recovered)
    if marked_unavailable:
        logger.info("Self-heal: marked %d meeting(s) as 'not available yet'", marked_unavailable)

"""
Huey task queue entry points.

Two SqliteHuey instances:
  huey       — GPU-heavy tasks (transcription, diarization, embedding, voiceprint)
  huey_light — I/O-bound tasks (download, transcode, ingest, clips)

Both use SQLite backends in database/ — no external service required.

Worker commands:
  huey_consumer app.worker.huey       -w 1 -k thread   (GPU worker)
  huey_consumer app.worker.huey_light -w 1 -k thread   (light worker)
"""

import logging
import os
import sys
from datetime import datetime

from huey import SqliteHuey, signals

from app.config import BASE_DIR, ORPHAN_RECOVERY_SECONDS

logger = logging.getLogger(__name__)


def _set_below_normal_priority():
    """Set worker process priority to below-normal so it never starves the desktop."""
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
            logger.info("Worker priority set to BELOW_NORMAL")
        else:
            os.nice(5)
            logger.info("Worker niceness increased by 5")
    except Exception as exc:
        logger.warning("Could not set process priority (non-fatal): %s", exc)


_set_below_normal_priority()

_huey_db = str(BASE_DIR / "database" / "huey.db")
_huey_light_db = str(BASE_DIR / "database" / "huey_light.db")

huey = SqliteHuey("civic_media", filename=_huey_db)
huey_light = SqliteHuey("civic_media_light", filename=_huey_light_db)


def _recover_orphaned_jobs():
    """Mark ALL active (running/queued) processing_jobs as error on startup.

    If the worker just started, NO task should be running — any such jobs are
    orphans from a previous crash. The backfill system's next-* endpoints will
    re-discover and re-queue legitimate work.
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
                "Self-heal on startup: reset %d orphaned job(s)",
                len(orphans),
            )
        finally:
            db.close()
    except Exception as exc:
        logger.warning("recover_orphaned_jobs failed (non-fatal): %s", exc)


# Run orphan recovery when this module is first imported by a worker process.
# The huey_consumer imports this module at startup, so this runs once before
# any tasks are consumed.
_recover_orphaned_jobs()

# Import tasks so their @huey.task() / @huey_light.task() decorators register
# them in the Huey task registry. Without this, the consumer can't find tasks.
import app.tasks  # noqa: F401, E402

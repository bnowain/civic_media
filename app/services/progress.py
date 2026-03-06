"""
Centralized progress tracking helper.

Writes to BOTH the processing_jobs DB table (authoritative state) AND
progress.json (backward compat for existing media/news/clip endpoints).
Also publishes to Redis pub/sub for the SSE stream.

Public API
──────────
  create_job(db, meeting_id, stage, celery_task_id=None) -> ProcessingJob
  update_progress(db, meeting_id, stage_label, pct, detail="")
  complete_job(db, meeting_id)
  fail_job(db, meeting_id, error_msg)

Design notes
────────────
- "Active job" is found by meeting_id + status IN ('queued', 'running').
  There should be at most one at a time; if multiple exist (shouldn't happen)
  the most-recently-queued one wins.
- If no active job is found (e.g. called from a legacy code path), DB write
  is silently skipped and only progress.json is written.
- Redis publish is non-critical: wrapped in try/except, always fire-and-forget.
- All file writes use encoding='utf-8' (CLAUDE.md §14 — Windows cp1252 default breaks Unicode).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import CELERY_BROKER, MEDIA_DIR

logger = logging.getLogger(__name__)

_REDIS_CHANNEL = "civic_media:progress"


# ── Redis pub/sub (fire-and-forget) ──────────────────────────────────────────

def _redis_sync():
    """Return a short-timeout synchronous Redis client, or None on failure."""
    try:
        import redis
        url = CELERY_BROKER.replace("localhost", "127.0.0.1")
        return redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    except Exception:
        return None


def _publish(event_type: str, meeting_id: str, **kwargs) -> None:
    """Publish a progress event to Redis pub/sub. Non-critical — never raises."""
    try:
        r = _redis_sync()
        if r is None:
            return
        payload = json.dumps({"type": event_type, "meeting_id": meeting_id, **kwargs},
                             default=str)
        r.publish(_REDIS_CHANNEL, payload)
        r.close()
    except Exception as exc:
        logger.debug("progress publish failed (non-fatal): %s", exc)


# ── progress.json writer (backward compat) ───────────────────────────────────

def _write_json(meeting_id: str, stage: str, pct: int,
                detail: str = "", error: bool = False) -> None:
    """Write media/{meeting_id}/progress.json for legacy endpoints."""
    p = MEDIA_DIR / meeting_id / "progress.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "stage": stage,
            "pct": pct,
            "detail": detail,
            "error": error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write progress.json for %s: %s", meeting_id, exc)


# ── Active-job lookup ────────────────────────────────────────────────────────

def _active_job(db: Session, meeting_id: str):
    """Return the active (queued or running) ProcessingJob for a meeting, or None."""
    try:
        from app.models import ProcessingJob
        return (
            db.query(ProcessingJob)
            .filter(
                ProcessingJob.meeting_id == meeting_id,
                ProcessingJob.status.in_(["queued", "running"]),
            )
            .order_by(ProcessingJob.queued_at.desc())
            .first()
        )
    except Exception as exc:
        logger.warning("_active_job lookup failed for %s: %s", meeting_id, exc)
        return None


# ── Public API ───────────────────────────────────────────────────────────────

def create_job(
    db: Session,
    meeting_id: str,
    stage: str,
    celery_task_id: Optional[str] = None,
):
    """
    Insert a new ProcessingJob row with status='queued', or return an existing
    queued/running job for the same meeting_id (idempotent, prevents orphans).

    Three cases:
    1. Existing "queued" job → reuse it (update stage/task_id).
    2. Existing "running" job → absorb it (task retry or re-queue scenario;
       the old attempt is dead, this is the new one taking over).
    3. No active job → create a new one.

    This prevents the #1 cause of stuck jobs: a task retry creating a new
    "queued" job while the old "running" job is never cleaned up.
    """
    from app.models import ProcessingJob

    now = datetime.utcnow()

    # Find ANY active (queued or running) job for this meeting
    existing = (
        db.query(ProcessingJob)
        .filter(
            ProcessingJob.meeting_id == meeting_id,
            ProcessingJob.status.in_(["queued", "running"]),
        )
        .order_by(ProcessingJob.queued_at.desc())
        .first()
    )
    if existing:
        if existing.status == "running":
            logger.info(
                "create_job: absorbing orphaned running job %s for %s "
                "(was stage=%s, now stage=%s)",
                existing.job_id, meeting_id, existing.stage, stage,
            )
        existing.stage = stage
        existing.status = "queued"
        if celery_task_id:
            existing.celery_task_id = celery_task_id
        existing.updated_at = now
        existing.error_msg = None
        db.commit()
        db.refresh(existing)
        logger.debug("create_job: reused existing job %s for %s/%s",
                      existing.job_id, meeting_id, stage)
        return existing

    job = ProcessingJob(
        meeting_id=meeting_id,
        stage=stage,
        status="queued",
        celery_task_id=celery_task_id,
        pct=0,
        queued_at=now,
        updated_at=now,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    _write_json(meeting_id, f"Queued ({stage})", 0)
    _publish("status", meeting_id, status="queued", stage=stage)
    logger.debug("create_job: %s/%s job_id=%s", meeting_id, stage, job.job_id)
    return job


def update_progress(
    db: Session,
    meeting_id: str,
    stage_label: str,
    pct: int,
    detail: str = "",
) -> None:
    """
    Update the active job for this meeting.
    Transitions queued→running on first call; updates pct/stage_label/detail.
    Also writes progress.json and publishes SSE event.
    """
    job = _active_job(db, meeting_id)
    if job is not None:
        try:
            if job.status == "queued":
                job.status = "running"
                job.started_at = datetime.utcnow()
            job.pct = pct
            job.stage_label = stage_label
            job.detail = detail
            job.updated_at = datetime.utcnow()
            db.commit()
        except Exception as exc:
            logger.warning("update_progress DB write failed for %s: %s", meeting_id, exc)
            try:
                db.rollback()
            except Exception:
                pass

    # Always write progress.json (backward compat, even if no active job)
    _write_json(meeting_id, stage_label, pct, detail)
    _publish("progress", meeting_id, stage_label=stage_label, pct=pct, detail=detail)


def complete_job(db: Session, meeting_id: str) -> None:
    """Mark the active job as done (status='done', pct=100, completed_at=now)."""
    job = _active_job(db, meeting_id)
    if job is not None:
        try:
            job.status = "done"
            job.pct = 100
            job.stage_label = "Complete"
            job.completed_at = datetime.utcnow()
            job.updated_at = datetime.utcnow()
            db.commit()
        except Exception as exc:
            logger.warning("complete_job DB write failed for %s: %s", meeting_id, exc)
            try:
                db.rollback()
            except Exception:
                pass

    _write_json(meeting_id, "Complete", 100)
    _publish("complete", meeting_id, pct=100)


def fail_job(db: Session, meeting_id: str, error_msg: str) -> None:
    """Mark the active job as failed (status='error', error_msg set).

    Resilient to dirty session state: if the session has been rolled back
    or is otherwise broken, falls back to a fresh session to ensure the
    job is marked as error. A stuck "running" job is the worst outcome —
    we must prevent it even if the original session is dead.
    """
    def _do_fail(session: Session) -> bool:
        job = _active_job(session, meeting_id)
        if job is None:
            return False
        try:
            job.status = "error"
            job.error_msg = str(error_msg)[:1000]
            job.completed_at = datetime.utcnow()
            job.updated_at = datetime.utcnow()
            session.commit()
            return True
        except Exception as exc:
            logger.warning("fail_job DB write failed for %s: %s", meeting_id, exc)
            try:
                session.rollback()
            except Exception:
                pass
            return False

    # Try with the caller's session first
    if not _do_fail(db):
        # Fallback: fresh session (handles post-rollback dirty state)
        try:
            from app.database import SessionLocal
            fresh = SessionLocal()
            try:
                _do_fail(fresh)
            finally:
                fresh.close()
        except Exception as exc:
            logger.warning("fail_job fallback session failed for %s: %s", meeting_id, exc)

    _write_json(meeting_id, "Error", 0, str(error_msg)[:500], error=True)
    _publish("error", meeting_id, error_msg=str(error_msg)[:500])

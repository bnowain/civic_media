"""
Task dispatch — calls Huey task functions by name.

Keeps the same send_task(task_name, args, kwargs) API so all existing callers
work unchanged. Internally maps string task names to the actual Huey-decorated
functions, which handle serialization and queue push via SqliteHuey.
"""

import logging

logger = logging.getLogger(__name__)

# Lazy import to avoid circular imports at module level.
# Task functions are imported on first call.
_task_map: dict | None = None


def _get_task_map() -> dict:
    global _task_map
    if _task_map is not None:
        return _task_map

    from app.tasks import (
        process_video_task,
        process_pdf_task,
        extract_multi_voiceprints_task,
        rerun_voiceprints_task,
        process_newscast_task,
        retag_content_task,
        ingest_radio_task,
        transcode_video_task,
        primegov_discover_task,
        primegov_download_task,
        export_clip_task,
        cleanup_clips_task,
        full_ingest_task,
        check_minutes_task,
    )

    _task_map = {
        "tasks.process_video": process_video_task,
        "tasks.process_pdf": process_pdf_task,
        "tasks.extract_multi_voiceprints": extract_multi_voiceprints_task,
        "tasks.rerun_voiceprints": rerun_voiceprints_task,
        "tasks.process_newscast": process_newscast_task,
        "tasks.retag_content": retag_content_task,
        "tasks.ingest_radio": ingest_radio_task,
        "tasks.transcode_video": transcode_video_task,
        "tasks.primegov_discover": primegov_discover_task,
        "tasks.primegov_download": primegov_download_task,
        "tasks.export_clip": export_clip_task,
        "tasks.cleanup_clips": cleanup_clips_task,
        "tasks.full_ingest": full_ingest_task,
        "tasks.check_minutes": check_minutes_task,
    }
    return _task_map


# Tasks that take meeting_id as first arg and should get a queued ProcessingJob
# immediately on dispatch (so the UI can show them before the worker picks them up).
# Maps task_name → initial stage name.
_MEETING_TASK_STAGES = {
    "tasks.process_video":      "process",
    "tasks.full_ingest":        "download",
    "tasks.transcode_video":    "transcode",
    "tasks.primegov_download":  "download",
}


def _create_queued_job(task_name: str, args: list | None, kwargs: dict | None,
                       task_id: str) -> None:
    """Create a 'queued' ProcessingJob so the task is visible before the worker runs it."""
    stage = _MEETING_TASK_STAGES.get(task_name)
    if not stage:
        return  # Not a meeting-processing task

    meeting_id = (args[0] if args else None) or (kwargs or {}).get("meeting_id")
    if not meeting_id:
        return

    try:
        from app.database import SessionLocal
        from app.services.progress import create_job
        db = SessionLocal()
        try:
            create_job(db, meeting_id, stage, task_id=task_id)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Pre-create queued job failed for %s (non-fatal): %s", meeting_id, exc)


def send_task(task_name: str, args: list | None = None, kwargs: dict | None = None,
              queue: str | None = None) -> str:
    """Dispatch a task by name. Returns the task ID (UUID string).

    Creates a 'queued' ProcessingJob immediately for meeting-processing tasks
    so the UI can show them before the worker picks them up.

    The queue parameter is ignored — Huey routes tasks based on which
    instance they're registered with (huey vs huey_light).
    """
    task_map = _get_task_map()
    task_fn = task_map.get(task_name)
    if task_fn is None:
        raise ValueError(f"Unknown task: {task_name}")

    # Call the Huey task function — this enqueues it and returns a Result handle.
    result = task_fn(*(args or []), **(kwargs or {}))

    # result.id is the Huey task ID
    task_id = result.id if result else "unknown"

    # Create a visible "queued" job in the DB immediately
    _create_queued_job(task_name, args, kwargs, str(task_id))

    logger.info("Dispatched %s [%s]", task_name, task_id)
    return str(task_id)

"""
Video transcoder — 540p downscale via ffmpeg.

Runs as a background thread from the API endpoint so it works
without Celery. Writes progress to media/{meeting_id}/progress.json
for the UI to poll.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _probe_duration(file_path: str) -> float | None:
    """Get media duration in seconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             file_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _write_progress(meeting_id: str, stage: str, pct: int, detail: str = "", error: bool = False):
    """Thin wrapper: delegates to the centralized progress helper (DB + file + pub/sub)."""
    from app.database import SessionLocal
    from app.services.progress import update_progress, fail_job
    db = SessionLocal()
    try:
        if error:
            fail_job(db, meeting_id, detail or stage)
        else:
            update_progress(db, meeting_id, stage, pct, detail)
    except Exception:
        pass
    finally:
        db.close()


def _auto_dispatch_pipeline(meeting_id: str, media_id: str) -> None:
    """Dispatch the processing pipeline via Celery after a successful transcode."""
    try:
        from app.tasks import process_video_task
        from app.services.worker_manager import ensure_worker
        ensure_worker()
        process_video_task.delay(meeting_id, media_id)
        logger.info("Auto-dispatched processing pipeline for %s", meeting_id)
    except Exception as exc:
        logger.warning(
            "Could not auto-dispatch pipeline for %s: %s — "
            "click Process in the UI to start manually.",
            meeting_id, exc,
        )


def run_transcode(meeting_id: str, media_id: str, auto_process: bool = True) -> dict:
    """
    Transcode a video to 540p (960x540) using ffmpeg.
    Deletes the original file on success and updates MediaFile.

    If auto_process is True (default), automatically dispatches the
    processing pipeline via Celery after a successful transcode.

    Designed to run in a background thread — creates its own DB session.
    """
    from app.database import SessionLocal
    from app import models

    db = SessionLocal()
    try:
        media = db.query(models.MediaFile).filter_by(media_id=media_id).first()
        if not media:
            _write_progress(meeting_id, "Error", 0, "MediaFile not found", error=True)
            return {"error": "MediaFile not found", "status": "error"}

        original_path = Path(media.file_path)

        # Build output path: same dir, add _540p suffix
        stem = original_path.stem
        out_path = original_path.parent / f"{stem}_540p.mp4"

        if not original_path.exists():
            # Source file is gone — check if a valid 540p already exists on disk.
            # This happens when transcode completed (or was done externally) but
            # the DB record was never updated (e.g. crash after file deletion).
            if out_path.exists() and out_path.stat().st_size > 10_000:
                out_dur = _probe_duration(str(out_path))
                if out_dur:
                    logger.info(
                        "Source missing for %s but valid 540p found — recovering DB record",
                        meeting_id,
                    )
                    media.file_path = str(out_path)
                    media.transcode_status = "transcoded"
                    media.duration = out_dur
                    db.commit()
                    _write_progress(meeting_id, "Transcode complete", 100,
                                    f"540p recovered: {out_path.stat().st_size / (1024*1024):.0f} MB")
                    if auto_process:
                        _auto_dispatch_pipeline(meeting_id, media_id)
                    return {"meeting_id": meeting_id, "status": "transcoded", "recovered": True}
            # No recovery possible — reset status so the button stays clickable
            media.transcode_status = "pending"
            db.commit()
            _write_progress(meeting_id, "Error", 0, "Source file not found", error=True)
            return {"error": "Source file not found", "status": "error"}

        # Check if a valid 540p already exists (e.g. previous run created it
        # but crashed before updating the DB — common with WinError 32).
        if out_path.exists() and out_path.stat().st_size > 10_000:
            src_dur = _probe_duration(str(original_path))
            out_dur = _probe_duration(str(out_path))
            if src_dur and out_dur and abs(src_dur - out_dur) < 5.0:
                logger.info(
                    "Valid 540p already exists for %s (%.1fs vs %.1fs) — "
                    "skipping transcode, updating DB",
                    meeting_id, out_dur, src_dur,
                )
                # Try to delete original
                import time as _time
                for attempt in range(4):
                    try:
                        original_path.unlink()
                        break
                    except PermissionError:
                        if attempt < 3:
                            _time.sleep(2 * (attempt + 1))
                        else:
                            logger.warning(
                                "Could not delete original %s — clean up manually.",
                                original_path.name,
                            )

                media.file_path = str(out_path)
                media.transcode_status = "transcoded"
                if out_dur:
                    media.duration = out_dur
                db.commit()

                new_size = out_path.stat().st_size
                _write_progress(meeting_id, "Transcode complete", 100,
                                f"540p already existed: {new_size / (1024*1024):.0f} MB")

                if auto_process:
                    _auto_dispatch_pipeline(meeting_id, media_id)

                return {"meeting_id": meeting_id, "status": "transcoded",
                        "original_size": original_path.stat().st_size if original_path.exists() else 0,
                        "new_size": new_size}

        media.transcode_status = "transcoding"
        db.commit()

        _write_progress(meeting_id, "Transcoding to 540p", 5, f"Source: {original_path.name}")

        cmd = [
            "ffmpeg", "-y",
            "-progress", "pipe:1", "-nostats",
            "-i", str(original_path),
            "-vf", "scale=-2:540",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ]

        # Get source duration for progress calculation
        duration_sec = media.duration
        if not duration_sec:
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet",
                     "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1",
                     str(original_path)],
                    capture_output=True, text=True, timeout=30,
                )
                if probe.returncode == 0 and probe.stdout.strip():
                    duration_sec = float(probe.stdout.strip())
            except Exception:
                pass
        duration_us = (duration_sec or 3600) * 1_000_000

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        for line in proc.stdout:
            decoded = line.decode("utf-8", errors="ignore").strip()
            if decoded.startswith("out_time_us="):
                val = decoded.split("=", 1)[1].strip()
                if val.isdigit() and int(val) > 0 and duration_us > 0:
                    pct = min(95, int(int(val) / duration_us * 100))
                    _write_progress(meeting_id, "Transcoding to 540p", pct, f"{pct}% complete")

        proc.wait(timeout=7200)

        if proc.returncode != 0:
            media.transcode_status = "pending"
            db.commit()
            _write_progress(meeting_id, "Transcode failed", 0, "ffmpeg error", error=True)
            return {"error": "ffmpeg failed", "status": "error"}

        if not out_path.exists() or out_path.stat().st_size < 1000:
            media.transcode_status = "pending"
            db.commit()
            _write_progress(meeting_id, "Transcode failed", 0, "Output file too small", error=True)
            return {"error": "Output file invalid", "status": "error"}

        # Get new duration
        new_duration = None
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 str(out_path)],
                capture_output=True, text=True, timeout=30,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                new_duration = float(probe.stdout.strip())
        except Exception:
            pass

        # Delete original, update record
        original_size = original_path.stat().st_size
        new_size = out_path.stat().st_size

        # Try to delete the original — retry with backoff on file-lock errors.
        # If deletion still fails, proceed anyway (540p is valid, original
        # can be cleaned up later).
        import time as _time
        deleted_original = False
        for attempt in range(4):
            try:
                original_path.unlink()
                deleted_original = True
                break
            except PermissionError:
                if attempt < 3:
                    _time.sleep(2 * (attempt + 1))
                else:
                    logger.warning(
                        "Could not delete original %s (file locked) — "
                        "540p is valid, proceeding anyway. Clean up manually.",
                        original_path.name,
                    )

        media.file_path = str(out_path)
        media.transcode_status = "transcoded"
        if new_duration:
            media.duration = new_duration
        db.commit()

        _write_progress(meeting_id, "Transcode complete", 100,
                        f"540p: {new_size / (1024*1024):.0f} MB "
                        f"(was {original_size / (1024*1024):.0f} MB, "
                        f"saved {(1 - new_size/original_size)*100:.0f}%)")

        logger.info("Transcoded %s: %s -> %s (%.0f MB -> %.0f MB)",
                     meeting_id, original_path.name, out_path.name,
                     original_size / (1024*1024), new_size / (1024*1024))

        # Auto-dispatch processing pipeline if requested
        if auto_process:
            _auto_dispatch_pipeline(meeting_id, media_id)

        return {"meeting_id": meeting_id, "status": "transcoded",
                "original_size": original_size, "new_size": new_size}

    except Exception as exc:
        db.rollback()
        logger.exception("Transcode failed for meeting %s", meeting_id)
        try:
            media = db.query(models.MediaFile).filter_by(media_id=media_id).first()
            if media:
                media.transcode_status = "pending"
                db.commit()
        except Exception:
            pass
        _write_progress(meeting_id, "Transcode failed", 0, str(exc)[:200], error=True)
        return {"meeting_id": meeting_id, "status": "error", "error": str(exc)[:500]}
    finally:
        db.close()

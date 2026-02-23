"""
Video transcoder — 540p downscale via ffmpeg.

Runs as a background thread from the API endpoint so it works
without Celery. Writes progress to media/{meeting_id}/progress.json
for the UI to poll.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.config import MEDIA_DIR

logger = logging.getLogger(__name__)


def _write_progress(meeting_id: str, stage: str, pct: int, detail: str = "", error: bool = False):
    p = MEDIA_DIR / meeting_id / "progress.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "stage": stage,
            "pct": pct,
            "detail": detail,
            "error": error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception:
        pass


def run_transcode(meeting_id: str, media_id: str) -> dict:
    """
    Transcode a video to 540p (960x540) using ffmpeg.
    Deletes the original file on success and updates MediaFile.

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
        if not original_path.exists():
            _write_progress(meeting_id, "Error", 0, "Source file not found", error=True)
            return {"error": "Source file not found", "status": "error"}

        media.transcode_status = "transcoding"
        db.commit()

        # Build output path: same dir, add _540p suffix
        stem = original_path.stem
        out_path = original_path.parent / f"{stem}_540p.mp4"

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
        original_path.unlink()

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

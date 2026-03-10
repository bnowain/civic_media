"""
Granicus asset downloader.

Unlike Shasta County / PrimeGov (which requires Playwright + HLS extraction),
Granicus provides direct MP4 URLs at archive-video.granicus.com.  These can
be fed straight to ffmpeg — no headless browser needed.

Video download:   ffmpeg -i {mp4_url} -c copy  → full-res MP4
Transcode:        ffmpeg -i full_res -vf scale=-2:540 → _540p.mp4
Documents:        httpx streaming download of DocumentViewer.php PDF URLs
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time as _time
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import MEDIA_DIR, DOCUMENTS_DIR
from app.models import Meeting, MediaFile, Document
from app.paths import to_relative

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 3600   # 1 hr total ffmpeg budget
_STALL_TIMEOUT = 300       # 5 min with no progress tick → kill


def _cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _get_duration(file_path: str) -> Optional[float]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


def download_video(
    db: Session,
    meeting: Meeting,
    output_dir: Path,
    print_progress: bool = False,
) -> dict:
    """
    Download a Granicus meeting video using ffmpeg (direct MP4 URL, no Playwright).

    Returns {status: "complete"|"skipped"|"error", file_path?, media_id?, error?}
    """
    if not meeting.video_url:
        return {"status": "error", "error": "No video_url on meeting record"}

    # Check if already downloaded
    existing = (
        db.query(MediaFile)
        .filter_by(meeting_id=meeting.meeting_id)
        .filter(MediaFile.file_type.in_(["video", "audio"]))
        .first()
    )
    if existing:
        logger.info("[%s] Video already downloaded — skipping", meeting.meeting_id)
        return {"status": "skipped", "detail": "Already downloaded", "file_path": existing.file_path}

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[^\w\s-]', '', meeting.title)[:60].strip().replace(' ', '_')
    output_path = output_dir / f"{meeting.meeting_date}_{safe_title}.mp4"

    if print_progress:
        print(f"    Downloading: {output_path.name} …")

    progress_file = Path(tempfile.mktemp(suffix=".txt", prefix="ffprog_"))
    stderr_file = Path(tempfile.mktemp(suffix=".txt", prefix="ffstderr_"))

    cmd = [
        "ffmpeg", "-y",
        "-progress", str(progress_file), "-nostats",
        "-i", meeting.video_url,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        str(output_path),
    ]

    try:
        with open(stderr_file, "w", encoding="utf-8", errors="replace") as stderr_fh:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=stderr_fh,
            )

        last_tick_time = _time.monotonic()
        start_time = last_tick_time
        file_pos = 0

        while proc.poll() is None:
            elapsed = _time.monotonic() - start_time
            if elapsed > _DOWNLOAD_TIMEOUT:
                proc.kill(); proc.wait(timeout=10)
                _cleanup(progress_file, stderr_file)
                return {"status": "error", "error": "Download timed out (1hr)"}

            if _time.monotonic() - last_tick_time > _STALL_TIMEOUT:
                proc.kill(); proc.wait(timeout=10)
                _cleanup(progress_file, stderr_file)
                return {"status": "error", "error": "Download stalled (5min no progress)"}

            _time.sleep(5)

            try:
                with open(progress_file, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(file_pos)
                    new_data = f.read()
                    file_pos = f.tell()
                for line in new_data.splitlines():
                    if line.startswith("out_time_us="):
                        val = line.split("=", 1)[1].strip()
                        if val.isdigit() and int(val) > 0:
                            last_tick_time = _time.monotonic()
                            if print_progress:
                                sec = int(val) / 1_000_000
                                print(f"    … {int(sec)}s downloaded", end="\r", flush=True)
            except FileNotFoundError:
                pass

        proc.wait(timeout=60)
        _cleanup(progress_file)

        if proc.returncode != 0:
            stderr_tail = ""
            try:
                stderr_tail = stderr_file.read_text(encoding="utf-8", errors="ignore")[-500:]
            except Exception:
                pass
            _cleanup(stderr_file)
            return {"status": "error", "error": f"ffmpeg failed: {stderr_tail[-200:]}"}

        _cleanup(stderr_file)

    except Exception as exc:
        _cleanup(progress_file, stderr_file)
        return {"status": "error", "error": str(exc)}

    if not output_path.exists() or output_path.stat().st_size < 1000:
        return {"status": "error", "error": "Output file missing or too small"}

    duration = _get_duration(str(output_path))

    media_file = MediaFile(
        meeting_id=meeting.meeting_id,
        file_type="video",
        file_path=to_relative(str(output_path)),
        duration=duration,
        transcode_status="pending",
    )
    db.add(media_file)
    meeting.media_directory = to_relative(str(output_dir))
    db.flush()

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "[%s] Downloaded %s (%.1f MB, %.0fs)",
        meeting.meeting_id, output_path.name, size_mb, duration or 0,
    )
    if print_progress:
        print(f"\n    OK Downloaded {output_path.name} ({size_mb:.0f} MB)")

    return {
        "status": "complete",
        "media_id": media_file.media_id,
        "file_path": str(output_path),
        "duration": duration,
    }


def transcode_video(
    db: Session,
    media_file: MediaFile,
    output_dir: Path,
    print_progress: bool = False,
) -> dict:
    """
    Transcode a downloaded Granicus video to 540p in-place.

    Produces {stem}_540p.mp4 alongside the original.  Updates the MediaFile
    record to point to the 540p file and sets transcode_status="transcoded".
    """
    from app.paths import to_absolute
    original_path = Path(to_absolute(media_file.file_path))

    if "_540p" in original_path.stem:
        media_file.transcode_status = "transcoded"
        db.flush()
        return {"status": "already_transcoded", "file_path": str(original_path)}

    if not original_path.exists():
        return {"status": "error", "error": f"Source file not found: {original_path}"}

    out_path = original_path.parent / f"{original_path.stem}_540p.mp4"

    if print_progress:
        print(f"    Transcoding to 540p: {out_path.name} …")

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

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        # Read duration for progress calculation
        duration_us = 0
        if media_file.duration:
            duration_us = int(media_file.duration * 1_000_000)

        last_pct = 0
        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if line.startswith("out_time_us="):
                val = line.split("=", 1)[1].strip()
                if val.isdigit() and int(val) > 0 and duration_us > 0:
                    pct = min(99, int(int(val) / duration_us * 100))
                    if pct > last_pct + 5 and print_progress:
                        print(f"    … {pct}%", end="\r", flush=True)
                        last_pct = pct

        proc.wait(timeout=7200)

        if proc.returncode != 0:
            return {"status": "error", "error": f"ffmpeg transcode returned {proc.returncode}"}

    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        return {"status": "error", "error": str(exc)}

    if not out_path.exists() or out_path.stat().st_size < 1000:
        return {"status": "error", "error": "Transcoded file missing or too small"}

    # Update MediaFile to point to 540p file; delete original to save space
    old_path = original_path
    media_file.file_path = to_relative(str(out_path))
    media_file.transcode_status = "transcoded"
    db.flush()

    try:
        old_path.unlink()
    except Exception:
        pass

    size_mb = out_path.stat().st_size / (1024 * 1024)
    logger.info(
        "[%s] Transcoded to 540p: %s (%.1f MB)",
        media_file.meeting_id, out_path.name, size_mb,
    )
    if print_progress:
        print(f"\n    OK Transcoded to {out_path.name} ({size_mb:.0f} MB)")

    return {
        "status": "complete",
        "file_path": str(out_path),
    }


def download_document(
    db: Session,
    meeting: Meeting,
    doc_type: str,
    print_progress: bool = False,
) -> dict:
    """
    Download a PDF document (agenda or minutes) for a Granicus meeting.

    The URL is the resolved DocumentViewer.php URL (no redirect chain needed).
    Creates a Document record and queues OCR via task queue.

    doc_type: "agenda" | "minutes"
    """
    url_map = {"agenda": meeting.agenda_url, "minutes": meeting.minutes_url}
    url = url_map.get(doc_type)
    if not url:
        return {"status": "skipped", "detail": f"No {doc_type}_url on meeting"}

    # Skip if already downloaded
    existing = db.query(Document).filter_by(
        meeting_id=meeting.meeting_id, document_type=doc_type
    ).first()
    if existing:
        return {"status": "skipped", "detail": f"{doc_type} already downloaded"}

    docs_dir = DOCUMENTS_DIR / meeting.meeting_id
    docs_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[^\w\s-]', '', meeting.title)[:60].strip().replace(' ', '_')
    filename = f"{meeting.meeting_date}_{safe_title}_{doc_type}.pdf"
    output_path = docs_dir / filename

    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            content = r.content
    except httpx.HTTPError as exc:
        logger.error("[%s] Failed to download %s: %s", meeting.meeting_id, doc_type, exc)
        return {"status": "error", "error": str(exc)}

    # Verify it looks like a PDF
    if not content.startswith(b"%PDF") and len(content) < 500:
        return {"status": "error", "error": f"Response doesn't look like a PDF ({len(content)} bytes)"}

    output_path.write_bytes(content)

    doc = Document(
        meeting_id=meeting.meeting_id,
        document_type=doc_type,
        file_path=to_relative(str(output_path)),
    )
    db.add(doc)
    db.flush()

    size_kb = output_path.stat().st_size / 1024
    logger.info(
        "[%s] Downloaded %s: %s (%.1f KB)",
        meeting.meeting_id, doc_type, filename, size_kb,
    )
    if print_progress:
        print(f"    OK {doc_type.capitalize()}: {filename} ({size_kb:.0f} KB)")

    # Queue OCR (non-blocking — workers must be running)
    try:
        from app.services.task_dispatch import send_task
        send_task("tasks.process_pdf", args=[doc.document_id])
    except Exception as exc:
        logger.debug("OCR task queue unavailable (non-fatal): %s", exc)

    return {
        "status": "complete",
        "document_id": doc.document_id,
        "file_path": str(output_path),
    }

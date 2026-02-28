"""
PrimeGov asset downloader.

- Video: Playwright extracts m3u8 URL from Swagit page → ffmpeg copies to MP4
- Documents: httpx downloads compiled PDF from PrimeGov
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import MEDIA_DIR, DOCUMENTS_DIR
from app.models import Meeting, MediaFile, Document

logger = logging.getLogger(__name__)

M3U8_PATTERN = re.compile(
    r"https?://archive-stream\.granicus\.com[^\s\"']+playlist\.m3u8"
)

# Grace period: if a meeting is within this many days of its scheduled date,
# a download failure means the recording isn't posted yet ("Not available yet"),
# not a hard error. After this window, errors are real errors.
_NOT_AVAILABLE_GRACE_DAYS = 5


def _within_grace_period(meeting_date, grace_days: int = _NOT_AVAILABLE_GRACE_DAYS) -> bool:
    """Return True if the meeting's date is within the grace window from today."""
    if meeting_date is None:
        return False
    try:
        if isinstance(meeting_date, datetime):
            md = meeting_date.date()
        elif isinstance(meeting_date, date):
            md = meeting_date
        else:
            md = date.fromisoformat(str(meeting_date)[:10])
        delta = (date.today() - md).days
        return delta <= grace_days
    except Exception:
        return False


def _write_progress(meeting_id: str, stage: str, pct: int = 0, detail: str = "", error: bool = False) -> None:
    """Write download progress — delegates to centralized helper (DB + file + pub/sub)."""
    from app.services.progress import update_progress, fail_job
    # downloader.py has a db session available at the call site; use a short-lived one here
    # since we can't easily thread db through all call sites without refactoring.
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        if error:
            fail_job(db, meeting_id, detail or stage)
        else:
            update_progress(db, meeting_id, stage, pct, detail)
    except Exception as exc:
        logger.warning("Could not write progress for %s: %s", meeting_id, exc)
    finally:
        db.close()


def extract_m3u8_url(swagit_url: str) -> Optional[str]:
    """
    Load a Swagit video page in headless Chromium and extract the HLS m3u8 URL.
    Returns the m3u8 URL or None if not found.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "Playwright not installed. Run: pip install playwright && playwright install chromium"
        )
        return None

    logger.info("Extracting m3u8 from Swagit page: %s", swagit_url)

    try:
        import threading

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            m3u8_url = None
            found_event = threading.Event()

            # Intercept network requests to catch the m3u8 URL
            def handle_request(request):
                nonlocal m3u8_url
                if "playlist.m3u8" in request.url:
                    m3u8_url = request.url
                    found_event.set()

            page.on("request", handle_request)

            # Use domcontentloaded — don't wait for networkidle (Swagit
            # analytics keep firing and cause 30s+ timeouts)
            page.goto(swagit_url, wait_until="domcontentloaded", timeout=30000)

            # Wait up to 15s for the m3u8 request to fire
            if not m3u8_url:
                page.wait_for_timeout(15000)

            # If not caught via network, try page content
            if not m3u8_url:
                content = page.content()
                match = M3U8_PATTERN.search(content)
                if match:
                    m3u8_url = match.group(0)

            # Also check inline scripts
            if not m3u8_url:
                scripts = page.evaluate(
                    "() => Array.from(document.querySelectorAll('script'))"
                    ".map(s => s.textContent).join('\\n')"
                )
                match = M3U8_PATTERN.search(scripts)
                if match:
                    m3u8_url = match.group(0)

            browser.close()

            if m3u8_url:
                logger.info("Found m3u8 URL: %s", m3u8_url)
            else:
                logger.warning("No m3u8 URL found in Swagit page: %s", swagit_url)

            return m3u8_url

    except Exception as exc:
        logger.exception("Failed to extract m3u8 from %s: %s", swagit_url, exc)
        return None


def download_video(db: Session, meeting_id: str) -> dict:
    """
    Download video for a meeting.

    1. Look up meeting's video_url (Swagit page)
    2. Extract m3u8 HLS stream URL via Playwright
    3. Download with ffmpeg (-c copy, fast)
    4. Create MediaFile record

    Returns result dict.
    """
    meeting = db.query(Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        return {"error": "Meeting not found", "status": "error"}

    if not meeting.video_url:
        return {"error": "No video URL available", "status": "error"}

    # Check if video already downloaded
    existing_media = (
        db.query(MediaFile)
        .filter_by(meeting_id=meeting_id)
        .filter(MediaFile.file_type.in_(["video", "audio"]))
        .first()
    )
    if existing_media:
        return {"status": "skipped", "detail": "Video already downloaded"}

    _write_progress(meeting_id, "Extracting video URL...", 10)

    # Extract m3u8 URL
    m3u8_url = extract_m3u8_url(meeting.video_url)
    if not m3u8_url:
        if _within_grace_period(meeting.meeting_date):
            _write_progress(meeting_id, "Not available yet", 0,
                            "Recording not posted yet — will retry automatically")
            logger.info("[%s] Video not available yet (meeting is recent, within grace period)", meeting_id)
            return {"error": "Video not yet available", "status": "not_available_yet"}
        _write_progress(meeting_id, "Error", 0, "Could not extract video stream URL", error=True)
        return {"error": "Could not extract m3u8 URL", "status": "error"}

    # Prepare output path
    media_dir = MEDIA_DIR / meeting_id
    media_dir.mkdir(parents=True, exist_ok=True)

    # Use meeting date and sanitized title for filename
    safe_title = re.sub(r'[^\w\s-]', '', meeting.title)[:60].strip().replace(' ', '_')
    output_filename = f"{meeting.meeting_date}_{safe_title}.mp4"
    output_path = media_dir / output_filename

    _write_progress(meeting_id, "Downloading video...", 20, "ffmpeg stream copy")

    # Download with ffmpeg.
    # Video is stream-copied (fast), but audio is re-encoded to clean AAC-LC.
    # This prevents the "HLS segment boundary AAC profile mismatch" corruption that
    # occurs when segments with different AAC configurations (band limits, sample
    # rate headers) are concatenated with -c copy: the resulting MP4 plays in
    # most players but causes ffmpeg's audio decoder to fail mid-extraction,
    # producing a truncated WAV and a pipeline crash on the process step.
    cmd = [
        "ffmpeg", "-y",
        "-i", m3u8_url,
        "-c:v", "copy",         # Video: stream copy (no re-encode, fast)
        "-c:a", "aac",          # Audio: re-encode to clean AAC-LC
        "-b:a", "128k",         # Match typical HLS audio bitrate
        str(output_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max for long meetings
        )
        if proc.returncode != 0:
            logger.error("ffmpeg failed: %s", proc.stderr[-500:] if proc.stderr else "")
            if _within_grace_period(meeting.meeting_date):
                _write_progress(meeting_id, "Not available yet", 0,
                                "Stream download failed — recording may not be ready yet")
                return {"error": "ffmpeg failed (within grace period)", "status": "not_available_yet"}
            _write_progress(meeting_id, "Error", 0, "ffmpeg download failed", error=True)
            return {"error": "ffmpeg failed", "status": "error"}
    except subprocess.TimeoutExpired:
        _write_progress(meeting_id, "Error", 0, "Download timed out", error=True)
        return {"error": "Download timed out", "status": "error"}

    if not output_path.exists() or output_path.stat().st_size < 1000:
        _write_progress(meeting_id, "Error", 0, "Downloaded file too small or missing", error=True)
        return {"error": "Downloaded file invalid", "status": "error"}

    # Get duration
    duration = _get_duration(str(output_path))

    # Upsert MediaFile record — update if a video record already exists
    # (handles retried downloads without creating duplicate records).
    media_file = (
        db.query(MediaFile)
        .filter_by(meeting_id=meeting_id, file_type="video")
        .first()
    )
    if media_file:
        media_file.file_path = str(output_path)
        media_file.duration = duration
        media_file.transcode_status = "pending"
    else:
        media_file = MediaFile(
            meeting_id=meeting_id,
            file_type="video",
            file_path=str(output_path),
            duration=duration,
            transcode_status="pending",
        )
        db.add(media_file)

    # Set media directory on meeting
    meeting.media_directory = str(media_dir)
    db.commit()

    _write_progress(meeting_id, "Video downloaded", 100, f"{output_filename}")

    logger.info(
        "Downloaded video for meeting %s: %s (%.1f MB)",
        meeting_id, output_filename,
        output_path.stat().st_size / (1024 * 1024),
    )
    return {
        "status": "complete",
        "media_id": media_file.media_id,
        "file_path": str(output_path),
        "duration": duration,
    }


def download_document(
    db: Session,
    meeting_id: str,
    doc_type: str = "agenda",
) -> dict:
    """
    Download a PDF document (agenda or minutes) for a meeting.

    Args:
        db: Database session
        meeting_id: Meeting ID
        doc_type: "agenda" or "minutes"

    Returns result dict.
    """
    meeting = db.query(Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        return {"error": "Meeting not found", "status": "error"}

    url_map = {"agenda": meeting.agenda_url, "minutes": meeting.minutes_url, "packet": meeting.packet_url}
    url = url_map.get(doc_type)
    if not url:
        return {"error": f"No {doc_type} URL available", "status": "error"}

    # Check if already downloaded
    existing_doc = (
        db.query(Document)
        .filter_by(meeting_id=meeting_id, document_type=doc_type)
        .first()
    )
    if existing_doc:
        return {"status": "skipped", "detail": f"{doc_type} already downloaded"}

    # Download PDF
    docs_dir = DOCUMENTS_DIR / meeting_id
    docs_dir.mkdir(parents=True, exist_ok=True)

    safe_title = re.sub(r'[^\w\s-]', '', meeting.title)[:60].strip().replace(' ', '_')
    filename = f"{meeting.meeting_date}_{safe_title}_{doc_type}.pdf"
    output_path = docs_dir / filename

    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            output_path.write_bytes(r.content)
    except httpx.HTTPError as exc:
        logger.error("Failed to download %s for %s: %s", doc_type, meeting_id, exc)
        return {"error": f"Download failed: {exc}", "status": "error"}

    if not output_path.exists() or output_path.stat().st_size < 100:
        return {"error": "Downloaded file too small", "status": "error"}

    # Create Document record
    doc = Document(
        meeting_id=meeting_id,
        document_type=doc_type,
        file_path=str(output_path),
    )
    db.add(doc)
    db.commit()

    logger.info(
        "Downloaded %s for meeting %s: %s (%.1f KB)",
        doc_type, meeting_id, filename,
        output_path.stat().st_size / 1024,
    )

    # Run OCR — try Celery first, fall back to direct extraction
    try:
        from app.tasks import process_pdf_task
        from app.services.worker_manager import ensure_worker
        ensure_worker()
        process_pdf_task.delay(doc.document_id)
    except Exception:
        logger.debug("Celery unavailable, running OCR directly")
        try:
            from app.services.pdf_ingestor import extract_text
            text = extract_text(str(output_path))
            if text:
                doc.ocr_text = text
                db.commit()
                logger.info("Direct OCR: %d chars for %s", len(text), doc_type)
        except Exception as ocr_exc:
            logger.warning("Direct OCR failed: %s", ocr_exc)

    return {
        "status": "complete",
        "document_id": doc.document_id,
        "file_path": str(output_path),
    }


def _get_duration(file_path: str) -> Optional[float]:
    """Get media duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None

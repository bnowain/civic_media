"""
Celery task definitions.

Six tasks:
  - process_video_task:            Full video ingestion pipeline.
  - process_pdf_task:              PDF text extraction (native + OCR fallback).
  - extract_multi_voiceprints_task: Extra voiceprints from long confirmed segments.
  - rerun_voiceprints_task:        Background voiceprint re-evaluation after confirmation.
  - export_clip_task:              FFmpeg clip export (video/audio/audio-to-MP4).
  - cleanup_clips_task:            Delete old export files, keep metadata.

All tasks use their own DB sessions (never share across task boundaries).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.worker import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.process_video",
    max_retries=1,
    default_retry_delay=15,
)
def process_video_task(self, meeting_id: str, media_id: str) -> dict:
    """
    Run the full video ingestion pipeline for a single video file.
    Returns a summary dict with segment count.
    """
    from app.database import SessionLocal
    from app.services.pipeline import run_video_pipeline

    db = SessionLocal()
    try:
        run_video_pipeline(db, meeting_id, media_id)

        from app.models import TranscriptSegment
        count = db.query(TranscriptSegment).filter_by(meeting_id=meeting_id).count()
        return {"meeting_id": meeting_id, "segment_count": count, "status": "complete"}

    except Exception as exc:
        db.rollback()
        logger.exception("process_video_task failed for meeting %s", meeting_id)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="tasks.process_pdf",
    max_retries=1,
    default_retry_delay=10,
)
def process_pdf_task(self, document_id: str) -> dict:
    """
    Extract text from a PDF document and store it in the database.
    Also writes a .txt file to ocr_text/{meeting_id}/.
    """
    from app.database import SessionLocal
    from app.models import Document
    from app.services.pdf_ingestor import extract_text
    from app.config import OCR_TEXT_DIR

    db = SessionLocal()
    try:
        doc = db.query(Document).filter_by(document_id=document_id).first()
        if not doc:
            logger.error("Document %s not found", document_id)
            return {"error": "not_found"}

        text = extract_text(doc.file_path)
        doc.ocr_text = text
        db.commit()

        ocr_dir = OCR_TEXT_DIR / doc.meeting_id
        ocr_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(doc.file_path).stem
        out_file = ocr_dir / f"{stem}.txt"
        out_file.write_text(text, encoding="utf-8")

        logger.info("PDF processed: %s (%d chars)", doc.file_path, len(text))
        return {"document_id": document_id, "char_count": len(text), "status": "complete"}

    except Exception as exc:
        db.rollback()
        logger.exception("process_pdf_task failed for document %s", document_id)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    name="tasks.extract_multi_voiceprints",
    max_retries=0,
)
def extract_multi_voiceprints_task(segment_id: str, person_id: str) -> dict:
    """
    Extract additional voiceprints from non-overlapping windows of a long
    confirmed segment.  The pipeline's embedding phase only uses the first
    MAX_EMBED_AUDIO_SEC of audio; this task harvests the rest so the person's
    voiceprint pool captures more vocal diversity.

    Dispatched after a human confirmation for segments longer than
    MULTI_CLIP_MIN_SEGMENT.  Runs before the rerun_voiceprints_task in the
    Celery queue (FIFO with concurrency=1) so the new voiceprints are
    available when re-evaluation starts.
    """
    from app.database import SessionLocal
    from app.config import MAX_EMBED_AUDIO_SEC, MEDIA_DIR, MULTI_CLIP_DURATION
    from app.services.voiceprint import MIN_VOICEPRINT_DURATION
    from app.services import embedder
    from app import models

    db = SessionLocal()
    try:
        segment = db.query(models.TranscriptSegment).filter_by(
            segment_id=segment_id,
        ).first()
        if not segment:
            logger.warning("extract_multi_voiceprints: segment %s not found", segment_id)
            return {"error": "segment not found"}

        audio_path = str(MEDIA_DIR / segment.meeting_id / "audio.wav")
        if not Path(audio_path).exists():
            logger.warning("extract_multi_voiceprints: audio not found at %s", audio_path)
            return {"error": "audio not found"}

        # Build windows starting after the region already covered
        clip_start = segment.start_time + MAX_EMBED_AUDIO_SEC
        windows: list[tuple[float, float]] = []
        while clip_start < segment.end_time:
            clip_end = min(clip_start + MULTI_CLIP_DURATION, segment.end_time)
            if clip_end - clip_start >= MIN_VOICEPRINT_DURATION:
                windows.append((clip_start, clip_end))
            clip_start = clip_end

        if not windows:
            return {"segment_id": segment_id, "extra_voiceprints": 0}

        embeddings = embedder.extract_embeddings_batch(audio_path, windows)

        added = 0
        for emb in embeddings:
            if emb is not None:
                vp = models.Voiceprint(
                    person_id=person_id,
                    embedding=embedder.serialize(emb),
                    source_segment_id=segment_id,
                )
                db.add(vp)
                added += 1

        db.commit()
        logger.info(
            "extract_multi_voiceprints: added %d extra voiceprints for segment %s "
            "(%d windows from %.1fs segment)",
            added, segment_id, len(windows),
            segment.end_time - segment.start_time,
        )
        return {"segment_id": segment_id, "extra_voiceprints": added}

    except Exception:
        db.rollback()
        logger.exception(
            "extract_multi_voiceprints failed for segment %s", segment_id,
        )
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="tasks.process_newscast",
    max_retries=1,
    default_retry_delay=15,
)
def process_newscast_task(self, newscast_id: str, skip_commercial_strip: bool = False) -> dict:
    """Run the full TV news processing pipeline for a newscast."""
    from app.database import SessionLocal
    from app.services.news_pipeline import run_news_pipeline

    db = SessionLocal()
    try:
        run_news_pipeline(db, newscast_id, skip_commercial_strip)

        from app.models import TVNewsSegment
        count = db.query(TVNewsSegment).filter_by(newscast_id=newscast_id).count()
        return {"newscast_id": newscast_id, "segment_count": count, "status": "complete"}

    except Exception as exc:
        db.rollback()
        # Update newscast status to error
        try:
            from app.models import TVNewscast
            newscast = db.query(TVNewscast).filter_by(newscast_id=newscast_id).first()
            if newscast:
                newscast.status = "error"
                newscast.error_detail = str(exc)[:500]
                db.commit()
        except Exception:
            pass
        logger.exception("process_newscast_task failed for newscast %s", newscast_id)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    name="tasks.retag_content",
    max_retries=0,
)
def retag_content_task(content_type: str, content_id: str) -> dict:
    """Re-tag a content item via Atlas LLM."""
    from app.database import SessionLocal
    from app.services.atlas_client import tag_content
    from app.services.tagging import apply_tags, apply_mentions
    from app import models

    db = SessionLocal()
    try:
        # Get the text content to send to Atlas
        text = ""
        metadata = {}

        if content_type == "meeting":
            segments = (
                db.query(models.TranscriptSegment)
                .filter_by(meeting_id=content_id)
                .order_by(models.TranscriptSegment.start_time)
                .all()
            )
            text = " ".join(s.text for s in segments if s.text)
            meeting = db.query(models.Meeting).filter_by(meeting_id=content_id).first()
            if meeting:
                metadata = {"title": meeting.title, "governing_body": meeting.governing_body}

        elif content_type == "tv_news_segment":
            segment = db.query(models.TVNewsSegment).filter_by(segment_id=content_id).first()
            if segment:
                text = segment.transcript or ""

        if not text:
            return {"error": "no text found", "content_type": content_type, "content_id": content_id}

        result = tag_content(content_type, content_id, text, metadata)
        if not result:
            return {"error": "atlas unavailable", "content_type": content_type, "content_id": content_id}

        tag_count = apply_tags(db, content_type, content_id, result.get("tags", []))
        mention_count = apply_mentions(db, content_type, content_id, result.get("mentions", []))

        return {
            "content_type": content_type,
            "content_id": content_id,
            "tags_applied": tag_count,
            "mentions_applied": mention_count,
        }
    except Exception:
        db.rollback()
        logger.exception("retag_content_task failed for %s/%s", content_type, content_id)
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="tasks.ingest_radio",
    max_retries=0,
)
def ingest_radio_task(self, source_id: str | None = None) -> dict:
    """
    Scrape radio show sources, download new episodes, and create
    unprocessed Meeting records. Does NOT auto-process.
    """
    from app.database import SessionLocal
    from app.services.ingest import run_ingest

    db = SessionLocal()
    try:
        run_ingest(db, source_id)
        return {"status": "complete", "source_id": source_id}
    except Exception:
        db.rollback()
        logger.exception("ingest_radio_task failed")
        raise
    finally:
        db.close()


@celery_app.task(
    name="tasks.rerun_voiceprints",
    max_retries=0,
)
def rerun_voiceprints_task(meeting_id: str) -> dict:
    """
    Re-evaluate all unverified segments in a meeting against current voiceprints.
    Runs in the background after a human confirmation so the HTTP response
    returns immediately without blocking on 1700+ segment re-evaluations.
    """
    from app.database import SessionLocal
    from app.services import voiceprint as vp_service

    db = SessionLocal()
    try:
        count = vp_service.rerun_unverified_segments(db, meeting_id)
        logger.info(
            "rerun_voiceprints_task complete: %d segments re-evaluated for meeting %s",
            count, meeting_id,
        )
        return {"meeting_id": meeting_id, "segments_reprocessed": count}
    except Exception:
        db.rollback()
        logger.exception("rerun_voiceprints_task failed for meeting %s", meeting_id)
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="tasks.export_clip",
    max_retries=1,
    default_retry_delay=10,
)
def export_clip_task(self, clip_id: str) -> dict:
    """
    Export a clip via FFmpeg.  Supports video, audio, and audio-to-MP4
    (when a cover image is set).
    """
    import subprocess
    from app.database import SessionLocal
    from app.config import CLIPS_DIR
    from app import models

    db = SessionLocal()
    try:
        clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
        if not clip:
            logger.error("export_clip_task: clip %s not found", clip_id)
            return {"error": "not_found"}

        clip.export_status = "exporting"
        clip.export_error = None
        db.commit()

        clip_dir = CLIPS_DIR / clip_id
        clip_dir.mkdir(parents=True, exist_ok=True)
        source = clip.source_media_path

        import json as _json

        if clip.cover_image_path and Path(clip.cover_image_path).exists():
            out_path = clip_dir / "clip.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-progress", "pipe:1", "-nostats",
                "-loop", "1", "-i", clip.cover_image_path,
                "-ss", str(clip.start_time),
                "-to", str(clip.end_time),
                "-i", source,
                "-c:v", "libx264", "-tune", "stillimage",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest", "-pix_fmt", "yuv420p",
                str(out_path),
            ]
        elif clip.media_type == "video":
            out_path = clip_dir / "clip.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-progress", "pipe:1", "-nostats",
                "-ss", str(clip.start_time),
                "-to", str(clip.end_time),
                "-i", source,
                "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out_path),
            ]
        else:
            out_path = clip_dir / "clip.mp3"
            cmd = [
                "ffmpeg", "-y",
                "-progress", "pipe:1", "-nostats",
                "-ss", str(clip.start_time),
                "-to", str(clip.end_time),
                "-i", source,
                "-c:a", "libmp3lame", "-q:a", "2",
                str(out_path),
            ]

        progress_json = clip_dir / "progress.json"
        progress_json.write_text(_json.dumps({"pct": 0, "status": "exporting"}))
        duration_us = clip.duration * 1_000_000

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        for line in proc.stdout:
            decoded = line.decode("utf-8", errors="ignore").strip()
            if decoded.startswith("out_time_us="):
                val = decoded.split("=", 1)[1].strip()
                if val.isdigit() and int(val) > 0 and duration_us > 0:
                    pct = min(99, int(int(val) / duration_us * 100))
                    try:
                        progress_json.write_text(_json.dumps({
                            "pct": pct, "status": "exporting",
                        }))
                    except Exception:
                        pass

        proc.wait(timeout=600)

        if progress_json.exists():
            progress_json.unlink()

        if proc.returncode != 0:
            clip.export_status = "error"
            clip.export_error = f"FFmpeg exited with code {proc.returncode}"
            db.commit()
            logger.error("export_clip_task FFmpeg error for %s: exit code %d", clip_id, proc.returncode)
            return {"clip_id": clip_id, "status": "error"}

        clip.export_path = str(out_path)
        clip.export_status = "ready"
        db.commit()
        logger.info("export_clip_task complete: %s → %s", clip_id, out_path)

        # Auto-cleanup old exports so abandoned clips don't pile up
        cleanup_clips_task.delay()

        return {"clip_id": clip_id, "status": "ready"}

    except Exception as exc:
        db.rollback()
        try:
            clip = db.query(models.Clip).filter_by(clip_id=clip_id).first()
            if clip:
                clip.export_status = "error"
                clip.export_error = str(exc)[:500]
                db.commit()
        except Exception:
            pass
        logger.exception("export_clip_task failed for clip %s", clip_id)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    name="tasks.cleanup_clips",
    max_retries=0,
)
def cleanup_clips_task() -> dict:
    """
    Delete export files for downloaded or expired clips.
    Keeps metadata and thumbnails for re-export.
    """
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.config import CLIP_CLEANUP_HOURS
    from app import models

    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=CLIP_CLEANUP_HOURS)
        cleaned = 0

        clips = (
            db.query(models.Clip)
            .filter(
                models.Clip.export_status == "ready",
                models.Clip.export_path.isnot(None),
            )
            .all()
        )

        for clip in clips:
            should_clean = (
                clip.downloaded_at is not None
                or clip.created_at < cutoff
            )
            if not should_clean:
                continue

            if clip.export_path:
                p = Path(clip.export_path)
                if p.exists():
                    p.unlink()

            clip.export_status = "cleaned"
            clip.export_path = None
            cleaned += 1

        db.commit()
        logger.info("cleanup_clips_task: cleaned %d clips", cleaned)
        return {"cleaned": cleaned}
    except Exception:
        db.rollback()
        logger.exception("cleanup_clips_task failed")
        raise
    finally:
        db.close()

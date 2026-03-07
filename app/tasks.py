"""
Huey task definitions.

Tasks on `huey` (GPU worker):
  - process_video_task:             Full video ingestion pipeline.
  - process_pdf_task:               PDF text extraction (native + OCR fallback).
  - extract_multi_voiceprints_task: Extra voiceprints from long confirmed segments.
  - rerun_voiceprints_task:         Background voiceprint re-evaluation.
  - process_newscast_task:          TV news processing pipeline.

Tasks on `huey_light` (light worker):
  - primegov_discover_task:         PrimeGov meeting discovery.
  - primegov_download_task:         PrimeGov asset download.
  - transcode_video_task:           FFmpeg 540p transcode.
  - ingest_radio_task:              Radio show scraper.
  - retag_content_task:             Re-tag content via Atlas LLM.
  - export_clip_task:               FFmpeg clip export.
  - cleanup_clips_task:             Delete old export files.
  - full_ingest_task:               Download + transcode + process in one task.

All tasks use their own DB sessions (never share across task boundaries).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.worker import huey, huey_light
from app.config import MEDIA_DIR, TV_NEWS_DIR
from app.paths import to_relative, to_absolute

logger = logging.getLogger(__name__)


def _write_error_progress(base_dir: Path, item_id: str, error_msg: str) -> None:
    """Write an error state to progress.json so the UI can detect failure."""
    p = base_dir / item_id / "progress.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "stage": "Error",
            "pct": 0,
            "detail": str(error_msg)[:500],
            "error": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as exc:
        logger.warning("Could not write error progress.json: %s", exc)


# ── GPU-heavy tasks (huey) ───────────────────────────────────────────────────


@huey.task(retries=3, retry_delay=60, context=True)
def process_video_task(meeting_id: str, media_id: str, task=None) -> dict:
    """Run the full video ingestion pipeline for a single video file."""
    from app.database import SessionLocal
    from app.services.pipeline import run_video_pipeline
    from app.services.progress import create_job, complete_job, fail_job

    task_id = task.id if task else None

    db = SessionLocal()
    try:
        create_job(db, meeting_id, "process", task_id=task_id)
        result = run_video_pipeline(db, meeting_id, media_id)

        # Auto-skipped meetings return a dict — don't mark complete
        if isinstance(result, dict) and result.get("status") == "auto_skipped":
            fail_job(db, meeting_id, result.get("reason", "auto-skipped"))
            return result

        from app.models import TranscriptSegment
        count = db.query(TranscriptSegment).filter_by(meeting_id=meeting_id).count()
        complete_job(db, meeting_id)
        return {"meeting_id": meeting_id, "segment_count": count, "status": "complete"}

    except Exception as exc:
        db.rollback()
        logger.exception(
            "process_video_task failed for meeting %s", meeting_id,
        )
        fail_job(db, meeting_id, str(exc))
        raise
    finally:
        db.close()


@huey.task(retries=1, retry_delay=10, context=True)
def process_pdf_task(document_id: str, task=None) -> dict:
    """Extract text from a PDF document and store it in the database."""
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

        text = extract_text(to_absolute(doc.file_path))
        doc.ocr_text = text
        db.commit()

        ocr_dir = OCR_TEXT_DIR / doc.meeting_id
        ocr_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(doc.file_path).stem
        out_file = ocr_dir / f"{stem}.txt"
        out_file.write_text(text, encoding="utf-8")

        logger.info("PDF processed: %s (%d chars)", doc.file_path, len(text))

        # Auto-ingest votes if this is a minutes document with OCR text
        if doc.document_type == "minutes" and text:
            try:
                _ingest_minutes_votes(db, doc)
            except Exception as vote_exc:
                logger.warning("Vote ingest failed for document %s: %s", document_id, vote_exc)

        return {"document_id": document_id, "char_count": len(text), "status": "complete"}

    except Exception as exc:
        db.rollback()
        logger.exception("process_pdf_task failed for document %s", document_id)
        raise
    finally:
        db.close()


@huey.task()
def extract_multi_voiceprints_task(segment_id: str, person_id: str) -> dict:
    """Extract additional voiceprints from non-overlapping windows of a long segment."""
    from app.database import SessionLocal
    from app.config import MAX_EMBED_AUDIO_SEC, MULTI_CLIP_DURATION, EMBED_END_MARGIN
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

        extracted_media = (
            db.query(models.MediaFile)
            .filter(
                models.MediaFile.meeting_id == segment.meeting_id,
                models.MediaFile.file_path.like("%\\_extracted.wav", escape="\\"),
            )
            .first()
        )
        if not extracted_media or not Path(to_absolute(extracted_media.file_path)).exists():
            logger.warning("extract_multi_voiceprints: extracted audio not found for meeting %s", segment.meeting_id)
            return {"error": "audio not found"}
        audio_path = to_absolute(extracted_media.file_path)

        primary = embedder.compute_embed_window(segment.start_time, segment.end_time)
        if primary is None:
            return {"segment_id": segment_id, "extra_voiceprints": 0}
        usable_end = segment.end_time - EMBED_END_MARGIN
        clip_start = primary[1]
        windows: list[tuple[float, float]] = []
        while clip_start < usable_end:
            clip_end = min(clip_start + MULTI_CLIP_DURATION, usable_end)
            if clip_end - clip_start >= MIN_VOICEPRINT_DURATION:
                windows.append((clip_start, clip_end))
            clip_start = clip_end

        if not windows:
            return {"segment_id": segment_id, "extra_voiceprints": 0}

        embeddings = embedder.extract_embeddings_batch(audio_path, windows)

        added = 0
        for (clip_start, clip_end), emb in zip(windows, embeddings):
            if emb is not None:
                vp = models.Voiceprint(
                    person_id=person_id,
                    embedding=embedder.serialize(emb),
                    source_segment_id=segment_id,
                    source_duration=clip_end - clip_start,
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


@huey.task(retries=3, retry_delay=60, context=True)
def process_newscast_task(newscast_id: str, skip_commercial_strip: bool = False, task=None) -> dict:
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
        _write_error_progress(TV_NEWS_DIR, newscast_id, str(exc))
        raise
    finally:
        db.close()


@huey.task()
def rerun_voiceprints_task(meeting_id: str) -> dict:
    """Re-evaluate all unverified segments against current voiceprints."""
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


# ── Light tasks (huey_light) ────────────────────────────────────────────────


@huey_light.task()
def retag_content_task(content_type: str, content_id: str) -> dict:
    """Re-tag a content item via Atlas LLM."""
    from app.database import SessionLocal
    from app.services.atlas_client import tag_content
    from app.services.tagging import apply_tags, apply_mentions
    from app import models

    db = SessionLocal()
    try:
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
                metadata = {"title": meeting.title, "group_name": meeting.group_name}

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


@huey_light.task()
def ingest_radio_task(source_id: str | None = None) -> dict:
    """Scrape radio show sources, download new episodes, create Meeting records."""
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


@huey_light.task(retries=1, retry_delay=30, context=True)
def transcode_video_task(meeting_id: str, media_id: str, task=None) -> dict:
    """Transcode a video to 540p (960x540) using ffmpeg."""
    import subprocess
    from app.database import SessionLocal
    from app import models
    from app.services.progress import create_job, update_progress, complete_job, fail_job

    task_id = task.id if task else None

    db = SessionLocal()
    try:
        create_job(db, meeting_id, "transcode", task_id=task_id)

        media = db.query(models.MediaFile).filter_by(media_id=media_id).first()
        if not media:
            fail_job(db, meeting_id, "MediaFile not found")
            return {"error": "MediaFile not found", "status": "error"}

        original_path = Path(to_absolute(media.file_path))

        # Guard: already a _540p file — just mark as transcoded
        if "_540p" in original_path.stem:
            media.transcode_status = "transcoded"
            db.commit()
            complete_job(db, meeting_id)
            logger.info("Already transcoded (%s) — skipping", original_path.name)
            return {"meeting_id": meeting_id, "status": "transcoded", "already_540p": True}

        if not original_path.exists():
            fail_job(db, meeting_id, "Source file not found")
            return {"error": "Source file not found", "status": "error"}

        media.transcode_status = "transcoding"
        db.commit()

        stem = original_path.stem
        out_path = original_path.parent / f"{stem}_540p.mp4"

        update_progress(db, meeting_id, "Transcoding to 540p", 5,
                        f"Source: {original_path.name}")

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
                    update_progress(db, meeting_id, "Transcoding to 540p", pct,
                                    f"{pct}% complete")

        proc.wait(timeout=7200)

        if proc.returncode != 0:
            media.transcode_status = "pending"
            db.commit()
            fail_job(db, meeting_id, "Transcode failed (ffmpeg error)")
            return {"error": "ffmpeg failed", "status": "error"}

        if not out_path.exists() or out_path.stat().st_size < 1000:
            media.transcode_status = "pending"
            db.commit()
            fail_job(db, meeting_id, "Transcoded file too small or missing")
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

        # Delete original file
        original_size = original_path.stat().st_size
        new_size = out_path.stat().st_size
        original_path.unlink()

        # Update MediaFile record
        media.file_path = to_relative(str(out_path))
        media.transcode_status = "transcoded"
        if new_duration:
            media.duration = new_duration
        db.commit()

        complete_job(db, meeting_id)

        logger.info(
            "Transcoded %s: %s → %s (%.0f MB → %.0f MB)",
            meeting_id, original_path.name, out_path.name,
            original_size / (1024*1024), new_size / (1024*1024),
        )
        return {
            "meeting_id": meeting_id,
            "status": "transcoded",
            "original_size": original_size,
            "new_size": new_size,
        }

    except Exception as exc:
        db.rollback()
        logger.exception("transcode_video_task failed for meeting %s", meeting_id)
        try:
            media = db.query(models.MediaFile).filter_by(media_id=media_id).first()
            if media:
                media.transcode_status = "pending"
                db.commit()
        except Exception:
            pass
        fail_job(db, meeting_id, str(exc))
        return {"meeting_id": meeting_id, "status": "error", "error": str(exc)[:500]}
    finally:
        db.close()


@huey_light.task()
def primegov_discover_task(
    committee_ids: list[int] | None = None,
    years: list[int] | None = None,
    mode: str = "update",
) -> dict:
    """Run PrimeGov meeting discovery."""
    from app.database import SessionLocal
    from app.services.primegov.discovery import run_discovery

    db = SessionLocal()
    try:
        return run_discovery(db, committee_ids, years, mode=mode)
    except Exception:
        db.rollback()
        logger.exception("primegov_discover_task failed")
        raise
    finally:
        db.close()


@huey_light.task(retries=1, retry_delay=30, context=True)
def primegov_download_task(
    meeting_id: str,
    download_video: bool = True,
    download_agenda: bool = True,
    download_minutes: bool = True,
    download_packet: bool = True,
    auto_process: bool = False,
    task=None,
) -> dict:
    """Download PrimeGov assets (video, agenda, minutes) for a single meeting."""
    from app.database import SessionLocal
    from app.services.primegov.downloader import (
        download_video as dl_video,
        download_document as dl_doc,
    )
    from app.services.progress import create_job, update_progress, complete_job, fail_job

    task_id = task.id if task else None

    db = SessionLocal()
    results = {}
    try:
        create_job(db, meeting_id, "download", task_id=task_id)

        if download_video:
            results["video"] = dl_video(db, meeting_id)
            video_status = results["video"].get("status", "")
            if video_status == "error":
                fail_job(db, meeting_id, results["video"].get("error", "Download failed"))
                return {"meeting_id": meeting_id, "results": results}

        if download_agenda:
            update_progress(db, meeting_id, "Downloading agenda PDF...", 50)
            results["agenda"] = dl_doc(db, meeting_id, "agenda")

        if download_minutes:
            update_progress(db, meeting_id, "Downloading minutes PDF...", 65)
            results["minutes"] = dl_doc(db, meeting_id, "minutes")

        if download_packet:
            update_progress(db, meeting_id, "Downloading packet PDF...", 80)
            results["packet"] = dl_doc(db, meeting_id, "packet")

        complete_job(db, meeting_id)

        # Auto-process if video was downloaded successfully
        if auto_process and results.get("video", {}).get("status") == "complete":
            media_id = results["video"].get("media_id")
            if media_id:
                from app.services.task_dispatch import send_task
                send_task("tasks.process_video", args=[meeting_id, media_id])
                results["auto_process"] = "queued"

        return {"meeting_id": meeting_id, "results": results}

    except Exception as exc:
        db.rollback()
        logger.exception("primegov_download_task failed for meeting %s", meeting_id)
        fail_job(db, meeting_id, str(exc))
        raise
    finally:
        db.close()


@huey_light.task(retries=1, retry_delay=10, context=True)
def export_clip_task(clip_id: str, task=None) -> dict:
    """Export a clip via FFmpeg."""
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
        source = to_absolute(clip.source_media_path)

        import json as _json

        cover_abs = to_absolute(clip.cover_image_path) if clip.cover_image_path else None
        if cover_abs and Path(cover_abs).exists():
            out_path = clip_dir / "clip.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-progress", "pipe:1", "-nostats",
                "-loop", "1", "-i", cover_abs,
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

        clip.export_path = to_relative(str(out_path))
        clip.export_status = "ready"
        db.commit()
        logger.info("export_clip_task complete: %s → %s", clip_id, out_path)

        # Auto-cleanup old exports
        from app.services.task_dispatch import send_task
        send_task("tasks.cleanup_clips")

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
        raise
    finally:
        db.close()


@huey_light.task()
def full_ingest_task(meeting_id: str) -> dict:
    """Download + transcode + process in one task."""
    from app.database import SessionLocal
    from app.services.primegov.downloader import download_video as dl_video
    from app.services.transcoder import run_transcode
    from app.services.pipeline import run_video_pipeline
    from app.services.progress import create_job, complete_job, fail_job

    logger.info("full_ingest_task: starting for meeting %s", meeting_id)

    # Stage 1: Download (skip if media already exists)
    db = SessionLocal()
    try:
        create_job(db, meeting_id, "download")
        dl_result = dl_video(db, meeting_id)
        if dl_result.get("status") in ("complete", "skipped"):
            complete_job(db, meeting_id)
        else:
            fail_job(db, meeting_id, dl_result.get("error", "Download failed"))
    finally:
        db.close()

    if dl_result.get("status") not in ("complete", "skipped"):
        logger.error("full_ingest_task: download failed for %s: %s", meeting_id, dl_result)
        return {"meeting_id": meeting_id, "status": "error", "stage": "download", "detail": dl_result}

    # Get media_id — from download result or from DB if already existed
    media_id = dl_result.get("media_id")
    if not media_id:
        db = SessionLocal()
        try:
            from app import models as _m
            existing = (
                db.query(_m.MediaFile)
                .filter(
                    _m.MediaFile.meeting_id == meeting_id,
                    _m.MediaFile.file_type.in_(["video", "audio"]),
                    ~_m.MediaFile.file_path.like("%_extracted.wav"),
                )
                .first()
            )
            media_id = existing.media_id if existing else None
        finally:
            db.close()

    if not media_id:
        logger.error("full_ingest_task: no media_id for %s", meeting_id)
        return {"meeting_id": meeting_id, "status": "error", "stage": "download", "detail": "no media_id"}

    logger.info("full_ingest_task: download complete for %s (media_id=%s)", meeting_id, media_id)

    # Stage 2: Transcode
    db = SessionLocal()
    try:
        create_job(db, meeting_id, "transcode")
    finally:
        db.close()

    tc_result = run_transcode(meeting_id, media_id, auto_process=False)

    db = SessionLocal()
    try:
        if tc_result.get("status") in ("transcoded", "skipped"):
            complete_job(db, meeting_id)
        else:
            fail_job(db, meeting_id, tc_result.get("error", "Transcode failed"))
    finally:
        db.close()

    if tc_result.get("status") not in ("transcoded", "skipped"):
        logger.error("full_ingest_task: transcode failed for %s: %s", meeting_id, tc_result)
        return {"meeting_id": meeting_id, "status": "error", "stage": "transcode", "detail": tc_result}

    logger.info("full_ingest_task: transcode complete for %s", meeting_id)

    # Stage 3: Pipeline
    db = SessionLocal()
    try:
        create_job(db, meeting_id, "process")
        result = run_video_pipeline(db, meeting_id, media_id)

        if isinstance(result, dict) and result.get("status") == "auto_skipped":
            fail_job(db, meeting_id, result.get("reason", "auto-skipped"))
            logger.warning("full_ingest_task: auto-skipped %s", meeting_id)
            return {"meeting_id": meeting_id, "status": "auto_skipped", "stage": "process"}

        from app.models import TranscriptSegment
        count = db.query(TranscriptSegment).filter_by(meeting_id=meeting_id).count()
        complete_job(db, meeting_id)
    except Exception as exc:
        fail_job(db, meeting_id, str(exc))
        raise
    finally:
        db.close()

    logger.info("full_ingest_task: pipeline complete for %s (%d segments)", meeting_id, count)
    return {"meeting_id": meeting_id, "status": "complete", "segment_count": count}


@huey_light.task()
def cleanup_clips_task() -> dict:
    """Delete export files for downloaded or expired clips."""
    from datetime import timedelta
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
                p = Path(to_absolute(clip.export_path))
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


# ── Minutes vote ingest helpers (unchanged) ──────────────────────────────────

def _ingest_minutes_votes(db, doc) -> None:
    """Auto-ingest votes from a minutes document after OCR completes."""
    from app import models
    from app.services.minutes_parser import parse_minutes
    from sqlalchemy import func

    meeting = db.query(models.Meeting).filter_by(meeting_id=doc.meeting_id).first()
    if not meeting:
        return

    existing = db.query(func.count(models.MeetingVote.vote_id)) \
                 .filter(models.MeetingVote.meeting_id == doc.meeting_id).scalar()
    if existing > 0:
        logger.info("Vote ingest: skipped %s — %d votes already exist", doc.meeting_id, existing)
        return

    result = parse_minutes(
        ocr_text=doc.ocr_text,
        meeting_id=meeting.meeting_id,
        document_id=doc.document_id,
        meeting_date=meeting.meeting_date,
        group_name=meeting.group_name,
    )

    doc.minutes_parse_status = result.parse_status
    doc.minutes_parse_notes = result.parse_notes_json
    db.commit()

    if result.parse_status not in ("ok", "empty"):
        _log_vote_ingest(meeting.meeting_date, meeting.title, 0,
                         result.parse_status, result.unmatched_paragraphs,
                         skipped=True)
        logger.warning("Vote ingest: FLAGGED %s %s — parse_status=%s, "
                        "%d unmatched paragraphs (not ingested, needs review)",
                        meeting.meeting_date, meeting.title[:40],
                        result.parse_status, len(result.unmatched_paragraphs))
        return

    if not result.votes:
        _log_vote_ingest(meeting.meeting_date, meeting.title, 0,
                         result.parse_status, [])
        logger.info("Vote ingest: %s %s — no votes (special meeting) [%s]",
                     meeting.meeting_date, meeting.title[:40], result.parse_status)
        return

    person_map = _build_vote_person_lookup(db)

    saved = 0
    for v in result.votes:
        row = models.MeetingVote(
            vote_id=v.vote_id,
            meeting_id=v.meeting_id,
            document_id=v.document_id,
            meeting_date=v.meeting_date,
            group_name=v.group_name,
            agenda_section=v.agenda_section,
            item_description=v.item_description,
            resolution_number=v.resolution_number,
            outcome=v.outcome,
            vote_tally=v.vote_tally,
            mover=v.mover,
            seconder=v.seconder,
            mover_person_id=person_map.get(v.mover),
            seconder_person_id=person_map.get(v.seconder),
        )
        db.add(row)
        for name in v.yes_members:
            db.add(models.VoteMember(vote_id=v.vote_id, member_name=name,
                                     vote_value="yes", person_id=person_map.get(name)))
        for name in v.no_members:
            db.add(models.VoteMember(vote_id=v.vote_id, member_name=name,
                                     vote_value="no", person_id=person_map.get(name)))
        for name in v.absent_members:
            db.add(models.VoteMember(vote_id=v.vote_id, member_name=name,
                                     vote_value="absent", person_id=person_map.get(name)))
        saved += 1

    db.commit()

    _log_vote_ingest(meeting.meeting_date, meeting.title, saved,
                     result.parse_status, result.unmatched_paragraphs)
    logger.info("Vote ingest: %s %s — %d votes [%s]",
                meeting.meeting_date, meeting.title[:40], saved, result.parse_status)


def _build_vote_person_lookup(db) -> dict:
    """Build last-name → person_id mapping from supervisor roster."""
    import json as _json
    from app import models

    roster_path = Path(__file__).parent.parent / "config" / "supervisors.json"
    if not roster_path.exists():
        return {}
    try:
        data = _json.loads(roster_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    last_to_full = {}
    for entry in data.get("district_supervisors", []):
        full = entry.get("supervisor", "")
        last = full.rsplit(" ", 1)[-1]
        if last not in last_to_full:
            last_to_full[last] = full
    people = {p.canonical_name: p.person_id for p in db.query(models.Person).all()}
    return {last: people[full] for last, full in last_to_full.items() if full in people}


def _log_vote_ingest(meeting_date, title, vote_count, status, unmatched,
                     skipped=False):
    """Append vote ingest result to logs/minutes_vote_ingest.log."""
    log_dir = Path(__file__).parent.parent / "logs"
    log_file = log_dir / "minutes_vote_ingest.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            flag = "FLAGGED " if skipped else ""
            f.write(f"{ts}  {flag}{meeting_date}  {title[:55]:<55}  "
                    f"votes={vote_count}  status={status}\n")
            if unmatched:
                for i, para in enumerate(unmatched[:5], 1):
                    f.write(f"  [{i}] {para[:200]}\n")
    except Exception:
        pass

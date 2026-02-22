"""
TV News processing pipeline orchestrator.

4-stage pipeline (simpler than meeting pipeline — no diarization):
  1. Commercial stripping (optional — Comskip + FFmpeg)
  2. Audio extraction (reuses audio_extractor)
  3. Transcription (reuses transcriber — no diarization)
  4. Atlas story segmentation (LLM-powered segment/tag/mention)

Progress is written to tv_news/{newscast_id}/progress.json.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.config import TV_NEWS_DIR
from app.services import audio_extractor, transcriber

logger = logging.getLogger(__name__)


def _write_progress(newscast_id: str, stage: str, pct: int, detail: str = "") -> None:
    p = TV_NEWS_DIR / newscast_id / "progress.json"
    try:
        p.write_text(json.dumps({"stage": stage, "pct": pct, "detail": detail}))
    except Exception as exc:
        logger.warning("Could not write progress.json: %s", exc)


def _update_status(db: Session, newscast: models.TVNewscast, status: str, error: str = None) -> None:
    newscast.status = status
    newscast.updated_at = datetime.utcnow()
    if error:
        newscast.error_detail = error
    db.commit()


def run_news_pipeline(
    db: Session,
    newscast_id: str,
    skip_commercial_strip: bool = False,
) -> None:
    """
    Full TV news processing pipeline.

    Args:
        db: Active SQLAlchemy session (owned by Celery task).
        newscast_id: UUID of the newscast.
        skip_commercial_strip: If True, skip Comskip commercial detection.
    """
    newscast = db.query(models.TVNewscast).filter_by(newscast_id=newscast_id).first()
    if not newscast:
        raise ValueError(f"Newscast {newscast_id} not found")

    if not newscast.source_file:
        raise ValueError(f"Newscast {newscast_id} has no source file")

    source_path = newscast.source_file
    newscast_dir = TV_NEWS_DIR / newscast_id
    newscast_dir.mkdir(parents=True, exist_ok=True)

    logger.info("[%s] News pipeline start — source: %s", newscast_id, source_path)
    _write_progress(newscast_id, "Starting...", 0)

    # ── Stage 1: Commercial stripping ────────────────────────────────────────
    video_path = source_path

    if not skip_commercial_strip:
        _update_status(db, newscast, "stripping")
        _write_progress(newscast_id, "Stripping commercials", 5)
        logger.info("[%s] Stripping commercials...", newscast_id)

        try:
            from app.services.commercial_stripper import strip_commercials
            cleaned_path = str(newscast_dir / "cleaned.mp4")
            output, breaks = strip_commercials(
                source_path, cleaned_path, station=newscast.station,
            )
            video_path = output
            newscast.cleaned_file = output
            db.commit()
            logger.info("[%s] Commercial strip done: %d breaks removed", newscast_id, len(breaks))
        except Exception as exc:
            logger.warning("[%s] Commercial stripping failed, using original: %s", newscast_id, exc)
            video_path = source_path
    else:
        logger.info("[%s] Skipping commercial strip (user request)", newscast_id)

    # ── Stage 2: Audio extraction ────────────────────────────────────────────
    _update_status(db, newscast, "transcribing")
    audio_path = str(newscast_dir / "audio.wav")

    if Path(audio_path).exists():
        logger.info("[%s] Audio already extracted — skipping.", newscast_id)
    else:
        _write_progress(newscast_id, "Extracting audio", 15)
        logger.info("[%s] Extracting audio...", newscast_id)

        duration = audio_extractor.extract_audio(video_path, audio_path)
        newscast.duration_seconds = duration
        db.commit()
        logger.info("[%s] Audio extracted: %.1fs", newscast_id, duration)

    # ── Stage 3: Transcription ───────────────────────────────────────────────
    transcription_cache = newscast_dir / "transcription.json"

    if transcription_cache.exists():
        logger.info("[%s] Loading cached transcription", newscast_id)
        raw_segments = json.loads(transcription_cache.read_text())
    else:
        _write_progress(newscast_id, "Transcribing", 25)
        logger.info("[%s] Transcribing...", newscast_id)

        def _progress(fraction: float) -> None:
            pct = 25 + int(fraction * 40)
            _write_progress(newscast_id, "Transcribing", pct)

        raw_segments = transcriber.transcribe(
            audio_path,
            audio_duration=newscast.duration_seconds or 0.0,
            on_progress=_progress,
        )
        transcription_cache.write_text(json.dumps(raw_segments))
        logger.info("[%s] Transcription: %d segments", newscast_id, len(raw_segments))

    # Store transcription chunks in DB
    existing_chunks = db.query(models.TVNewsTranscriptionChunk).filter_by(
        newscast_id=newscast_id
    ).count()

    if existing_chunks == 0:
        for seg in raw_segments:
            chunk = models.TVNewsTranscriptionChunk(
                newscast_id=newscast_id,
                start_time=seg["start"],
                end_time=seg["end"],
                text=seg["text"],
            )
            db.add(chunk)
        db.commit()
        logger.info("[%s] Stored %d transcription chunks", newscast_id, len(raw_segments))

    # ── Stage 4: Atlas story segmentation ────────────────────────────────────
    _update_status(db, newscast, "tagging")
    _write_progress(newscast_id, "Segmenting stories", 70)

    full_transcript = " ".join(seg["text"] for seg in raw_segments if seg.get("text"))

    try:
        from app.services.atlas_client import segment_and_tag
        atlas_result = segment_and_tag(
            transcript=full_transcript,
            content_type="tv_news",
            metadata={
                "newscast_id": newscast_id,
                "station": newscast.station,
                "air_date": newscast.air_date,
            },
        )

        if atlas_result and atlas_result.get("segments"):
            _write_progress(newscast_id, "Saving segments", 85)

            for seg_data in atlas_result["segments"]:
                segment = models.TVNewsSegment(
                    newscast_id=newscast_id,
                    title=seg_data.get("title"),
                    summary=seg_data.get("summary"),
                    start_time=seg_data.get("start_time"),
                    end_time=seg_data.get("end_time"),
                    story_type=seg_data.get("story_type"),
                    transcript=seg_data.get("transcript"),
                )
                db.add(segment)
            db.commit()

            # Apply tags and mentions if provided
            if atlas_result.get("tags") or atlas_result.get("mentions"):
                from app.services.tagging import apply_tags, apply_mentions
                for seg in db.query(models.TVNewsSegment).filter_by(newscast_id=newscast_id).all():
                    apply_tags(db, "tv_news_segment", seg.segment_id, atlas_result.get("tags", []))
                    apply_mentions(db, "tv_news_segment", seg.segment_id, atlas_result.get("mentions", []))

            logger.info(
                "[%s] Atlas: %d story segments created",
                newscast_id, len(atlas_result["segments"]),
            )
        else:
            logger.info("[%s] Atlas unavailable or returned no segments", newscast_id)

    except Exception as exc:
        logger.warning("[%s] Atlas segmentation failed (non-fatal): %s", newscast_id, exc)

    # ── Complete ─────────────────────────────────────────────────────────────
    newscast.status = "complete"
    newscast.processed_at = datetime.utcnow()
    newscast.updated_at = datetime.utcnow()
    db.commit()

    _write_progress(newscast_id, "Complete", 100)
    logger.info("[%s] News pipeline complete", newscast_id)

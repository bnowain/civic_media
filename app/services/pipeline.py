"""
Video processing pipeline orchestrator.

Runs the full sequence for a single video:
  1. Extract audio -> WAV
  2. Transcribe audio -> text segments with timestamps
  3. Diarize audio -> speaker turn labels
  4. Align transcript + diarization
  5. Extract speaker embeddings
  6. Match against voiceprint library
  7. Persist everything to the database

Each step is logged individually so Celery task logs show clear progress.
Each step is also idempotent: if a retry occurs, completed steps are skipped.
Progress is written to media/{meeting_id}/progress.json for UI polling.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.config import MEDIA_DIR
from app.services import (
    aligner,
    audio_extractor,
    diarizer,
    embedder,
    transcriber,
    voiceprint,
)

logger = logging.getLogger(__name__)


def _write_progress(meeting_dir: Path, stage: str, pct: int, detail: str = "") -> None:
    """Write progress.json so the status endpoint can report to the UI."""
    p = meeting_dir / "progress.json"
    p.write_text(json.dumps({"stage": stage, "pct": pct, "detail": detail}))


def run_video_pipeline(db: Session, meeting_id: str, media_id: str) -> None:
    """
    Full video ingestion pipeline.

    Args:
        db:         Active SQLAlchemy session (owned by the Celery task).
        meeting_id: UUID of the parent meeting.
        media_id:   UUID of the MediaFile record for the uploaded video.

    Raises:
        ValueError: If the media record is not found.
        RuntimeError: If audio extraction fails.
    """
    # -- 0. Load the MediaFile record -----------------------------------------
    media = db.query(models.MediaFile).filter_by(media_id=media_id).first()
    if not media:
        raise ValueError(f"MediaFile {media_id} not found")

    video_path = media.file_path
    logger.info("[%s] Pipeline start - video: %s", meeting_id, video_path)

    meeting_dir = MEDIA_DIR / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    audio_path = str(meeting_dir / "audio.wav")

    # -- 1. Extract mono 16kHz WAV --------------------------------------------
    audio_record = (
        db.query(models.MediaFile)
        .filter_by(meeting_id=meeting_id, file_type="audio")
        .first()
    )

    if audio_record and Path(audio_path).exists():
        logger.info("[%s] Audio already extracted - skipping.", meeting_id)
        duration = audio_record.duration
    else:
        _write_progress(meeting_dir, "Extracting audio", 5)
        logger.info("[%s] Extracting audio...", meeting_id)
        duration = audio_extractor.extract_audio(video_path, audio_path)
        media.duration = duration
        audio_record = models.MediaFile(
            meeting_id=meeting_id,
            file_type="audio",
            file_path=audio_path,
            duration=duration,
        )
        db.add(audio_record)
        db.commit()
        logger.info("[%s] Audio extracted: %.1fs", meeting_id, duration)

    # -- 2. Transcribe --------------------------------------------------------
    existing_segments = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .all()
    )

    if existing_segments:
        complete = any(s.embedding is not None for s in existing_segments)
        if complete:
            logger.info(
                "[%s] Pipeline already complete (%d segments) - skipping.",
                meeting_id,
                len(existing_segments),
            )
            _write_progress(meeting_dir, "Complete", 100,
                            f"{len(existing_segments)} segments")
            return
        else:
            logger.info(
                "[%s] %d raw transcript segments found - resuming at diarization.",
                meeting_id,
                len(existing_segments),
            )
            raw_segments = [
                {"start": s.start_time, "end": s.end_time, "text": s.text}
                for s in existing_segments
            ]
    else:
        _write_progress(meeting_dir, "Transcribing", 10,
                        "This step takes 10-15 minutes")
        logger.info("[%s] Transcribing...", meeting_id)
        raw_segments = transcriber.transcribe(audio_path)
        logger.info("[%s] %d transcript segments", meeting_id, len(raw_segments))

        for seg_data in raw_segments:
            segment = models.TranscriptSegment(
                meeting_id=meeting_id,
                start_time=seg_data["start"],
                end_time=seg_data["end"],
                text=seg_data["text"],
                raw_speaker_label=None,
                embedding=None,
            )
            db.add(segment)
        db.commit()
        logger.info("[%s] Raw transcript committed to DB.", meeting_id)

    # -- 3. Diarize -----------------------------------------------------------
    _write_progress(meeting_dir, "Diarizing speakers", 40,
                    "Identifying who is speaking")
    logger.info("[%s] Diarizing...", meeting_id)
    diar_segments = diarizer.diarize(audio_path)
    logger.info("[%s] %d diarization turns", meeting_id, len(diar_segments))

    # -- 4. Align -------------------------------------------------------------
    _write_progress(meeting_dir, "Aligning transcript", 60)
    logger.info("[%s] Aligning...", meeting_id)
    aligned_segments = aligner.align(raw_segments, diar_segments)

    # -- 5-7. Embed, match, update persisted segments -------------------------
    logger.info("[%s] Extracting embeddings and updating segments...", meeting_id)

    db_segments = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .order_by(models.TranscriptSegment.start_time)
        .all()
    )
    seg_map = {(s.start_time, s.end_time): s for s in db_segments}
    total = len(aligned_segments)

    for i, seg_data in enumerate(aligned_segments):
        # Update progress every 50 segments
        if i % 50 == 0:
            pct = 60 + int((i / total) * 35) if total else 95
            _write_progress(
                meeting_dir, "Extracting voice embeddings", pct,
                f"{i}/{total} segments"
            )

        key = (seg_data["start"], seg_data["end"])
        segment = seg_map.get(key)
        if segment is None:
            segment = models.TranscriptSegment(
                meeting_id=meeting_id,
                start_time=seg_data["start"],
                end_time=seg_data["end"],
                text=seg_data["text"],
            )
            db.add(segment)
            db.flush()

        emb_array = embedder.extract_embedding(
            audio_path,
            seg_data["start"],
            seg_data["end"],
        )
        segment.raw_speaker_label = seg_data.get("raw_speaker_label")
        segment.embedding = embedder.serialize(emb_array) if emb_array is not None else None
        db.flush()

        if emb_array is not None:
            voiceprint.run_voiceprint_matching(db, segment)

    db.commit()
    _write_progress(meeting_dir, "Complete", 100, f"{len(aligned_segments)} segments")
    logger.info("[%s] Pipeline complete - %d segments stored.", meeting_id, len(aligned_segments))

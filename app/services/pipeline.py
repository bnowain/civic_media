"""
Video processing pipeline orchestrator.

Runs the full sequence for a single video:
  1. Extract audio → WAV
  2. Transcribe audio → text segments with timestamps
  3. Diarize audio → speaker turn labels
  4. Align transcript + diarization
  5. Extract speaker embeddings
  6. Match against voiceprint library
  7. Persist everything to the database

Each step is logged individually so Celery task logs show clear progress.
"""

from __future__ import annotations

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
    # ── 0. Load the MediaFile record ──────────────────────────────────────────
    media = db.query(models.MediaFile).filter_by(media_id=media_id).first()
    if not media:
        raise ValueError(f"MediaFile {media_id} not found")

    video_path = media.file_path
    logger.info("[%s] Pipeline start — video: %s", meeting_id, video_path)

    # ── 1. Extract mono 16kHz WAV ─────────────────────────────────────────────
    meeting_dir = MEDIA_DIR / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    audio_path = str(meeting_dir / "audio.wav")

    logger.info("[%s] Extracting audio...", meeting_id)
    duration = audio_extractor.extract_audio(video_path, audio_path)

    # Update video duration and create audio record
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

    # ── 2. Transcribe ─────────────────────────────────────────────────────────
    logger.info("[%s] Transcribing...", meeting_id)
    raw_segments = transcriber.transcribe(audio_path)
    logger.info("[%s] %d transcript segments", meeting_id, len(raw_segments))

    # ── 3. Diarize ────────────────────────────────────────────────────────────
    logger.info("[%s] Diarizing...", meeting_id)
    diar_segments = diarizer.diarize(audio_path)
    logger.info("[%s] %d diarization turns", meeting_id, len(diar_segments))

    # ── 4. Align ──────────────────────────────────────────────────────────────
    logger.info("[%s] Aligning...", meeting_id)
    aligned_segments = aligner.align(raw_segments, diar_segments)

    # ── 5–7. Embed, match, persist ────────────────────────────────────────────
    logger.info("[%s] Extracting embeddings and persisting segments...", meeting_id)

    for seg_data in aligned_segments:
        # Extract embedding for this time slice
        emb_array = embedder.extract_embedding(
            audio_path,
            seg_data["start"],
            seg_data["end"],
        )
        embedding_bytes = embedder.serialize(emb_array) if emb_array is not None else None

        segment = models.TranscriptSegment(
            meeting_id=meeting_id,
            start_time=seg_data["start"],
            end_time=seg_data["end"],
            text=seg_data["text"],
            raw_speaker_label=seg_data.get("raw_speaker_label"),
            embedding=embedding_bytes,
        )
        db.add(segment)
        db.flush()  # populate segment_id before voiceprint matching

        if emb_array is not None:
            voiceprint.run_voiceprint_matching(db, segment)

    db.commit()
    logger.info("[%s] Pipeline complete — %d segments stored.", meeting_id, len(aligned_segments))

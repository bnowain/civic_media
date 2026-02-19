"""
Video processing pipeline orchestrator.

Runs the full sequence for a single video:
  1. Extract audio -> WAV
  2. Transcribe audio -> text segments with timestamps
  3. Diarize audio -> speaker turn labels
  4. Align transcript + diarization
  5. Extract speaker embeddings  (committed every EMBED_BATCH_SIZE segments)
  6. Match against voiceprint library
  7. Persist everything to the database

Each step is logged individually so Celery task logs show clear progress.
Each step is idempotent: if a retry occurs, completed steps are skipped.
Progress is written to media/{meeting_id}/progress.json for UI polling.

Resume checkpoints:
  - No segments in DB          -> full run from transcription
  - Segments exist, no embeddings -> skip transcription, re-diarize/align/embed
  - All segments have embeddings -> already complete, skip entire pipeline
  - Partial embeddings          -> re-run embed step for segments missing embeddings only
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

EMBED_BATCH_SIZE = 50   # commit to DB every N embeddings so a crash loses minimal work


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

    # -- 2. Transcribe (or resume) --------------------------------------------
    existing_segments = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .order_by(models.TranscriptSegment.start_time)
        .all()
    )

    segments_with_embeddings = [s for s in existing_segments if s.embedding is not None]
    segments_without_embeddings = [s for s in existing_segments if s.embedding is None]

    if existing_segments and not segments_without_embeddings:
        # Every segment already has an embedding — fully complete
        logger.info(
            "[%s] Pipeline already complete (%d segments) - skipping.",
            meeting_id, len(existing_segments),
        )
        _write_progress(meeting_dir, "Complete", 100,
                        f"{len(existing_segments)} segments")
        return

    if existing_segments:
        # Transcription already done — resume at diarization/embedding
        logger.info(
            "[%s] Resuming: %d segments in DB (%d have embeddings, %d need embeddings).",
            meeting_id,
            len(existing_segments),
            len(segments_with_embeddings),
            len(segments_without_embeddings),
        )
        raw_segments = [
            {"start": s.start_time, "end": s.end_time, "text": s.text}
            for s in existing_segments
        ]
    else:
        # Fresh run — transcribe
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

        # Reload so we have DB-assigned IDs and exact stored timestamps
        existing_segments = (
            db.query(models.TranscriptSegment)
            .filter_by(meeting_id=meeting_id)
            .order_by(models.TranscriptSegment.start_time)
            .all()
        )
        segments_without_embeddings = existing_segments

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

    # Build lookup by segment_id using stored timestamps (exact float match)
    # Key off stored DB timestamps, not aligner output, to avoid float drift
    db_segments = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id)
        .order_by(models.TranscriptSegment.start_time)
        .all()
    )
    # Map by rounded timestamp to tolerate minor float drift between aligner and DB
    seg_map: dict[tuple, models.TranscriptSegment] = {}
    for s in db_segments:
        key = (round(s.start_time, 3), round(s.end_time, 3))
        seg_map[key] = s

    # Only embed segments that are missing embeddings
    needs_embedding = {
        (round(s.start_time, 3), round(s.end_time, 3))
        for s in segments_without_embeddings
    }

    # -- 5-7. Embed, match, update persisted segments -------------------------
    total = len(aligned_segments)
    to_embed = [s for s in aligned_segments
                if (round(s["start"], 3), round(s["end"], 3)) in needs_embedding]

    logger.info(
        "[%s] Extracting embeddings for %d segments (%d already done)...",
        meeting_id, len(to_embed), total - len(to_embed),
    )

    batch_count = 0
    for i, seg_data in enumerate(to_embed):
        if i % EMBED_BATCH_SIZE == 0:
            pct = 60 + int((i / max(len(to_embed), 1)) * 35)
            _write_progress(
                meeting_dir, "Extracting voice embeddings", pct,
                f"{i}/{len(to_embed)} segments"
            )

        key = (round(seg_data["start"], 3), round(seg_data["end"], 3))
        segment = seg_map.get(key)

        if segment is None:
            # Aligner produced a timestamp not in DB — create it
            logger.debug(
                "[%s] No DB segment for key %s — creating new segment.", meeting_id, key
            )
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

        batch_count += 1
        if batch_count >= EMBED_BATCH_SIZE:
            # Commit every EMBED_BATCH_SIZE segments — crash loses at most one batch
            db.commit()
            batch_count = 0
            logger.debug("[%s] Committed batch at segment %d/%d", meeting_id, i, len(to_embed))

        if emb_array is not None:
            voiceprint.run_voiceprint_matching(db, segment)

    # Final commit for the last partial batch
    db.commit()

    # Release the cached waveform — it's a full meeting WAV, potentially 2-3 GB
    embedder.clear_audio_cache()

    _write_progress(meeting_dir, "Complete", 100, f"{total} segments")
    logger.info("[%s] Pipeline complete - %d segments stored.", meeting_id, total)

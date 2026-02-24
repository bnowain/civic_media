"""
Video processing pipeline orchestrator.

Runs the full sequence for a single video:
  1. Extract audio → preprocessed WAV (loudnorm + high-pass)
  2. Transcribe audio → text segments with timestamps + confidence metadata
  3. Diarize audio → speaker turn labels
  4. Align transcript + diarization (word-level when available)
  5. Extract speaker embeddings (batched — BATCH_SIZE segments per GPU pass)
  6. Match against voiceprint library
  7. Persist everything to the database

Checkpoints (each step is skipped on retry if already complete):
  - audio.wav on disk + MediaFile(audio) in DB     → skip audio extraction
  - transcription.json on disk OR TranscriptSegment rows in DB
                                                    → skip transcription
  - diarization.json on disk                       → skip diarization (~10-20 min)
  - Any segment with embedding != None             → pipeline already complete

Progress is written to progress.json and polled by the UI every 4 seconds.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.config import MEDIA_DIR
from app.utils import generate_media_filename
from app.services import (
    aligner,
    audio_extractor,
    diarizer,
    embedder,
    transcriber,
    voiceprint,
)

logger = logging.getLogger(__name__)


# ── Progress helpers ──────────────────────────────────────────────────────────

def _write_progress(meeting_id: str, stage: str, pct: int, detail: str = "") -> None:
    """Write progress.json for UI polling."""
    p = MEDIA_DIR / meeting_id / "progress.json"
    try:
        p.write_text(json.dumps({
            "stage": stage, "pct": pct, "detail": detail,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as exc:
        logger.warning("Could not write progress.json: %s", exc)


# ── Diarization cache helpers ─────────────────────────────────────────────────

def _diarization_cache_path(meeting_id: str) -> Path:
    return MEDIA_DIR / meeting_id / "diarization.json"


def _save_diarization(meeting_id: str, diar_segments: list[dict]) -> None:
    """Save diarization results to disk so retries skip the 10-20 min step."""
    p = _diarization_cache_path(meeting_id)
    try:
        p.write_text(json.dumps(diar_segments))
        logger.info("[%s] Diarization cached to %s", meeting_id, p)
    except Exception as exc:
        logger.warning("[%s] Could not cache diarization: %s", meeting_id, exc)


def _load_diarization(meeting_id: str) -> list[dict] | None:
    """Load cached diarization results. Returns None if not present or corrupt."""
    p = _diarization_cache_path(meeting_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        logger.info(
            "[%s] Loaded %d diarization turns from cache.", meeting_id, len(data)
        )
        return data
    except Exception as exc:
        logger.warning("[%s] Diarization cache corrupt, re-running: %s", meeting_id, exc)
        return None


# ── Transcription cache helpers ──────────────────────────────────────────────

def _transcription_cache_path(meeting_id: str) -> Path:
    return MEDIA_DIR / meeting_id / "transcription.json"


def _save_transcription(meeting_id: str, segments: list[dict]) -> None:
    """Cache full transcription (with word timestamps) to disk."""
    p = _transcription_cache_path(meeting_id)
    try:
        p.write_text(json.dumps(segments))
        logger.info("[%s] Transcription cached to %s (%d segments)", meeting_id, p, len(segments))
    except Exception as exc:
        logger.warning("[%s] Could not cache transcription: %s", meeting_id, exc)


def _load_transcription(meeting_id: str) -> list[dict] | None:
    """Load cached transcription with word data. Returns None if unavailable."""
    p = _transcription_cache_path(meeting_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        logger.info(
            "[%s] Loaded %d transcript segments from cache (with word data).",
            meeting_id, len(data),
        )
        return data
    except Exception as exc:
        logger.warning("[%s] Transcription cache corrupt: %s", meeting_id, exc)
        return None


# ── Main pipeline ─────────────────────────────────────────────────────────────

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
    _write_progress(meeting_id, "Starting...", 0)

    # -- 1. Extract preprocessed mono 16kHz WAV -------------------------------
    meeting_dir = MEDIA_DIR / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)

    # Load meeting metadata for intelligent filename
    meeting_obj = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    extracted_filename = generate_media_filename(
        meeting_obj.meeting_date if meeting_obj else None,
        meeting_obj.title if meeting_obj else None,
        ".wav",
        suffix="_extracted",
    )
    audio_path = str(meeting_dir / extracted_filename)

    # Check for existing extracted audio (any *_extracted.wav)
    audio_record = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.meeting_id == meeting_id,
            models.MediaFile.file_path.like("%\\_extracted.wav", escape="\\"),
        )
        .first()
    )

    if audio_record and Path(audio_record.file_path).exists():
        audio_path = audio_record.file_path
        logger.info("[%s] Audio already extracted - skipping.", meeting_id)
        duration = audio_record.duration
    else:
        _write_progress(meeting_id, "Extracting audio", 1)
        logger.info("[%s] Extracting audio...", meeting_id)

        def _audio_progress(fraction: float) -> None:
            # Map ffmpeg's 0.0–1.0 to pipeline's 1–9%
            pct = 1 + int(fraction * 8)
            _write_progress(meeting_id, "Extracting audio", pct)

        duration = audio_extractor.extract_audio(
            video_path, audio_path, on_progress=_audio_progress,
        )
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
            _write_progress(meeting_id, "Complete", 100)
            return

    # Try loading cached transcription (preserves word timestamps for
    # word-level diarization alignment on resume)
    raw_segments = _load_transcription(meeting_id)

    if raw_segments is not None:
        logger.info(
            "[%s] Using cached transcription (%d segments).",
            meeting_id, len(raw_segments),
        )
    elif existing_segments:
        # Fall back to DB reconstruction (no word data — aligner will
        # use segment-level alignment instead of word-level)
        logger.info(
            "[%s] %d raw transcript segments in DB (no word data) - "
            "skipping transcription.",
            meeting_id,
            len(existing_segments),
        )
        raw_segments = [
            {
                "start":          s.start_time,
                "end":            s.end_time,
                "text":           s.text,
                "avg_logprob":    s.avg_logprob,
                "no_speech_prob": s.no_speech_prob,
            }
            for s in existing_segments
        ]
    else:
        _write_progress(meeting_id, "Transcribing", 10)
        logger.info("[%s] Transcribing...", meeting_id)

        def _transcribe_progress(fraction: float) -> None:
            # Map transcription 0.0–1.0 to pipeline's 10–39%
            pct = 10 + int(fraction * 29)
            _write_progress(meeting_id, "Transcribing", pct)

        raw_segments = transcriber.transcribe(
            audio_path,
            audio_duration=duration or 0.0,
            on_progress=_transcribe_progress,
        )
        logger.info("[%s] %d transcript segments", meeting_id, len(raw_segments))

        # Cache to disk (with word data) for resume
        _save_transcription(meeting_id, raw_segments)

        # Also save to DB for the checkpoint check
        for seg_data in raw_segments:
            segment = models.TranscriptSegment(
                meeting_id=meeting_id,
                start_time=seg_data["start"],
                end_time=seg_data["end"],
                text=seg_data["text"],
                raw_speaker_label=None,
                embedding=None,
                avg_logprob=seg_data.get("avg_logprob"),
                no_speech_prob=seg_data.get("no_speech_prob"),
            )
            db.add(segment)
        db.commit()
        logger.info("[%s] Raw transcript committed to DB.", meeting_id)

    # -- 3. Diarize -----------------------------------------------------------
    diar_segments = _load_diarization(meeting_id)

    if diar_segments is not None:
        logger.info(
            "[%s] Diarization loaded from cache (%d turns) - skipping.",
            meeting_id, len(diar_segments),
        )
    else:
        _write_progress(meeting_id, "Diarizing speakers", 40)
        logger.info("[%s] Diarizing...", meeting_id)
        diar_segments = diarizer.diarize(audio_path)
        logger.info("[%s] %d diarization turns", meeting_id, len(diar_segments))
        _save_diarization(meeting_id, diar_segments)

    # -- 4. Align -------------------------------------------------------------
    _write_progress(meeting_id, "Aligning transcript", 60)
    logger.info("[%s] Aligning...", meeting_id)
    aligned_segments = aligner.align(raw_segments, diar_segments)
    total = len(aligned_segments)

    # -- 5-7. Batch embed, match, update persisted segments -------------------
    logger.info(
        "[%s] Extracting embeddings in batches (batch_size=%d) for %d segments...",
        meeting_id, embedder.BATCH_SIZE, total,
    )
    _write_progress(meeting_id, "Extracting voice embeddings", 61, f"0/{total} segments")

    # Delete the original raw transcript segments — they will be replaced by
    # the aligned (merged) segments below.  Without this, both the originals
    # and the merged versions remain in the DB, producing duplicate lines in
    # the UI.  SegmentAssignment cascade-deletes automatically.
    db.query(models.TranscriptSegment).filter_by(meeting_id=meeting_id).delete()
    db.flush()

    # Center extraction: trim both margins from segment boundaries before
    # embedding extraction. Falls back to original bounds if the window
    # computation returns None (embedder's min-duration check handles it).
    segment_times = []
    for s in aligned_segments:
        window = embedder.compute_embed_window(s["start"], s["end"])
        if window is not None:
            segment_times.append(window)
        else:
            segment_times.append((s["start"], s["end"]))
    all_embeddings = embedder.extract_embeddings_batch(audio_path, segment_times)

    # Pre-load voiceprints once for matching (avoids per-segment DB queries)
    preloaded  = voiceprint._load_all_voiceprints(db)
    person_map = {p.person_id: p for p in db.query(models.Person).all()}

    # Resolve effective venue for venue-aware matching
    effective_venue_id = None
    if meeting_obj:
        effective_venue_id = meeting_obj.venue_id or (
            meeting_obj.governing_body_ref.default_venue_id
            if meeting_obj.governing_body_ref else None
        )

    for i, (seg_data, emb_array) in enumerate(zip(aligned_segments, all_embeddings)):
        if i % 50 == 0:
            pct = 61 + int((i / total) * 34)  # 61->95%
            _write_progress(
                meeting_id, "Extracting voice embeddings", pct,
                f"{i}/{total} segments"
            )

        segment = models.TranscriptSegment(
            meeting_id=meeting_id,
            start_time=seg_data["start"],
            end_time=seg_data["end"],
            text=seg_data["text"],
            raw_speaker_label=seg_data.get("raw_speaker_label"),
            avg_logprob=seg_data.get("avg_logprob"),
            no_speech_prob=seg_data.get("no_speech_prob"),
            overlap_ratio=seg_data.get("overlap_ratio", 0.0),
            embedding=embedder.serialize(emb_array) if emb_array is not None else None,
        )
        db.add(segment)
        db.flush()

        if emb_array is not None:
            voiceprint.run_voiceprint_matching(
                db, segment, preloaded=preloaded, person_map=person_map,
                venue_id=effective_venue_id,
            )

    # Set processed_at timestamp
    from datetime import datetime
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if meeting:
        meeting.processed_at = datetime.utcnow()

    db.commit()

    embedder.clear_audio_cache()

    _write_progress(meeting_id, "Complete", 100)
    logger.info("[%s] Pipeline complete - %d segments stored.", meeting_id, len(aligned_segments))

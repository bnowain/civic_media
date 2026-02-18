"""
Transcription service using faster-whisper.
Model is loaded once and cached for the lifetime of the worker process.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.config import WHISPER_COMPUTE, WHISPER_DEVICE, WHISPER_MODEL

if TYPE_CHECKING:
    from faster_whisper import WhisperModel as _WhisperModelType

logger = logging.getLogger(__name__)

_model: "_WhisperModelType | None" = None


def _get_model() -> "_WhisperModelType":
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        logger.info(
            "Loading Whisper model '%s' on %s (%s)...",
            WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE,
        )
        _model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE,
        )
        logger.info("Whisper model loaded.")
    return _model


def transcribe(audio_path: str) -> list[dict]:
    """
    Transcribe a WAV file.

    Returns a list of segment dicts:
        [{"start": float, "end": float, "text": str}, ...]

    Segments with empty text are excluded.
    """
    model = _get_model()

    logger.info("Transcribing %s ...", audio_path)

    segments_gen, info = model.transcribe(
        audio_path,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=False,
        language=None,   # auto-detect
    )

    results = []
    for seg in segments_gen:
        text = seg.text.strip()
        if text:
            results.append({
                "start": round(seg.start, 3),
                "end":   round(seg.end,   3),
                "text":  text,
            })

    logger.info("Transcription complete: %d segments", len(results))
    return results

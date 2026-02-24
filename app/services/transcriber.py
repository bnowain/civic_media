"""
Transcription service using faster-whisper.

Changes from original:
  - beam_size raised to 10 (max accuracy; safe on RTX 5090)
  - language forced to "en" (removes auto-detect non-determinism)
  - temperature=0 (fully deterministic greedy/beam decoding)
  - VAD parameters set explicitly (deterministic chunking across reruns)
  - initial_prompt loaded from config/vocab_hints.yml (improves proper noun accuracy)
  - avg_logprob and no_speech_prob stored per segment (enables confidence-based review)
  - word_timestamps=True for word-level diarization alignment
  - hallucination filtering (high compression ratio, very high no-speech prob)

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

# ── Decoding settings ─────────────────────────────────────────────────────────

# Beam size: higher = more accurate, slower. 10 is the practical ceiling for
# large-v3. Diminishing returns beyond this.
BEAM_SIZE = 10

# Force English. For Redding civic meetings this is always correct and removes
# a source of non-determinism (auto-detect can flip on noisy/silent segments).
LANGUAGE = "en"

# Temperature fallback: try deterministic decoding first (temp=0), but if the
# output has high compression ratio (repetition) or low log-prob, retry with
# progressively higher temperatures.  Prevents Whisper from getting stuck in
# infinite decoding loops on silence/applause/noise sections.
TEMPERATURE = (0, 0.2, 0.4, 0.6, 0.8, 1.0)

# ── VAD parameters ────────────────────────────────────────────────────────────
# Set ALL parameters explicitly so chunking is identical across reruns.
# These are tuned for webcast audio with occasional silence/applause gaps.
#
# threshold:               speech probability to consider a frame "speech"
# min_speech_duration_ms:  shortest segment to keep (filters mic pops)
# max_speech_duration_s:   force-split very long unbroken speech blocks
# min_silence_duration_ms: silence gap required to end a speech segment
# speech_pad_ms:           padding added to each side of a speech segment

VAD_PARAMETERS = {
    "threshold":               0.5,
    "min_speech_duration_ms":  250,
    "max_speech_duration_s":   15,   # tighter chunks for manageable segments
    "min_silence_duration_ms": 500,  # matches original; good for civic meetings
    "speech_pad_ms":           400,  # 400ms of context on each side
}

# ── Suspicious segment thresholds (for flagging in review UI) ─────────────────
# Segments below these values are candidates for manual review.
LOW_CONFIDENCE_LOGPROB    = -1.0   # avg_logprob below this = low confidence
HIGH_NO_SPEECH_PROB       = 0.6    # no_speech_prob above this = likely silence

# ── Hallucination filtering ──────────────────────────────────────────────────
# Segments exceeding these thresholds are dropped entirely (not just flagged).
# compression_ratio measures text repetitiveness (zlib). Values above ~2.4
# almost always indicate Whisper looping on the same phrase.
HALLUCINATION_COMPRESSION_RATIO = 2.4
HALLUCINATION_NO_SPEECH_PROB    = 0.9   # near-certain silence → drop
MIN_SEGMENT_DURATION            = 0.1  # seconds; zero-duration = hallucination artifact


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


def transcribe(
    audio_path: str,
    audio_duration: float = 0.0,
    on_progress: "Callable[[float], None] | None" = None,
) -> list[dict]:
    """
    Transcribe a preprocessed WAV file.

    Args:
        audio_path:     Path to the 16kHz mono WAV.
        audio_duration: Total audio duration in seconds (for progress reporting).
        on_progress:    Optional callback receiving a float 0.0–1.0.

    Returns a list of segment dicts:
        [
            {
                "start":          float,   # seconds
                "end":            float,   # seconds
                "text":           str,
                "avg_logprob":    float,   # confidence proxy (0 = perfect, -inf = bad)
                "no_speech_prob": float,   # probability this segment is silence
                "words":          list,    # word-level timestamps for diarization
            },
            ...
        ]

    Segments with empty text are excluded.
    Hallucinated segments (high compression ratio or very high no-speech
    probability) are filtered out.
    Suspicious segments (low confidence) are logged as warnings.
    """
    from app.services.vocab import load_initial_prompt

    model = _get_model()
    initial_prompt = load_initial_prompt()

    logger.info(
        "Transcribing %s  [beam=%d, lang=%s, temp=%s, vad=True, word_timestamps=True]",
        audio_path, BEAM_SIZE, LANGUAGE, TEMPERATURE,
    )
    if initial_prompt:
        logger.info("Using vocab prompt (%d chars)", len(initial_prompt))

    segments_gen, info = model.transcribe(
        audio_path,
        beam_size=BEAM_SIZE,
        language=LANGUAGE,
        temperature=TEMPERATURE,
        vad_filter=True,
        vad_parameters=VAD_PARAMETERS,
        word_timestamps=True,
        initial_prompt=initial_prompt or None,
    )

    logger.info(
        "Detected language: %s (prob=%.2f)",
        info.language, info.language_probability,
    )

    results = []
    suspicious_count = 0
    hallucination_count = 0
    last_progress_pct = -1

    for seg in segments_gen:
        # Report progress based on how far through the audio we are
        if on_progress and audio_duration > 0:
            pct = int(min(seg.end / audio_duration, 1.0) * 100)
            if pct > last_progress_pct:
                last_progress_pct = pct
                on_progress(seg.end / audio_duration)

        text = seg.text.strip()
        if not text:
            continue

        # Zero/near-zero duration segments are artifacts, not real speech
        seg_duration = seg.end - seg.start
        if seg_duration < MIN_SEGMENT_DURATION:
            hallucination_count += 1
            logger.warning(
                "Filtered zero-duration segment [%.1f-%.1f]: dur=%.3fs | %r",
                seg.start, seg.end, seg_duration, text[:80],
            )
            continue

        avg_logprob    = round(seg.avg_logprob,    4)
        no_speech_prob = round(seg.no_speech_prob, 4)
        compression    = getattr(seg, "compression_ratio", 0.0) or 0.0

        # Filter hallucinations — drop entirely, don't just flag
        if compression > HALLUCINATION_COMPRESSION_RATIO:
            hallucination_count += 1
            logger.warning(
                "Filtered hallucination [%.1f-%.1f]: compression=%.2f | %r",
                seg.start, seg.end, compression, text[:80],
            )
            continue

        if no_speech_prob > HALLUCINATION_NO_SPEECH_PROB:
            hallucination_count += 1
            logger.warning(
                "Filtered silence hallucination [%.1f-%.1f]: no_speech=%.3f | %r",
                seg.start, seg.end, no_speech_prob, text[:80],
            )
            continue

        # Flag suspicious (but keep) — logged for review
        is_suspicious = (
            avg_logprob    < LOW_CONFIDENCE_LOGPROB or
            no_speech_prob > HIGH_NO_SPEECH_PROB
        )
        if is_suspicious:
            suspicious_count += 1
            logger.warning(
                "Suspicious segment [%.1f-%.1f]: logprob=%.3f no_speech=%.3f | %r",
                seg.start, seg.end, avg_logprob, no_speech_prob, text[:80],
            )

        # Build word-level data for diarization alignment
        words = []
        if seg.words:
            words = [
                {
                    "start":       round(w.start, 3),
                    "end":         round(w.end,   3),
                    "word":        w.word,
                    "probability": round(w.probability, 4),
                }
                for w in seg.words
            ]

        results.append({
            "start":          round(seg.start, 3),
            "end":            round(seg.end,   3),
            "text":           text,
            "avg_logprob":    avg_logprob,
            "no_speech_prob": no_speech_prob,
            "words":          words,
        })

    logger.info(
        "Transcription complete: %d segments (%d suspicious, %d hallucinations filtered)",
        len(results), suspicious_count, hallucination_count,
    )

    return results

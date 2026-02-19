"""
Speaker embedding extraction using SpeechBrain ECAPA-TDNN.

Extracts a fixed-size speaker embedding vector from a time-sliced
section of audio. Embeddings are serialised as raw numpy bytes for
storage in SQLite BLOB columns.

Audio loading uses soundfile instead of torchaudio to bypass the broken
torchcodec backend on PyTorch nightly (2.12.0.dev+cu128). This mirrors
the patch applied to pyannote/audio/core/io.py.

The full waveform is cached in memory after the first load so that
processing 7000+ segments does not re-read the WAV file from disk on
every call.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import numpy as np

from app.config import EMBEDDING_DEVICE, EMBEDDING_MODEL, MIN_EMBED_DURATION

if TYPE_CHECKING:
    from speechbrain.inference.speaker import EncoderClassifier as _EC

logger = logging.getLogger(__name__)

_classifier: "_EC | None" = None

# ── Audio cache ───────────────────────────────────────────────────────────────
# Keyed by audio_path so the waveform is only read from disk once per pipeline
# run. This avoids thousands of full-file reads during the embedding loop.
_audio_cache: dict[str, tuple[np.ndarray, int]] = {}


def _load_audio(audio_path: str) -> tuple[np.ndarray, int]:
    """
    Load a WAV file as a float32 numpy array, cached by path.

    Returns:
        (waveform, sample_rate) where waveform is shape (channels, samples).
    """
    if audio_path not in _audio_cache:
        import soundfile as sf
        data, sr = sf.read(audio_path, dtype="float32", always_2d=True)
        # soundfile returns (samples, channels) — transpose to (channels, samples)
        waveform = data.T
        _audio_cache[audio_path] = (waveform, sr)
        logger.info(
            "Loaded audio into cache: %s  (%.1f min, %d Hz)",
            audio_path, waveform.shape[1] / sr / 60, sr,
        )
    return _audio_cache[audio_path]


def clear_audio_cache() -> None:
    """
    Release cached audio from memory.
    Call this after the embedding loop completes to free RAM.
    """
    _audio_cache.clear()
    logger.info("Audio cache cleared.")


# ── Model ─────────────────────────────────────────────────────────────────────

def _get_classifier() -> "_EC":
    global _classifier
    if _classifier is None:
        from speechbrain.inference.speaker import EncoderClassifier
        logger.info("Loading SpeechBrain ECAPA-TDNN embedding model...")
        _classifier = EncoderClassifier.from_hparams(
            source=EMBEDDING_MODEL,
            run_opts={"device": EMBEDDING_DEVICE},
        )
        logger.info("Embedding model loaded.")
    return _classifier


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_embedding(audio_path: str, start: float, end: float) -> np.ndarray | None:
    """
    Extract a speaker embedding for the audio slice [start, end] seconds.

    Uses a cached in-memory waveform — the audio file is only read from
    disk once regardless of how many segments are processed.

    Returns:
        1-D numpy float32 array, or None if segment is too short or fails.
    """
    import torch

    duration = end - start
    if duration < MIN_EMBED_DURATION:
        return None

    try:
        waveform_np, sr = _load_audio(audio_path)
    except Exception as exc:
        logger.warning("Could not load audio for embedding: %s", exc)
        return None

    # Resample if needed (should be 16kHz already from audio_extractor)
    if sr != 16000:
        try:
            import torchaudio
            t = torch.from_numpy(waveform_np)
            t = torchaudio.functional.resample(t, sr, 16000)
            waveform_np = t.numpy()
            sr = 16000
        except Exception as exc:
            logger.warning("Resample failed, using original sr=%d: %s", sr, exc)

    start_frame = int(start * sr)
    end_frame   = int(end   * sr)
    slice_np    = waveform_np[:, start_frame:end_frame]

    if slice_np.shape[1] < int(MIN_EMBED_DURATION * sr):
        return None

    slice_wav = torch.from_numpy(slice_np)

    # SpeechBrain expects shape (batch, time) — take first channel
    if slice_wav.dim() == 2:
        slice_wav = slice_wav[0:1]  # (1, samples)

    classifier = _get_classifier()

    try:
        with torch.no_grad():
            embedding = classifier.encode_batch(slice_wav)
        return embedding.squeeze().cpu().numpy().astype(np.float32)
    except Exception as exc:
        logger.warning("Embedding extraction failed for [%.2f, %.2f]: %s", start, end, exc)
        return None


# ── Serialisation ─────────────────────────────────────────────────────────────

def serialize(embedding: np.ndarray) -> bytes:
    """Serialise a numpy array to bytes for SQLite BLOB storage."""
    buf = io.BytesIO()
    np.save(buf, embedding)
    return buf.getvalue()


def deserialize(blob: bytes) -> np.ndarray:
    """Deserialise a numpy array from SQLite BLOB bytes."""
    return np.load(io.BytesIO(blob))

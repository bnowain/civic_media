"""
Speaker embedding extraction using SpeechBrain ECAPA-TDNN.

Extracts a fixed-size speaker embedding vector from a time-sliced
section of audio. Embeddings are serialised as raw numpy bytes for
storage in SQLite BLOB columns.
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


def extract_embedding(audio_path: str, start: float, end: float) -> np.ndarray | None:
    """
    Extract a speaker embedding for the audio slice [start, end] seconds.

    Returns:
        1-D numpy float32 array, or None if segment is too short or load fails.
    """
    import torch
    import torchaudio

    duration = end - start
    if duration < MIN_EMBED_DURATION:
        return None

    try:
        waveform, sr = torchaudio.load(audio_path)
    except Exception as exc:
        logger.warning("Could not load audio for embedding: %s", exc)
        return None

    if sr != 16000:
        waveform = torchaudio.functional.resample(waveform, sr, 16000)
        sr = 16000

    start_frame = int(start * sr)
    end_frame   = int(end   * sr)
    slice_wav   = waveform[:, start_frame:end_frame]

    if slice_wav.shape[1] < int(MIN_EMBED_DURATION * sr):
        return None

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

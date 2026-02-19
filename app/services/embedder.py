"""
Speaker embedding extraction using SpeechBrain ECAPA-TDNN.

Extracts a fixed-size speaker embedding vector from a time-sliced
section of audio. Embeddings are serialised as raw numpy bytes for
storage in SQLite BLOB columns.

Audio loading uses soundfile instead of torchaudio to bypass the broken
torchcodec backend on PyTorch nightly (2.12.0.dev+cu128). This mirrors
the patch applied to pyannote/audio/core/io.py.

The full waveform is cached in memory after the first load so that
processing 1700+ segments does not re-read the WAV file from disk on
every call.

Batch extraction runs ECAPA in chunks of BATCH_SIZE segments, padding
shorter slices to match the longest in each chunk. This keeps the GPU
fully utilised rather than running 1700 sequential single-item passes.
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

# Segments per GPU forward pass. 32 is a safe default for 24GB+ VRAM.
# Lower to 16 if you see OOM errors during the embedding phase.
BATCH_SIZE = 32

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


# ── Single extraction (kept for compatibility / reprocess single segment) ─────

def extract_embedding(audio_path: str, start: float, end: float) -> np.ndarray | None:
    """
    Extract a speaker embedding for the audio slice [start, end] seconds.

    Uses the cached in-memory waveform. Returns None if the segment is
    too short or extraction fails.
    """
    results = extract_embeddings_batch(audio_path, [(start, end)])
    return results[0]


# ── Batch extraction ──────────────────────────────────────────────────────────

def extract_embeddings_batch(
    audio_path: str,
    segments: list[tuple[float, float]],
) -> list[np.ndarray | None]:
    """
    Extract speaker embeddings for a list of (start, end) segments in batches.

    Segments shorter than MIN_EMBED_DURATION return None. All valid slices in
    each batch are padded to the length of the longest slice in that batch,
    then run through ECAPA in a single GPU forward pass.

    Args:
        audio_path: Path to the 16kHz mono WAV file.
        segments:   List of (start_sec, end_sec) tuples.

    Returns:
        List of numpy float32 arrays (or None) in the same order as input.
    """
    import torch

    if not segments:
        return []

    try:
        waveform_np, sr = _load_audio(audio_path)
    except Exception as exc:
        logger.warning("Could not load audio for batch embedding: %s", exc)
        return [None] * len(segments)

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

    min_frames = int(MIN_EMBED_DURATION * sr)
    classifier = _get_classifier()
    results: list[np.ndarray | None] = [None] * len(segments)

    # Build index of valid segments
    valid: list[tuple[int, np.ndarray]] = []  # (original_index, slice)
    for idx, (start, end) in enumerate(segments):
        duration = end - start
        if duration < MIN_EMBED_DURATION:
            continue
        start_frame = int(start * sr)
        end_frame   = int(end   * sr)
        slice_np    = waveform_np[0, start_frame:end_frame]  # mono, shape (samples,)
        if slice_np.shape[0] < min_frames:
            continue
        valid.append((idx, slice_np))

    if not valid:
        return results

    # Process in chunks of BATCH_SIZE
    for chunk_start in range(0, len(valid), BATCH_SIZE):
        chunk = valid[chunk_start : chunk_start + BATCH_SIZE]

        # Pad all slices to the length of the longest in this chunk
        max_len = max(s.shape[0] for _, s in chunk)
        padded = []
        for _, s in chunk:
            pad = max_len - s.shape[0]
            padded.append(np.pad(s, (0, pad), mode="constant") if pad > 0 else s)

        # Stack to (batch, time) tensor
        batch_tensor = torch.from_numpy(np.stack(padded, axis=0))  # (B, T)

        try:
            with torch.no_grad():
                embeddings = classifier.encode_batch(batch_tensor)  # (B, 1, D)
            embeddings_np = embeddings.squeeze(1).cpu().numpy().astype(np.float32)  # (B, D)

            for i, (orig_idx, _) in enumerate(chunk):
                results[orig_idx] = embeddings_np[i]

        except Exception as exc:
            logger.warning(
                "Batch embedding failed for chunk %d-%d: %s",
                chunk_start, chunk_start + len(chunk), exc,
            )
            # Fall back to single extraction for failed chunks
            for orig_idx, slice_np in chunk:
                try:
                    t = torch.from_numpy(slice_np).unsqueeze(0)  # (1, T)
                    with torch.no_grad():
                        emb = classifier.encode_batch(t)
                    results[orig_idx] = emb.squeeze().cpu().numpy().astype(np.float32)
                except Exception as exc2:
                    logger.warning("Single fallback also failed for segment %d: %s", orig_idx, exc2)

    return results


# ── Serialisation ─────────────────────────────────────────────────────────────

def serialize(embedding: np.ndarray) -> bytes:
    """Serialise a numpy array to bytes for SQLite BLOB storage."""
    buf = io.BytesIO()
    np.save(buf, embedding)
    return buf.getvalue()


def deserialize(blob: bytes) -> np.ndarray:
    """Deserialise a numpy array from SQLite BLOB bytes."""
    return np.load(io.BytesIO(blob))

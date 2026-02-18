"""
Speaker diarization service using pyannote.audio.
Pipeline is loaded once and cached for the worker process.
Requires HF_TOKEN environment variable for pyannote model access.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from app.config import PYANNOTE_MODEL

if TYPE_CHECKING:
    from pyannote.audio import Pipeline as _PipelineType

logger = logging.getLogger(__name__)

_pipeline: "_PipelineType | None" = None


def _get_pipeline() -> "_PipelineType":
    global _pipeline
    if _pipeline is None:
        import torch
        from pyannote.audio import Pipeline

        token = os.environ.get("HF_TOKEN", "")
        if not token:
            raise EnvironmentError(
                "HF_TOKEN environment variable is required for pyannote.audio. "
                "Accept the model licence at https://hf.co/pyannote/speaker-diarization-3.1 "
                "then set HF_TOKEN=<your token>."
            )

        logger.info("Loading pyannote diarization pipeline...")
        _pipeline = Pipeline.from_pretrained(PYANNOTE_MODEL, token=token)

        if torch.cuda.is_available():
            _pipeline.to(torch.device("cuda"))
            logger.info("pyannote pipeline on CUDA.")
        else:
            logger.warning("CUDA unavailable — pyannote running on CPU (slow).")

    return _pipeline


def diarize(audio_path: str) -> list[dict]:
    """
    Run speaker diarization on a WAV file.

    Returns:
        [{"start": float, "end": float, "speaker": str}, ...]
        e.g. speaker = "SPEAKER_00"
    """
    pipeline = _get_pipeline()

    logger.info("Diarizing %s ...", audio_path)
    diarization = pipeline(audio_path)

    results = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        results.append({
            "start":   round(turn.start, 3),
            "end":     round(turn.end,   3),
            "speaker": speaker,
        })

    logger.info("Diarization complete: %d speaker turns", len(results))
    return results

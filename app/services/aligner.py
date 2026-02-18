"""
Segment aligner.

Merges Whisper transcript segments with pyannote diarization segments.
Strategy: for each transcript segment, find the diarization speaker turn
with maximum time overlap. This is a purely deterministic operation —
no ML, no thresholds.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def align(
    transcript_segments: list[dict],
    diarization_segments: list[dict],
) -> list[dict]:
    """
    Assign a raw_speaker_label to each transcript segment.

    Args:
        transcript_segments: Output of transcriber.transcribe().
            Each dict: {start, end, text}
        diarization_segments: Output of diarizer.diarize().
            Each dict: {start, end, speaker}

    Returns:
        Transcript segments with "raw_speaker_label" added.
        Label is None if no diarization overlap was found.
    """
    results = []

    for t in transcript_segments:
        t_start = t["start"]
        t_end   = t["end"]
        t_dur   = t_end - t_start

        speaker = None

        if t_dur > 0 and diarization_segments:
            best_overlap = 0.0

            for d in diarization_segments:
                # Compute overlap in seconds
                overlap = max(
                    0.0,
                    min(t_end, d["end"]) - max(t_start, d["start"])
                )
                if overlap > best_overlap:
                    best_overlap = overlap
                    speaker = d["speaker"]

        aligned = dict(t)
        aligned["raw_speaker_label"] = speaker
        results.append(aligned)

    assigned = sum(1 for r in results if r["raw_speaker_label"] is not None)
    logger.info(
        "Alignment complete: %d/%d segments assigned a speaker label",
        assigned, len(results),
    )

    return results

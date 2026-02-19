"""
Segment aligner.

Merges Whisper transcript segments with pyannote diarization segments.
Strategy: for each transcript segment, find the diarization speaker turn
with maximum time overlap. This is a purely deterministic operation —
no ML, no thresholds.

After speaker assignment, adjacent segments from the same speaker are
merged when the gap between them is small or either segment is very short.
This prevents one-word fragments produced by aggressive diarization splits.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Merge adjacent same-speaker segments when:
#   - the gap between them is under this threshold (seconds), OR
#   - either segment is shorter than MIN_SEGMENT_DURATION
MAX_MERGE_GAP      = 1.5   # seconds
MIN_SEGMENT_DURATION = 2.0  # seconds


def align(
    transcript_segments: list[dict],
    diarization_segments: list[dict],
) -> list[dict]:
    """
    Assign a raw_speaker_label to each transcript segment, then merge
    adjacent same-speaker fragments into longer coherent turns.

    Args:
        transcript_segments: Output of transcriber.transcribe().
            Each dict: {start, end, text}
        diarization_segments: Output of diarizer.diarize().
            Each dict: {start, end, speaker}

    Returns:
        Transcript segments with "raw_speaker_label" added, with short
        adjacent same-speaker fragments merged together.
        Label is None if no diarization overlap was found.
    """
    assigned = _assign_speakers(transcript_segments, diarization_segments)
    merged   = _merge_adjacent(assigned)

    logger.info(
        "Alignment complete: %d segments → %d after merging (assigned: %d)",
        len(assigned),
        len(merged),
        sum(1 for r in merged if r["raw_speaker_label"] is not None),
    )

    return merged


# ── Speaker assignment ────────────────────────────────────────────────────────

def _assign_speakers(
    transcript_segments: list[dict],
    diarization_segments: list[dict],
) -> list[dict]:
    """Assign the best-overlap diarization speaker to each transcript segment."""
    results = []

    for t in transcript_segments:
        t_start = t["start"]
        t_end   = t["end"]
        t_dur   = t_end - t_start

        speaker = None

        if t_dur > 0 and diarization_segments:
            best_overlap = 0.0
            for d in diarization_segments:
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

    return results


# ── Adjacent segment merging ──────────────────────────────────────────────────

def _merge_adjacent(segments: list[dict]) -> list[dict]:
    """
    Merge consecutive segments from the same speaker when:
      - the gap between them is ≤ MAX_MERGE_GAP, OR
      - either segment is shorter than MIN_SEGMENT_DURATION

    Text is joined with a space. Timing spans the full merged range.
    """
    if not segments:
        return segments

    merged = [dict(segments[0])]

    for curr in segments[1:]:
        prev = merged[-1]

        same_speaker = (
            curr["raw_speaker_label"] is not None
            and curr["raw_speaker_label"] == prev["raw_speaker_label"]
        )

        if not same_speaker:
            merged.append(dict(curr))
            continue

        gap           = curr["start"] - prev["end"]
        prev_duration = prev["end"]  - prev["start"]
        curr_duration = curr["end"]  - curr["start"]
        should_merge  = (
            gap <= MAX_MERGE_GAP
            or prev_duration < MIN_SEGMENT_DURATION
            or curr_duration < MIN_SEGMENT_DURATION
        )

        if should_merge:
            prev["end"]  = curr["end"]
            prev["text"] = prev["text"].rstrip() + " " + curr["text"].lstrip()
        else:
            merged.append(dict(curr))

    return merged

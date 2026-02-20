"""
Segment aligner.

Merges Whisper transcript segments with pyannote diarization segments.

Two alignment strategies:
  - Word-level (preferred): when word timestamps are available, each word
    is assigned to the diarization speaker with maximum overlap, then
    consecutive same-speaker words are regrouped into segments. This
    accurately splits speaker turns that fall mid-sentence.
  - Segment-level (fallback): when word data is unavailable (legacy cache),
    each whole transcript segment is assigned to the best-overlap speaker.

After speaker assignment, adjacent segments from the same speaker are
merged when the gap is small or either segment is very short, capped at
MAX_MERGE_SENTENCES to keep blocks readable.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Merge adjacent same-speaker segments when:
#   - the gap between them is under this threshold (seconds), OR
#   - either segment is shorter than MIN_SEGMENT_DURATION
MAX_MERGE_GAP        = 1.5   # seconds
MIN_SEGMENT_DURATION = 3.0   # seconds — absorb more short fragments
MAX_MERGE_SENTENCES  = 5     # allow 3-5 sentence segments


def align(
    transcript_segments: list[dict],
    diarization_segments: list[dict],
) -> list[dict]:
    """
    Assign a raw_speaker_label to each transcript segment, then merge
    adjacent same-speaker fragments into longer coherent turns.

    Uses word-level alignment when word timestamps are present (from
    transcriber with word_timestamps=True). Falls back to segment-level
    alignment for legacy data without word timestamps.

    Args:
        transcript_segments: Output of transcriber.transcribe().
            Each dict: {start, end, text, words?, avg_logprob?, no_speech_prob?}
        diarization_segments: Output of diarizer.diarize().
            Each dict: {start, end, speaker}

    Returns:
        Transcript segments with "raw_speaker_label" added, with short
        adjacent same-speaker fragments merged together.
        Label is None if no diarization overlap was found.
    """
    has_words = any(seg.get("words") for seg in transcript_segments)

    if has_words:
        assigned = _assign_speakers_by_word(transcript_segments, diarization_segments)
        mode = "word-level"
    else:
        assigned = _assign_speakers(transcript_segments, diarization_segments)
        mode = "segment-level"

    merged = _merge_adjacent(assigned)
    merged = _absorb_runts(merged)

    logger.info(
        "Alignment complete (%s): %d input → %d assigned → %d after merging",
        mode, len(transcript_segments), len(assigned), len(merged),
    )

    return merged


# ── Post-merge runt absorption ──────────────────────────────────────────────

def _absorb_runts(segments: list[dict]) -> list[dict]:
    """
    Second pass: merge segments shorter than MIN_SEGMENT_DURATION
    into their nearest same-speaker neighbor.
    Prefer merging into the previous segment; fall back to next.
    """
    if len(segments) <= 1:
        return segments

    result = [dict(segments[0])]
    for curr in segments[1:]:
        duration = curr["end"] - curr["start"]
        prev = result[-1]
        same_as_prev = (
            curr["raw_speaker_label"] is not None
            and curr["raw_speaker_label"] == prev["raw_speaker_label"]
        )
        if duration < MIN_SEGMENT_DURATION and same_as_prev:
            prev["end"] = curr["end"]
            prev["text"] = prev["text"].rstrip() + " " + curr["text"].lstrip()
        else:
            result.append(dict(curr))
    return result


# ── Word-level speaker assignment ────────────────────────────────────────────

def _assign_speakers_by_word(
    transcript_segments: list[dict],
    diarization_segments: list[dict],
) -> list[dict]:
    """
    Assign speakers at word granularity, then regroup into
    speaker-homogeneous segments.

    Each word's time span is compared against diarization turns to find
    the best-overlap speaker. Consecutive same-speaker words are then
    grouped into segments. This correctly handles speaker changes that
    occur mid-sentence (e.g., a question followed by its answer in the
    same Whisper segment).
    """
    # Flatten all words, carrying parent segment metadata
    all_words: list[dict] = []

    for seg in transcript_segments:
        words = seg.get("words")
        if not words:
            # No word data for this segment — treat as a single unit
            all_words.append({
                "start":         seg["start"],
                "end":           seg["end"],
                "word":          " " + seg["text"],  # leading space for join consistency
                "avg_logprob":   seg.get("avg_logprob"),
                "no_speech_prob": seg.get("no_speech_prob"),
            })
            continue

        for w in words:
            all_words.append({
                "start":         w["start"],
                "end":           w["end"],
                "word":          w["word"],
                "avg_logprob":   seg.get("avg_logprob"),
                "no_speech_prob": seg.get("no_speech_prob"),
            })

    if not all_words:
        return []

    # Assign best-overlap diarization speaker to each word
    for w in all_words:
        w_start, w_end = w["start"], w["end"]
        best_speaker = None
        best_overlap = 0.0

        if w_end > w_start and diarization_segments:
            for d in diarization_segments:
                overlap = max(
                    0.0,
                    min(w_end, d["end"]) - max(w_start, d["start"])
                )
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = d["speaker"]

        w["speaker"] = best_speaker

    # Group consecutive same-speaker words into segments
    groups: list[list[dict]] = [[all_words[0]]]

    for w in all_words[1:]:
        if w["speaker"] == groups[-1][-1]["speaker"]:
            groups[-1].append(w)
        else:
            groups.append([w])

    # Convert word groups to segment dicts
    segments: list[dict] = []

    for group in groups:
        text = "".join(w["word"] for w in group).strip()
        if not text:
            continue

        # Average confidence metrics across words in the group
        logprobs  = [w["avg_logprob"]   for w in group if w.get("avg_logprob") is not None]
        no_speech = [w["no_speech_prob"] for w in group if w.get("no_speech_prob") is not None]

        segments.append({
            "start":             group[0]["start"],
            "end":               group[-1]["end"],
            "text":              text,
            "raw_speaker_label": group[0]["speaker"],
            "avg_logprob":       round(sum(logprobs) / len(logprobs), 4) if logprobs else None,
            "no_speech_prob":    round(sum(no_speech) / len(no_speech), 4) if no_speech else None,
        })

    return segments


# ── Segment-level speaker assignment (fallback) ─────────────────────────────

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

    Merging stops once a block reaches MAX_MERGE_SENTENCES sentence endings.
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

        # Count sentence-ending punctuation already in the accumulated block
        prev_sentences = len(re.findall(r'[.!?]+', prev["text"]))

        should_merge = (
            prev_sentences < MAX_MERGE_SENTENCES
            and (
                gap <= MAX_MERGE_GAP
                or prev_duration < MIN_SEGMENT_DURATION
                or curr_duration < MIN_SEGMENT_DURATION
            )
        )

        if should_merge:
            prev["end"]  = curr["end"]
            prev["text"] = prev["text"].rstrip() + " " + curr["text"].lstrip()
        else:
            merged.append(dict(curr))

    return merged

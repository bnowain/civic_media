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

After speaker assignment, a trailing-word reassignment pass corrects for
pyannote's ~200-500ms latency on speaker change detection.  Acknowledgment
words ("yeah", "oh", "well", etc.) that bleed onto the end of the previous
speaker's segment are moved to the next speaker where they belong.

Adjacent segments from the same speaker are then merged when the gap is
small or either segment is very short, capped at MAX_MERGE_SENTENCES to
keep blocks readable.
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

# ── Trailing-word reassignment ───────────────────────────────────────────────
# Pyannote detects speaker changes ~200-500ms late, so the first word of a new
# speaker often gets assigned to the outgoing speaker.  At zero-gap speaker
# transitions, if the last word of the outgoing segment is an acknowledgment /
# response-starter word, move it to the next speaker's segment.
#
# Conservative set — these almost never end a thought naturally when followed
# immediately by a different speaker's turn.  "well" and "so" are excluded
# because they legitimately end sentences ("that worked out well").
TRAILING_REASSIGN_GAP = 0.3   # seconds — only at tight speaker handoffs
TRAILING_REASSIGN_MIN_WORDS = 3  # don't cannibalize very short segments

_ACKNOWLEDGMENT_WORDS = frozenset({
    "yeah", "yep", "yes", "oh", "okay", "ok", "right", "sure",
    "absolutely", "boy", "wow", "no", "nah", "nope", "huh",
    "hmm", "mm", "uh-huh", "really",
})


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

    reassigned = _reassign_trailing_words(assigned)
    merged = _merge_adjacent(reassigned)
    merged = _absorb_runts(merged)

    # Compute overlap ratios on final merged segments
    _compute_overlap_ratios(merged, diarization_segments)

    reassign_count = len(assigned) - len(reassigned) if len(assigned) != len(reassigned) else "n/a"
    logger.info(
        "Alignment complete (%s): %d input → %d assigned → %d after reassign → %d after merging",
        mode, len(transcript_segments), len(assigned), len(reassigned), len(merged),
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


# ── Overlap ratio computation ────────────────────────────────────────────────

def _compute_overlap_ratios(
    segments: list[dict],
    diarization_segments: list[dict],
) -> None:
    """
    For each segment, compute what fraction of its duration has competing
    diarization speakers (crosstalk). Modifies dicts in place by adding
    an 'overlap_ratio' key.

    A segment's assigned speaker is its raw_speaker_label. Any diarization
    turn from a DIFFERENT speaker that overlaps the segment's time range
    contributes to the overlap total. The ratio is overlap_time / seg_duration.
    """
    if not diarization_segments:
        for seg in segments:
            seg["overlap_ratio"] = 0.0
        return

    for seg in segments:
        seg_start = seg["start"]
        seg_end = seg["end"]
        seg_dur = seg_end - seg_start
        assigned_speaker = seg.get("raw_speaker_label")

        if seg_dur <= 0 or assigned_speaker is None:
            seg["overlap_ratio"] = 0.0
            continue

        overlap_time = 0.0
        for d in diarization_segments:
            if d["speaker"] == assigned_speaker:
                continue
            overlap = max(0.0, min(seg_end, d["end"]) - max(seg_start, d["start"]))
            overlap_time += overlap

        seg["overlap_ratio"] = round(min(overlap_time / seg_dur, 1.0), 4)


# ── Trailing-word reassignment (pyannote latency correction) ─────────────────

def _reassign_trailing_words(segments: list[dict]) -> list[dict]:
    """
    Fix pyannote's late speaker-change detection by moving trailing
    acknowledgment words from the outgoing speaker to the next speaker.

    At each speaker transition with gap < TRAILING_REASSIGN_GAP:
      1. Check if the last word of the outgoing segment is in
         _ACKNOWLEDGMENT_WORDS.
      2. If the outgoing segment has >= TRAILING_REASSIGN_MIN_WORDS
         remaining after removal, move the word.
      3. Adjust timestamps on both segments.

    This runs BEFORE merging so that the moved words get naturally
    absorbed into the next speaker's merged block.
    """
    if len(segments) <= 1:
        return segments

    result = [dict(s) for s in segments]
    moved = 0

    for i in range(len(result) - 1):
        curr = result[i]
        nxt = result[i + 1]

        # Only at speaker transitions
        if curr["raw_speaker_label"] == nxt["raw_speaker_label"]:
            continue
        if curr["raw_speaker_label"] is None or nxt["raw_speaker_label"] is None:
            continue

        # Only at tight handoffs (pyannote detected the change nearby)
        gap = nxt["start"] - curr["end"]
        if gap > TRAILING_REASSIGN_GAP:
            continue

        # Extract words from the outgoing segment's text
        text = curr["text"].rstrip()
        words = text.split()
        if len(words) < TRAILING_REASSIGN_MIN_WORDS:
            continue

        # Check the last word
        last_word = words[-1].lower().strip(".,;:!?'\"")
        if last_word not in _ACKNOWLEDGMENT_WORDS:
            continue

        # Move the trailing word: remove from current, prepend to next
        removed_word = words.pop()
        curr["text"] = " ".join(words)

        # Estimate new end time for current segment.
        # Rough: shrink proportionally by word count ratio.
        orig_word_count = len(words) + 1
        duration = curr["end"] - curr["start"]
        word_duration = duration / orig_word_count if orig_word_count > 0 else 0
        curr["end"] = round(curr["end"] - word_duration, 3)

        # Prepend to next segment
        nxt["text"] = removed_word + " " + nxt["text"].lstrip()
        nxt["start"] = curr["end"]

        moved += 1

    if moved:
        logger.info("Trailing-word reassignment: moved %d acknowledgment word(s)", moved)

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

        seg_start = group[0]["start"]
        seg_end   = group[-1]["end"]

        # Zero-duration groups are Whisper alignment artifacts (a word whose
        # start == end gets speaker=None and forms its own isolated group,
        # producing a duplicate timestamp in the output).  Merge the text into
        # the previous segment instead of emitting a zero-length entry.
        if seg_end <= seg_start:
            if segments:
                segments[-1]["text"] = segments[-1]["text"].rstrip() + " " + text
            # else: nothing before it — silently drop (edge case)
            continue

        segments.append({
            "start":             seg_start,
            "end":               seg_end,
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

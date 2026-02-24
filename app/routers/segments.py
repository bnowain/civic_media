"""
Transcript segment endpoints.

GET    /api/segments/{meeting_id}            — all segments, optional filter.
GET    /api/segments/{meeting_id}/{seg_id}   — single segment detail.
PATCH  /api/segments/{segment_id}/edit       — manually correct segment text.
GET    /api/segments/{meeting_id}/export     — download as SRT, TXT, JSON, or PDF.
"""

import json
import re
import textwrap

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session, joinedload

from app import models, schemas
from app.config import SIMILARITY_MEDIUM
from app.database import get_db

router = APIRouter(prefix="/api/segments", tags=["segments"])


# ── List segments ─────────────────────────────────────────────────────────────

@router.get("/{meeting_id}", response_model=list[schemas.SegmentOut])
def list_segments(
    meeting_id: str,
    filter: str | None = Query(
        None,
        description="Filter preset: 'unknown' | 'low_confidence' | 'tagged'",
    ),
    db: Session = Depends(get_db),
):
    """
    Return transcript segments for a meeting, ordered by start_time.

    Optional filters:
      - unknown:        segments with no predicted speaker
      - low_confidence: segments with similarity score below SIMILARITY_MEDIUM
    """
    segments = (
        db.query(models.TranscriptSegment)
        .options(
            joinedload(models.TranscriptSegment.assignment)
            .joinedload(models.SegmentAssignment.predicted_person)
        )
        .filter_by(meeting_id=meeting_id)
        .order_by(models.TranscriptSegment.start_time)
        .all()
    )

    if filter == "unknown":
        segments = [
            s for s in segments
            if not s.assignment or s.assignment.predicted_person_id is None
        ]
    elif filter == "low_confidence":
        segments = [
            s for s in segments
            if (
                s.assignment
                and not s.assignment.verified
                and s.assignment.similarity_score is not None
                and s.assignment.similarity_score < SIMILARITY_MEDIUM
            )
        ]
    elif filter == "tagged":
        segments = [
            s for s in segments
            if s.assignment and s.assignment.tagged
        ]

    return segments


# ── Single segment ────────────────────────────────────────────────────────────

@router.get("/{meeting_id}/export")
def export_transcript(
    meeting_id: str,
    format: str = Query("srt", description="Export format: srt | txt | json | pdf"),
    db: Session = Depends(get_db),
):
    """
    Export the full transcript in SRT, plain text, JSON, or PDF format.
    Speaker names use confirmed/predicted person names where available,
    falling back to raw diarization labels.
    """
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    segments = (
        db.query(models.TranscriptSegment)
        .options(
            joinedload(models.TranscriptSegment.assignment)
            .joinedload(models.SegmentAssignment.predicted_person)
        )
        .filter_by(meeting_id=meeting_id)
        .order_by(models.TranscriptSegment.start_time)
        .all()
    )

    if not segments:
        raise HTTPException(404, "No transcript segments found")

    safe_title = "".join(
        c if c.isalnum() or c in " _-" else "_"
        for c in meeting.title
    ).strip()

    fmt = format.lower()

    if fmt == "srt":
        content = _to_srt(segments)
        filename = f"{safe_title}.srt"
        media_type = "text/plain; charset=utf-8"
    elif fmt == "txt":
        content = _to_txt(segments, meeting=meeting)
        filename = f"{safe_title}.txt"
        media_type = "text/plain; charset=utf-8"
    elif fmt == "json":
        content = _to_json(segments)
        filename = f"{safe_title}.json"
        media_type = "application/json; charset=utf-8"
    elif fmt == "pdf":
        from app.services.pdf_export import generate_transcript_pdf

        content = generate_transcript_pdf(segments, meeting)
        filename = f"{safe_title}.pdf"
        media_type = "application/pdf"
    else:
        raise HTTPException(400, "format must be srt, txt, json, or pdf")

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@router.get("/{meeting_id}/{segment_id}", response_model=schemas.SegmentOut)
def get_segment(meeting_id: str, segment_id: str, db: Session = Depends(get_db)):
    seg = (
        db.query(models.TranscriptSegment)
        .filter_by(meeting_id=meeting_id, segment_id=segment_id)
        .first()
    )
    if not seg:
        raise HTTPException(404, "Segment not found")
    return seg


# ── Manual text edit ──────────────────────────────────────────────────────────

@router.patch("/{segment_id}/source-type", response_model=schemas.SegmentOut)
def update_source_type(
    segment_id: str,
    payload: schemas.SourceTypeUpdate,
    db: Session = Depends(get_db),
):
    """
    Update the source type of a transcript segment (in_person or telephone).
    Does not trigger reprocessing.
    """
    if payload.source_type not in ("in_person", "telephone"):
        raise HTTPException(400, "source_type must be 'in_person' or 'telephone'")

    seg = db.query(models.TranscriptSegment).filter_by(segment_id=segment_id).first()
    if not seg:
        raise HTTPException(404, "Segment not found")

    seg.source_type = payload.source_type
    db.commit()
    db.refresh(seg)
    return seg


@router.patch("/{segment_id}/edit", response_model=schemas.SegmentOut)
def edit_segment(
    segment_id: str,
    payload: schemas.SegmentEdit,
    db: Session = Depends(get_db),
):
    """
    Manually correct the text of a transcript segment.
    Does not affect speaker assignment or embeddings.
    """
    seg = db.query(models.TranscriptSegment).filter_by(segment_id=segment_id).first()
    if not seg:
        raise HTTPException(404, "Segment not found")

    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Text cannot be empty")

    seg.text = text
    db.commit()
    db.refresh(seg)
    return seg


# ── Format helpers ────────────────────────────────────────────────────────────

def _speaker_name(seg: models.TranscriptSegment) -> str:
    """Return the best available speaker name for a segment.
    'Ignore' assignments render as 'unnamed speaker'."""
    if seg.assignment and seg.assignment.predicted_person:
        name = seg.assignment.predicted_person.canonical_name
        if name == "Ignore":
            return "unnamed speaker"
        return name
    return seg.raw_speaker_label or "Unknown"


def _fmt_srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_display_time(seconds: float) -> str:
    """Format seconds as a clean display timestamp: H:MM:SS"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ── Text cleanup helpers (TXT export) ───────────────────────────────────────

def _is_trivial(text: str) -> bool:
    """True if segment text is just noise (punctuation, single char, etc.)."""
    cleaned = text.strip().strip(".!?,;:")
    return len(cleaned) < 2


def _capitalize_first(text: str) -> str:
    """Capitalize the first letter of a string."""
    if not text:
        return text
    return text[0].upper() + text[1:]


def _capitalize_sentences(text: str) -> str:
    """Capitalize the first letter after sentence-ending punctuation."""
    return re.sub(
        r'([.?!])\s+([a-z])',
        lambda m: m.group(1) + " " + m.group(2).upper(),
        text,
    )


def _clean_fillers(text: str) -> str:
    """Remove standalone 'uh' and 'um' filler words (with optional comma)."""
    text = re.sub(r'\buh\b,?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bum\b,?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


def _format_phone_numbers(text: str) -> str:
    """Reassemble spaced-out digit sequences that look like phone numbers."""
    def _try_phone(match: re.Match) -> str:
        digits = re.sub(r'\D', '', match.group())
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        if len(digits) == 7:
            return f"{digits[:3]}-{digits[3:]}"
        return match.group()

    return re.sub(r'\b\d+(?:\s+\d+){2,}\b', _try_phone, text)


# ── Export formatters ────────────────────────────────────────────────────────

def _to_srt(segments: list) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        speaker = _speaker_name(seg)
        lines.append(str(i))
        lines.append(f"{_fmt_srt_time(seg.start_time)} --> {_fmt_srt_time(seg.end_time)}")
        lines.append(f"[{speaker}] {seg.text.strip()}")
        lines.append("")
    return "\n".join(lines)


def _group_by_speaker(segments: list) -> list[dict]:
    """Group consecutive segments by the same speaker into blocks."""
    groups: list[dict] = []
    for seg in segments:
        speaker = _speaker_name(seg)
        if groups and groups[-1]["speaker"] == speaker:
            groups[-1]["segments"].append(seg)
        else:
            groups.append({"speaker": speaker, "segments": [seg]})
    return groups


_PARA_TARGET_CHARS = 500  # ideal paragraph length — split at nearest sentence end
_SHORT_CONTINUATION = 60  # segments shorter than this join without a period

# Words that signal the speaker was mid-sentence when interrupted.
# If a turn ends on one of these, don't add a trailing period.
_INCOMPLETE_TRAIL_WORDS = frozenset({
    "a", "an", "the", "and", "but", "so", "or", "nor", "yet",
    "to", "of", "in", "on", "at", "by", "for", "from", "with", "about",
    "is", "was", "are", "were", "be", "been", "being",
    "i", "you", "he", "she", "it", "we", "they",
    "my", "your", "his", "her", "its", "our", "their",
    "i'm", "i've", "i'll", "i'd", "it's", "he's", "she's",
    "we're", "they're", "you're", "that's", "there's",
    "this", "that", "these", "those", "not", "just", "like",
    "if", "when", "where", "which", "who", "how", "what", "than",
})


def _split_paragraphs(text: str, target: int = _PARA_TARGET_CHARS) -> list[str]:
    """Split long text into paragraphs at sentence boundaries closest to *target*."""
    if len(text) <= target + 100:
        return [text]

    # Collect every position right after sentence-ending punctuation + space
    breaks = [m.end() for m in re.finditer(r'[.?!]\s+', text)]
    if not breaks:
        return [text]

    paragraphs: list[str] = []
    start = 0
    while start < len(text):
        # If what's left fits comfortably, take it all
        if len(text) - start <= target + 100:
            paragraphs.append(text[start:].strip())
            break

        # Find the sentence boundary closest to the ideal break point
        ideal = start + target
        best, best_dist = None, float("inf")
        for bp in breaks:
            if bp <= start:
                continue
            dist = abs(bp - ideal)
            if dist < best_dist:
                best_dist = dist
                best = bp
            # Once we're moving away from ideal, stop early
            if bp > ideal and dist > best_dist:
                break

        if best is None:
            # No usable sentence boundary — keep everything
            paragraphs.append(text[start:].strip())
            break

        paragraphs.append(text[start:best].strip())
        start = best

    return [p for p in paragraphs if p]


def _to_txt(segments: list, meeting=None) -> str:
    parts: list[str] = []

    # ── Header ───────────────────────────────────────────────────────────
    if meeting:
        parts.append("=" * 80)
        parts.append(meeting.title)
        if meeting.meeting_date:
            parts.append(f"Date: {meeting.meeting_date}")
        # Collect unique named speakers in order of appearance
        speakers, seen = [], set()
        for seg in segments:
            name = _speaker_name(seg)
            if name not in seen and name not in ("Unknown", "unnamed speaker"):
                seen.add(name)
                speakers.append(name)
        if speakers:
            parts.append(f"Speakers: {', '.join(speakers)}")
        parts.append("=" * 80)
        parts.append("")

    # ── Body ─────────────────────────────────────────────────────────────
    for group in _group_by_speaker(segments):
        # Drop trivial noise segments
        valid = [s for s in group["segments"] if not _is_trivial(s.text)]
        if not valid:
            continue

        start = _fmt_display_time(valid[0].start_time)
        parts.append(f"[{group['speaker']}] {start}")

        # 1. Split segments into natural groups at >2s silence gaps
        natural_groups: list[list[str]] = []
        current: list[str] = []
        for i, seg in enumerate(valid):
            text = seg.text.strip()
            if not text:
                continue
            if i > 0 and current:
                gap = seg.start_time - valid[i - 1].end_time
                if gap > 2.0:
                    natural_groups.append(current)
                    current = []
            current.append(text)
        if current:
            natural_groups.append(current)

        # 2. Within each natural group, join segments with punctuation logic
        joined_groups: list[str] = []
        for seg_texts in natural_groups:
            joined = ""
            for j, t in enumerate(seg_texts):
                if j == 0:
                    joined = t
                    continue
                prev_has_punct = joined and joined[-1] in ".?!"
                prev_is_short = len(joined) < 30
                is_substantial = len(t) >= _SHORT_CONTINUATION
                if prev_has_punct:
                    joined += " " + _capitalize_first(t)
                elif is_substantial and not prev_is_short:
                    joined += ". " + _capitalize_first(t)
                else:
                    joined += " " + t

            joined = _clean_fillers(joined)
            joined = _capitalize_first(joined)
            joined = _capitalize_sentences(joined)
            joined = _format_phone_numbers(joined)
            joined_groups.append(joined)

        # 3. Split long groups into paragraphs at the nearest sentence end
        all_paragraphs: list[str] = []
        for text in joined_groups:
            all_paragraphs.extend(_split_paragraphs(text))

        # 4. Add a trailing period only if the turn ends on a content word
        #    (i.e. a complete thought).  Skip if it trails off on a
        #    conjunction / preposition / article — the speaker was interrupted.
        if all_paragraphs:
            last = all_paragraphs[-1]
            if last and last[-1] not in ".?!":
                final_word = last.rstrip().split()[-1].lower().strip(".,;:'\"")
                if final_word not in _INCOMPLETE_TRAIL_WORDS:
                    all_paragraphs[-1] = last + "."

        # 5. Word-wrap each paragraph and assemble
        formatted = [textwrap.fill(p, width=80) for p in all_paragraphs]
        parts.append("\n\n".join(formatted))
        parts.append("")  # blank line between speaker turns

    return "\n".join(parts)


def _to_json(segments: list) -> str:
    data = [
        {
            "segment_id": seg.segment_id,
            "start": seg.start_time,
            "end": seg.end_time,
            "speaker": _speaker_name(seg),
            "text": seg.text.strip(),
            "verified": seg.assignment.verified if seg.assignment else False,
        }
        for seg in segments
    ]
    return json.dumps(data, indent=2, ensure_ascii=False)

"""
Transcript segment endpoints.

GET    /api/segments/{meeting_id}            — all segments, optional filter.
GET    /api/segments/{meeting_id}/{seg_id}   — single segment detail.
PATCH  /api/segments/{segment_id}/edit       — manually correct segment text.
GET    /api/segments/{meeting_id}/export     — download as SRT, TXT, or JSON.
"""

import json

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
        description="Filter preset: 'unknown' | 'low_confidence'",
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

    return segments


# ── Single segment ────────────────────────────────────────────────────────────

@router.get("/{meeting_id}/export")
def export_transcript(
    meeting_id: str,
    format: str = Query("srt", description="Export format: srt | txt | json"),
    db: Session = Depends(get_db),
):
    """
    Export the full transcript in SRT, plain text, or JSON format.
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
        content = _to_txt(segments)
        filename = f"{safe_title}.txt"
        media_type = "text/plain; charset=utf-8"
    elif fmt == "json":
        content = _to_json(segments)
        filename = f"{safe_title}.json"
        media_type = "application/json; charset=utf-8"
    else:
        raise HTTPException(400, "format must be srt, txt, or json")

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
    """Return the best available speaker name for a segment."""
    if seg.assignment and seg.assignment.predicted_person:
        return seg.assignment.predicted_person.canonical_name
    return seg.raw_speaker_label or "Unknown"


def _fmt_srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _to_srt(segments: list) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        speaker = _speaker_name(seg)
        lines.append(str(i))
        lines.append(f"{_fmt_srt_time(seg.start_time)} --> {_fmt_srt_time(seg.end_time)}")
        lines.append(f"[{speaker}] {seg.text.strip()}")
        lines.append("")
    return "\n".join(lines)


def _to_txt(segments: list) -> str:
    lines = []
    current_speaker = None
    for seg in segments:
        speaker = _speaker_name(seg)
        if speaker != current_speaker:
            if lines:
                lines.append("")
            lines.append(f"{speaker}:")
            current_speaker = speaker
        lines.append(f"  {seg.text.strip()}")
    return "\n".join(lines)


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

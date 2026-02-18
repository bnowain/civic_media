"""
Transcript segment read endpoints.

GET /api/segments/{meeting_id}           — all segments, optional filter.
GET /api/segments/{meeting_id}/{seg_id}  — single segment detail.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app import models, schemas
from app.config import SIMILARITY_MEDIUM
from app.database import get_db

router = APIRouter(prefix="/api/segments", tags=["segments"])


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

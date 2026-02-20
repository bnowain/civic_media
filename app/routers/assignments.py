"""
Speaker assignment endpoints — the voiceprint learning loop.

POST /api/assignments/{segment_id}/confirm    — confirm or correct a speaker.
POST /api/assignments/{segment_id}/reprocess  — re-match one unverified segment.
POST /api/assignments/reprocess/{meeting_id}  — re-match all unverified in a meeting.
GET  /api/assignments/{segment_id}            — read current assignment for a segment.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.services import voiceprint as vp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assignments", tags=["assignments"])


@router.post("/{segment_id}/confirm", response_model=schemas.AssignmentOut)
def confirm_assignment(
    segment_id: str,
    payload: schemas.ConfirmAssignment,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Core learning endpoint.

    When a user confirms or corrects a speaker:
      1. The segment's embedding is stored as a new voiceprint for that person.
      2. The assignment is marked verified and committed immediately.
      3. A background Celery task re-evaluates all unverified segments so the
         HTTP response returns instantly (no blocking on 1700+ re-evaluations).

    Embeddings are NEVER overwritten — each confirmation adds a new row.
    Verified assignments are NEVER touched by automatic matching.
    """
    segment = db.query(models.TranscriptSegment).filter_by(segment_id=segment_id).first()
    if not segment:
        raise HTTPException(404, "Segment not found")

    person = db.query(models.Person).filter_by(person_id=payload.person_id).first()
    if not person:
        raise HTTPException(404, "Person not found")

    # 1. Add embedding as a new voiceprint (additive — never overwrites)
    #    Quality gate: skip segments shorter than MIN_VOICEPRINT_DURATION —
    #    ECAPA-TDNN needs ~2s of speech to produce a reliable embedding.
    from app.services.voiceprint import MIN_VOICEPRINT_DURATION

    seg_duration = segment.end_time - segment.start_time

    if segment.embedding and seg_duration >= MIN_VOICEPRINT_DURATION:
        new_vp = models.Voiceprint(
            person_id=payload.person_id,
            embedding=segment.embedding,
        )
        db.add(new_vp)
        logger.info(
            "Added voiceprint for person '%s' from segment %s (%.1fs)",
            person.canonical_name, segment_id, seg_duration,
        )
    elif segment.embedding:
        logger.info(
            "Segment %s too short for voiceprint (%.1fs < %.1fs) — "
            "assignment confirmed but voiceprint not added.",
            segment_id, seg_duration, MIN_VOICEPRINT_DURATION,
        )
    else:
        logger.warning(
            "Segment %s has no embedding — voiceprint not added, assignment still confirmed.",
            segment_id,
        )

    # 2. Create or update the assignment row
    assign = segment.assignment
    if assign is None:
        assign = models.SegmentAssignment(segment_id=segment_id)
        db.add(assign)

    assign.predicted_person_id = payload.person_id
    assign.similarity_score    = 1.0   # human-confirmed = perfect confidence
    assign.verified            = True

    db.commit()
    db.refresh(assign)

    # 3. Dispatch re-evaluation to background — returns immediately to UI
    from app.tasks import rerun_voiceprints_task
    rerun_voiceprints_task.delay(segment.meeting_id)
    logger.info(
        "Queued background voiceprint re-evaluation for meeting %s",
        segment.meeting_id,
    )

    return assign


@router.post("/{segment_id}/reprocess", response_model=schemas.AssignmentOut)
def reprocess_segment(segment_id: str, db: Session = Depends(get_db)):
    """
    Force re-match a single unverified segment against current voiceprints.
    Verified segments cannot be reprocessed (use /confirm to change them).
    """
    segment = db.query(models.TranscriptSegment).filter_by(segment_id=segment_id).first()
    if not segment:
        raise HTTPException(404, "Segment not found")

    if segment.assignment and segment.assignment.verified:
        raise HTTPException(400, "Segment is verified — use /confirm to reassign.")

    vp_service.run_voiceprint_matching(db, segment)
    db.refresh(segment)

    if segment.assignment is None:
        raise HTTPException(500, "Assignment not created — segment may lack an embedding.")

    return segment.assignment


@router.post("/reprocess/{meeting_id}")
def reprocess_meeting(meeting_id: str, db: Session = Depends(get_db)):
    """
    Re-run voiceprint matching for all unverified segments in a meeting.
    Triggered manually from the review interface — runs in background.
    """
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    from app.tasks import rerun_voiceprints_task
    rerun_voiceprints_task.delay(meeting_id)
    return {"meeting_id": meeting_id, "status": "queued"}


@router.get("/{segment_id}", response_model=schemas.AssignmentOut)
def get_assignment(segment_id: str, db: Session = Depends(get_db)):
    assign = (
        db.query(models.SegmentAssignment)
        .filter_by(segment_id=segment_id)
        .first()
    )
    if not assign:
        raise HTTPException(404, "No assignment for this segment")
    return assign

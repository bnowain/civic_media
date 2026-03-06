"""
Speaker assignment endpoints — the voiceprint learning loop.

POST /api/assignments/{segment_id}/confirm    — confirm or correct a speaker.
POST /api/assignments/{segment_id}/unconfirm  — remove confirmation and voiceprints.
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

    from app.services.voiceprint import MIN_VOICEPRINT_DURATION
    from app.config import MAX_OVERLAP_FOR_VOICEPRINT

    seg_duration = segment.end_time - segment.start_time
    seg_overlap = segment.overlap_ratio or 0.0
    assign = segment.assignment

    # ── Handle re-confirmation to a DIFFERENT person ──────────────────────
    # Delete ALL old voiceprints created from this segment (including
    # multi-clip extras) so they don't poison the previous person's pool.
    if assign and assign.verified and assign.predicted_person_id != payload.person_id:
        old_person_id = assign.predicted_person_id
        # Strategy 1: delete all by source_segment_id (post-migration voiceprints)
        deleted_count = db.query(models.Voiceprint).filter_by(
            source_segment_id=segment_id,
            person_id=old_person_id,
        ).delete(synchronize_session="fetch")

        # Strategy 2: byte-equality fallback (pre-migration voiceprints without source_segment_id)
        if deleted_count == 0 and segment.embedding:
            old_vp = db.query(models.Voiceprint).filter_by(
                person_id=old_person_id,
                embedding=segment.embedding,
            ).first()
            if old_vp:
                db.delete(old_vp)
                deleted_count = 1

        if deleted_count:
            logger.info(
                "Deleted %d old voiceprint(s) from person %s (re-confirm segment %s)",
                deleted_count, old_person_id, segment_id,
            )
        else:
            logger.warning(
                "Re-confirm: could not find old voiceprints for segment %s / person %s "
                "(segment may have been too short to create any)",
                segment_id, old_person_id,
            )

    # ── Create new voiceprint ─────────────────────────────────────────────
    #    Quality gate: skip segments shorter than MIN_VOICEPRINT_DURATION —
    #    ECAPA-TDNN needs ~2s of speech to produce a reliable embedding.

    # Resolve effective venue: meeting-level override > governing body default
    meeting = db.query(models.Meeting).filter_by(meeting_id=segment.meeting_id).first()
    effective_venue_id = None
    if meeting:
        effective_venue_id = meeting.venue_id or (
            meeting.group_ref.default_venue_id
            if meeting.group_ref else None
        )

    if segment.embedding and seg_duration >= MIN_VOICEPRINT_DURATION and seg_overlap <= MAX_OVERLAP_FOR_VOICEPRINT:
        new_vp = models.Voiceprint(
            person_id=payload.person_id,
            embedding=segment.embedding,
            source_segment_id=segment_id,
            venue_id=effective_venue_id,
            source_type=segment.source_type,
            source_duration=seg_duration,
        )
        db.add(new_vp)
        logger.info(
            "Added voiceprint for person '%s' from segment %s (%.1fs, overlap=%.2f)",
            person.canonical_name, segment_id, seg_duration, seg_overlap,
        )
    elif segment.embedding and seg_overlap > MAX_OVERLAP_FOR_VOICEPRINT:
        logger.info(
            "Segment %s has too much speaker overlap (%.0f%% > %.0f%%) — "
            "assignment confirmed but voiceprint not added.",
            segment_id, seg_overlap * 100, MAX_OVERLAP_FOR_VOICEPRINT * 100,
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

    # ── Update assignment row ─────────────────────────────────────────────
    if assign is None:
        assign = models.SegmentAssignment(segment_id=segment_id)
        db.add(assign)

    assign.predicted_person_id = payload.person_id
    assign.similarity_score    = 1.0   # human-confirmed = perfect confidence
    assign.verified            = True
    assign.tagged              = False  # upgrade from tag to confirm

    db.commit()
    db.refresh(assign)

    # 3. Dispatch background tasks — returns immediately to UI.
    #    Multi-clip extraction is queued BEFORE rerun so that with
    #    concurrency=1 the extra voiceprints exist by the time
    #    re-evaluation starts.
    #    If workers are busy (solo pool can't respond to ping while processing),
    #    the tasks still go to Redis and will be picked up when a worker is free.
    try:
        from app.config import MULTI_CLIP_MIN_SEGMENT
        from app.services.task_dispatch import send_task

        if segment.embedding and seg_duration >= MULTI_CLIP_MIN_SEGMENT:
            send_task("tasks.extract_multi_voiceprints", args=[segment_id, payload.person_id])
            logger.info(
                "Queued multi-clip voiceprint extraction for segment %s (%.1fs)",
                segment_id, seg_duration,
            )

        send_task("tasks.rerun_voiceprints", args=[segment.meeting_id])
        logger.info(
            "Queued background voiceprint re-evaluation for meeting %s",
            segment.meeting_id,
        )
    except Exception as e:
        logger.warning(
            "Could not queue background tasks for segment %s: %s (confirm still saved)",
            segment_id, e,
        )

    return assign


@router.post("/{segment_id}/unconfirm", response_model=schemas.AssignmentOut)
def unconfirm_assignment(
    segment_id: str,
    db: Session = Depends(get_db),
):
    """
    Remove a confirmed speaker assignment and delete its voiceprints.

    Resets the segment to unverified so automatic matching can re-evaluate it.
    Triggers a background rerun so the system immediately re-matches.
    """
    segment = db.query(models.TranscriptSegment).filter_by(segment_id=segment_id).first()
    if not segment:
        raise HTTPException(404, "Segment not found")

    assign = segment.assignment
    if not assign or not assign.verified:
        raise HTTPException(400, "Segment is not confirmed — nothing to unconfirm.")

    old_person_id = assign.predicted_person_id

    # ── Delete voiceprints created from this segment ───────────────────────
    deleted_count = db.query(models.Voiceprint).filter_by(
        source_segment_id=segment_id,
        person_id=old_person_id,
    ).delete(synchronize_session="fetch")

    # Byte-equality fallback for pre-migration voiceprints
    if deleted_count == 0 and segment.embedding:
        old_vp = db.query(models.Voiceprint).filter_by(
            person_id=old_person_id,
            embedding=segment.embedding,
        ).first()
        if old_vp:
            db.delete(old_vp)
            deleted_count = 1

    logger.info(
        "Unconfirm: deleted %d voiceprint(s) from person %s for segment %s",
        deleted_count, old_person_id, segment_id,
    )

    # ── Reset assignment to unverified ─────────────────────────────────────
    assign.verified = False
    assign.similarity_score = None
    assign.tagged = False

    db.commit()
    db.refresh(assign)

    # ── Trigger background rerun so system re-matches this segment ─────────
    try:
        from app.services.task_dispatch import send_task
        send_task("tasks.rerun_voiceprints", args=[segment.meeting_id])
        logger.info(
            "Queued background voiceprint re-evaluation for meeting %s (unconfirm)",
            segment.meeting_id,
        )
    except Exception as e:
        logger.warning(
            "Could not queue rerun for meeting %s: %s (unconfirm still saved)",
            segment.meeting_id, e,
        )

    return assign


@router.post("/{segment_id}/tag", response_model=schemas.AssignmentOut)
def tag_assignment(
    segment_id: str,
    payload: schemas.TagAssignment,
    db: Session = Depends(get_db),
):
    """
    Tag a speaker for display only — no voiceprint created, no rerun triggered.
    Tagged segments are protected from auto-overwrite but can be upgraded to
    confirmed later.
    """
    segment = db.query(models.TranscriptSegment).filter_by(segment_id=segment_id).first()
    if not segment:
        raise HTTPException(404, "Segment not found")

    person = db.query(models.Person).filter_by(person_id=payload.person_id).first()
    if not person:
        raise HTTPException(404, "Person not found")

    assign = segment.assignment
    if assign is None:
        assign = models.SegmentAssignment(segment_id=segment_id)
        db.add(assign)

    assign.predicted_person_id = payload.person_id
    assign.similarity_score = None    # no voiceprint match — manual tag
    assign.tagged = True
    assign.verified = False           # tagged is NOT confirmed

    db.commit()
    db.refresh(assign)
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

    # Resolve effective venue for venue-aware matching
    mtg = db.query(models.Meeting).filter_by(meeting_id=segment.meeting_id).first()
    effective_venue_id = None
    if mtg:
        effective_venue_id = mtg.venue_id or (
            mtg.group_ref.default_venue_id
            if mtg.group_ref else None
        )

    vp_service.run_voiceprint_matching(db, segment, venue_id=effective_venue_id)
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

    from app.services.task_dispatch import send_task
    send_task("tasks.rerun_voiceprints", args=[meeting_id])
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

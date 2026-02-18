"""
Voiceprint learning engine.

This is the core of Phase 1. It implements:
  - Cosine similarity between speaker embeddings.
  - On-demand centroid computation (mean of all embeddings per person).
  - Segment-to-person matching.
  - Batch re-evaluation of unverified segments after new confirmations.

Design principles:
  - Centroids are never stored; they are always computed fresh from raw embeddings.
  - Old embeddings are never deleted.
  - Verified assignments are never overwritten by automatic matching.
  - All learning is purely additive.
"""

from __future__ import annotations

import logging

import numpy as np
from sqlalchemy.orm import Session

from app.config import SIMILARITY_MEDIUM
from app.models import Person, SegmentAssignment, TranscriptSegment, Voiceprint
from app.services.embedder import deserialize

logger = logging.getLogger(__name__)


# ── Maths ─────────────────────────────────────────────────────────────────────

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-8:
        return 0.0
    return float(np.dot(a, b) / denom)


def compute_centroid(embeddings: list[np.ndarray]) -> np.ndarray:
    """Mean of a list of embedding arrays."""
    return np.mean(np.stack(embeddings, axis=0), axis=0)


# ── Per-person centroid ───────────────────────────────────────────────────────

def get_person_centroid(db: Session, person_id: str) -> np.ndarray | None:
    """
    Compute the centroid embedding for a person from all their stored voiceprints.
    Returns None if the person has no voiceprints.
    """
    vps = db.query(Voiceprint).filter(Voiceprint.person_id == person_id).all()
    if not vps:
        return None
    arrays = [deserialize(vp.embedding) for vp in vps]
    return compute_centroid(arrays)


# ── Single-segment matching ───────────────────────────────────────────────────

def run_voiceprint_matching(db: Session, segment: TranscriptSegment) -> None:
    """
    Match one segment against all known person centroids.
    Updates (or creates) the segment's SegmentAssignment row.
    Does NOT touch verified assignments.
    """
    if segment.embedding is None:
        return

    seg_vec = deserialize(segment.embedding)
    people  = db.query(Person).all()

    best_person = None
    best_score  = 0.0

    for person in people:
        centroid = get_person_centroid(db, person.person_id)
        if centroid is None:
            continue
        score = cosine_similarity(seg_vec, centroid)
        if score > best_score:
            best_score  = score
            best_person = person

    # Fetch or create assignment row
    assign = segment.assignment
    if assign is None:
        assign = SegmentAssignment(segment_id=segment.segment_id)
        db.add(assign)

    # Only auto-update if NOT verified by a human
    if not assign.verified:
        if best_person and best_score >= SIMILARITY_MEDIUM:
            assign.predicted_person_id = best_person.person_id
            assign.similarity_score    = round(best_score, 4)
        else:
            # Record the score even if below threshold so UI can display it
            assign.predicted_person_id = None
            assign.similarity_score    = round(best_score, 4) if best_person else None

    db.commit()


# ── Batch re-evaluation ───────────────────────────────────────────────────────

def rerun_unverified_segments(
    db: Session,
    meeting_id: str | None = None,
) -> int:
    """
    Re-evaluate all unverified and unassigned segments.

    Called immediately after a human confirms an assignment so that
    the new voiceprint improves predictions across the board.

    Args:
        db:         Active database session.
        meeting_id: If given, restrict re-evaluation to one meeting.

    Returns:
        Number of segments re-evaluated.
    """
    query = (
        db.query(TranscriptSegment)
        .outerjoin(SegmentAssignment,
                   TranscriptSegment.segment_id == SegmentAssignment.segment_id)
        .filter(
            (SegmentAssignment.verified.is_(False)) |
            (SegmentAssignment.assignment_id.is_(None))
        )
    )

    if meeting_id:
        query = query.filter(TranscriptSegment.meeting_id == meeting_id)

    segments = query.all()
    logger.info("Re-evaluating %d unverified segments...", len(segments))

    for seg in segments:
        run_voiceprint_matching(db, seg)

    logger.info("Batch re-evaluation complete.")
    return len(segments)


# ── Confidence label ──────────────────────────────────────────────────────────

def confidence_label(score: float | None) -> str:
    """Convert a similarity score to a human-readable confidence tier."""
    if score is None:
        return "unknown"
    from app.config import SIMILARITY_HIGH
    if score >= SIMILARITY_HIGH:
        return "high"
    if score >= SIMILARITY_MEDIUM:
        return "medium"
    return "unknown"

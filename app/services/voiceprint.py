"""
Voiceprint learning engine.

This is the core of Phase 1. It implements:
  - Cosine similarity between speaker embeddings.
  - Top-K similarity matching (compare against individual voiceprints,
    average the best K scores per person).
  - Segment-to-person matching.
  - Batch re-evaluation of unverified segments after new confirmations.

Design principles:
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

# Number of best-matching voiceprints to average when scoring a person.
# Using top-K instead of a single centroid avoids dilution from noisy
# embeddings and captures natural speaker variability (mic distance, etc.).
TOP_K = 3

# Minimum segment duration (seconds) for a voiceprint to be useful.
# ECAPA-TDNN needs ~2s of speech to produce a reliable embedding.
# This is checked at voiceprint creation time, not at matching time.
MIN_VOICEPRINT_DURATION = 2.0


# ── Maths ─────────────────────────────────────────────────────────────────────

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-8:
        return 0.0
    return float(np.dot(a, b) / denom)


def top_k_similarity(
    seg_vec: np.ndarray,
    voiceprint_arrays: list[np.ndarray],
    k: int = TOP_K,
) -> float:
    """
    Compare a segment embedding against a list of voiceprint embeddings.
    Returns the mean of the top-K cosine similarities.

    If fewer than K voiceprints exist, averages all of them.
    """
    if not voiceprint_arrays:
        return 0.0

    # Vectorised cosine similarity: stack voiceprints into a matrix,
    # compute all dot products in one shot.
    vp_matrix = np.stack(voiceprint_arrays, axis=0)          # (N, D)
    norms = np.linalg.norm(vp_matrix, axis=1)                # (N,)
    seg_norm = np.linalg.norm(seg_vec)
    denoms = norms * seg_norm
    # Avoid division by zero
    safe = denoms > 1e-8
    scores = np.zeros(len(voiceprint_arrays), dtype=np.float64)
    scores[safe] = vp_matrix[safe] @ seg_vec / denoms[safe]

    # Average the top-K scores
    top_k_scores = np.sort(scores)[-k:]
    return float(np.mean(top_k_scores))


# ── Load all voiceprints into memory ─────────────────────────────────────────

def _load_all_voiceprints(db: Session) -> dict[str, list[np.ndarray]]:
    """
    Load every voiceprint from the DB, grouped by person_id.
    Returns {person_id: [np.ndarray, ...]}.

    Called once at the start of a batch re-evaluation so we don't
    re-query per segment.
    """
    all_vps = db.query(Voiceprint).all()
    by_person: dict[str, list[np.ndarray]] = {}
    for vp in all_vps:
        arr = deserialize(vp.embedding)
        by_person.setdefault(vp.person_id, []).append(arr)
    return by_person


# ── Single-segment matching ───────────────────────────────────────────────────

def run_voiceprint_matching(
    db: Session,
    segment: TranscriptSegment,
    preloaded: dict[str, list[np.ndarray]] | None = None,
    person_map: dict[str, Person] | None = None,
) -> None:
    """
    Match one segment against all known person voiceprints using top-K
    similarity. Updates (or creates) the segment's SegmentAssignment row.
    Does NOT touch verified assignments.

    Args:
        db:         Active database session.
        segment:    The segment to match.
        preloaded:  Optional pre-loaded voiceprints from _load_all_voiceprints().
                    If None, voiceprints are loaded from the DB per-call.
        person_map: Optional {person_id: Person} lookup. If None, queried from DB.
    """
    if segment.embedding is None:
        return

    seg_vec = deserialize(segment.embedding)

    # Use preloaded data if available, otherwise query per-call
    if preloaded is not None:
        vp_by_person = preloaded
    else:
        vp_by_person = _load_all_voiceprints(db)

    if person_map is not None:
        people_lookup = person_map
    else:
        people_lookup = {p.person_id: p for p in db.query(Person).all()}

    best_person = None
    best_score  = 0.0

    for person_id, vp_arrays in vp_by_person.items():
        score = top_k_similarity(seg_vec, vp_arrays)
        if score > best_score:
            best_score  = score
            best_person = people_lookup.get(person_id)

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

    db.flush()


# ── Batch re-evaluation ───────────────────────────────────────────────────────

def rerun_unverified_segments(
    db: Session,
    meeting_id: str | None = None,
) -> int:
    """
    Re-evaluate all unverified and unassigned segments.

    Called immediately after a human confirms an assignment so that
    the new voiceprint improves predictions across the board.

    Voiceprints and person records are loaded once up front to avoid
    redundant DB queries per segment.

    Args:
        db:         Active database session.
        meeting_id: If given, restrict re-evaluation to one meeting.

    Returns:
        Number of segments re-evaluated.
    """
    # Pre-load all voiceprints and people once
    preloaded  = _load_all_voiceprints(db)
    person_map = {p.person_id: p for p in db.query(Person).all()}

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
        run_voiceprint_matching(db, seg, preloaded=preloaded, person_map=person_map)

    db.commit()
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

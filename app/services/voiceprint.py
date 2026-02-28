"""
Voiceprint learning engine.

This is the core of Phase 1. It implements:
  - Cosine similarity between speaker embeddings.
  - Top-K similarity matching (compare against individual voiceprints,
    average the best K scores per person).
  - Coherence gate (exclude voiceprints that are outliers for their person).
  - Segment-to-person matching.
  - Batch re-evaluation of unverified segments after new confirmations.

Design principles:
  - Old embeddings are never deleted by automatic matching.
  - Verified assignments are never overwritten by automatic matching.
  - All learning is purely additive.
  - Outlier voiceprints are excluded from matching, not deleted.
"""

from __future__ import annotations

import logging

import numpy as np
from sqlalchemy.orm import Session

from app.config import SIMILARITY_MEDIUM, VENUE_FAMILIARITY_BOOST, VOICEPRINT_COHERENCE_THRESHOLD
from app.models import Person, SegmentAssignment, TranscriptSegment, Voiceprint
from app.services.embedder import deserialize

# Default duration assumed for voiceprints with no source_duration recorded.
DEFAULT_VOICEPRINT_DURATION = 5.0

logger = logging.getLogger(__name__)

# Number of best-matching voiceprints to average when scoring a person.
# Using top-K instead of a single centroid avoids dilution from noisy
# embeddings and captures natural speaker variability (mic distance, etc.).
TOP_K = 3

# Minimum segment duration (seconds) for a voiceprint to be useful.
# ECAPA-TDNN needs ~2s of speech to produce a reliable embedding,
# plus margins (0.5s start + 2.0s end) are trimmed from both edges
# to avoid speaker bleed.  4s total ≈ 1.5s usable center audio.
MIN_VOICEPRINT_DURATION = 4.0


# ── Maths ─────────────────────────────────────────────────────────────────────

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-8:
        return 0.0
    return float(np.dot(a, b) / denom)


def top_k_similarity(
    seg_vec: np.ndarray,
    voiceprint_entries: list[tuple[str, np.ndarray, str | None, float | None]],
    k: int = TOP_K,
    venue_id: str | None = None,
    venue_boost: float = 0.0,
) -> tuple[float, list[str]]:
    """
    Compare a segment embedding against a list of voiceprint embeddings.
    Returns (weighted_mean_of_top_k_scores, [voiceprint_ids_in_top_k]).

    Venue boost: if venue_id is set and a voiceprint's venue matches,
    its score is boosted by venue_boost before top-K selection.

    Duration weighting: top-K scores are averaged with weights proportional
    to source_duration (longer segments = more reliable embeddings).

    If fewer than K voiceprints exist, averages all of them.
    """
    if not voiceprint_entries:
        return 0.0, []

    vp_ids = [vp_id for vp_id, _, _, _ in voiceprint_entries]
    vp_arrays = [arr for _, arr, _, _ in voiceprint_entries]
    vp_venues = [vid for _, _, vid, _ in voiceprint_entries]
    vp_durations = [dur for _, _, _, dur in voiceprint_entries]

    # Vectorised cosine similarity: stack voiceprints into a matrix,
    # compute all dot products in one shot.
    vp_matrix = np.stack(vp_arrays, axis=0)          # (N, D)
    norms = np.linalg.norm(vp_matrix, axis=1)        # (N,)
    seg_norm = np.linalg.norm(seg_vec)
    denoms = norms * seg_norm
    # Avoid division by zero
    safe = denoms > 1e-8
    scores = np.zeros(len(vp_arrays), dtype=np.float64)
    scores[safe] = vp_matrix[safe] @ seg_vec / denoms[safe]

    # Apply venue boost before top-K selection
    if venue_id and venue_boost > 0:
        for i, vp_venue in enumerate(vp_venues):
            if vp_venue == venue_id:
                scores[i] += venue_boost

    # Get indices of top-K scores
    if len(scores) <= k:
        top_indices = list(range(len(scores)))
    else:
        top_indices = np.argpartition(scores, -k)[-k:].tolist()

    top_k_scores = scores[top_indices]
    top_k_vp_ids = [vp_ids[i] for i in top_indices]

    # Duration-weighted mean of top-K scores
    weights = np.array([
        vp_durations[i] if vp_durations[i] is not None else DEFAULT_VOICEPRINT_DURATION
        for i in top_indices
    ], dtype=np.float64)
    weight_sum = weights.sum()
    if weight_sum > 0:
        weighted_score = float(np.dot(top_k_scores, weights) / weight_sum)
    else:
        weighted_score = float(np.mean(top_k_scores))

    return weighted_score, top_k_vp_ids


# ── Load all voiceprints into memory ─────────────────────────────────────────

def _load_all_voiceprints(db: Session) -> dict[str, list[tuple[str, np.ndarray, str | None, float | None]]]:
    """
    Load every voiceprint from the DB, grouped by person_id.
    Returns {person_id: [(voiceprint_id, np.ndarray, venue_id, source_duration), ...]}.

    Called once at the start of a batch re-evaluation so we don't
    re-query per segment.
    """
    all_vps = db.query(Voiceprint).all()
    by_person: dict[str, list[tuple[str, np.ndarray, str | None, float | None]]] = {}
    for vp in all_vps:
        arr = deserialize(vp.embedding)
        by_person.setdefault(vp.person_id, []).append(
            (vp.voiceprint_id, arr, vp.venue_id, getattr(vp, "source_duration", None))
        )
    return by_person


# ── Coherence gate ───────────────────────────────────────────────────────────

def _apply_coherence_gate(
    vp_by_person: dict[str, list[tuple[str, np.ndarray, str | None, float | None]]],
    threshold: float = VOICEPRINT_COHERENCE_THRESHOLD,
) -> dict[str, list[tuple[str, np.ndarray, str | None, float | None]]]:
    """
    Filter out voiceprints that are outliers for their person.

    For each person, computes the centroid (mean) of all their voiceprints,
    then excludes any voiceprint with cosine similarity to the centroid
    below the threshold. This prevents bad confirmations (wrong person's
    voice) from polluting matching, without deleting the voiceprint.

    Safety: never excludes ALL voiceprints for a person — always keeps
    the one closest to the centroid.
    """
    filtered: dict[str, list[tuple[str, np.ndarray, str | None, float | None]]] = {}

    for person_id, entries in vp_by_person.items():
        if len(entries) <= 1:
            # Single voiceprint — no coherence check possible
            filtered[person_id] = entries
            continue

        arrays = np.stack([arr for _, arr, _, _ in entries], axis=0)  # (N, D)
        centroid = arrays.mean(axis=0)                                # (D,)

        # Cosine similarity of each voiceprint to centroid
        norms = np.linalg.norm(arrays, axis=1)
        centroid_norm = np.linalg.norm(centroid)
        denoms = norms * centroid_norm
        safe = denoms > 1e-8
        scores = np.zeros(len(entries), dtype=np.float64)
        scores[safe] = arrays[safe] @ centroid / denoms[safe]

        # Keep voiceprints above threshold
        kept = [
            (entries[i], scores[i])
            for i in range(len(entries))
            if scores[i] >= threshold
        ]

        if not kept:
            # All below threshold — keep the one closest to centroid
            best_idx = int(np.argmax(scores))
            kept = [(entries[best_idx], scores[best_idx])]
            logger.debug(
                "Coherence gate: all %d voiceprints for person %s below %.2f; "
                "keeping best (%.3f)",
                len(entries), person_id, threshold, scores[best_idx],
            )

        excluded_count = len(entries) - len(kept)
        if excluded_count > 0:
            logger.debug(
                "Coherence gate: excluded %d/%d voiceprints for person %s",
                excluded_count, len(entries), person_id,
            )

        filtered[person_id] = [entry for entry, _ in kept]

    return filtered


# ── Single-segment matching ───────────────────────────────────────────────────

def run_voiceprint_matching(
    db: Session,
    segment: TranscriptSegment,
    preloaded: dict[str, list[tuple[str, np.ndarray, str | None, float | None]]] | None = None,
    person_map: dict[str, Person] | None = None,
    venue_id: str | None = None,
) -> list[str]:
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
        venue_id:   Optional venue to apply familiarity boost for same-venue voiceprints.

    Returns:
        List of voiceprint IDs that were in the top-K for the winning person.
    """
    if segment.embedding is None:
        return []

    seg_vec = deserialize(segment.embedding)

    # Use preloaded data if available, otherwise query per-call
    if preloaded is not None:
        vp_by_person = preloaded
    else:
        vp_by_person = _apply_coherence_gate(_load_all_voiceprints(db))

    if person_map is not None:
        people_lookup = person_map
    else:
        people_lookup = {p.person_id: p for p in db.query(Person).all()}

    best_person = None
    best_score  = 0.0
    best_vp_ids: list[str] = []

    for person_id, vp_entries in vp_by_person.items():
        score, vp_ids = top_k_similarity(
            seg_vec, vp_entries,
            venue_id=venue_id, venue_boost=VENUE_FAMILIARITY_BOOST,
        )
        if score > best_score:
            best_score  = score
            best_person = people_lookup.get(person_id)
            best_vp_ids = vp_ids

    # Fetch or create assignment row
    assign = segment.assignment
    if assign is None:
        assign = SegmentAssignment(segment_id=segment.segment_id)
        db.add(assign)

    # Only auto-update if NOT verified or tagged by a human
    if not assign.verified and not assign.tagged:
        if best_person and best_score >= SIMILARITY_MEDIUM:
            assign.predicted_person_id = best_person.person_id
            assign.similarity_score    = round(best_score, 4)
        else:
            # Record the score even if below threshold so UI can display it
            assign.predicted_person_id = None
            assign.similarity_score    = round(best_score, 4) if best_person else None
            best_vp_ids = []   # No match above threshold — don't credit VPs

    db.flush()
    return best_vp_ids


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
    redundant DB queries per segment. A coherence gate filters out
    outlier voiceprints before matching begins.

    Args:
        db:         Active database session.
        meeting_id: If given, restrict re-evaluation to one meeting.

    Returns:
        Number of segments re-evaluated.
    """
    # Pre-load all voiceprints and people once
    raw_voiceprints = _load_all_voiceprints(db)
    preloaded = _apply_coherence_gate(raw_voiceprints)
    person_map = {p.person_id: p for p in db.query(Person).all()}

    # Log coherence gate summary
    total_raw = sum(len(v) for v in raw_voiceprints.values())
    total_filtered = sum(len(v) for v in preloaded.values())
    if total_raw != total_filtered:
        logger.info(
            "Coherence gate: using %d/%d voiceprints (%d excluded)",
            total_filtered, total_raw, total_raw - total_filtered,
        )

    # Resolve effective venue for the meeting (meeting-level > governing body default)
    from app.models import Meeting
    effective_venue_id = None
    if meeting_id:
        mtg = db.query(Meeting).filter_by(meeting_id=meeting_id).first()
        if mtg:
            effective_venue_id = mtg.venue_id or (
                mtg.group_ref.default_venue_id
                if mtg.group_ref else None
            )

    query = (
        db.query(TranscriptSegment)
        .outerjoin(SegmentAssignment,
                   TranscriptSegment.segment_id == SegmentAssignment.segment_id)
        .filter(
            (SegmentAssignment.assignment_id.is_(None)) |
            (
                (SegmentAssignment.verified.is_(False)) &
                (SegmentAssignment.tagged.is_(False))
            )
        )
    )

    if meeting_id:
        query = query.filter(TranscriptSegment.meeting_id == meeting_id)

    segments = query.all()
    logger.info("Re-evaluating %d unverified segments...", len(segments))

    for seg in segments:
        run_voiceprint_matching(
            db, seg, preloaded=preloaded, person_map=person_map,
            venue_id=effective_venue_id,
        )

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

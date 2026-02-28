"""
votes.py — API endpoints for meeting vote records parsed from minutes.

GET  /api/votes/{meeting_id}           All votes for a specific meeting
GET  /api/votes/{meeting_id}/unmatched  Vote-like paragraphs the parser couldn't match
GET  /api/votes/{meeting_id}/parse-status
                                        Whether minutes were parsed and how well
GET  /api/votes?member=&outcome=&start=&end=&group_name=
                                        Filtered cross-meeting vote search
POST /api/votes/backfill               Re-parse all meetings with partial/unrecognized status
GET  /api/reference/brown-act/sections?q=
                                        Search Brown Act sections by keyword
"""

import threading

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db, SessionLocal
from app import models

router = APIRouter()


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _vote_row(v: models.MeetingVote, include_members: bool = True) -> dict:
    d = {
        "vote_id":           v.vote_id,
        "meeting_id":        v.meeting_id,
        "document_id":       v.document_id,
        "meeting_date":      v.meeting_date,
        "group_name":    v.group_name,
        "agenda_section":    v.agenda_section,
        "item_description":  v.item_description,
        "resolution_number": v.resolution_number,
        "outcome":           v.outcome,
        "vote_tally":        v.vote_tally,
        "mover":             v.mover,
        "seconder":          v.seconder,
    }
    if include_members:
        d["members"] = [
            {"name": m.member_name, "vote": m.vote_value}
            for m in v.members
        ]
    return d


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/votes/{meeting_id}")
def get_votes_for_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
):
    """All vote records for a specific meeting, with individual member votes."""
    votes = (
        db.query(models.MeetingVote)
          .filter(models.MeetingVote.meeting_id == meeting_id)
          .order_by(models.MeetingVote.created_at)
          .all()
    )
    return {"meeting_id": meeting_id, "votes": [_vote_row(v) for v in votes]}


@router.get("/api/votes/{meeting_id}/unmatched")
def get_unmatched_paragraphs(
    meeting_id: str,
    db: Session = Depends(get_db),
):
    """
    Return vote-like paragraphs from minutes that the parser could not match.

    Use this to review format gaps and write new parser patterns. Each entry
    is a raw paragraph string that looked like a vote motion but didn't match
    any known regex pattern for this governing body.
    """
    docs = (
        db.query(models.Document)
          .filter(
              models.Document.meeting_id == meeting_id,
              models.Document.document_type == "minutes",
          )
          .all()
    )
    if not docs:
        raise HTTPException(404, "No minutes documents found for this meeting")

    results = []
    for d in docs:
        unmatched = []
        if d.minutes_parse_notes:
            import json
            try:
                notes = json.loads(d.minutes_parse_notes)
                unmatched = notes.get("unmatched_paragraphs", [])
            except (json.JSONDecodeError, AttributeError):
                pass
        results.append({
            "document_id":          d.document_id,
            "minutes_parse_status": d.minutes_parse_status,
            "unmatched_count":      len(unmatched),
            "unmatched_paragraphs": unmatched,
        })

    return {
        "meeting_id": meeting_id,
        "documents":  results,
    }


@router.get("/api/votes/{meeting_id}/parse-status")
def get_parse_status(
    meeting_id: str,
    db: Session = Depends(get_db),
):
    """Return the parse status for the minutes document(s) of a meeting."""
    docs = (
        db.query(models.Document)
          .filter(
              models.Document.meeting_id == meeting_id,
              models.Document.document_type == "minutes",
          )
          .all()
    )
    if not docs:
        raise HTTPException(404, "No minutes documents found for this meeting")

    return {
        "meeting_id": meeting_id,
        "documents": [
            {
                "document_id":          d.document_id,
                "minutes_parse_status": d.minutes_parse_status,
                "minutes_parse_notes":  d.minutes_parse_notes,
                "has_ocr_text":         bool(d.ocr_text),
            }
            for d in docs
        ],
    }


@router.get("/api/votes")
def search_votes(
    member:        Optional[str] = Query(None, description="Supervisor last name"),
    vote_value:    Optional[str] = Query(None, description="yes | no | abstain | absent"),
    outcome:       Optional[str] = Query(None, description="Unanimously Carried, Carried, Failed, etc."),
    group_name: Optional[str] = Query(None),
    start_date:    Optional[str] = Query(None, description="YYYY-MM-DD"),
    end_date:      Optional[str] = Query(None, description="YYYY-MM-DD"),
    section:       Optional[str] = Query(None, description="Agenda section, e.g. Consent Calendar"),
    limit:         int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    """
    Cross-meeting vote search. Filter by supervisor name, outcome, date range, etc.

    Examples:
      /api/votes?member=Crye&vote_value=no          — all Crye dissents
      /api/votes?outcome=Failed+—+No+Second         — all motions with no second
      /api/votes?member=Long&start_date=2025-01-01  — Long's votes since Jan 2025
    """
    q = db.query(models.MeetingVote)

    if member or vote_value:
        q = q.join(models.VoteMember,
                   models.VoteMember.vote_id == models.MeetingVote.vote_id)
        if member:
            q = q.filter(models.VoteMember.member_name.ilike(f"%{member}%"))
        if vote_value:
            q = q.filter(models.VoteMember.vote_value == vote_value.lower())

    if outcome:
        q = q.filter(models.MeetingVote.outcome.ilike(f"%{outcome}%"))
    if group_name:
        q = q.filter(models.MeetingVote.group_name.ilike(f"%{group_name}%"))
    if start_date:
        q = q.filter(models.MeetingVote.meeting_date >= start_date)
    if end_date:
        q = q.filter(models.MeetingVote.meeting_date <= end_date)
    if section:
        q = q.filter(models.MeetingVote.agenda_section.ilike(f"%{section}%"))

    votes = q.order_by(models.MeetingVote.meeting_date.desc()).limit(limit).all()
    return {
        "count": len(votes),
        "votes": [_vote_row(v, include_members=True) for v in votes],
    }


# ── Backfill ──────────────────────────────────────────────────────────────────

def _run_backfill() -> dict:
    """Re-parse all meetings with partial or unrecognized parse status.

    Runs in a background thread so the API can return immediately.
    Imports the ingest script's helpers directly to reuse the same logic.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    from scripts.ingest_minutes_votes import ingest_meeting, _has_failed_parse

    db = SessionLocal()
    retried = errors = total_votes = 0
    try:
        failed_docs = (
            db.query(models.Document)
              .filter(
                  models.Document.document_type == "minutes",
                  models.Document.ocr_text.isnot(None),
                  models.Document.minutes_parse_status.in_(["partial", "unrecognized"]),
              )
              .all()
        )
        meeting_ids = list({d.meeting_id for d in failed_docs})

        for mid in meeting_ids:
            meeting = db.query(models.Meeting).filter(
                models.Meeting.meeting_id == mid
            ).first()
            if not meeting:
                continue
            try:
                count, _ = ingest_meeting(db, meeting, dry_run=False, force=True)
                total_votes += count
                retried += 1
            except Exception:
                errors += 1
    finally:
        db.close()

    return {"retried": retried, "errors": errors, "votes_saved": total_votes}


@router.post("/api/votes/backfill")
def trigger_backfill(background_tasks: BackgroundTasks):
    """
    Re-parse all meetings whose minutes have a partial or unrecognized parse status.

    Runs in the background — returns immediately with a count of queued meetings.
    Use after updating a parser pattern to pick up previously unmatched minutes.
    """
    db = SessionLocal()
    try:
        count = (
            db.query(models.Document)
              .filter(
                  models.Document.document_type == "minutes",
                  models.Document.ocr_text.isnot(None),
                  models.Document.minutes_parse_status.in_(["partial", "unrecognized"]),
              )
              .count()
        )
    finally:
        db.close()

    if count == 0:
        return {"queued": 0, "message": "No meetings with partial/unrecognized parse status."}

    background_tasks.add_task(_run_backfill)
    return {
        "queued": count,
        "message": f"Backfill started for {count} document(s). Check parse-status endpoints for progress.",
    }


# ── Brown Act reference ───────────────────────────────────────────────────────

@router.get("/api/reference/brown-act/sections")
def search_brown_act(
    q:     Optional[str] = Query(None, description="Keyword or section number"),
    limit: int = Query(10, le=200),
    db: Session = Depends(get_db),
):
    """
    Search Brown Act sections by keyword or section number.
    Returns matching sections with their full text.
    """
    ref_doc = db.query(models.ReferenceDocument) \
                .filter(models.ReferenceDocument.name == "Brown Act 2026") \
                .first()
    if not ref_doc:
        raise HTTPException(404, "Brown Act not ingested — run scripts/ingest_brown_act.py")

    query = db.query(models.ReferenceSection) \
              .filter(models.ReferenceSection.ref_doc_id == ref_doc.ref_doc_id)

    if q:
        query = query.filter(
            models.ReferenceSection.text.ilike(f"%{q}%") |
            models.ReferenceSection.section_num.ilike(f"%{q}%") |
            models.ReferenceSection.title.ilike(f"%{q}%")
        )

    sections = query.order_by(models.ReferenceSection.seq).limit(limit).all()
    return {
        "query": q,
        "count": len(sections),
        "sections": [
            {
                "section_num": s.section_num,
                "title":       s.title,
                "text":        s.text[:2000],  # truncate for API response
            }
            for s in sections
        ],
    }

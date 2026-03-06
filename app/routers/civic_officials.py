"""
Civic officials, chair history, and frequent speakers API.

GET  /api/civic-officials                — List officials (filterable by jurisdiction, body_type)
GET  /api/civic-officials/chairs         — Chair/vice-chair history (date-aware)
GET  /api/civic-officials/speakers       — Frequent public speakers
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import CivicOfficial, CivicChairHistory, CivicFrequentSpeaker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/civic-officials", tags=["civic-officials"])


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_aliases(aliases_json: str) -> list:
    try:
        return json.loads(aliases_json) if aliases_json else []
    except (json.JSONDecodeError, TypeError):
        return []


@router.get("")
def list_officials(
    jurisdiction: Optional[str] = Query(None),
    body_type: Optional[str] = Query(None),
    current_only: bool = Query(True),
    meeting_date: Optional[str] = Query(None, description="ISO date — resolves chair/vice-chair for that date"),
    db: Session = Depends(_get_db),
):
    """List civic officials, optionally filtered. Date-aware chair resolution."""
    q = db.query(CivicOfficial)
    if jurisdiction:
        q = q.filter(CivicOfficial.official_jurisdiction == jurisdiction)
    if body_type:
        q = q.filter(CivicOfficial.official_body_type == body_type)
    if current_only:
        q = q.filter(CivicOfficial.official_is_current == 1)

    officials = q.all()

    # Build chair lookup if meeting_date provided
    chair_map = {}
    if meeting_date:
        chair_q = db.query(CivicChairHistory)
        if jurisdiction:
            chair_q = chair_q.filter(CivicChairHistory.jurisdiction == jurisdiction)
        if body_type:
            chair_q = chair_q.filter(CivicChairHistory.body_type == body_type)
        for ch in chair_q.all():
            if ch.chair_start <= meeting_date and (ch.chair_end is None or ch.chair_end >= meeting_date):
                chair_map[ch.chair_name] = ch.chair_role

    result = []
    for o in officials:
        row = {
            "official_id": o.official_id,
            "official_jurisdiction": o.official_jurisdiction,
            "official_body_type": o.official_body_type,
            "official_body_name": o.official_body_name,
            "official_person_name": o.official_person_name,
            "official_role": o.official_role,
            "official_district": o.official_district,
            "official_is_current": o.official_is_current,
            "official_aliases": _parse_aliases(o.official_aliases),
            "official_notes": o.official_notes,
            "person_id": o.person_id,
        }
        if meeting_date and o.official_person_name in chair_map:
            row["active_chair_role"] = chair_map[o.official_person_name]
        result.append(row)

    return result


@router.get("/chairs")
def list_chairs(
    jurisdiction: Optional[str] = Query(None),
    body_type: Optional[str] = Query(None),
    db: Session = Depends(_get_db),
):
    """List chair/vice-chair history."""
    q = db.query(CivicChairHistory)
    if jurisdiction:
        q = q.filter(CivicChairHistory.jurisdiction == jurisdiction)
    if body_type:
        q = q.filter(CivicChairHistory.body_type == body_type)

    return [
        {
            "chair_id": ch.chair_id,
            "jurisdiction": ch.jurisdiction,
            "body_type": ch.body_type,
            "chair_name": ch.chair_name,
            "chair_start": ch.chair_start,
            "chair_end": ch.chair_end,
            "chair_role": ch.chair_role,
        }
        for ch in q.order_by(CivicChairHistory.chair_start.desc()).all()
    ]


@router.get("/speakers")
def list_speakers(
    jurisdiction: Optional[str] = Query(None),
    body_type: Optional[str] = Query(None),
    db: Session = Depends(_get_db),
):
    """List known frequent public speakers."""
    q = db.query(CivicFrequentSpeaker)
    if jurisdiction:
        q = q.filter(CivicFrequentSpeaker.speaker_jurisdiction == jurisdiction)
    if body_type:
        q = q.filter(CivicFrequentSpeaker.speaker_body_type == body_type)

    return [
        {
            "speaker_id": s.speaker_id,
            "speaker_jurisdiction": s.speaker_jurisdiction,
            "speaker_body_type": s.speaker_body_type,
            "canonical_name": s.canonical_name,
            "speaker_aliases": _parse_aliases(s.speaker_aliases),
            "appearance_count": s.appearance_count,
            "last_seen": s.last_seen,
            "speaker_notes": s.speaker_notes,
            "person_id": s.person_id,
        }
        for s in q.order_by(CivicFrequentSpeaker.appearance_count.desc()).all()
    ]

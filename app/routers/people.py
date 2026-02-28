"""
People (speaker identity) endpoints.

GET    /api/people/            — list all known people (with voiceprint counts)
POST   /api/people/            — create a new person
GET    /api/people/{person_id} — get one person
PATCH  /api/people/{person_id} — rename a person
DELETE /api/people/{person_id} — remove person (voiceprints cascade-deleted)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db

router = APIRouter(prefix="/api/people", tags=["people"])


@router.get("/", response_model=list[schemas.PersonSummary])
def list_people(q: str = "", db: Session = Depends(get_db)):
    query = (
        db.query(
            models.Person,
            func.count(models.Voiceprint.voiceprint_id).label("voiceprint_count"),
        )
        .outerjoin(models.Voiceprint)
    )
    if q.strip():
        query = query.filter(models.Person.canonical_name.ilike(f"%{q.strip()}%"))
    rows = (
        query
        .group_by(models.Person.person_id)
        .order_by(models.Person.canonical_name)
        .all()
    )
    return [
        schemas.PersonSummary(
            person_id=person.person_id,
            canonical_name=person.canonical_name,
            created_at=person.created_at,
            voiceprint_count=count,
        )
        for person, count in rows
    ]


@router.post("/", response_model=schemas.PersonOut, status_code=201)
def create_person(payload: schemas.PersonCreate, db: Session = Depends(get_db)):
    name = payload.canonical_name.strip()
    if not name:
        raise HTTPException(400, "canonical_name cannot be blank")

    existing = db.query(models.Person).filter_by(canonical_name=name).first()
    if existing:
        raise HTTPException(409, f"Person '{name}' already exists")

    person = models.Person(canonical_name=name)
    db.add(person)
    db.commit()
    db.refresh(person)
    return person


@router.get("/{person_id}", response_model=schemas.PersonOut)
def get_person(person_id: str, db: Session = Depends(get_db)):
    p = db.query(models.Person).filter_by(person_id=person_id).first()
    if not p:
        raise HTTPException(404, "Person not found")
    return p


@router.patch("/{person_id}", response_model=schemas.PersonOut)
def rename_person(
    person_id: str,
    payload: schemas.PersonCreate,
    db: Session = Depends(get_db),
):
    p = db.query(models.Person).filter_by(person_id=person_id).first()
    if not p:
        raise HTTPException(404, "Person not found")

    name = payload.canonical_name.strip()
    if not name:
        raise HTTPException(400, "canonical_name cannot be blank")

    existing = db.query(models.Person).filter_by(canonical_name=name).first()
    if existing and existing.person_id != person_id:
        raise HTTPException(409, f"Person '{name}' already exists")

    p.canonical_name = name
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{person_id}", status_code=204)
def delete_person(person_id: str, db: Session = Depends(get_db)):
    p = db.query(models.Person).filter_by(person_id=person_id).first()
    if not p:
        raise HTTPException(404, "Person not found")
    db.delete(p)
    db.commit()


@router.get("/{person_id}/voiceprint_count")
def voiceprint_count(person_id: str, db: Session = Depends(get_db)):
    p = db.query(models.Person).filter_by(person_id=person_id).first()
    if not p:
        raise HTTPException(404, "Person not found")
    count = db.query(models.Voiceprint).filter_by(person_id=person_id).count()
    return {"person_id": person_id, "voiceprint_count": count}


@router.get("/{person_id}/appearances", response_model=list[schemas.PersonAppearance])
def get_person_appearances(
    person_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    """List meetings where a person appears, newest first, with segment counts."""
    p = db.query(models.Person).filter_by(person_id=person_id).first()
    if not p:
        raise HTTPException(404, "Person not found")

    query = (
        db.query(
            models.Meeting,
            func.count(models.TranscriptSegment.segment_id).label("segment_count"),
        )
        .join(
            models.TranscriptSegment,
            models.TranscriptSegment.meeting_id == models.Meeting.meeting_id,
        )
        .join(
            models.SegmentAssignment,
            models.SegmentAssignment.segment_id == models.TranscriptSegment.segment_id,
        )
        .filter(models.SegmentAssignment.predicted_person_id == person_id)
        .group_by(models.Meeting.meeting_id)
    )

    if date_from:
        query = query.filter(models.Meeting.meeting_date >= date_from)
    if date_to:
        query = query.filter(models.Meeting.meeting_date <= date_to)

    rows = query.order_by(models.Meeting.meeting_date.desc()).all()

    return [
        schemas.PersonAppearance(
            meeting_id=meeting.meeting_id,
            title=meeting.title,
            group_name=meeting.group_name,
            meeting_type=meeting.meeting_type,
            meeting_date=meeting.meeting_date,
            category=meeting.category,
            segment_count=count,
        )
        for meeting, count in rows
    ]

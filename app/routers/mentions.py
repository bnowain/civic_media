"""People mentions endpoints — CRUD and cross-content lookup."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas

router = APIRouter(prefix="/api/mentions", tags=["mentions"])


@router.get("/for-content", response_model=list[schemas.PeopleMentionOut])
def mentions_for_content(
    content_type: str = Query(...),
    content_id: str = Query(...),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.PeopleMention)
        .filter_by(content_type=content_type, content_id=content_id)
        .all()
    )


@router.get("/person/{person_id}", response_model=list[schemas.PeopleMentionOut])
def mentions_for_person(person_id: str, db: Session = Depends(get_db)):
    """Get all content where a person is mentioned."""
    return (
        db.query(models.PeopleMention)
        .filter_by(person_id=person_id)
        .order_by(models.PeopleMention.created_at.desc())
        .all()
    )


@router.post("/", response_model=schemas.PeopleMentionOut, status_code=201)
def create_mention(payload: schemas.PeopleMentionCreate, db: Session = Depends(get_db)):
    # Check person exists
    person = db.query(models.Person).filter_by(person_id=payload.person_id).first()
    if not person:
        raise HTTPException(404, "Person not found")

    # Check for denial
    denial = (
        db.query(models.PeopleMentionDenial)
        .filter_by(
            person_id=payload.person_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        .first()
    )
    if denial:
        raise HTTPException(409, "This mention has been denied for this content")

    # Check for existing
    existing = (
        db.query(models.PeopleMention)
        .filter_by(
            person_id=payload.person_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(409, "Mention already exists")

    mention = models.PeopleMention(
        person_id=payload.person_id,
        content_type=payload.content_type,
        content_id=payload.content_id,
        source=payload.source,
        confidence=payload.confidence,
        context=payload.context,
    )
    db.add(mention)
    db.commit()
    db.refresh(mention)
    return mention


@router.post("/confirm/{mention_id}")
def confirm_mention(mention_id: str, db: Session = Depends(get_db)):
    mention = db.query(models.PeopleMention).filter_by(mention_id=mention_id).first()
    if not mention:
        raise HTTPException(404, "Mention not found")
    mention.source = "confirmed"
    db.commit()
    return {"status": "confirmed", "mention_id": mention_id}


@router.post("/deny", status_code=201)
def deny_mention(payload: schemas.PeopleMentionDenyRequest, db: Session = Depends(get_db)):
    # Remove existing mention
    existing = (
        db.query(models.PeopleMention)
        .filter_by(
            person_id=payload.person_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        .first()
    )
    if existing:
        db.delete(existing)

    # Create denial (idempotent)
    denial = (
        db.query(models.PeopleMentionDenial)
        .filter_by(
            person_id=payload.person_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        .first()
    )
    if not denial:
        denial = models.PeopleMentionDenial(
            person_id=payload.person_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        db.add(denial)

    db.commit()
    return {"status": "denied"}


@router.delete("/{mention_id}", status_code=204)
def delete_mention(mention_id: str, db: Session = Depends(get_db)):
    mention = db.query(models.PeopleMention).filter_by(mention_id=mention_id).first()
    if not mention:
        raise HTTPException(404, "Mention not found")
    db.delete(mention)
    db.commit()

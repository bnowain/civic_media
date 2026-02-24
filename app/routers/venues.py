"""
Venue CRUD endpoints.

GET    /api/venues/              — list all venues
POST   /api/venues/              — create a venue
PATCH  /api/venues/{venue_id}    — update a venue
DELETE /api/venues/{venue_id}    — delete (rejects if referenced)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db

router = APIRouter(prefix="/api/venues", tags=["venues"])


@router.get("/", response_model=list[schemas.VenueOut])
def list_venues(db: Session = Depends(get_db)):
    return db.query(models.Venue).order_by(models.Venue.name).all()


@router.post("/", response_model=schemas.VenueOut, status_code=201)
def create_venue(payload: schemas.VenueCreate, db: Session = Depends(get_db)):
    venue = models.Venue(
        name=payload.name,
        acoustic_type=payload.acoustic_type,
        notes=payload.notes,
    )
    db.add(venue)
    db.commit()
    db.refresh(venue)
    return venue


@router.patch("/{venue_id}", response_model=schemas.VenueOut)
def update_venue(
    venue_id: str,
    payload: schemas.VenueCreate,
    db: Session = Depends(get_db),
):
    venue = db.query(models.Venue).filter_by(venue_id=venue_id).first()
    if not venue:
        raise HTTPException(404, "Venue not found")

    venue.name = payload.name
    venue.acoustic_type = payload.acoustic_type
    venue.notes = payload.notes
    db.commit()
    db.refresh(venue)
    return venue


@router.delete("/{venue_id}", status_code=204)
def delete_venue(venue_id: str, db: Session = Depends(get_db)):
    venue = db.query(models.Venue).filter_by(venue_id=venue_id).first()
    if not venue:
        raise HTTPException(404, "Venue not found")

    # Reject if referenced by meetings, governing bodies, or voiceprints
    refs = []
    if db.query(models.Meeting).filter_by(venue_id=venue_id).first():
        refs.append("meetings")
    if db.query(models.GoverningBody).filter_by(default_venue_id=venue_id).first():
        refs.append("governing_bodies")
    if db.query(models.Voiceprint).filter_by(venue_id=venue_id).first():
        refs.append("voiceprints")

    if refs:
        raise HTTPException(
            409,
            f"Cannot delete venue — referenced by: {', '.join(refs)}",
        )

    db.delete(venue)
    db.commit()

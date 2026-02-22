"""Governing body lookup table endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas

router = APIRouter(prefix="/api/governing-bodies", tags=["governing-bodies"])


@router.get("/", response_model=list[schemas.GoverningBodyOut])
def list_governing_bodies(db: Session = Depends(get_db)):
    return (
        db.query(models.GoverningBody)
        .order_by(models.GoverningBody.name)
        .all()
    )


@router.post("/", response_model=schemas.GoverningBodyOut, status_code=201)
def create_governing_body(payload: schemas.GoverningBodyCreate, db: Session = Depends(get_db)):
    existing = db.query(models.GoverningBody).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(409, f"Governing body '{payload.name}' already exists")

    gb = models.GoverningBody(
        name=payload.name,
        display_name=payload.display_name or payload.name,
    )
    db.add(gb)
    db.commit()
    db.refresh(gb)
    return gb

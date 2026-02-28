"""Group lookup table endpoints (government bodies, radio/web shows, etc.)."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas

router = APIRouter(prefix="/api/groups", tags=["groups"])


@router.get("/", response_model=list[schemas.GroupOut])
def list_groups(
    group_type: Optional[str] = Query(None, description="Filter by group_type: 'government' or 'show'"),
    db: Session = Depends(get_db),
):
    q = db.query(models.Group)
    if group_type is not None:
        q = q.filter(models.Group.group_type == group_type)
    return q.order_by(models.Group.name).all()


@router.post("/", response_model=schemas.GroupOut, status_code=201)
def create_group(payload: schemas.GroupCreate, db: Session = Depends(get_db)):
    existing = db.query(models.Group).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(409, f"Group '{payload.name}' already exists")

    g = models.Group(
        name=payload.name,
        display_name=payload.display_name or payload.name,
        group_type=payload.group_type,
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return g

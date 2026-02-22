"""Tag CRUD and assignment endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas

router = APIRouter(prefix="/api/tags", tags=["tags"])


@router.get("/", response_model=list[schemas.TagOut])
def list_tags(
    tag_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(models.Tag)
    if tag_type:
        q = q.filter(models.Tag.tag_type == tag_type)
    return q.order_by(models.Tag.name).all()


@router.post("/", response_model=schemas.TagOut, status_code=201)
def create_tag(payload: schemas.TagCreate, db: Session = Depends(get_db)):
    existing = db.query(models.Tag).filter_by(name=payload.name).first()
    if existing:
        raise HTTPException(409, f"Tag '{payload.name}' already exists")

    tag = models.Tag(
        name=payload.name,
        parent_id=payload.parent_id,
        tag_type=payload.tag_type,
        description=payload.description,
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


@router.get("/for-content", response_model=list[schemas.TagAssignmentOut])
def tags_for_content(
    content_type: str = Query(...),
    content_id: str = Query(...),
    db: Session = Depends(get_db),
):
    """Get all tag assignments for a specific content item."""
    assignments = (
        db.query(models.TagAssignment)
        .filter_by(content_type=content_type, content_id=content_id)
        .all()
    )
    # Eagerly load the tag for each assignment
    results = []
    for a in assignments:
        tag = db.query(models.Tag).filter_by(tag_id=a.tag_id).first()
        out = schemas.TagAssignmentOut.model_validate(a)
        if tag:
            out.tag = schemas.TagOut.model_validate(tag)
        results.append(out)
    return results


@router.post("/assignments", response_model=schemas.TagAssignmentOut, status_code=201)
def create_tag_assignment(payload: schemas.TagAssignmentCreate, db: Session = Depends(get_db)):
    # Check tag exists
    tag = db.query(models.Tag).filter_by(tag_id=payload.tag_id).first()
    if not tag:
        raise HTTPException(404, "Tag not found")

    # Check for denial
    denial = (
        db.query(models.TagDenial)
        .filter_by(
            tag_id=payload.tag_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        .first()
    )
    if denial:
        raise HTTPException(409, "This tag has been denied for this content")

    # Check for existing assignment
    existing = (
        db.query(models.TagAssignment)
        .filter_by(
            tag_id=payload.tag_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(409, "Tag already assigned to this content")

    assignment = models.TagAssignment(
        tag_id=payload.tag_id,
        content_type=payload.content_type,
        content_id=payload.content_id,
        source=payload.source,
        confidence=payload.confidence,
        llm_context=payload.llm_context,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)

    out = schemas.TagAssignmentOut.model_validate(assignment)
    out.tag = schemas.TagOut.model_validate(tag)
    return out


@router.post("/assignments/{assignment_id}/confirm")
def confirm_tag_assignment(assignment_id: str, db: Session = Depends(get_db)):
    """Confirm an LLM-suggested tag by setting source to 'confirmed'."""
    assignment = db.query(models.TagAssignment).filter_by(assignment_id=assignment_id).first()
    if not assignment:
        raise HTTPException(404, "Tag assignment not found")

    assignment.source = "confirmed"
    db.commit()
    return {"status": "confirmed", "assignment_id": assignment_id}


@router.post("/deny", status_code=201)
def deny_tag(payload: schemas.TagDenyRequest, db: Session = Depends(get_db)):
    """Deny a tag for specific content. Removes existing assignment if present."""
    # Remove existing assignment
    existing = (
        db.query(models.TagAssignment)
        .filter_by(
            tag_id=payload.tag_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        .first()
    )
    if existing:
        db.delete(existing)

    # Create denial record (idempotent)
    denial = (
        db.query(models.TagDenial)
        .filter_by(
            tag_id=payload.tag_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        .first()
    )
    if not denial:
        denial = models.TagDenial(
            tag_id=payload.tag_id,
            content_type=payload.content_type,
            content_id=payload.content_id,
        )
        db.add(denial)

    db.commit()
    return {"status": "denied"}


@router.delete("/assignments/{assignment_id}", status_code=204)
def delete_tag_assignment(assignment_id: str, db: Session = Depends(get_db)):
    assignment = db.query(models.TagAssignment).filter_by(assignment_id=assignment_id).first()
    if not assignment:
        raise HTTPException(404, "Tag assignment not found")
    db.delete(assignment)
    db.commit()

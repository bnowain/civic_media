"""
Ingest system endpoints.

POST /api/ingest/run     — start ingest (all sources or specific source_id)
GET  /api/ingest/sources — list configured sources
GET  /api/ingest/status  — poll ingest progress
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@router.get("/sources", response_model=list[schemas.IngestSourceOut])
def list_sources(db: Session = Depends(get_db)):
    return db.query(models.IngestSource).order_by(models.IngestSource.name).all()


@router.post("/run", response_model=schemas.IngestRunResponse)
def run_ingest(
    source_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Start an ingest run. Optionally filter to a single source."""
    from app.tasks import ingest_radio_task

    if source_id:
        src = db.query(models.IngestSource).filter_by(source_id=source_id).first()
        if not src:
            raise HTTPException(404, "Ingest source not found")
        if not src.enabled:
            raise HTTPException(400, "Ingest source is disabled")

    result = ingest_radio_task.delay(source_id)
    return schemas.IngestRunResponse(
        task_id=result.id,
        status="queued",
        message=f"Ingest started{' for ' + source_id if source_id else ' for all sources'}",
    )


@router.get("/status", response_model=schemas.IngestStatusResponse)
def ingest_status():
    """Poll ingest progress from the progress file."""
    from app.config import BASE_DIR

    progress_path = BASE_DIR / "media" / "ingest_progress.json"
    if not progress_path.exists():
        return schemas.IngestStatusResponse(running=False)

    try:
        data = json.loads(progress_path.read_text())
        return schemas.IngestStatusResponse(**data)
    except Exception:
        return schemas.IngestStatusResponse(running=False)

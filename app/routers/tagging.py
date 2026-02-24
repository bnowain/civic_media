"""Atlas tagging integration — process incoming tags, trigger retag."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app import models

router = APIRouter(prefix="/api/tagging", tags=["tagging"])


class TaggingPayload(BaseModel):
    """Payload from Atlas LLM tagging pipeline."""
    content_type: str
    content_id: str
    tags: list[dict] = []        # [{name, tag_type, confidence, context}, ...]
    mentions: list[dict] = []    # [{person_name, confidence, context}, ...]


@router.post("/process")
def process_tags(payload: TaggingPayload, db: Session = Depends(get_db)):
    """Receive tags and mentions from Atlas and write them to the database."""
    from app.services.tagging import apply_tags, apply_mentions

    tag_count = apply_tags(
        db,
        content_type=payload.content_type,
        content_id=payload.content_id,
        tags=payload.tags,
    )
    mention_count = apply_mentions(
        db,
        content_type=payload.content_type,
        content_id=payload.content_id,
        mentions=payload.mentions,
    )

    return {
        "content_type": payload.content_type,
        "content_id": payload.content_id,
        "tags_applied": tag_count,
        "mentions_applied": mention_count,
    }


@router.post("/retag/{content_type}/{content_id}")
def retag_content(content_type: str, content_id: str, db: Session = Depends(get_db)):
    """Manually trigger a retag via Atlas for a specific content item."""
    from app.tasks import retag_content_task
    from app.services.worker_manager import ensure_worker
    ensure_worker()
    task = retag_content_task.delay(content_type, content_id)
    return {"task_id": task.id, "status": "queued"}

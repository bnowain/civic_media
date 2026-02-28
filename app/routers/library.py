"""Library recent media endpoint — merges meetings + tv_newscasts into a unified list."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/recent", response_model=list[schemas.RecentMediaItem])
def recent_media(
    limit: int = Query(5, ge=1, le=20),
    sort: str = Query("processed", regex="^(processed|event)$"),
    db: Session = Depends(get_db),
):
    """
    Return the most recent media items across all content types.
    sort=processed: order by processed_at desc (most recently finished first)
    sort=event: order by event date desc (meeting_date / air_date)
    """
    items: list[schemas.RecentMediaItem] = []

    # Meetings (both meeting and audio categories)
    meetings = db.query(models.Meeting).all()
    for m in meetings:
        items.append(schemas.RecentMediaItem(
            id=m.meeting_id,
            title=m.title,
            media_type="audio" if m.category == "audio" else "meeting",
            date=m.meeting_date,
            processed_at=m.processed_at,
            created_at=m.created_at,
            subtitle=m.group_name if m.group_name else None,
        ))

    # TV Newscasts
    newscasts = db.query(models.TVNewscast).all()
    for n in newscasts:
        items.append(schemas.RecentMediaItem(
            id=n.newscast_id,
            title=n.title,
            media_type="news",
            date=n.air_date,
            processed_at=n.processed_at,
            created_at=n.created_at,
            status=n.status,
            subtitle=n.station if n.station else None,
        ))

    # Sort
    if sort == "processed":
        # Items with processed_at first (newest), then by created_at
        items.sort(
            key=lambda x: (x.processed_at or x.created_at),
            reverse=True,
        )
    else:
        # Sort by event date (meeting_date / air_date)
        items.sort(
            key=lambda x: (x.date or "0000-00-00"),
            reverse=True,
        )

    return items[:limit]

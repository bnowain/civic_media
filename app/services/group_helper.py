"""
Shared helper for get-or-create Group records.

Used by radio ingest, PrimeGov discovery, and the meetings POST endpoint
to ensure consistent group_type tagging across all ingest paths.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Group

logger = logging.getLogger(__name__)


def ensure_group(
    db: Session,
    name: str,
    group_type: str = "government",
    display_name: Optional[str] = None,
) -> str:
    """
    Get or create a Group by name. Returns group_id.

    If the record already exists but has no group_type set, backfills it.
    """
    existing = db.query(Group).filter_by(name=name).first()
    if existing:
        if existing.group_type is None and group_type:
            existing.group_type = group_type
            db.flush()
        return existing.group_id

    g = Group(
        name=name,
        display_name=display_name or name,
        group_type=group_type,
    )
    db.add(g)
    db.flush()
    logger.info("Created group: %s (type=%s)", name, group_type)
    return g.group_id

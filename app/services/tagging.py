"""
Core tagging logic — writes tags and people mentions from Atlas responses.

Handles:
  - Tag creation (get-or-create by name)
  - Denial checking (skips denied tags/mentions)
  - Deduplication (skips existing assignments)
"""

from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from app import models

logger = logging.getLogger(__name__)


def apply_tags(
    db: Session,
    content_type: str,
    content_id: str,
    tags: list[dict],
) -> int:
    """
    Apply a list of tags to a content item.

    Each tag dict: {name, tag_type?, confidence?, context?}
    Returns the number of new tag assignments created.
    """
    count = 0
    for tag_data in tags:
        name = tag_data.get("name", "").strip()
        if not name:
            continue

        # Get or create the tag
        tag = db.query(models.Tag).filter_by(name=name).first()
        if not tag:
            tag = models.Tag(
                name=name,
                tag_type=tag_data.get("tag_type"),
            )
            db.add(tag)
            db.flush()

        # Check for denial
        denied = (
            db.query(models.TagDenial)
            .filter_by(tag_id=tag.tag_id, content_type=content_type, content_id=content_id)
            .first()
        )
        if denied:
            continue

        # Check for existing assignment
        existing = (
            db.query(models.TagAssignment)
            .filter_by(tag_id=tag.tag_id, content_type=content_type, content_id=content_id)
            .first()
        )
        if existing:
            continue

        assignment = models.TagAssignment(
            tag_id=tag.tag_id,
            content_type=content_type,
            content_id=content_id,
            source="llm",
            confidence=tag_data.get("confidence"),
            llm_context=tag_data.get("context"),
        )
        db.add(assignment)
        count += 1

    db.commit()
    return count


def apply_mentions(
    db: Session,
    content_type: str,
    content_id: str,
    mentions: list[dict],
) -> int:
    """
    Apply a list of people mentions to a content item.

    Each mention dict: {person_name, confidence?, context?}
    Returns the number of new mentions created.
    """
    count = 0
    for mention_data in mentions:
        person_name = mention_data.get("person_name", "").strip()
        if not person_name:
            continue

        # Find person by canonical name
        person = db.query(models.Person).filter_by(canonical_name=person_name).first()
        if not person:
            # Skip mentions for unknown people — don't auto-create Person records
            logger.info("Skipping mention for unknown person: %s", person_name)
            continue

        # Check for denial
        denied = (
            db.query(models.PeopleMentionDenial)
            .filter_by(person_id=person.person_id, content_type=content_type, content_id=content_id)
            .first()
        )
        if denied:
            continue

        # Check for existing mention
        existing = (
            db.query(models.PeopleMention)
            .filter_by(person_id=person.person_id, content_type=content_type, content_id=content_id)
            .first()
        )
        if existing:
            continue

        mention = models.PeopleMention(
            person_id=person.person_id,
            content_type=content_type,
            content_id=content_id,
            source="llm",
            confidence=mention_data.get("confidence"),
            context=mention_data.get("context"),
        )
        db.add(mention)
        count += 1

    db.commit()
    return count

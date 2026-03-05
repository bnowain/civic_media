"""
app/routers/comments.py — Article comments API.

Endpoints:
  POST   /api/articles/{article_id}/comments       — Bulk ingest comments
  GET    /api/articles/{article_id}/comments       — List (threaded or flat)
  GET    /api/articles/{article_id}/comments/count — Count comments
  DELETE /api/articles/{article_id}/comments/{id}  — Delete one comment
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import schemas
from app.database import get_db
from app.models import ArticleComment, NewsArticle

router = APIRouter(prefix="/api/articles", tags=["comments"])


# ── Serializer ───────────────────────────────────────────────────────────────

def _dt_iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _row_to_out(row: ArticleComment, reply_count: int = 0) -> dict:
    return {
        "comment_id":          row.comment_id,
        "article_id":          row.article_id,
        "platform_comment_id": row.platform_comment_id,
        "parent_comment_id":   row.parent_comment_id,
        "author_name":         row.author_name,
        "author_avatar_url":   row.author_avatar_url,
        "author_url":          row.author_url,
        "comment_text":        row.comment_text,
        "comment_html":        row.comment_html,
        "published_at":        row.published_at,
        "comment_status":      row.comment_status,
        "reaction_count":      row.reaction_count,
        "reply_count":         reply_count,
        "created_at":          _dt_iso(row.created_at),
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/{article_id}/comments/", status_code=201)
def ingest_comments(
    article_id: str,
    payload: schemas.ArticleCommentBulkCreate,
    db: Session = Depends(get_db),
):
    """
    Bulk ingest comments for an article.
    Deduplicates by (article_id, platform_comment_id).
    Resolves parent_comment_id from platform_parent_id if provided.
    Returns {"inserted": N, "existing": N}.
    """
    # Verify article exists
    article = db.query(NewsArticle).filter(
        NewsArticle.article_id == article_id
    ).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    inserted = existing = 0

    # First pass: insert all comments, building a platform_id → comment_id map
    id_map: dict[str, str] = {}  # platform_comment_id → our comment_id

    # Pre-load existing comments for this article to check dedup
    existing_rows = db.query(ArticleComment).filter(
        ArticleComment.article_id == article_id
    ).all()
    for row in existing_rows:
        if row.platform_comment_id:
            id_map[row.platform_comment_id] = row.comment_id

    for item in payload.comments:
        # Check dedup
        if item.platform_comment_id and item.platform_comment_id in id_map:
            existing += 1
            continue

        comment_id = str(uuid.uuid4())
        row = ArticleComment(
            comment_id          = comment_id,
            article_id          = article_id,
            platform_comment_id = item.platform_comment_id,
            parent_comment_id   = None,  # resolved in second pass
            author_name         = item.author_name,
            author_avatar_url   = item.author_avatar_url,
            author_url          = item.author_url,
            comment_text        = item.comment_text,
            comment_html        = item.comment_html,
            published_at        = item.published_at,
            comment_status      = item.comment_status,
            reaction_count      = item.reaction_count,
        )
        db.add(row)
        if item.platform_comment_id:
            id_map[item.platform_comment_id] = comment_id
        inserted += 1

    db.flush()

    # Second pass: resolve parent_comment_id from platform_parent_id
    for item in payload.comments:
        if item.platform_parent_id and item.platform_comment_id:
            our_id = id_map.get(item.platform_comment_id)
            parent_id = id_map.get(item.platform_parent_id)
            if our_id and parent_id:
                db.query(ArticleComment).filter(
                    ArticleComment.comment_id == our_id
                ).update({"parent_comment_id": parent_id})

    db.commit()
    return {"inserted": inserted, "existing": existing}


@router.get("/{article_id}/comments/count")
def count_comments(
    article_id: str,
    db: Session = Depends(get_db),
):
    """Return total comment count for an article."""
    total = db.query(func.count(ArticleComment.comment_id)).filter(
        ArticleComment.article_id == article_id
    ).scalar() or 0
    return {"count": total}


@router.get("/{article_id}/comments/",
            response_model=list[schemas.ArticleCommentOut])
def list_comments(
    article_id: str,
    threaded: bool = Query(True, description="If true, returns top-level comments with reply_count"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    List comments for an article.
    If threaded=true (default), returns only top-level comments with reply_count.
    If threaded=false, returns all comments flat.
    """
    query = db.query(ArticleComment).filter(
        ArticleComment.article_id == article_id
    )

    if threaded:
        query = query.filter(ArticleComment.parent_comment_id.is_(None))

    query = query.order_by(ArticleComment.published_at.asc())
    rows = query.offset(offset).limit(limit).all()

    # Compute reply counts
    if threaded and rows:
        comment_ids = [r.comment_id for r in rows]
        reply_counts = dict(
            db.query(
                ArticleComment.parent_comment_id,
                func.count(ArticleComment.comment_id),
            )
            .filter(ArticleComment.parent_comment_id.in_(comment_ids))
            .group_by(ArticleComment.parent_comment_id)
            .all()
        )
        return [_row_to_out(r, reply_counts.get(r.comment_id, 0)) for r in rows]

    return [_row_to_out(r) for r in rows]


@router.get("/{article_id}/comments/{comment_id}/replies",
            response_model=list[schemas.ArticleCommentOut])
def list_replies(
    article_id: str,
    comment_id: str,
    db: Session = Depends(get_db),
):
    """List direct replies to a specific comment."""
    rows = (
        db.query(ArticleComment)
        .filter(
            ArticleComment.article_id == article_id,
            ArticleComment.parent_comment_id == comment_id,
        )
        .order_by(ArticleComment.published_at.asc())
        .all()
    )
    return [_row_to_out(r) for r in rows]


@router.delete("/{article_id}/comments/{comment_id}", status_code=204)
def delete_comment(
    article_id: str,
    comment_id: str,
    db: Session = Depends(get_db),
):
    """Delete a single comment."""
    row = db.query(ArticleComment).filter(
        ArticleComment.comment_id == comment_id,
        ArticleComment.article_id == article_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Comment not found")
    db.delete(row)
    db.commit()

"""
app/routers/facebook.py — Facebook post ingest and query API.

Endpoints:
  POST   /api/facebook/posts           — Ingest post (auto-creates page, dedup by post_url)
  GET    /api/facebook/posts           — List with filters
  GET    /api/facebook/posts/count     — Count matching filters
  GET    /api/facebook/posts/exists    — Fast dedup check by URL
  GET    /api/facebook/posts/{id}      — Get one post
  DELETE /api/facebook/posts/{id}      — Delete one post
  GET    /api/facebook/pages           — List pages with post counts
  POST   /api/facebook/posts/{id}/comments  — Bulk comment ingest
  GET    /api/facebook/posts/{id}/comments  — List comments (threaded/flat)
"""

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, nullslast
from sqlalchemy.orm import Session

from app import schemas
from app.database import get_db
from app.models import FacebookComment, FacebookPage, FacebookPost

router = APIRouter(prefix="/api/facebook", tags=["facebook"])


# ── Serializer ───────────────────────────────────────────────────────────────

def _parse_json(val: str | None) -> list | None:
    if not val:
        return None
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def _dt_iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _post_to_out(row: FacebookPost, page_name: str | None = None,
                 db_comment_count: int = 0) -> dict:
    return {
        "post_id":             row.post_id,
        "page_id":             row.page_id,
        "page_name":           page_name,
        "platform_post_id":    row.platform_post_id,
        "post_url":            row.post_url,
        "author_name":         row.author_name,
        "author_profile_url":  row.author_profile_url,
        "post_text":           row.post_text,
        "published_at":        row.published_at,
        "content_type":        row.content_type,
        "shared_from_url":     row.shared_from_url,
        "shared_from_author":  row.shared_from_author,
        "reaction_count":      row.reaction_count,
        "comment_count":       row.comment_count,
        "share_count":         row.share_count,
        "view_count":          row.view_count,
        "media_urls":          _parse_json(row.media_urls),
        "external_links":      _parse_json(row.external_links),
        "capture_method":      row.capture_method,
        "extraction_strategy": row.extraction_strategy,
        "db_comment_count":    db_comment_count,
        "created_at":          _dt_iso(row.created_at),
        "updated_at":          _dt_iso(row.updated_at),
    }


def _comment_to_out(row: FacebookComment, reply_count: int = 0) -> dict:
    return {
        "comment_id":          row.comment_id,
        "post_id":             row.post_id,
        "platform_comment_id": row.platform_comment_id,
        "parent_comment_id":   row.parent_comment_id,
        "author_name":         row.author_name,
        "author_profile_url":  row.author_profile_url,
        "comment_text":        row.comment_text,
        "published_at":        row.published_at,
        "reaction_count":      row.reaction_count,
        "is_reply":            row.is_reply,
        "reply_count":         reply_count,
        "created_at":          _dt_iso(row.created_at),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    """Strip query params and trailing slash for dedup."""
    return url.split('?')[0].rstrip('/')


def _find_or_create_page(db: Session, page_name: str,
                         page_url: str | None = None) -> FacebookPage:
    page = db.query(FacebookPage).filter(
        FacebookPage.page_name == page_name
    ).first()
    if page:
        if page_url and not page.page_url:
            page.page_url = page_url
        return page
    page = FacebookPage(
        page_id=str(uuid.uuid4()),
        page_name=page_name,
        page_url=page_url,
    )
    db.add(page)
    db.flush()
    return page


def _apply_filters(query, page_name, q, date_from, date_to):
    if page_name:
        # Join to page to filter by name
        query = query.join(FacebookPage, FacebookPost.page_id == FacebookPage.page_id)
        query = query.filter(FacebookPage.page_name == page_name)
    if q:
        term = f'%{q}%'
        query = query.filter(
            FacebookPost.post_text.ilike(term) | FacebookPost.author_name.ilike(term)
        )
    if date_from:
        query = query.filter(FacebookPost.published_at >= date_from)
    if date_to:
        query = query.filter(FacebookPost.published_at <= date_to + 'T23:59:59')
    return query


# ── Post Endpoints ──────────────────────────────────────────────────────────

@router.post("/posts", status_code=201)
def ingest_post(payload: schemas.FacebookPostCreate, db: Session = Depends(get_db)):
    """
    Insert a new Facebook post. Returns 200 if already exists (by post_url),
    updating engagement counts. Returns 201 if newly inserted.
    Auto-creates the FacebookPage if needed.
    """
    url = _normalize_url(payload.post_url)

    def _to_json(val) -> str | None:
        return json.dumps(val) if val else None

    page = _find_or_create_page(db, payload.page_name, payload.page_url)

    existing = db.query(FacebookPost).filter(FacebookPost.post_url == url).first()
    if existing:
        # Update engagement counts (always overwrite with latest)
        changed = False
        for col in ('reaction_count', 'comment_count', 'share_count', 'view_count',
                     'engagement_json'):
            new_val = getattr(payload, col)
            if new_val is not None:
                old_val = getattr(existing, col)
                if old_val != new_val:
                    setattr(existing, col, new_val)
                    changed = True
        # Fill in previously null fields
        for col in ('author_name', 'author_profile_url', 'post_text', 'published_at',
                     'shared_from_url', 'shared_from_author', 'platform_post_id',
                     'extraction_strategy', 'content_type'):
            new_val = getattr(payload, col)
            if new_val is not None and getattr(existing, col) is None:
                setattr(existing, col, new_val)
                changed = True
        # Update media/links
        for col in ('media_urls', 'external_links'):
            new_val = getattr(payload, col)
            if new_val and not getattr(existing, col):
                setattr(existing, col, _to_json(new_val))
                changed = True
        if changed:
            db.commit()
            db.refresh(existing)
        page_name = page.page_name
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=200,
                            content=_post_to_out(existing, page_name))

    row = FacebookPost(
        post_id             = str(uuid.uuid4()),
        page_id             = page.page_id,
        platform_post_id    = payload.platform_post_id,
        post_url            = url,
        author_name         = payload.author_name,
        author_profile_url  = payload.author_profile_url,
        post_text           = payload.post_text,
        published_at        = payload.published_at,
        content_type        = payload.content_type,
        shared_from_url     = payload.shared_from_url,
        shared_from_author  = payload.shared_from_author,
        reaction_count      = payload.reaction_count,
        comment_count       = payload.comment_count,
        share_count         = payload.share_count,
        view_count          = payload.view_count,
        engagement_json     = payload.engagement_json,
        media_urls          = _to_json(payload.media_urls),
        external_links      = _to_json(payload.external_links),
        capture_method      = payload.capture_method,
        extraction_strategy = payload.extraction_strategy,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _post_to_out(row, page.page_name)


@router.get("/posts/count")
def count_posts(
    page_name: Optional[str] = Query(None),
    q:         Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(func.count(FacebookPost.post_id))
    query = _apply_filters(query, page_name, q, date_from, date_to)
    return {"count": query.scalar() or 0}


@router.get("/posts/exists")
def post_exists(url: str = Query(...), db: Session = Depends(get_db)):
    url_clean = _normalize_url(url)
    exists = (
        db.query(FacebookPost.post_id)
        .filter(FacebookPost.post_url == url_clean)
        .first()
    ) is not None
    return {"exists": exists}


@router.get("/posts", response_model=list[schemas.FacebookPostOut])
def list_posts(
    page_name: Optional[str] = Query(None),
    q:         Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    limit:  int = Query(50, ge=1, le=500),
    offset: int = Query(0,  ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(FacebookPost)
    query = _apply_filters(query, page_name, q, date_from, date_to)
    query = query.order_by(
        nullslast(FacebookPost.published_at.desc()),
        FacebookPost.created_at.desc(),
    )
    rows = query.offset(offset).limit(limit).all()

    if not rows:
        return []

    # Batch-fetch page names
    page_ids = {r.page_id for r in rows if r.page_id}
    page_map: dict[str, str] = {}
    if page_ids:
        pages = db.query(FacebookPage.page_id, FacebookPage.page_name).filter(
            FacebookPage.page_id.in_(page_ids)
        ).all()
        page_map = {pid: pn for pid, pn in pages}

    # Batch-fetch comment counts
    post_ids = [r.post_id for r in rows]
    comment_counts = dict(
        db.query(
            FacebookComment.post_id,
            func.count(FacebookComment.comment_id),
        )
        .filter(FacebookComment.post_id.in_(post_ids))
        .group_by(FacebookComment.post_id)
        .all()
    )

    return [
        _post_to_out(r, page_map.get(r.page_id), comment_counts.get(r.post_id, 0))
        for r in rows
    ]


@router.get("/posts/{post_id}", response_model=schemas.FacebookPostOut)
def get_post(post_id: str, db: Session = Depends(get_db)):
    row = db.query(FacebookPost).filter(FacebookPost.post_id == post_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Post not found")
    page_name = None
    if row.page_id:
        page = db.query(FacebookPage).filter(
            FacebookPage.page_id == row.page_id
        ).first()
        page_name = page.page_name if page else None
    cc = db.query(func.count(FacebookComment.comment_id)).filter(
        FacebookComment.post_id == post_id
    ).scalar() or 0
    return _post_to_out(row, page_name, cc)


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: str, db: Session = Depends(get_db)):
    row = db.query(FacebookPost).filter(FacebookPost.post_id == post_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Post not found")
    db.delete(row)
    db.commit()


# ── Page Endpoints ──────────────────────────────────────────────────────────

@router.get("/pages", response_model=list[schemas.FacebookPageOut])
def list_pages(db: Session = Depends(get_db)):
    """List all Facebook pages with post counts, sorted by count desc."""
    rows = (
        db.query(
            FacebookPage,
            func.count(FacebookPost.post_id).label("post_count"),
        )
        .outerjoin(FacebookPost, FacebookPage.page_id == FacebookPost.page_id)
        .group_by(FacebookPage.page_id)
        .order_by(func.count(FacebookPost.post_id).desc())
        .all()
    )
    return [
        {
            "page_id":    page.page_id,
            "page_name":  page.page_name,
            "page_url":   page.page_url,
            "page_type":  page.page_type,
            "post_count": count,
            "created_at": _dt_iso(page.created_at),
        }
        for page, count in rows
    ]


# ── Comment Endpoints ───────────────────────────────────────────────────────

@router.post("/posts/{post_id}/comments", status_code=201)
def ingest_comments(
    post_id: str,
    payload: schemas.FacebookCommentBulkCreate,
    db: Session = Depends(get_db),
):
    """
    Bulk ingest comments for a post.
    Deduplicates by (post_id, platform_comment_id).
    Two-pass: insert first, then resolve parent threading.
    """
    post = db.query(FacebookPost).filter(FacebookPost.post_id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    inserted = existing = 0
    id_map: dict[str, str] = {}

    # Pre-load existing comments
    existing_rows = db.query(FacebookComment).filter(
        FacebookComment.post_id == post_id
    ).all()
    for row in existing_rows:
        if row.platform_comment_id:
            id_map[row.platform_comment_id] = row.comment_id

    for item in payload.comments:
        if item.platform_comment_id and item.platform_comment_id in id_map:
            existing += 1
            continue

        comment_id = str(uuid.uuid4())
        row = FacebookComment(
            comment_id          = comment_id,
            post_id             = post_id,
            platform_comment_id = item.platform_comment_id,
            parent_comment_id   = None,
            author_name         = item.author_name,
            author_profile_url  = item.author_profile_url,
            comment_text        = item.comment_text,
            published_at        = item.published_at,
            reaction_count      = item.reaction_count,
            is_reply            = item.is_reply,
        )
        db.add(row)
        if item.platform_comment_id:
            id_map[item.platform_comment_id] = comment_id
        inserted += 1

    db.flush()

    # Second pass: resolve threading
    for item in payload.comments:
        if item.platform_parent_id and item.platform_comment_id:
            our_id = id_map.get(item.platform_comment_id)
            parent_id = id_map.get(item.platform_parent_id)
            if our_id and parent_id:
                db.query(FacebookComment).filter(
                    FacebookComment.comment_id == our_id
                ).update({"parent_comment_id": parent_id})

    db.commit()
    return {"inserted": inserted, "existing": existing}


@router.get("/posts/{post_id}/comments",
            response_model=list[schemas.FacebookCommentOut])
def list_comments(
    post_id: str,
    threaded: bool = Query(True),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(FacebookComment).filter(FacebookComment.post_id == post_id)

    if threaded:
        query = query.filter(FacebookComment.parent_comment_id.is_(None))

    query = query.order_by(FacebookComment.published_at.asc())
    rows = query.offset(offset).limit(limit).all()

    if threaded and rows:
        comment_ids = [r.comment_id for r in rows]
        reply_counts = dict(
            db.query(
                FacebookComment.parent_comment_id,
                func.count(FacebookComment.comment_id),
            )
            .filter(FacebookComment.parent_comment_id.in_(comment_ids))
            .group_by(FacebookComment.parent_comment_id)
            .all()
        )
        return [_comment_to_out(r, reply_counts.get(r.comment_id, 0)) for r in rows]

    return [_comment_to_out(r) for r in rows]

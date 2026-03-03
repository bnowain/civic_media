"""
app/routers/articles.py — News article ingest and query API.

Endpoints:
  POST /api/articles           — Ingest article (dedup by canonical_url)
  GET  /api/articles           — List with filters
  GET  /api/articles/count     — Count matching filters
  GET  /api/articles/sources   — Distinct source slugs + counts
  GET  /api/articles/exists    — Check if a URL is already ingested
  GET  /api/articles/{id}      — Get one article
  DELETE /api/articles/{id}    — Delete one article

content_type for tag/mention polymorphism: "news_article"
"""

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, nullslast
from sqlalchemy.orm import Session

from app import schemas
from app.database import get_db
from app.models import NewsArticle

router = APIRouter(prefix="/api/articles", tags=["articles"])


# ── Serializer ───────────────────────────────────────────────────────────────

def _parse_json(val: str | None) -> list | None:
    """Parse a JSON TEXT column, returning None on null/empty."""
    if not val:
        return None
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def _dt_iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _row_to_out(row: NewsArticle) -> dict:
    """Convert a NewsArticle ORM row to a JSON-safe dict matching NewsArticleOut."""
    return {
        "article_id":            row.article_id,
        "canonical_url":         row.canonical_url,
        "source_slug":           row.source_slug,
        "capture_method":        row.capture_method,
        "headline":              row.headline,
        "author":                row.author,
        "published_at":          row.published_at,
        "article_text":          row.article_text,
        "description":           row.description,
        "preview_image_url":     row.preview_image_url,
        "image_urls":            _parse_json(row.image_urls),
        "embedded_links":        _parse_json(row.embedded_links),
        "source_tags":           _parse_json(row.source_tags),
        "section":               row.section,
        "article_type":          row.article_type,
        "source_modified_at":    row.source_modified_at,
        "word_count":            row.word_count,
        "filter_reason":         row.filter_reason,
        "fetch_status":          row.fetch_status,
        "external_document_urls": _parse_json(row.external_document_urls),
        "created_at":            _dt_iso(row.created_at),
        "updated_at":            _dt_iso(row.updated_at),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _apply_filters(query, source_slug, q, date_from, date_to):
    if source_slug:
        query = query.filter(NewsArticle.source_slug == source_slug)
    if q:
        term = f'%{q}%'
        query = query.filter(
            NewsArticle.headline.ilike(term) | NewsArticle.article_text.ilike(term)
        )
    if date_from:
        query = query.filter(NewsArticle.published_at >= date_from)
    if date_to:
        # Include the entire end day
        query = query.filter(NewsArticle.published_at <= date_to + 'T23:59:59')
    return query


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/", response_model=schemas.NewsArticleOut, status_code=201)
def ingest_article(payload: schemas.NewsArticleCreate, db: Session = Depends(get_db)):
    """
    Insert a new article. Returns 200 if already exists (by canonical_url),
    201 if newly inserted.
    """
    url = payload.canonical_url.split('?')[0].rstrip('/')

    existing = db.query(NewsArticle).filter(NewsArticle.canonical_url == url).first()
    if existing:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=200, content=_row_to_out(existing))

    def _to_json(val) -> str | None:
        return json.dumps(val) if val else None

    row = NewsArticle(
        article_id             = str(uuid.uuid4()),
        canonical_url          = url,
        source_slug            = payload.source_slug,
        capture_method         = payload.capture_method,
        headline               = payload.headline,
        author                 = payload.author,
        published_at           = payload.published_at,
        article_text           = payload.article_text,
        description            = payload.description,
        preview_image_url      = payload.preview_image_url,
        image_urls             = _to_json(payload.image_urls),
        embedded_links         = _to_json(payload.embedded_links),
        source_tags            = _to_json(payload.source_tags),
        section                = payload.section,
        article_type           = payload.article_type,
        source_modified_at     = payload.source_modified_at,
        word_count             = payload.word_count,
        filter_reason          = payload.filter_reason,
        fetch_status           = payload.fetch_status,
        external_document_urls = _to_json(payload.external_document_urls),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_to_out(row)


@router.get("/count")
def count_articles(
    source_slug: Optional[str] = Query(None),
    q:           Optional[str] = Query(None),
    date_from:   Optional[str] = Query(None),
    date_to:     Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Return total count of articles matching filters."""
    query = db.query(func.count(NewsArticle.article_id))
    query = _apply_filters(query, source_slug, q, date_from, date_to)
    return {"count": query.scalar() or 0}


@router.get("/sources")
def list_article_sources(db: Session = Depends(get_db)):
    """Return list of source slugs with article counts, sorted by count desc."""
    rows = (
        db.query(NewsArticle.source_slug, func.count().label("n"))
        .group_by(NewsArticle.source_slug)
        .order_by(func.count().desc())
        .all()
    )
    return [{"source_slug": r[0], "count": r[1]} for r in rows]


@router.get("/exists")
def article_exists(url: str = Query(...), db: Session = Depends(get_db)):
    """Check whether a canonical_url is already ingested (fast dedup check)."""
    url_clean = url.split('?')[0].rstrip('/')
    exists = (
        db.query(NewsArticle.article_id)
        .filter(NewsArticle.canonical_url == url_clean)
        .first()
    ) is not None
    return {"exists": exists}


@router.get("/", response_model=list[schemas.NewsArticleOut])
def list_articles(
    source_slug: Optional[str] = Query(None),
    q:           Optional[str] = Query(None),
    date_from:   Optional[str] = Query(None),
    date_to:     Optional[str] = Query(None),
    limit:  int = Query(50, ge=1, le=500),
    offset: int = Query(0,  ge=0),
    db: Session = Depends(get_db),
):
    """
    List articles with optional filtering. Results are ordered newest-first
    (published_at desc, then created_at desc for undated articles).
    """
    query = db.query(NewsArticle)
    query = _apply_filters(query, source_slug, q, date_from, date_to)
    query = query.order_by(
        nullslast(NewsArticle.published_at.desc()),
        NewsArticle.created_at.desc(),
    )
    rows = query.offset(offset).limit(limit).all()
    return [_row_to_out(r) for r in rows]


@router.get("/{article_id}", response_model=schemas.NewsArticleOut)
def get_article(article_id: str, db: Session = Depends(get_db)):
    """Fetch a single article by its UUID."""
    row = db.query(NewsArticle).filter(NewsArticle.article_id == article_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    return _row_to_out(row)


@router.delete("/{article_id}", status_code=204)
def delete_article(article_id: str, db: Session = Depends(get_db)):
    """Delete a single article."""
    row = db.query(NewsArticle).filter(NewsArticle.article_id == article_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    db.delete(row)
    db.commit()

"""
Civic Meeting Media Processing Tool — FastAPI application.

Registers all routers, mounts static files and media directories,
initialises the database schema on first run.
"""

# Purge stale __pycache__ before any app imports — prevents Windows bytecode
# caching bugs after code edits.  The Atlas service manager also does this,
# but running it here ensures parity when civic_media is started standalone.
import shutil as _shutil
from pathlib import Path as _P
for _cache in (_P(__file__).resolve().parent).rglob("__pycache__"):
    _shutil.rmtree(_cache, ignore_errors=True)
del _shutil, _P, _cache

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, MEDIA_DIR
from app.database import engine, validate_schema_columns
from app import models
from app.routers import (
    assignments, clips, documents, governing_bodies, ingest, library, media,
    meetings, mentions, news, people, primegov, segments, system, tags, tagging,
    transcribe, venues,
)

# Create all tables (idempotent)
models.Base.metadata.create_all(bind=engine)

# Auto-fix any nullable columns that exist in ORM models but not in the DB
validate_schema_columns(models.Base)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup: clean up any abandoned clip exports
    try:
        from app.routers.clips import _run_cleanup
        from app.database import SessionLocal as _SL
        _db = _SL()
        cleaned = _run_cleanup(_db)
        _db.close()
        if cleaned:
            logger.info("Startup clip cleanup: removed %d old exports", cleaned)
    except Exception:
        logger.debug("Startup clip cleanup skipped", exc_info=True)

    # On startup: reset orphaned "exporting" clips to "error"
    # (daemon threads die on server restart, leaving status stuck)
    try:
        from app.database import SessionLocal as _SL2
        from app.config import CLIPS_DIR
        import time as _time
        _db2 = _SL2()
        orphans = (
            _db2.query(models.Clip)
            .filter(models.Clip.export_status == "exporting")
            .all()
        )
        recovered = 0
        for clip in orphans:
            # Check if progress.json is stale (>2 min old) or missing
            progress_file = CLIPS_DIR / clip.clip_id / "progress.json"
            stale = True
            if progress_file.exists():
                age = _time.time() - progress_file.stat().st_mtime
                stale = age > 120
            if stale:
                clip.export_status = "error"
                clip.export_error = "Server restarted during export"
                recovered += 1
        if recovered:
            _db2.commit()
            logger.info("Startup orphan recovery: reset %d stuck exports to error", recovered)
        _db2.close()
    except Exception:
        logger.debug("Startup orphan export recovery skipped", exc_info=True)

    yield


app = FastAPI(
    title="Civic Meeting Media Processing Tool",
    description="Local-first meeting transcription, diarization, and speaker refinement.",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

# ── API routers ──────────────────────────────────────────────────────────────
app.include_router(meetings.router)
app.include_router(media.router)
app.include_router(documents.router)
app.include_router(segments.router)
app.include_router(people.router)
app.include_router(assignments.router)
app.include_router(transcribe.router)
app.include_router(governing_bodies.router)
app.include_router(library.router)
app.include_router(news.router)
app.include_router(clips.router)
app.include_router(tags.router)
app.include_router(mentions.router)
app.include_router(tagging.router)
app.include_router(ingest.router)
app.include_router(primegov.router)
app.include_router(venues.router)
app.include_router(system.router)

# ── Static files ─────────────────────────────────────────────────────────────
_static_dir = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# /media/{meeting_id}/video is handled by media.router with range request support

# ── Frontend page routes ─────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(_static_dir / "index.html"))


@app.get("/review/{meeting_id}", include_in_schema=False)
def review_page(meeting_id: str):
    return FileResponse(str(_static_dir / "review.html"))


@app.get("/speakers", include_in_schema=False)
def speakers_page():
    return FileResponse(str(_static_dir / "speakers.html"))


@app.get("/news/{newscast_id}", include_in_schema=False)
def news_review_page(newscast_id: str):
    return FileResponse(str(_static_dir / "news_review.html"))


@app.get("/clips", include_in_schema=False)
def clips_page():
    return FileResponse(str(_static_dir / "clips.html"))


# ── Favicon ──────────────────────────────────────────────────────────────────
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/api/health", tags=["system"])
def health():
    return {"status": "ok"}

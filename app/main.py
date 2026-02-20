"""
Civic Meeting Media Processing Tool — FastAPI application.

Registers all routers, mounts static files and media directories,
initialises the database schema on first run.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, MEDIA_DIR
from app.database import engine
from app import models
from app.routers import assignments, documents, media, meetings, people, segments

# Create all tables (idempotent)
models.Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Civic Meeting Media Processing Tool",
    description="Local-first meeting transcription, diarization, and speaker refinement.",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# ── API routers ───────────────────────────────────────────────────────────────
app.include_router(meetings.router)
app.include_router(media.router)
app.include_router(documents.router)
app.include_router(segments.router)
app.include_router(people.router)
app.include_router(assignments.router)

# ── Static files ──────────────────────────────────────────────────────────────
_static_dir = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# /media/{meeting_id}/video is handled by media.router with range request support

# ── Frontend page routes ──────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(_static_dir / "index.html"))


@app.get("/review/{meeting_id}", include_in_schema=False)
def review_page(meeting_id: str):
    return FileResponse(str(_static_dir / "review.html"))


@app.get("/speakers", include_in_schema=False)
def speakers_page():
    return FileResponse(str(_static_dir / "speakers.html"))


# ── Favicon ───────────────────────────────────────────────────────────────────
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/api/health", tags=["system"])
def health():
    return {"status": "ok"}
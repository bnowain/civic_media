# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

civic_media is a local-first civic meeting transcription and speaker diarization tool. It processes meeting videos into searchable, speaker-attributed transcripts with a voiceprint learning system that improves speaker identification over time.

**Stack**: FastAPI + SQLAlchemy (SQLite/WAL) | Celery (solo pool) + Redis | faster-whisper (large-v3) | pyannote.audio 3.1 | SpeechBrain ECAPA-TDNN | Vanilla JS frontend

## Common Commands

```bash
# Start the API server (dev mode with auto-reload)
uvicorn app.main:app --reload

# Start the Celery worker (separate terminal)
celery -A app.worker worker --loglevel=info --concurrency=1 --pool=solo

# Or use the startup script (starts Redis, worker, and API together)
# Windows: .\start.ps1
# Linux: bash run.sh

# Initialize/reset database tables
python -c "from app.database import Base, engine; from app import models; Base.metadata.create_all(engine)"

# Run a migration script
python migrate_add_confidence.py

# Diagnostic checks (embeddings, voiceprints, segment state)
python diagnose.py

# Backfill missing voiceprints from verified assignments
python backfill_voiceprints.py
```

There is no test suite, linter config, or CI pipeline. Redis must be running for Celery tasks (Docker: `docker-compose up -d`).

## Architecture

### Processing Pipeline (`app/services/pipeline.py`)

Video upload triggers a Celery task that runs six sequential stages, each with checkpoints for resume:

1. **Audio extraction** (`audio_extractor.py`) — ffmpeg: video → mono 16kHz WAV with highpass + loudnorm
2. **Transcription** (`transcriber.py`) — faster-whisper: WAV → `TranscriptSegment` rows with confidence metadata
3. **Diarization** (`diarizer.py`) — pyannote.audio: WAV → speaker turn labels, cached to `diarization.json`
4. **Alignment** (`aligner.py`) — merges transcript segments with diarization by max time overlap, merges adjacent same-speaker fragments (gap ≤ 1.5s)
5. **Embedding** (`embedder.py`) — SpeechBrain ECAPA-TDNN: batch-32 GPU extraction, stored as serialized numpy arrays in DB
6. **Voiceprint matching** (`voiceprint.py`) — cosine similarity against person centroids, auto-assigns if ≥ 0.75

Progress is written to `media/{meeting_id}/progress.json`, polled by the UI every 4s.

### Voiceprint Learning Loop

The core differentiating feature. When a user confirms a speaker assignment:

1. Segment's embedding is added as a new `Voiceprint` row (purely additive, never deleted)
2. `SegmentAssignment` marked `verified=True` (never auto-touched again)
3. Background Celery task recomputes all person centroids and re-matches all unverified segments

Thresholds: high ≥ 0.92, medium ≥ 0.75 (both in `app/config.py`).

### Data Flow

- **Routers** (`app/routers/`) — HTTP endpoints, thin layer that validates input and calls services
- **Services** (`app/services/`) — all business logic, ML model invocations, file I/O
- **Models** (`app/models.py`) — SQLAlchemy ORM: Meeting, MediaFile, Document, TranscriptSegment, Person, Voiceprint, SegmentAssignment
- **Schemas** (`app/schemas.py`) — Pydantic v2 with `from_attributes = True`
- **Tasks** (`app/tasks.py`) — three Celery tasks: `process_video`, `process_pdf`, `rerun_voiceprints`
- **Config** (`app/config.py`) — all paths, thresholds, model identifiers, env var defaults

### Database

SQLite with WAL mode. Foreign keys enforced. Pragmas set in `app/database.py` on every connection. Celery tasks create their own sessions via `SessionLocal()` (never share sessions across task boundaries).

Key relationships: Meeting → MediaFile, Document, TranscriptSegment (cascade delete). TranscriptSegment → SegmentAssignment (1:1). Person → Voiceprint (1:many), SegmentAssignment (1:many).

### Frontend

Two-page vanilla JS SPA served from `app/static/`:
- **index.html/js** — meeting list, upload, live progress bars
- **review.html/js** — video player + synced transcript with speaker confirmation, text editing, filtering, and export (SRT/TXT/JSON)

### TXT Export Formatting

The TXT export (`GET /api/segments/{meeting_id}/export?format=txt`) applies a multi-stage text processing pipeline: segment joining with punctuation at boundaries, filler removal, sentence capitalization, phone number formatting, paragraph splitting at sentence boundaries, and smart trailing period logic. **Full spec with constants, thresholds, and known limitations**: `docs/txt_export_spec.md`. Check that doc before modifying any `_to_txt` helpers in `app/routers/segments.py`.

## Key Design Decisions

- **Embeddings stored as `LargeBinary`** — serialized numpy arrays in SQLite, not a vector DB
- **Single Celery worker** — one video at a time; GPU models can't parallelize
- **Purely additive voiceprints** — old embeddings retained, centroids always recomputed fresh
- **Pipeline is resumable** — each stage checks for existing data before running
- **Diarization cached to disk** — `diarization.json` saves 10-20 min on re-runs
- **Audio cached in memory during embedding** — avoids 1700+ disk reads per meeting

## Environment

- **GPU**: NVIDIA with CUDA (developed on RTX 5090 / CUDA 12.8)
- **PyTorch nightly** required for RTX 5090 — library patches must be reapplied after venv rebuild (see INSTALL.md)
- **HF_TOKEN** env var required for pyannote model access
- **External tools**: ffmpeg, ffprobe, tesseract (for PDF OCR), poppler-utils
- **Redis** required for Celery broker/backend (default: localhost:6379)

## Vocab Hints

`config/vocab_hints.yml` contains domain-specific terms (people, places, agencies) fed to Whisper's `initial_prompt` via `app/services/vocab.py`. Each term has an `active` flag. The prompt is capped at 850 chars.

## Atlas Integration

This project is a spoke in the **Atlas** hub-and-spoke ecosystem. Atlas is a central orchestration hub that routes queries across spoke apps. It lives in a sibling directory (`E:\0-Automated-Apps\Atlas`).

**Rules:**

1. Only modify **this** project by default. Do not modify other spoke projects or Atlas unless explicitly asked.
2. If approved, changes to other projects are allowed — but always propose first and wait for approval.
3. Suggest API endpoint changes in other spokes if they would improve integration, but never write code in another project without explicit approval.
4. This app must remain **independently functional** — it works on its own without Atlas or any other spoke.
5. **No spoke-to-spoke dependencies.** All cross-app communication goes through Atlas.
   **Approved exceptions** (documented peer service calls):
   - `Shasta-PRA-Backup → civic_media POST /api/transcribe` — Transcription-as-a-Service
   New cross-spoke calls must be approved and added to this exception list.
6. If modifying or removing an API endpoint that Atlas may depend on, **stop and warn** before proceeding.
7. New endpoints added for Atlas integration should be general-purpose and useful standalone, not tightly coupled to Atlas internals.

**Spoke projects** (sibling directories, may be loaded via `--add-dir` for reference):

- **civic_media** — meeting transcription, diarization, voiceprint learning (this project)
- **article-tracker** — local news aggregation and monitoring
- **Shasta-DB** — civic media archive browser and metadata editor (FastAPI/HTMX)
- **Facebook-Offline** — local personal Facebook archive for LLM querying (private, local only)
- **Shasta-PRA-Backup** — public records requests browser (uses this project's /api/transcribe)
- **Shasta-Campaign-Finance** — campaign finance disclosures from NetFile
- **Facebook-Monitor** — automated public Facebook page monitoring

## Testing

No formal test suite exists yet. Use Playwright for browser-based UI testing and pytest for API/service tests.

### Setup

```bash
pip install playwright pytest pytest-asyncio httpx
python -m playwright install chromium
```

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run only Playwright browser tests
pytest tests/ -v -k "browser"

# Run only API tests
pytest tests/ -v -k "api"
```

### Writing Tests

- **Browser tests** go in `tests/test_browser.py` — use Playwright to verify UI behavior (card states, progress bars, button transitions, inline PDF viewer, dialog flows)
- **API tests** go in `tests/test_api.py` — use httpx or TestClient against FastAPI endpoints
- **Service tests** go in `tests/test_services.py` — unit tests for pipeline stages, OCR, voiceprint matching
- Playwright is already installed in this project (used by PrimeGov scraper)
- The server must be running at localhost:8000 for browser tests
- Use `page.wait_for_selector()` and `page.wait_for_timeout()` to handle async UI updates (polling, progress bars)

### Key Flows to Test

1. **Meeting discovery**: Discover BOS → cards appear with "download" badges
2. **Download → Transcode → Process lifecycle**: download button → progress bar → transcode button → progress bar → process button
3. **Review page**: video player loads, document tabs show PDFs inline, transcript segments render
4. **Speaker assignment**: assign speaker → voiceprint created → reprocess matches
5. **Export**: SRT/TXT/JSON export produces valid files

## Master Schema & Codex References

**`E:\0-Automated-Apps\MASTER_SCHEMA.md`** — Canonical cross-project database
schema and API contracts. **HARD RULE: If you add, remove, or modify any database
tables, columns, API endpoints, or response shapes, you MUST update the Master
Schema before finishing your task.** Do not skip this — other projects read it to
understand this project's data contracts.

**`E:\0-Automated-Apps\MASTER_PROJECT.md`** describes the overall ecosystem
architecture and how all projects interconnect.

> **HARD RULE — READ AND UPDATE THE CODEX**
>
> **`E:\0-Automated-Apps\master_codex.md`** is the living interoperability codex.
> 1. **READ it** at the start of any session that touches APIs, schemas, tools,
>    chunking, person models, search, or integration with other projects.
> 2. **UPDATE it** before finishing any task that changes cross-project behavior.
>    This includes: new/changed API endpoints, database schema changes, new tools
>    or tool modifications in Atlas, chunking strategy changes, person model changes,
>    new cross-spoke dependencies, or completing items from a project's outstanding work list.
> 3. **DO NOT skip this.** The codex is how projects stay in sync. If you change
>    something that another project depends on and don't update the codex, the next
>    agent working on that project will build on stale assumptions and break things.

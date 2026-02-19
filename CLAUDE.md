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

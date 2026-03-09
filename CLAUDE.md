# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

civic_media is a local-first civic meeting transcription and speaker diarization tool. It processes meeting videos into searchable, speaker-attributed transcripts with a voiceprint learning system that improves speaker identification over time.

**Stack**: FastAPI + SQLAlchemy (SQLite/WAL) | Huey (SqliteHuey) | faster-whisper (large-v3) | pyannote.audio 3.1 | SpeechBrain ECAPA-TDNN | Vanilla JS frontend

## UI Feature Backlog

**`docs/ui-backlog.md`** — persistent list of requested UI changes. Read this at
session start if the task touches any frontend page. Never delete an entry without
user confirmation that it's working in the browser.

## Common Commands

```bash
# Start the API server (dev mode with auto-reload)
uvicorn app.main:app --reload

# Start the Huey GPU worker (separate terminal)
python -m huey.bin.huey_consumer app.worker.huey -w 1 -k thread -C --logfile logs/huey.log

# Start the Huey light worker (I/O tasks — separate terminal)
python -m huey.bin.huey_consumer app.worker.huey_light -w 1 -k thread -C --logfile logs/huey_light.log

# Or use the startup script (starts workers and API together)
# Windows: .huey_watchdog.cmd + start_light_worker.cmd + uvicorn
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

There is no test suite, linter config, or CI pipeline. No external services required — Huey uses SQLite for task queuing (`database/huey.db` and `database/huey_light.db`).

## Architecture

### Processing Pipeline (`app/services/pipeline.py`)

Video upload triggers a Huey task that runs six sequential stages, each with checkpoints for resume:

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
3. Background Huey task recomputes all person centroids and re-matches all unverified segments

Thresholds: high ≥ 0.92, medium ≥ 0.75 (both in `app/config.py`).

### Data Flow

- **Routers** (`app/routers/`) — HTTP endpoints, thin layer that validates input and calls services
- **Services** (`app/services/`) — all business logic, ML model invocations, file I/O
- **Models** (`app/models.py`) — SQLAlchemy ORM: Meeting, MediaFile, Document, TranscriptSegment, Person, Voiceprint, SegmentAssignment
- **Schemas** (`app/schemas.py`) — Pydantic v2 with `from_attributes = True`
- **Tasks** (`app/tasks.py`) — 16 Huey tasks across two queues: GPU (`process_video`, `process_pdf`, `extract_multi_voiceprints`, `rerun_voiceprints`, `process_newscast`) and light I/O (`retag_content`, `ingest_radio`, `transcode_video`, `primegov_discover`, `primegov_download`, `granicus_discover`, `granicus_download`, `export_clip`, `cleanup_clips`, `full_ingest`, `check_minutes`)
- **Config** (`app/config.py`) — all paths, thresholds, model identifiers, env var defaults

### Database

SQLite with WAL mode. Foreign keys enforced. Pragmas set in `app/database.py` on every connection. Huey tasks create their own sessions via `SessionLocal()` (never share sessions across task boundaries).

Key relationships: Meeting → MediaFile, Document, TranscriptSegment (cascade delete). TranscriptSegment → SegmentAssignment (1:1). Person → Voiceprint (1:many), SegmentAssignment (1:many).

### Frontend

Two-page vanilla JS SPA served from `app/static/`:
- **index.html/js** — meeting list, upload, live progress bars
- **review.html/js** — video player + synced transcript with speaker confirmation, text editing, filtering, and export (SRT/TXT/JSON/PDF)

### Transcript Export

Four export formats via `GET /api/segments/{meeting_id}/export?format=srt|txt|json|pdf`. SRT and JSON are raw/unprocessed. TXT and PDF share a text processing pipeline (filler removal, sentence capitalization, phone formatting, paragraph splitting, trailing period logic). PDF generation uses reportlab (`app/services/pdf_export.py`). **Full spec with shared helpers, constants, per-format rules, and known limitations**: `docs/export_spec.md`. Check that doc before modifying any export helpers in `app/routers/segments.py` or `app/services/pdf_export.py`.

## Key Design Decisions

- **Embeddings stored as `LargeBinary`** — serialized numpy arrays in SQLite, not a vector DB
- **Dual Huey workers** — GPU worker (1 thread, heavy ML tasks) + light worker (1 thread, I/O tasks like download/transcode)
- **Purely additive voiceprints** — old embeddings retained, centroids always recomputed fresh
- **Pipeline is resumable** — each stage checks for existing data before running
- **Diarization cached to disk** — `diarization.json` saves 10-20 min on re-runs
- **Audio cached in memory during embedding** — avoids 1700+ disk reads per meeting
- **Light worker runs 2 threads** — enables parallel downloads (e.g., one download + one transcode)

## Granicus Integration — Redding City Council

The City of Redding uses **Granicus** (not PrimeGov) for meeting archives.

| URL | Purpose |
|-----|---------|
| `https://reddingca.granicus.com/ViewPublisher.php?view_id=4` | Full archive listing (HTML) |
| `MediaPlayer.php?view_id=4&clip_id={N}` | Per-meeting page — has MP4 URL in JS |
| `AgendaViewer.php?view_id=4&clip_id={N}` | Agenda PDF (redirect chain → DocumentViewer) |
| `MinutesViewer.php?view_id=4&clip_id={N}&doc_id={UUID}` | Minutes PDF (same redirect pattern) |
| `archive-video.granicus.com/reddingca/reddingca_{UUID}.mp4` | Direct MP4 download |

**No API** — all discovery requires HTML scraping. The Granicus viewer redirects through
Google's doc viewer; `resolve_document_url()` in `scraper.py` extracts the inner PDF URL.

**Key dedup field:** `meetings.granicus_id` (the Granicus `clip_id` integer).

**Document structure** (same as Shasta BOS):
- `document_type = "agenda"` — agenda PDF (always available if meeting has video)
- `document_type = "minutes"` — approved minutes (posted ~3-4 weeks after meeting)

**Ingest files:**
```
app/services/granicus/
  scraper.py     — ViewPublisher + MediaPlayer HTML scraper, document URL resolver
  discovery.py   — create/update Meeting rows (dedup by granicus_id)
  downloader.py  — direct MP4 download + inline 540p transcode + PDF download
scripts/
  migrate_granicus_id.py  — one-time migration: adds granicus_id to meetings table
  granicus_backfill.py    — one-time bulk ingest: discover → register → download → transcode
```

**One-time backfill command** (run from Windows, not WSL):
```
powershell.exe -Command "cd 'E:\0-Automated-Apps\civic_media'; .\venv\Scripts\python.exe scripts\granicus_backfill.py"
```
Flags: `--discover-only`, `--no-video`, `--no-docs`, `--min-date YYYY-MM-DD`, `--dry-run`

**Ongoing discovery task:** `granicus_discover_task` (light worker) — re-run to register
new meetings. New meetings then need `granicus_download_task` to get video + docs.

## Rolling Minutes Availability Check

PrimeGov posts meeting minutes roughly **3–4 weeks after** the meeting occurs (observed range 14–40 days,
average ~23 days). Minutes are approved in batches (3–6 meetings at once), so a given meeting's minutes
may not appear until the next meeting or later.

To ensure minutes PDFs are captured after they become available:

- **`check_minutes_task`** (`app/tasks.py`) runs a two-phase check:
  1. Re-runs PrimeGov discovery for years within the window to refresh `minutes_url` on meetings
     where it wasn't available at initial ingest.
  2. Downloads minutes PDFs for any meeting that now has a `minutes_url` but no `Document` record.
- **Triggered automatically** every 24 hours by the self-heal background thread in `app/routers/backfill.py`.
- **Triggered on demand** via `POST /api/backfill/check-minutes?days=90` (default window: 90 days).
- Anything older than 90 days is treated as a one-off manual fix.

## Environment

- **GPU**: NVIDIA with CUDA (developed on RTX 5090 / CUDA 12.8)
- **PyTorch nightly** required for RTX 5090 — library patches must be reapplied after venv rebuild (see INSTALL.md)
- **HF_TOKEN** env var required for pyannote model access
- **External tools**: ffmpeg, ffprobe, tesseract (for PDF OCR), poppler-utils
- **No external services** — Huey uses SQLite for task queuing (no Redis/RabbitMQ needed)

## Vocab Hints

`config/vocab_hints.yml` contains domain-specific terms (people, places, agencies) fed to Whisper's `initial_prompt` via `app/services/vocab.py`. Each term has an `active` flag. The prompt is capped at 850 chars.

## Meeting Processing Rules

**`docs/meeting_processing_rules.md`** — Canonical rules for processing government
meeting transcripts: speaker inference (regex + LLM + role fallback), agenda detection,
grounding/anti-hallucination, and processing checklist. Read before building or modifying
any meeting summary pipeline.

Also in `docs/`:
- `summary_prompts.md` — Short/long summary prompt templates, Notable Moment types, Brown Act compliance
- `tag_taxonomy.md` — 107 canonical tags across 5 dimensions (TOPIC, AGENCY, ACTION, MONEY, PLACE)
- `diarization_codex.md` — Audio/voiceprint pipeline (speaker ID from audio, not text)

---

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
   - `Atlas → Mission_Control POST /models/run` (and related LLM endpoints) — Atlas delegates all LLM execution to Mission Control rather than using its own built-in LLM service.
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

### Lazy ChromaDB Sync (Atlas RAG)
Atlas maintains a centralized ChromaDB vector store. This project does NOT need its
own vector DB. Atlas fetches candidate records from this spoke's search API, chunks
deterministically, validates against ChromaDB cache, and embeds only new/stale chunks.
ChromaDB is a cache — this spoke's SQLite DB is the source of truth.

See: `Atlas/app/services/rag/deterministic_chunking.py` for this spoke's chunking strategy.

## DB Field Naming Rule

**HARD RULE — Field names must be globally unique and descriptive.**
Before adding any new column, check `MASTER_SCHEMA.md` for existing field names. If your
proposed name conflicts with or could be confused with an existing field in this or any other
spoke (e.g., a bare `group` column when `group_name`/`group_id`/`group_type` already exist),
stop and propose a more specific alternative. See root `CLAUDE.md §17` for the full rule.

## Master Schema & Codex References

**`E:\0-Automated-Apps\MASTER_SCHEMA.md`** — Canonical cross-project database
schema and API contracts. **HARD RULE: If you add, remove, or modify any database
tables, columns, API endpoints, or response shapes, you MUST update the Master
Schema before finishing your task.** Do not skip this — other projects read it to
understand this project's data contracts.

**`MASTER_SCHEMA.md` §11 — Canonical Query Patterns** — Ready-to-use SQL and REST
examples for every common civic_media query (meetings by body type, transcript search,
vote records, speaker attribution, etc.). **If you add a new queryable column or table,
add a pattern to §11 in the same session.** If you are building an LLM tool that
searches this project's data, start with §11 before writing any query.

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


## AI Learning CODEX — Hard Rules

> **`E: -Automated-Apps\AI-Learning-CODEX\INDEX.md`** — shared technical knowledge base for all agents.
>
> **HARD RULE 1 — CHECK BEFORE STRUGGLING**
> At session start (if task touches Python compat, Blender, IPC, metaclasses, timers, or OS-specific code)
> OR after 2 failed attempts on the same problem:
> Scan the Quick-Find symptom table in `INDEX.md`. If your symptom matches, read the linked file
> before trying another approach. Takes under 60 seconds. May save hours.
>
> **HARD RULE 2 — CONTRIBUTE BEFORE CLOSING**
> If you solved a problem that required multiple interactions or non-obvious research:
> Add a dated entry to the relevant topic file, then update `INDEX.md` (symptom table + last-updated date).
> Never add to a topic file without also updating INDEX.md.
> See **`E:\0-Automated-Apps\MASTER_INDEX.md`** for fast navigation into both documents.
> See **`E:\0-Automated-Apps\NEW_APP_INTAKE.md`** before starting any new application.

---

## Post-v1 TODO — Field Naming Improvements (LLM Inferability)

**Do not implement during active development. Schedule after Mission Control v1 is complete.**

These renames bring this project into full Rule 17 compliance (globally unique, descriptive field names).
Each rename requires: additive migration (add new column, copy data, update queries, drop old column),
MASTER_SCHEMA.md update, and a Changelog entry in master_codex.md.

| Table | Current Field | Rename To | Reason |
|-------|--------------|-----------|--------|
| `tv_newscasts` | `status` | `newscast_status` | Bare `status` is ambiguous across ecosystem tables |
| `processing_jobs` | `status` | `processing_job_status` | Bare `status` is ambiguous across ecosystem tables |

## Unified Tools — Syllego (MMI)

If this project needs to download media from an external URL and doesn't have its own ingest path for that platform, **Syllego (MMI)** is available as an optional shared library.

```python
import os
os.environ["MMI_CALLER"] = "civic-media"   # identifies this app in Syllego's log
import mmi
result = mmi.ingest(url)   # returns IngestionResult
if result.success:
    print(result.filename)
```

**Install:** `pip install -e "E:/0-Automated-Apps/Unified-Tools/Syllego"`
**Supports:** YouTube, Facebook, Rumble, TikTok, Instagram, Reddit, Vimeo, and more.
**Full API:** `E:\0-Automated-Apps\Unified-Tools\Syllego\AGENT_SPEC.md`

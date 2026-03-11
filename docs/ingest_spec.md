# Civic Media — Ingest Specification

**Last updated**: 2026-03-10 (rev 2)
**Maintainer**: civic_media project

---

## Overview

Civic Media ingests content from two distinct source categories:

| Category | DB `category` | Source | Trigger |
|----------|---------------|--------|---------|
| Radio / Audio Shows | `audio` | KCNR archive, SecureNet on-demand, Freedom in Action blog, Podbean RSS | Manual button → Celery task |
| Government Meetings | `meeting` | PrimeGov API + Swagit video CDN | Manual dialog → Celery task |

Both categories feed the same `meetings` table. The downstream processing pipeline (transcode → transcribe → diarize → embed → voiceprint) is identical regardless of source.

---

## Part 1 — Audio / Radio Show Ingest

### 1.1 How It Works

**Entry point**: `POST /api/ingest/run` → queues `ingest_radio_task` (Celery).
**Coordinator**: `app/services/ingest/__init__.py` — `run_ingest(db, source_id=None)`.

High-level flow:
```
1. Load enabled IngestSource records from DB
2. Build show_cutoffs: {show_name: newest_episode_date_in_DB}
3. For each source → scraper.scrape(show_cutoffs) → list[ScrapedEpisode]
4. Deduplicate against DB (by source_url, then by date+show_name)
5. download_episode() for each new episode → Meeting + MediaFile records
6. Enrich metadata on existing meetings (description, thumbnail_url)
```

### 1.2 Sources

#### KCNR Archive
- **File**: `app/services/ingest/kcnr.py`
- **Base URL**: `https://apps.kcnr1460.com`
- **Shows**:

| Show Name | Archive Path |
|-----------|-------------|
| Kevin Crye Show | `/Show/archive/kevin_crye` |
| Kevin Crye Show (Poke archive) | `/Show/archive/poke` |
| Free Fire Radio | `/Show/archive/free_fire_radio` |
| Jefferson State of Mine | `/Show/archive/jefferson_state` |

- **Pagination**: `?page=N` (1 to `max_pages`, default 20). Detects `[next]` link in HTML. Each page lists MP3 links with filenames encoding the date (`SHOW_YYYY-MM-DD.mp3`) or `/media/YYYY/MM/DD/` URL paths.
- **Date parsing**: Also handles ordinal prose dates ("Sunday, February 22nd, 2026").
- **Audio URL**: Direct MP3 link from page anchor tags.
- **Config JSON keys**: `base_url`, `shows` (list of show slugs), `max_pages`, `cutoff_date` (optional: excludes episodes with dates >= this value, used when a show migrated platforms).

#### SecureNet On-Demand
- **File**: `app/services/ingest/securenet.py`
- **Station URL**: `https://radio.securenetsystems.net/v5/KQMS`
- **Pagination**: None — single-page scrape. The page embeds a JavaScript `onDemandQueue[]` array with all available on-demand files.
- **Show name guessing**: Inferred from title/filename keywords ("hornet" → "Poke the Hornets Nest", "freedom" → "Freedom in Action", "kevin crye" → "Kevin Crye Show", etc.).
- **Audio URL**: Reconstructed from `streamRoot + stationCallSign + "/ondemand/" + filename`.
- **Note**: KQMS purges on-demand files after approximately 4 weeks. SecureNet is a secondary/fallback source for shows also available on KCNR.
- **Config JSON keys**: `station_id`, `station_url`, `shows`.

#### Freedom in Action Blog
- **File**: `app/services/ingest/freedominaction.py`
- **Base URL**: `https://www.freedominactionradio.com/blog-news`
- **Pagination**: Offset-based (`?offset=N`), follows `<a rel="next">` links.
- **Structure**: Each `<article>` is one blog post. Posts publish on Saturdays and typically contain two embedded audio files (Hour One, Hour Two). Audio is hosted on Squarespace CDN.
- **Audio URL**: Parsed from `<div class="sqs-audio-embed" data-url="...">` attributes.
- **Title format**: `"Freedom in Action — {blog post title} (Hour One)"` / `"(Hour Two)"`.
- **Duration**: From `data-duration-in-ms` attribute (milliseconds → seconds).
- **Config JSON keys**: `blog_url`, `max_pages`.

#### Podbean RSS
- **File**: `app/services/ingest/podbean.py`
- **Feed URL**: `https://feed.podbean.com/admind4/feed.xml`
- **Pagination**: Single RSS feed fetch. RSS items are sorted newest-first; early stop fires on cutoff date.
- **Fields**: Standard RSS/iTunes podcast namespace (`<enclosure>`, `<itunes:duration>`, `<itunes:image>`, `<pubDate>`).
- **Config JSON keys**: `feed_url`, `show_name`.

### 1.3 Incremental Sync Logic (Early Stop)

Each scraper accepts a `show_cutoffs: dict[str, str]` argument mapping show name → newest episode date already in DB (format: `YYYY-MM-DD`). Built once per run:

```python
SELECT group_name, MAX(meeting_date)
FROM meetings
WHERE category = 'audio'
GROUP BY group_name
```

**Behavior per scraper type**:

| Scraper | Cutoff Behavior |
|---------|----------------|
| KCNR | Per-show: on each page, adds only episodes newer than cutoff; stops paginating when a page contains any episode at/before cutoff |
| SecureNet | Post-fetch filter: drops all episodes at/before their show's cutoff date |
| Freedom in Action | Per-page: adds only newer episodes; stops paginating when page hits cutoff date |
| Podbean | RSS walk: breaks on first item at/before cutoff (feed is already sorted newest-first) |

**Effect**: A routine ingest run fetches at most a few pages per source rather than hundreds of archive pages. Full history is recovered automatically if the show has no prior content in DB (cutoff is absent from the dict).

### 1.4 Deduplication

Two-pass dedup before any download:

1. **By source URL**: `SELECT source_url FROM meetings WHERE source_url IN (...)` — skips exact URL matches.
2. **By date + show name**: `SELECT meeting_date, group_name FROM meetings WHERE category='audio'` — skips cross-source duplicates (same episode available on both KCNR and Podbean).

### 1.5 Episode Download

**File**: `app/services/ingest/downloader.py` — `download_episode(db, episode)`.

- Streams audio file via HTTP (15s connect / 300s read timeout) to `media/{meeting_id}/{date}_{show_name}.{ext}`.
- Creates `Meeting` record (`category='audio'`) and `MediaFile` record (`file_type='audio'`).
- Sets `group_id` via `ensure_group(db, show_name, group_type='show')` — creates the group record if it doesn't exist.
- No processing is triggered automatically. The backfill system handles downstream steps.

### 1.6 Backfill Behavior for Audio

**Backfill runs once per episode** — there is no document concept for radio shows. Once an audio file is downloaded and the `Meeting` record exists, there is nothing further to discover about it. The incremental sync ensures new episodes are picked up on subsequent runs, but already-downloaded episodes are never re-fetched.

---

## Part 2 — Government Meeting Ingest (PrimeGov)

### 2.1 How It Works

**Entry point**: `POST /api/primegov/discover?committee_ids=3&mode=update` → queues `primegov_discover_task` (Celery).
**Discovery**: `app/services/primegov/discovery.py` — `run_discovery(db, committee_ids, years, mode)`.
**Download**: `app/services/primegov/downloader.py` — `download_video()` and `download_document()`.

High-level flow:
```
1. Discover meetings from PrimeGov API (see §2.3)
2. Create/update Meeting records in DB
3. Auto-queue primegov_download_task for any meeting that gained new document URLs
4. Backfill UI manually drives: Download → Transcode → Process pipeline
```

### 2.2 Committees

Configured in `app/services/primegov/scraper.py`:

| Committee ID | Name |
|-------------|------|
| 3 | Board of Supervisors (default) |
| 2 | Air Pollution Control Hearing Board |
| 4 | Housing Authority |
| 5 | In-Home Supportive Services |
| 6 | Planning Commission |
| 7 | Waterworks District #1 |

The PrimeGov dialog in the UI defaults to committee ID 3 (Board of Supervisors). Multiple committees can be selected simultaneously.

### 2.3 Discovery

**API endpoints used**:
- `GET https://shastacounty.primegov.com/api/v2/PublicPortal/GetArchivedMeetingYears` — list of years with archived meetings
- `GET .../ListArchivedMeetingsByCommitteeId?year=Y&committeeId=C` — meetings for a given year
- `GET .../ListUpcomingMeetingsByCommitteeId?committeeId=C` — scheduled upcoming meetings

**Per-meeting data captured**:
- `primegov_id` (int): Stable PrimeGov identifier — used as upsert key
- `title`, `meeting_date` (YYYY-MM-DD), `committee_name`
- `video_url`: Swagit page URL if recording is available (e.g., `https://shastacountyca.new.swagit.com/videos/349956`)
- `agenda_url`, `minutes_url`, `packet_url`: PrimeGov compiled PDF download URLs

**Document URL format**:
```
https://shastacounty.primegov.com/Public/CompiledDocument?meetingTemplateId={id}&compileOutputType=1
```

### 2.4 Sync Modes

The PrimeGov dialog exposes a **"Full history"** checkbox (unchecked = update mode by default):

| Mode | Trigger | Scope | Use Case |
|------|---------|-------|----------|
| `update` | Default (checkbox unchecked) | 90 days before newest meeting in DB | Routine weekly sync to pick up new meetings and newly-posted documents |
| `full` | "Full history" checkbox checked | All available years | Initial population, recovery, or manual audit |

**Why 90 days for update mode**: New meetings are obviously within this window. More importantly, minutes are often posted weeks after the meeting date. A 90-day window ensures that a meeting from two months ago can still have its newly-posted minutes discovered and downloaded.

### 2.5 Upsert Behavior

Discovery does **not overwrite existing data**. For each discovered meeting:

- **If `primegov_id` is new** → create full `Meeting` record.
- **If `primegov_id` already exists** → update only fields that are currently NULL:
  - `video_url` — recording may appear days after the meeting
  - `agenda_url` — sometimes posted same day, sometimes after
  - `minutes_url` — typically posted 1–4 weeks after meeting
  - `packet_url` — may accompany agenda or minutes

This means running discovery again never overwrites a field that was already populated.

### 2.6 Auto-Queue on New Document URLs

After the upsert loop commits, discovery checks which meetings transitioned from `NULL → non-NULL` on any document field. For each such meeting, it immediately queues a `primegov_download_task` scoped to only the newly-available assets:

```python
primegov_download_task.delay(
    meeting.meeting_id,
    download_video=(video_url changed),
    download_agenda=(agenda_url changed),
    download_minutes=(minutes_url changed),
    download_packet=(packet_url changed),
)
```

**Effect**: Running a weekly `update` discovery automatically downloads minutes and packets for recent meetings as soon as PrimeGov posts them — no manual intervention needed.

### 2.7 Video Download

**File**: `app/services/primegov/downloader.py` — `download_video()`.

1. Load Swagit page in headless Chromium (Playwright) and intercept the HLS `playlist.m3u8` network request.
2. Download with ffmpeg:
   - **Video**: Stream-copied (`-c:v copy`) — no re-encoding, fast.
   - **Audio**: Re-encoded to clean AAC-LC (`-c:a aac -b:a 128k`) — **prevents HLS segment boundary corruption** (see §2.8).
3. Output: `media/{meeting_id}/{date}_{title}.mp4`
4. Creates `MediaFile` record (`file_type='video'`).
5. **Grace period**: If the meeting is within 5 days of its scheduled date and the Swagit page has no recording yet, returns `"not_available_yet"` instead of a hard error. The backfill system will retry on the next run.

### 2.8 Why Audio Is Re-Encoded During Download

Shasta County government meetings are served as HLS streams (`.m3u8` playlists of `.ts` segments). Each HLS segment carries its own AAC codec configuration header. The segments are produced by different encoder instances and can have subtly different AAC profile settings — different numbers of scalefactor bands, sample rate indices, etc.

`ffmpeg -c copy` concatenates these segments into a single MP4 without decoding — fast, but it preserves the per-segment differences. When the pipeline later tries to extract a WAV using an audio filter chain (highpass + loudnorm), the AAC decoder encounters the profile change at the segment boundary and aborts with `"Invalid data found when processing input"`, producing a truncated WAV file.

Re-encoding audio to a single continuous AAC-LC stream at download time eliminates this entirely. The encoding overhead is negligible (~2 minutes for a 4-hour meeting).

**Fallback in pipeline**: If a pre-existing video with this issue is encountered, `app/services/pipeline.py` will:
1. Detect the corrupt WAV via duration check (< 10% of expected).
2. Run `probe_audio_errors()` to confirm.
3. Automatically fall back to `extract_audio_from_url()`, fetching the HLS source directly — ffmpeg's HLS demuxer handles per-segment codec headers transparently.

### 2.9 Document Download

**File**: `app/services/primegov/downloader.py` — `download_document(db, meeting_id, doc_type)`.

- HTTP GET to the PrimeGov compiled PDF URL (60s timeout).
- Saves to `documents/{meeting_id}/{date}_{title}_{doc_type}.pdf`.
- Creates `Document` record and queues `process_pdf_task` (OCR extraction).
- `doc_type` is one of: `agenda`, `minutes`, `packet`, `addendum`.

**Addendum handling**: PrimeGov posts addenda as separate meeting entries titled
`"Addendum: <committee name>"`. Discovery merges these onto the parent meeting by
setting `addendum_url` on that meeting (added 2026-03-10). The addendum PDF is then
downloaded as a `Document` with `document_type='addendum'`. It is stored separately
from the original `agenda` — `agenda_url` always points to the original agenda posted
before the meeting; `addendum_url` is populated only if PrimeGov publishes a late addition.

**OCR engine** (`app/services/pdf_ingestor.py`): two-stage pipeline.
  1. **pdfplumber** — native text extraction for digital/searchable PDFs (instant, no GPU).
  2. **Surya OCR** (PyTorch + CUDA, RTX 5090) — transformer-based model for scanned/image pages.
     Falls back to Surya when pdfplumber returns < 50 characters.
     Predictors are cached in-process after first load (~2–5s warm-up).

Surya replaced the previous Tesseract → EasyOCR fallback chain on 2026-03-10.
See `SURYA_INSTALL.md` for dependency notes and model cache location.

### 2.10 Automated Document Polling Schedule

The self-heal background thread (started on FastAPI startup) drives two recurring checks:

| Task | Cadence | Trigger | What it does |
|------|---------|---------|-------------|
| `check_upcoming_docs_task` | **Every hour** | Auto + `POST /api/backfill/check-upcoming-docs` | Re-runs PrimeGov discovery, then downloads any missing agenda/packet/addendum for meetings within the next 30 days |
| `check_minutes_task` | **Every 7 days** | Auto + `POST /api/backfill/check-minutes` | Re-runs discovery for both PrimeGov and Granicus, then downloads minutes for any meeting (past 90 days) that now has a `minutes_url` |

**Why hourly for upcoming docs**: Agenda and packet PDFs are typically posted 1–5 days before
the meeting but the exact time is unpredictable. Hourly polling ensures they are captured and
OCR'd well before the meeting date. Addenda can appear with even less notice (same day).

**Why weekly for minutes**: Minutes are posted 1–4 weeks after the meeting. Daily polling is
unnecessary overhead; weekly is sufficient to capture them promptly.

**Manual trigger**: Both endpoints accept optional parameters and return immediately with a
task ID. The actual work runs in the Huey light worker.

### 2.10 Backfill Behavior for Government Meetings

Government meetings have **persistent documents that may appear long after the meeting date**. This is the key difference from audio shows:

| Asset | When Available | Action |
|-------|---------------|--------|
| Video recording | Same day or next business day | Download once; never re-download |
| Agenda | Day of meeting or day before | Download once |
| Minutes | 1–4 weeks after meeting | **May be NULL at initial discovery; picked up on next `update` sync** |
| Packet | Same as agenda | Download once |

**The update-mode discovery cycle handles this automatically**:
- Weekly `update` discovery (90-day window) picks up any new `minutes_url` values that PrimeGov has published.
- The auto-queue mechanism downloads them immediately without user action.
- Because upsert only touches NULL fields, no already-downloaded document is ever overwritten.

**"Backfill once" does NOT apply to government meeting documents**. An `update` sync should be run regularly (weekly is sufficient) so that minutes published after the initial discovery are captured. All other assets (video, agenda, packet) follow the standard "download once, never repeat" rule.

---

## Part 3 — Backfill Pipeline

The backfill system (`app/routers/backfill.py`) drives the three processing stages after ingest:

```
Download → Transcode → Process
```

Each stage is triggered one meeting at a time by the UI ("Download All", "Transcode All", "Process All" buttons), which call `/api/backfill/next-{stage}` in a loop and wait for worker-idle between each step.

### 3.1 Stage Definitions

| Stage | Task | Input | Output | Notes |
|-------|------|-------|--------|-------|
| Download | `primegov_download_task` | `Meeting.video_url` | `MediaFile(file_type='video')` | Audio shows skip this — file already on disk from ingest |
| Transcode | `transcode_video_task` | Original MP4 | 540p MP4 | Replaces original; saves disk space |
| Process | `process_video_task` | Transcoded MP4 or audio file | `TranscriptSegment` rows + speaker embeddings | Full ML pipeline (transcribe → diarize → embed → voiceprint) |

### 3.2 State Tracking

All job state is stored in the `processing_jobs` table:

```
stage:  'download' | 'transcode' | 'process'
status: 'queued' | 'running' | 'done' | 'error'
```

SSE (`GET /api/backfill/events`) streams real-time progress from Redis pub/sub (`civic_media:progress` channel).

### 3.3 Stuck Job Recovery

Jobs stale for > 10 minutes are auto-reset to `error` by `_do_reset_stuck()`, which runs before every `next-*` selection. The backfill UI also exposes `POST /api/backfill/reset-stuck` for manual use.

**Crash counter**: If a meeting accumulates ≥ 3 `error` process jobs, the pipeline raises `RuntimeError` immediately on the next attempt (prevents infinite crash-retry loops on bad files). Clear with `POST /api/backfill/clear-errors/{meeting_id}`.

### 3.4 Skip Set

`POST /api/backfill/skip/{meeting_id}` adds a meeting to a Redis skip set, permanently excluding it from auto-queue selection without deleting any data. `unskip` reverses this.

---

## Part 4 — Summary Table

| Source | Content Type | Pagination | Incremental Logic | One-Time or Recurring |
|--------|-------------|-----------|------------------|----------------------|
| KCNR Archive | Audio (MP3) | Page-based (`?page=N`) | Early-stop on `show_cutoffs[show_name]` | Recurring — new episodes weekly |
| SecureNet On-Demand | Audio (M4A) | None (single page) | Post-fetch filter by cutoff | Recurring — rolling 4-week window |
| Freedom in Action Blog | Audio (MP3, Squarespace CDN) | Offset-based (`?offset=N`) | Early-stop on `show_cutoffs["Freedom in Action"]` | Recurring — new episodes weekly |
| Podbean RSS | Audio (enclosure) | RSS walk (newest-first) | Break on first item at/before cutoff | Recurring — new episodes weekly |
| PrimeGov (Meetings) | Meeting metadata | REST API per year per committee | `update` mode: 90-day window | Recurring — weekly recommended |
| PrimeGov (Video) | MP4 via Swagit/HLS | N/A (per-meeting) | Download once; skip if `MediaFile` exists | **One-time per meeting** |
| PrimeGov (Agenda/Packet) | PDF | N/A (per-meeting) | Hourly until downloaded; skip if `Document` exists | **Hourly until captured, then one-time** |
| PrimeGov (Addendum) | PDF | N/A (per-meeting) | Hourly check; only present for some meetings | **Hourly until captured (rare), then one-time** |
| PrimeGov (Minutes) | PDF | N/A (per-meeting) | Weekly check; download once when `minutes_url` becomes non-NULL | **Weekly until minutes posted, then one-time** |

---

## Part 5 — Running Ingest

### Audio Shows

```
UI: Media Library → Audio tab → "Ingest Radio Shows" button
API: POST /api/ingest/run                   (all enabled sources)
     POST /api/ingest/run?source_id={id}    (single source)
```

All enabled sources run in the same Celery task. The coordinator builds `show_cutoffs` once and passes it to every scraper.

### Government Meetings

```
UI: Media Library → "Discover BOS" button → PrimeGov dialog
    - Select committees (default: Board of Supervisors)
    - "Full history" unchecked = update mode (last 90 days) [DEFAULT]
    - "Full history" checked = full scan of all available years
API: POST /api/primegov/discover?committee_ids=3&mode=update
     POST /api/primegov/discover?committee_ids=3&mode=full
```

**Recommended routine**: Run `update` mode weekly. Run `full` mode once during initial setup or after a gap in coverage.

### Backfill (All Sources)

```
UI: Backfill Manager (http://localhost:8000/static/backfill.html)
    - "Download All" → drives /api/backfill/next-download loop
    - "Transcode All" → drives /api/backfill/next-transcode loop
    - "Process All" → drives /api/backfill/next-process loop
API: POST /api/backfill/process-now/{meeting_id}   (specific meeting)
     POST /api/backfill/full/{meeting_id}           (download+transcode+process chain)
```

---

## Part 6 — Known Constraints and Edge Cases

| Issue | Behavior | Mitigation |
|-------|---------|------------|
| KQMS SecureNet purges after ~4 weeks | Old episodes are permanently gone | KCNR archive is the primary source; SecureNet is supplementary |
| Swagit recording not posted at time of discovery | `download_video()` returns `"not_available_yet"` | Grace period (5 days); re-run backfill download to retry |
| PrimeGov minutes not posted at time of discovery | `minutes_url` is NULL | Weekly `update` discovery auto-queues download when URL appears |
| HLS segment AAC profile mismatch in pre-2025 video downloads | Pipeline audio extraction fails at segment boundary | Pipeline fallback: re-fetches audio from original HLS source via `extract_audio_from_url()`. **New downloads** are immune — audio is re-encoded at download time |
| Worker crash during processing | Job left in `running` state; stuck-reset converts to `error` | Crash counter (3+ errors → auto-skip); clear with `POST /api/backfill/clear-errors/{id}` |
| Freedom in Action migrated CDN | Pre-Dec 2025: KQMS SecureNet (purged). Dec 2025+: Squarespace CDN | Scraper targets Squarespace CDN; older episodes are unrecoverable |
| Manual upload uses `supplemental` type | Upload router previously only accepted `agenda`, `minutes`, `supplemental` — `packet` was rejected | Fixed 2026-03-10: `packet` added to `_VALID_DOC_TYPES` in `routers/documents.py` |
| Meeting discovered before documents posted | `agenda_url`/`packet_url` already in DB when docs are uploaded manually; auto-queue does not re-fire | Upload manually via API: `POST /api/documents/{meeting_id}/upload` with `document_type=agenda` or `packet` |

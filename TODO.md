# TODO — civic_media

## Immediate (run these scripts)

### Seed new tags
New ACTION tags (Adversarial, Revelation, Accusation, Testimony, Conduct, Recusal, Legal Warning,
Recess — Disruption, Room Cleared, Brown Act) and new TOPIC tag (Ethics) added to `seed_tags.py`
but not yet seeded into the DB.
```bash
python seed_tags.py
```

### Ingest Brown Act
PDF stored at `E:\0-Automated-Apps\Brown-Act-2026.pdf`. Requires PyMuPDF.
```bash
pip install pymupdf
python scripts/ingest_brown_act.py
```

### Re-run backfill to pick up skipped transcodes
The 2026-02-24 backfill run used old code. Several meetings were skipped/failed:
- ~8 meetings with stuck `transcode_status = "transcoding"` not reset
- 2 meetings (2025-10-21, 2025-08-19) have valid 540p files but DB still says "pending"
```bash
python backfill_bos.py --limit 50
```

### Backfill meeting votes
127+ minutes OCR text files ready to parse. Script is idempotent.
```bash
python scripts/ingest_minutes_votes.py
```

---

## High Priority — civic_media (Near-Term)

### ✅ Roll out `program_type` Steps 1–4 — DONE (2026-03-02)
- ✅ Step 1: `app/models.py` — `program_type` ORM column added
- ✅ Step 2: `app/schemas.py` — `program_type` in MeetingCreate / MeetingUpdate / MeetingOut
- ✅ Step 3: `app/routers/meetings.py` — `?program_type=` filter + auto-derive on create
- ✅ Step 4: `app/services/primegov/discovery.py` — filter + set on new records

### Roll out `program_type` Steps 5–8 — Cleanup
| Step | File | What |
|------|------|------|
| 5 | `app/services/ingest/__init__.py` + `downloader.py` | Set `program_type='media_broadcast'` on new audio records |
| 6 | `app/main.py` | Startup group tagging uses `program_type` instead of `category` |
| 7 | `app/routers/library.py` | Derive `media_type` from `program_type` |
| 8 | `app/routers/people.py` | Include `program_type` in appearance responses |

### Worker status pill on review.html
The worker pill (green/red/yellow) is only on index.html. Review page also dispatches Celery tasks
(confirm speaker → rerun voiceprints) but has no worker visibility. Add the same pill there.

### Summary ingest pipeline
Parse `---TAGS-*:` tag footer from LLM-generated summaries → create `tag_assignments` records →
strip footer before writing to `summary_short`/`summary_long`. Not yet built.
Needs a `POST /api/meetings/{id}/summary/ingest` endpoint.

### Brown Act RAG embedding
`reference_sections` rows are in the DB but not yet embedded into ChromaDB.
Needs `reference_sections` added as a source type in Atlas `POST /api/rag/pre-index`.

### Update Master Schema and Codex
- Update `E:\0-Automated-Apps\MASTER_SCHEMA.md` with new endpoints (worker-health, restart-worker, program_type filter)
- Update `E:\0-Automated-Apps\master_codex.md` with worker health integration + program_type details

### Commit backfill_bos.py to git
`backfill_bos.py` is untracked. Has important fixes (stale detection, _probe_duration, auto_process=False).
Decide if it should be tracked or stay as a local utility.

---

## Diarization Improvements — `diarization-improvement.txt`
Plan written by external LLM. Full spec in that file. Implements venue context + source type
as matching signals without replacing the existing pipeline.

### Schema changes needed
- `venues` table — already exists ✅
- `governing_bodies.default_venue_id` — already exists ✅
- `meetings.venue_id` — already exists ✅
- `transcript_segments.source_type` — NOT NULL DEFAULT 'in_person' — **needs migration**
- `voiceprints.venue_id` — nullable — **needs migration**
- `voiceprints.source_type` — DEFAULT 'in_person' — **needs migration**

### Stage 6 matching changes
- Venue-aware candidate pool (Tier 1 venue-familiar + boost, Tier 2 rest)
- Source-type pool separation — hard split, not a boost (in_person vs telephone pools)
- Venue-weighted centroid computation (prevents outlier venue from skewing centroid)

### Confirmation flow changes
- Capture `venue_id` + `source_type` on voiceprint at confirmation time

### UI changes (review.html)
- Source type toggle: `[ In Person ]  [ Phone ]` — immediate DB write on click
- Venue display on meeting settings (override badge, default badge, acoustic_type label)
- Candidate list: venue familiarity badge + score display (raw → adjusted)
- Segment list: telephone badge on phone-sourced segments

---

## Civic Breakdown — Public Platform
Full roadmap: `docs/public-vps-roadmap.md`
9-phase implementation plan from domain purchase to public launch.

### Phase 0 — Purchases (Do First, No Code Required)
- [ ] Register `civicbreakdown.com` (~$12/year) — **do today**
- [ ] Sign up for Wasabi cloud storage (wasabi.com)
- [ ] Provision Hetzner CX32 VPS (~$8/mo) when ready to build
- [ ] Create Anthropic API account (separate from Claude Code) at console.anthropic.com
- [ ] Create DeepSeek API account at platform.deepseek.com
- [ ] Install Tailscale on home machine
- [ ] Enable Wix Members + Wix Pricing Plans on northstatebreakdown.com

### Phase 1 — Local Prep (civic_media changes for public sync)
- [ ] Install boto3, write `app/services/wasabi.py` upload helpers
- [ ] Post-pipeline hook: upload 540p video + PDFs to Wasabi after processing
- [ ] Define public sync schema (which tables/columns are safe to expose)
- [ ] Write `scripts/export_public_snapshot.py` — sanitized SQLite export
- [ ] Write `scripts/sync_to_vps.py` — rsync snapshots to VPS
- [ ] Seed job: `scripts/generate_seed_queries.py` — auto-generates "On The Record" content

### Phase 2–9
See `docs/public-vps-roadmap.md` for full step-by-step checklist.

### Home preview (before VPS)
- [ ] Build Civic Breakdown public frontend (read-only, home data)
- [ ] Set up Cloudflare Tunnel: `civicbreakdown.com` → local FastAPI at localhost:8000
- [ ] Show private preview to potential subscribers for early commitments

---

## Signal-Desk — New Project (Replacing Article Tracker + Facebook-Monitor)
Local capture tool. Not a spoke. Produces queryable data for Atlas.
- Two data streams: articles (metadata public on Civic Breakdown) + Facebook (internal)
- No port / no spoke registration — Atlas queries it directly
- **Status**: Not yet started. Design data model before building.

---

## Low Priority

### Clean up orphaned original video files
Several meetings have BOTH the original (multi-GB) and 540p files because unlink() failed with WinError 32.
Check media/ dirs for meetings that have both {name}.mp4 and {name}_540p.mp4.

### DuplicateNodenameWarning from Celery
Backfill log shows: "Received multiple replies from node name: celery@Strix-790E-2024"
Consider longer kill wait or unique node names.

### WinError 32 root cause investigation
Something holds file handles on downloaded .mp4 files. Retry+proceed is a workaround but root cause unknown.

### Add other committees to backfill
Currently only BOS (committee_id=3). Could add Planning Commission, Housing, etc.

### Minutes parser extension — other governing bodies
Redding City Council minutes format will be different from BOS.
When adding: check `/api/votes/{id}/unmatched`, write new `_parse_*` function,
register in `PARSER_REGISTRY` in `app/services/minutes_parser.py`.

### Shasta County Board of Education (SCOE) meeting monitor
- eboardsolutions client ID: `S=36030461`
- Blocked by Incapsula via urllib — try Playwright
- Video likely on YouTube/Vimeo — match by date

### Scheduled re-discovery
Auto-detect new minutes/videos on a schedule instead of manual discovery runs.

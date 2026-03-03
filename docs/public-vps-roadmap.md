# Ecosystem — Public VPS Roadmap

**Status**: Planning
**Goal**: Launch a public-facing version of the Atlas ecosystem backed by cloud storage,
a VPS, and a hybrid LLM inference layer — keeping all heavy processing and private data local.

---

## Ecosystem Migration Map

| Project | Goes Public? | What Gets Exposed | Notes |
|---------|-------------|-------------------|-------|
| **Civic Media — Governing Meetings** | ✅ Full | Transcripts, speaker attribution, votes, summaries | Core of the public app |
| **Civic Media — Radio / Web / TV Shows** | 🟡 Metadata only | Summary, episode title, air date, station/source | No audio, no transcript text — copyright |
| **Shasta-PRA** | ✅ Full | Public records requests, documents, status | Explicitly queryable online |
| **Shasta-Campaign-Finance** | ✅ Full | Contributions, expenditures, candidates | Pure public records from NetFile |
| **Signal-Desk — Articles** | 🟡 Metadata only | Summary, publish date, source/outlet | Same model as radio shows — no full text |
| **Signal-Desk — Facebook data** | ❌ Internal | Nothing yet | Internal research only; exposure TBD |
| **Public Atlas** | ✅ New build | Stripped public interface, no admin tools | Completely different app from local Atlas |
| **Shasta-DB** | ❌ Local only | — | Local file archive, not for migration |
| **Facebook-Offline** | ❌ Local only | — | Private personal data |
| **Facebook-Monitor** | ❌ Deprecated | — | Replaced by Signal-Desk |
| **Article Tracker** | ❌ Deprecated | — | Replaced by Signal-Desk |
| **Signal-Desk (the app)** | ❌ Local only | — | Capture/ingestion tool, not a spoke |
| **Mission Control** | ❌ Local only | — | Internal knowledge base and agent tooling |
| **Atlas (admin)** | ❌ Local only | — | Internal orchestration stays home |

---

## The "Metadata Only" Content Model

For copyrighted or legally sensitive content (radio shows, web shows, TV, articles),
the public site exposes a **summary card** — not the original material.

### What a summary card contains
| Field | Example |
|-------|---------|
| Title / Episode name | "Kevin Crye Show — March 1, 2026" |
| Air date | 2026-03-01 |
| Source / Station | KCNR 1460 AM |
| LLM-generated summary | "Topics: water rate increases, county budget shortfall..." |
| People mentioned | [Kevin Crye, Supervisor Jones] |
| Topics / Tags | [Budget, Water, Infrastructure] |
| Source URL | https://apps.kcnr1460.com/... (original, links out) |

### What it does NOT contain
- Audio or video files
- Full transcript text
- Verbatim quotes

### Why this works
- Content is discoverable and searchable without reproducing copyrighted material
- Users can find "Kevin Crye talked about water rates on March 1" and follow the source link
- Qualifies as indexing / bibliographic reference, not reproduction
- Same model newspapers use for aggregation indexes

---

## Signal-Desk — Role in the Ecosystem

Signal-Desk is a **new local application** replacing both Article Tracker and Facebook-Monitor.
It is not a spoke — it has no /api/health endpoint and does not register with Atlas as a spoke.
Instead, it is a **data producer** that Atlas queries directly.

### What Signal-Desk does
- Monitors and captures public pages (news sites, social media, public Facebook pages)
- Ingests captured content (articles, posts, video, audio) into a local SQLite database
- Generates LLM summaries and metadata for each captured item
- Feeds Atlas with queryable data via a read API

### Two data streams
| Stream | Content | Public exposure |
|--------|---------|----------------|
| **Articles** | News articles from tracked outlets | Summary + date + outlet only |
| **Facebook data** | Public page posts, videos | Internal research only (TBD) |

### What goes to the VPS
- Article summary cards (metadata only, same model as radio shows)
- Synced nightly alongside other spoke data
- No original article text, no paywalled content, no Facebook data

### What stays local
- Full article text and HTML
- Facebook post content and media
- The Signal-Desk app itself
- All capture logic and browser automation

---

## Public Atlas — New Build

The public-facing Atlas is **not** a stripped version of the current local Atlas.
It is a **new application** with a completely different interface designed for public users.

### What it is NOT
- No admin tools
- No spoke management UI
- No pipeline controls
- No voiceprint review
- No internal agent/codex tools

### What it IS
- Public search interface across all public spokes
- LLM-powered Q&A about Shasta County civic activity
- Person/entity lookup (public figures only — supervisors, candidates, officials)
- Cross-domain search (find a supervisor's votes + campaign donors + PRA requests + public statements)
- Embeddable widgets (vote records, meeting summaries) for potential media partners

### Architecture
- Separate FastAPI app, different codebase from local Atlas
- Reads from VPS-side SQLite replicas of each public spoke
- Uses ChromaDB replica for semantic search
- Routes LLM queries through the same home Ollama tunnel + cloud fallback
- Has its own port, its own domain, its own UI

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         HOME (Local)                            │
│                                                                 │
│  civic_media pipeline (Whisper, pyannote, SpeechBrain)         │
│  Signal-Desk (capture, ingest, summarize articles + FB)        │
│  Shasta-PRA (records browser)                                  │
│  Shasta-Campaign-Finance (NetFile sync)                        │
│  Shasta-DB (local file archive — stays here forever)           │
│  Facebook-Offline (private data — stays here forever)          │
│  Mission Control (internal codex)                              │
│  Local Atlas (admin hub, all spokes)                           │
│  Ollama (RTX 5090) ◄──── secure tunnel (Tailscale) ───────┐   │
│  ChromaDB (local, source of truth)                         │   │
└─────────────────────────────────┬───────────────────────────┘   │
                                  │ nightly sync push             │
                    ┌─────────────▼────────────────────────┐      │
                    │       Cloud Storage (~$60/mo)         │      │
                    │       Wasabi / Backblaze B2 (10TB)    │      │
                    │                                       │      │
                    │  Meeting videos (governing only)      │      │
                    │  PDF documents (agendas, minutes, PRA)│      │
                    │  Generated clips                      │      │
                    └─────────────┬────────────────────────┘      │
                                  │                               │
                    ┌─────────────▼────────────────────────┐      │
                    │               VPS                     │      │
                    │                                       │      │
                    │  Public Atlas (new app)               │      │
                    │  ├── Civic Media (meetings only)      │      │
                    │  ├── Shasta-PRA                       │      │
                    │  ├── Shasta-Campaign-Finance          │      │
                    │  ├── Show metadata (no content)       │      │
                    │  └── Article metadata (no content)    │      │
                    │                                       │      │
                    │  SQLite replicas (synced nightly)     │      │
                    │  ChromaDB replica (synced nightly)    │      │
                    │  ffmpeg (clipping only)               │      │
                    │  Nginx + SSL                          │      │
                    │                                       │      │
                    │  LLM Router:                          │      │
                    │    Primary → home Ollama ─────────────┘      │
                    │    Fallback → Claude / OpenAI API            │
                    └──────────────────────────────────────────────┘
```

---

## What Each Public Spoke Exposes

### Civic Media (meetings)
- Full meeting transcripts with speaker attribution
- Verified speaker identities (supervisors, officials, public commenters)
- Structured vote records (who voted yes/no/abstain on what)
- LLM summaries (short + long)
- Meeting documents (agendas, minutes, packets) — PDF links from cloud storage
- Video player (stream from cloud storage)
- Clip sharing

### Civic Media (shows — metadata only)
- Show name, episode title, air date, station
- LLM summary of topics covered
- People mentioned (tagged persons)
- Topics/tags
- Link to original source (no audio served from VPS)

### Shasta-PRA
- Request titles, request dates, status
- Response documents (PDFs served from cloud storage)
- Requestor (public field if available)
- Agency / department

### Shasta-Campaign-Finance
- Candidate and committee names
- Contribution records (amount, date, contributor name, employer)
- Expenditure records
- Filing periods and totals
- Cross-links to people who appear in other spokes (supervisors, officials)

### Signal-Desk Articles (metadata only)
- Headline, publish date, outlet name
- LLM summary
- Topics/tags
- People mentioned
- Link to original source (no article text served from VPS)

---

## What the VPS Does NOT Have

- Any voiceprint data or speaker embedding models
- Full article text or audio/video from copyrighted shows
- Facebook data of any kind
- The Signal-Desk application
- Shasta-DB data
- Pipeline controls, admin tools, or spoke management
- Write access to any spoke database
- The local Atlas admin interface

---

## VPS Stack

- **FastAPI** — Public Atlas app + read-only spoke APIs
- **SQLite** — Read replicas of each public spoke (synced nightly)
- **ChromaDB** — Vector search replica (synced nightly)
- **ffmpeg** — Clipping only (no transcoding, no pipeline)
- **Nginx + Caddy** — Reverse proxy, SSL, static files
- **LLM Router** — Tailscale tunnel to home Ollama + cloud fallback

### Estimated VPS specs
- 4 vCPU, 8–16GB RAM, 50GB SSD
- No GPU required — all ML stays home
- Hetzner CX32 or equivalent (~$15–20/month)

---

## Cloud Storage

**Recommended: Wasabi** (~$6.99/TB/month, no egress fees)

| Content | Size estimate | Notes |
|---------|--------------|-------|
| Governing meeting videos (540p) | ~4TB | Grows ~10GB/meeting |
| PRA documents | ~100GB | PDFs |
| Agendas / minutes / packets | ~50GB | PDFs |
| Generated clips | ~50GB | On-demand, cached |
| **Total** | **~4.2TB** | Well within 10TB budget |

---

## Sync Mechanism

### Per-meeting (immediate after pipeline completes)
- Upload 540p video to Wasabi
- Upload PDFs to Wasabi

### Nightly batch
- SQLite snapshot export for each public spoke → rsync to VPS
- ChromaDB export → rsync to VPS
- Signal-Desk article metadata export → append to VPS replica

### What never syncs
- Voiceprints table
- Processing jobs table
- Raw/original video files
- Full article text
- Any Facebook data
- Shasta-DB data

---

## LLM Inference Layer

**Mission Control already has this built.** Do not rebuild it.

Mission Control (`E:\0-Automated-Apps\Mission_Control`) has a full adaptive LLM router:
- Hardware-aware: detects GPU/VRAM, routes to fastest available local model first
- Tier system: Fast (Qwen 2.5 7B) → Reasoning (Qwen 2.5 32B) → Planner (Claude Opus)
- Cloud fallback chain: DeepSeek → Claude Haiku → GPT-4.1 Mini → Claude Opus
- Adaptive override: learns which models succeed on which task types (30-day rolling window)
- Retry escalation: 3 failures → escalate to next capability class automatically
- Full REST API: `POST /router/select` + LiteLLM execution

### How Civic Breakdown uses it

```
User query (Civic Breakdown VPS)
    │
    ▼
POST http://mission-control:8860/router/select
    { "task_type": "generic" | "reasoning" | "architecture_design" }
    │
    ▼
Mission Control routes to best available model
(local Ollama via Tailscale if reachable, cloud fallback if not)
    │
    ▼
Response streamed back to VPS → user
```

### Task type mapping for Civic Breakdown queries
| Query type | task_type | Expected model |
|------------|-----------|---------------|
| Simple lookup, keyword expansion | `generic` | Fast (Qwen 2.5 7B local) |
| Cross-domain synthesis, meeting analysis | `generic` | Reasoning (Qwen 2.5 32B local) |
| Legal/compliance, Brown Act interpretation | `architecture_design` | Planner (Claude Opus) |

### Open mode cost control
- During open mode: cap cloud API calls at **75/day** across all users
- Mission Control tracks `model_source` on every call — query this for daily cloud usage
- If cap hit: route remaining queries to local Ollama only (no cloud), graceful message if home down
- Config: `llm_open_mode_daily_cloud_cap=75` in platform_settings

**Home tunnel**: Tailscale — install on home machine and VPS, Mission Control reachable on Tailscale IP

---

## Clipping Feature

VPS-side on-demand clip generation for governing meeting segments.
Clips are always available — the generated files are temporary.

### Flow
1. User selects time range on public transcript
2. `GET /api/clip/{meeting_id}?start=120&end=180`
3. VPS checks `clips` table — does a valid (non-expired) file exist on Wasabi?
   - **Yes** → return Wasabi URL immediately
   - **No** → generate: ffmpeg fetches source from Wasabi, cuts segment (stream copy), uploads clip
4. Wasabi URL returned to user
5. Clip record written: `generated_at=now`, `expires_at=now+48h`

### Expiry / cleanup
- A nightly job scans `clips` table for rows where `expires_at < now`
- Deletes the file from Wasabi, marks record `expired`
- Source videos are permanent — clips can always be regenerated on next request
- 48h window is enough for sharing (someone posts a link, their audience watches it same day)

### Concurrency limits
- **Server-wide**: max 4 concurrent ffmpeg processes (CX32) / 6 concurrent (CX42)
- **Per-user**: max 2 concurrent clip generations — prevents one user occupying all server slots
- Users beyond the limit see queue position + ETA, not an error
- Queue implemented as Redis list; users poll `/api/clip/{clip_id}/status` for readiness

### Storage impact
- A 2-minute clip at 540p ≈ ~50MB
- 48h TTL keeps Wasabi clip storage minimal regardless of traffic
- At 35 clips/day with 48h TTL: max ~3.5GB of clips stored at any time (~$0.02/mo on Wasabi)

---

## Semantic Query Cache

Shared cache for LLM responses, served to any user whose query is semantically similar.
Turns trending story traffic from a cost problem into a feature.

### How it works
```
Query arrives
      │
      ▼
Embed query (small fast model)
      │
      ▼
ChromaDB similarity search on query_cache collection
(filters: expires_at > now, same query_type)
      │
      ├── similarity > 0.88 ──► Serve cached response
      │                          • ~50ms, instant
      │                          • Does NOT count against quota
      │                          • Shows "Answer from 3 hours ago"
      │                          • Increments hit_count
      │
      └── no match ────────────► Run LLM
                                  Store in query_cache
                                  Count against quota
                                  Serve to user
```

### Cache fields (ChromaDB query_cache collection)
| Field | Value |
|-------|-------|
| `query_text` | Original query |
| `response_text` | LLM response |
| `query_type` | standard \| deep_research |
| `created_at` | Timestamp |
| `hit_count` | Times served to other users |
| `seeded` | True if generated by seed job, not a real user |
| `expires_at` | Cleared on sync push — not time-based |

### Similarity threshold
**0.88 recommended** — catches rephrased versions of the same question without
conflating distinct ones. "How did Jones vote on water?" and "What was Supervisor
Jones's vote on the water contract?" both hit. "How did Jones vote on housing?" does not.

### Cache lifetime
Cached queries persist **until the next sync push** — not on a fixed timer.
- Sync arrives → flush entire cache → seed job runs immediately
- On-demand sync (meeting just processed) → same: flush + reseed
- Popular questions from this morning stay cached all day until new data arrives
- No stale answer risk — cache is always coherent with current dataset

### Quota impact
| Event | Quota cost |
|-------|-----------|
| Cache miss — standard | 1 standard query |
| Cache miss — deep research | 1 deep research query |
| Cache hit — any tier | **0 — free, instant** |

---

## "In The Know" — Seeded Cache + Trending Section

After every sync push, a seed job pre-populates the cache with relevant current
questions so the homepage always has content and the first real user never hits cold start.

### Seed job flow
```
Sync push completes
      │
      ▼
Flush query cache
      │
      ▼
Inspect what's new:
  recent meeting, new votes, new PRA filings,
  new campaign finance entries, recently active people
      │
      ▼
Auto-generate 5–8 seed queries based on new data
  "What happened at the [date] BOS meeting?"
  "How did supervisors vote on [most recent motion]?"
  "What PRA requests were filed this week?"
  "Who spoke the most at the last meeting?"
  "What topics came up most in recent meetings?"
      │
      ▼
Run each through LLM (standard tier — cheap)
Store in query_cache with seeded=true, hit_count=0
      │
      ▼
Homepage "In The Know" section populates from seeded entries
```

### "In The Know" homepage section
- 4–6 pre-generated Q&A cards, always current, updates after every sync
- Cards show: question + short answer preview + "Read more / Ask follow-up" CTA
- Anonymous users can read cards freely — follow-up questions require login
- Real user queries with high hit_count surface as **"People are asking..."** alongside seeded cards
- Section name: **"On The Record"**

### Why this works for trending stories
- NSB publishes a story → readers flood Civic Breakdown asking the same question
- First visitor misses cache → LLM runs, result cached
- Every subsequent visitor → instant, free, quota untouched
- hit_count rises → question surfaces prominently in "People are asking..."
- Feels like a reactive, living platform from day one — even with zero organic traffic

---

## Master Implementation Checklist

Complete step-by-step from first purchase to public launch.
Work through phases in order — each phase unblocks the next.

---

### Phase 0 — Purchases & Accounts (Do Before Any Code)

**Domains**
- [ ] Register `civicbreakdown.com` (~$12/year) — do this today
- [ ] Note: `civic.northstatebreakdown.com` redirect configured later in Phase 8

**Cloud Storage — Wasabi**
- [ ] Create Wasabi account at wasabi.com
- [ ] Create bucket: `civicbreakdown-media` (US East region, public read)
- [ ] Create bucket: `civicbreakdown-clips` (US East, public read, separate for easy TTL management)
- [ ] Generate API keys (access key + secret key) — store in `.env`, never in code
- [ ] Test upload/download with a sample file

**VPS — Hetzner**
- [ ] Create Hetzner account at hetzner.com
- [ ] Provision CX32 (4 vCPU, 8GB RAM, 80GB SSD) — Ashburn VA or Hillsboro OR
- [ ] Add SSH key during provisioning
- [ ] Note server IP address
- [ ] Set up firewall rules: allow 22 (SSH), 80 (HTTP), 443 (HTTPS), block everything else

**LLM APIs**
- [ ] Create Anthropic API account (separate from Claude Code subscription) at console.anthropic.com
- [ ] Add billing, set monthly spend cap ($50 to start)
- [ ] Generate Anthropic API key — store in `.env`
- [ ] Create DeepSeek API account at platform.deepseek.com
- [ ] Generate DeepSeek API key — store in `.env`
- [ ] Add both keys to Mission Control `config/models.json`

**Tailscale**
- [ ] Create Tailscale account at tailscale.com (free tier is sufficient)
- [ ] Install Tailscale on home machine
- [ ] Note home machine's Tailscale IP (e.g. `100.x.x.x`)

**Monitoring**
- [ ] Create UptimeRobot account (free tier) at uptimerobot.com
- [ ] Will configure monitors in Phase 8

**Wix**
- [ ] In Wix dashboard: enable Wix Members (if not already active)
- [ ] Create Pricing Plans: Community ($5), Researcher ($15), Pro ($30), Unlimited ($50)
- [ ] Enable Wix Headless / OAuth — note Client ID and public key
- [ ] Set up plan → feature mapping documentation for VPS auth layer

---

### Phase 1 — Local Preparation (Home Machine)

**Wasabi upload integration**
- [ ] Install `boto3` in civic_media venv (`pip install boto3`)
- [ ] Add Wasabi credentials to civic_media `.env`
- [ ] Write `app/services/wasabi.py` — upload, delete, presigned URL helpers
- [ ] Add post-pipeline hook: after `process_video_task` completes, upload 540p MP4 to Wasabi
- [ ] Add post-download hook: upload agenda/minutes/packet PDFs to Wasabi after download
- [ ] Test: process one meeting, confirm video + PDFs appear in Wasabi bucket
- [ ] Update `MediaFile` and `Document` models to store `wasabi_url` alongside local path
- [ ] Repeat for Shasta-PRA: upload PRA response documents to Wasabi after download

**Define public sync schemas**
- [ ] For each public spoke, document which tables and columns are safe to expose
- [ ] civic_media: meetings, transcript_segments (verified only), segment_assignments, people, votes, vote_members, reference_sections, tags
- [ ] civic_media: exclude voiceprints, processing_jobs, raw file paths
- [ ] Shasta-PRA: requests, documents, statuses — exclude any internal notes
- [ ] Campaign-Finance: all tables (fully public data)
- [ ] Write `scripts/export_public_snapshot.py` — exports each spoke to a sanitized SQLite file
- [ ] Test export: confirm no private fields leak into output

**Build sync scripts**
- [ ] Write `scripts/sync_to_vps.py` — rsync sanitized SQLite snapshots to VPS over SSH
- [ ] Write `scripts/sync_chromadb_to_vps.py` — export ChromaDB collections, rsync to VPS
- [ ] Add per-meeting on-demand sync: after pipeline completes, trigger sync for that meeting only
- [ ] Schedule nightly full sync via Windows Task Scheduler (or cron if WSL)
- [ ] Test full sync end-to-end: run script, SSH to VPS, confirm data arrived

**Seed job scaffolding**
- [ ] Write `scripts/generate_seed_queries.py` — inspects recent meetings/votes/filings, generates 5–8 questions, calls Mission Control LLM, stores results
- [ ] Hook seed job to run after every sync push (full and on-demand)
- [ ] Test: run manually, confirm 5–8 Q&A pairs generated and stored

---

### Phase 2 — VPS Base Setup

**Server configuration**
- [ ] SSH into VPS as root
- [ ] Create non-root user, add to sudo group, disable root SSH login
- [ ] Update system packages (`apt update && apt upgrade`)
- [ ] Install: Python 3.11, pip, ffmpeg, sqlite3, git, redis-server, nginx
- [ ] Install Tailscale on VPS (`curl -fsSL https://tailscale.com/install.sh | sh`)
- [ ] Connect VPS to Tailscale network (`tailscale up`)
- [ ] Confirm VPS can reach home Mission Control via Tailscale IP
- [ ] Clone Civic Breakdown repo to `/var/www/civicbreakdown`
- [ ] Create Python venv, install dependencies
- [ ] Create `.env` file on VPS with all API keys and config

**Web server & SSL**
- [ ] Install Caddy (`apt install caddy`) — handles SSL automatically via Let's Encrypt
- [ ] Configure Caddyfile: `civicbreakdown.com` → FastAPI on port 8000
- [ ] Point `civicbreakdown.com` DNS A record to VPS IP (in domain registrar)
- [ ] Wait for DNS propagation, confirm SSL certificate issued
- [ ] Test: `https://civicbreakdown.com` returns 200

**Process management**
- [ ] Install `supervisor` for process management
- [ ] Configure supervisor: FastAPI app, Redis, nightly sync cron
- [ ] Set up log rotation for FastAPI and sync logs
- [ ] Configure automatic restart on crash

**First data load**
- [ ] Run full sync from home: push SQLite snapshots + ChromaDB to VPS
- [ ] SSH to VPS, confirm all tables populated correctly
- [ ] Confirm Wasabi URLs resolve correctly for a sample meeting video
- [ ] Run seed job manually, confirm "On The Record" data generated

---

### Phase 3 — Public Spoke APIs

**civic_media read-only API**
- [ ] Build `GET /api/meetings/` — public meetings (governing only, program_type=governing_meeting)
- [ ] Build `GET /api/meetings/{id}` — meeting detail with Wasabi video URL
- [ ] Build `GET /api/meetings/{id}/transcript` — verified transcript segments with speaker names
- [ ] Build `GET /api/meetings/{id}/votes` — vote records and member breakdown
- [ ] Build `GET /api/search/meetings` — keyword + filter search across transcripts
- [ ] Build `GET /api/shows/` — metadata-only show cards (no audio URLs)
- [ ] Confirm no voiceprints, processing jobs, or internal paths in any response

**Shasta-PRA read-only API**
- [ ] Build `GET /api/pra/requests/` — list with filter by agency, date, status
- [ ] Build `GET /api/pra/requests/{id}` — detail with Wasabi document URLs
- [ ] Build `GET /api/pra/search` — keyword search across request titles and descriptions

**Campaign Finance read-only API**
- [ ] Build `GET /api/finance/contributions/` — filterable by candidate, date, amount
- [ ] Build `GET /api/finance/candidates/` — candidate and committee list
- [ ] Build `GET /api/finance/search` — cross-field search

**Signal-Desk article metadata API**
- [ ] Build `GET /api/articles/` — summary cards only (no full text)
- [ ] Build `GET /api/articles/{id}` — title, date, outlet, summary, topics, source URL

**Cross-domain person API**
- [ ] Build `GET /api/people/{id}` — public figure profile aggregating appearances across all spokes
- [ ] Build `GET /api/people/search?q=` — name search across unified people directory

**Validation**
- [ ] Review every endpoint response — confirm no private data exposed
- [ ] Test with a fresh browser session (no auth) — confirm public endpoints return data
- [ ] Load test with 20 concurrent requests — confirm FastAPI handles without errors

---

### Phase 4 — Auth & Tier Enforcement

**Wix OAuth integration**
- [ ] Write `app/auth/wix.py` — validate Wix JWT against Wix public key
- [ ] Write `app/auth/tiers.py` — map Wix member plan → internal tier enum
- [ ] Middleware: extract token from `Authorization` header on every request
- [ ] Middleware: attach `request.state.user` (tier, member_id, or anonymous)

**Open mode toggle**
- [ ] Create `platform_settings` table: `{key, value, updated_at}`
- [ ] Seed: `open_mode=true`, `open_mode_expires=NULL`, `llm_open_mode_backend=home_only`, `llm_open_mode_daily_cloud_cap=75`
- [ ] Build `POST /admin/toggle-open-mode` — password-protected endpoint
- [ ] Auth middleware reads open_mode flag — bypasses tier checks when true

**Query rate limiting**
- [ ] Create `query_usage` table: `{member_id, query_type, query_date, count}`
- [ ] Write `app/auth/limits.py` — enforce per-tier daily limits, return 429 with `X-Queries-Remaining`
- [ ] Handle 4-hour rolling window for Unlimited tier (Redis sorted set by timestamp)

**Free tier lifetime clip enforcement**
- [ ] Add `clips_used_lifetime` column to member usage tracking
- [ ] Clip endpoint checks this before generating — redirects to upgrade after 1

**Upgrade redirect flow**
- [ ] Clip button always rendered in frontend for all tiers
- [ ] On click: check tier → if not eligible, redirect to Wix upgrade page
- [ ] Show remaining query count in UI header for logged-in users

---

### Phase 5 — LLM Layer

**Mission Control integration**
- [ ] Confirm Tailscale tunnel: VPS → home Mission Control at `http://{tailscale-ip}:8860`
- [ ] Write `app/llm/router.py` — wraps `POST /router/select` + execute calls to Mission Control
- [ ] Implement health check: ping MC before each query, detect home-down state
- [ ] Standard query path: task_type=`generic`, routes to DeepSeek/local Ollama
- [ ] Deep research path: task_type=`reasoning`, routes to Claude Sonnet via MC

**Fallback handling**
- [ ] Home down + open mode: route to cloud with 75/day cap
- [ ] Home down + gated mode: route to cloud DeepSeek (cheap fallback), no Claude
- [ ] Cap tracking: query `model_source` from MC execution logs for daily cloud usage count
- [ ] If all caps hit: return graceful message, not error

**Semantic query cache**
- [ ] Add `query_cache` collection to ChromaDB on VPS
- [ ] Write `app/llm/cache.py` — embed query, similarity search, store/retrieve
- [ ] Set similarity threshold: 0.88
- [ ] Cache hits: serve instantly, do not count against quota, show timestamp
- [ ] Cache storage: `{query_text, response_text, query_type, created_at, hit_count, seeded}`
- [ ] Flush cache on every sync push (full and on-demand)

**Seed job (On The Record)**
- [ ] Run seed job after every cache flush
- [ ] Generate 5–8 questions from recent data (last synced meeting, votes, filings)
- [ ] Run through standard LLM tier (DeepSeek — cheap)
- [ ] Store with `seeded=true`
- [ ] Test: after sync, confirm 5–8 seeded entries in cache

**Abuse prevention**
- [ ] Query deduplication: hash query per user, return cached if same query within 60s (no quota burn)
- [ ] Platform-wide deep research hard cap: 300/day (Redis counter, resets midnight)
- [ ] Cost alerting: daily cron checks Claude API spend, emails alert at 80% of monthly budget
- [ ] Suspicious pattern flag: log accounts hitting exactly N queries every day across multiple IPs

---

### Phase 6 — Clipping

**Clip API**
- [ ] Create `clips` table: `{clip_id, meeting_id, start_sec, end_sec, wasabi_path, generated_at, expires_at, hit_count}`
- [ ] Build `POST /api/clip` — auth check, tier check, queue or generate
- [ ] Build `GET /api/clip/{clip_id}/status` — poll for queue position or ready URL
- [ ] ffmpeg clip generation: fetch source from Wasabi, stream copy, upload clip to `civicbreakdown-clips` bucket
- [ ] Return Wasabi URL when ready

**Queue & concurrency**
- [ ] Redis list as clip queue
- [ ] Max 4 concurrent ffmpeg processes server-wide
- [ ] Max 2 concurrent per user (3 for Unlimited tier)
- [ ] Priority queue: Pro/Unlimited jump ahead of Community/Researcher in queue
- [ ] Show queue position + ETA to waiting users

**Expiry & cleanup**
- [ ] Nightly job: scan clips table for expired entries, delete from Wasabi, mark expired in DB
- [ ] On re-request of expired clip: regenerate transparently (user sees same flow as first time)

**Frontend clip UI**
- [ ] Clip button rendered on every video segment for all tiers
- [ ] Non-eligible tier: button click → upgrade redirect
- [ ] Eligible tier: button opens time range selector → submits → polls status → shows download/share link
- [ ] Share link: permanent URL (regenerates on demand if expired)

---

### Phase 7 — Civic Breakdown Frontend (Public Atlas)

**Core layout**
- [ ] Design system: brand colors, typography consistent with North State Breakdown
- [ ] Homepage: "On The Record" section (seeded Q&A cards) + search bar + recent meetings
- [ ] Responsive: mobile-first, works on phones (people share clips on mobile)
- [ ] Header: Civic Breakdown logo, search, login/account, query counter for logged-in users

**Search & browse**
- [ ] Global keyword search across all spokes (meetings, PRA, campaign finance, articles)
- [ ] Filter panel: date range, governing body, person, topic, data source
- [ ] Results page: mixed cards (meetings, votes, PRA requests, articles) sorted by relevance

**Meeting pages**
- [ ] Meeting detail: video player (streams from Wasabi), transcript with speaker labels
- [ ] Transcript synced to video — clicking a segment jumps to that moment
- [ ] Vote records displayed below transcript
- [ ] Clip button on each segment and on video player timeline
- [ ] Export options per tier

**Person pages**
- [ ] Public figure profile: all meeting appearances, all votes, campaign donors, PRA requests
- [ ] Cross-domain timeline: "On this date Jones spoke about X, voted Y, received $Z from W"

**LLM Q&A interface**
- [ ] Ask bar on every page — contextual (on a meeting page, query defaults to that meeting)
- [ ] Standard query: instant, DeepSeek, shows remaining count
- [ ] Deep research toggle: clearly labeled, shows remaining count, upgrade prompt if at limit
- [ ] Response shows sources (which meetings, votes, filings were used)
- [ ] Follow-up question input maintains conversation context (client-side history, 3-turn window)
- [ ] Cache hit indicator: "Based on a search from 2 hours ago" (transparent, builds trust)

**"On The Record" section**
- [ ] 4–6 seeded Q&A cards on homepage
- [ ] "People are asking..." section shows high-hit_count real user queries
- [ ] Cards link to relevant meeting/person/data pages
- [ ] Refreshes after every sync — timestamp shown ("Updated 3 hours ago")

**Account & upgrade**
- [ ] Login via Wix OAuth — "Sign in with your North State Breakdown account"
- [ ] Account page: usage stats (queries used today, clips used, plan details)
- [ ] Upgrade page: tier comparison table with Wix Pricing Plan links
- [ ] Upgrade prompts are contextual — triggered at limit, not random

---

### Phase 8 — DNS, Redirects & Monitoring

**DNS setup**
- [ ] Point `civicbreakdown.com` A record to Hetzner VPS IP (if not done in Phase 2)
- [ ] In Wix DNS settings: add CNAME record `civic` → VPS IP/hostname
- [ ] Set up 301 redirect: `civic.northstatebreakdown.com` → `https://civicbreakdown.com`
- [ ] Confirm both URLs resolve correctly with valid SSL
- [ ] Add `www.civicbreakdown.com` → redirect to `civicbreakdown.com` (no www)

**Monitoring**
- [ ] UptimeRobot: monitor `https://civicbreakdown.com/api/health` every 5 minutes
- [ ] UptimeRobot: alert via email + SMS if down > 2 minutes
- [ ] UptimeRobot: monitor home Tailscale connection health endpoint
- [ ] Set up Claude API spend alert in Anthropic console (80% of monthly cap)
- [ ] Set up DeepSeek API spend alert
- [ ] Weekly email digest to self: queries served, cache hit rate, clips generated, revenue estimate

**Backups**
- [ ] Nightly backup of VPS SQLite replicas to a separate Wasabi path (`/backups/`)
- [ ] Retain 7 days of backups, auto-delete older

---

### Phase 9 — Launch

**Pre-launch checklist**
- [ ] All Phase 0–8 items complete
- [ ] Full end-to-end test: anonymous browse, free signup, standard query, deep research, clip generation
- [ ] Test open mode toggle: enable → all features accessible → disable → tier enforcement kicks in
- [ ] Test Wix plan upgrade flow: free → Community → confirm query limits change immediately
- [ ] Test home Ollama down scenario: confirm graceful fallback to cloud with cap
- [ ] Test sync: process a new meeting locally, confirm it appears on VPS within minutes
- [ ] Test seed job: confirm "On The Record" updates after sync
- [ ] Review all API responses one more time for private data leakage
- [ ] Set platform_settings: `open_mode=true`, `open_mode_expires=` (set a date 30 days out)

**Launch**
- [ ] Enable open mode
- [ ] Announce on North State Breakdown — link to `civicbreakdown.com`
- [ ] Post `civic.northstatebreakdown.com` in Wix nav or article footers
- [ ] Monitor UptimeRobot and server logs for first 24 hours
- [ ] Watch Claude API spend in Anthropic console

**Post-launch (first month)**
- [ ] Monitor query patterns — what are people actually asking?
- [ ] Review "People are asking..." — tune seed query generation to match real interest
- [ ] Adjust open mode cloud cap if spend is higher or lower than expected
- [ ] When ready: flip open_mode to false — gated tier enforcement begins
- [ ] Announce paid tiers on North State Breakdown

---

## Infrastructure Decisions (Resolved)

| Decision | Choice | Reason |
|----------|--------|--------|
| **Cloud storage** | Wasabi (~$6.99/TB/mo) | No egress fees — media serving is free regardless of traffic |
| **VPS provider** | Hetzner CX32 (~$8/mo) | Best cost/performance. 4 vCPU / 8GB RAM / 80GB SSD. Upgrade to CX42 ($17) if needed |
| **VPS location** | Ashburn VA or Hillsboro OR | US-based, low latency for US audience |
| **Auth model** | Gated (Wix Members + Pricing Plans) | See §Auth below |

---

## Auth Architecture

### Tiers

| Tier | Price | Std queries/day | Deep research/day | Clips/day | Concurrent clips |
|------|-------|----------------|------------------|-----------|-----------------|
| **Open mode** | — | Unlimited | Unlimited | Unlimited | 2 |
| **Anonymous** | $0 | 0 | 0 | 0 | 0 |
| **Free** | $0 | 3 | 0 | 1 (lifetime) | 0 after first |
| **Community** | $5/mo | Unlimited | 2 | 3 | 1 |
| **Researcher** | $15/mo | Unlimited | 8 | 10 | 1 |
| **Pro** | $30/mo | Unlimited | 20 | Unlimited | 2 |
| **Unlimited** | $50/mo | Unlimited | 15/4hr rolling | Unlimited | 3 |

**Key distinction**: Browse and keyword search are always free and require no login.
LLM smart lookup and clipping are the gated features.

### What each tier gets

**Anonymous** — no login
- Browse, keyword search, filter
- Read full transcripts, vote records, campaign finance, PRA
- Pre-generated meeting summaries
- Clip button visible — clicks prompt sign-up

**Free — $0**
- Everything anonymous gets
- 3 standard LLM queries/day
- **1 complimentary clip** (lifetime) — clip button works once, then redirects to upgrade
- Clip button remains visible on all videos after first use (always nudging upgrade)

**Community — $5/mo**
- Everything in Free
- 2 deep research queries/day
- 3 clips/day, 1 concurrent generation
- Basic export (TXT/PDF transcripts)
- Up to 5 saved searches
- Weekly email digest

**Researcher — $15/mo**
- Everything in Community
- 8 deep research queries/day
- 10 clips/day, 1 concurrent generation
- Full data export — CSV, JSON (votes, campaign finance, speaker attribution)
- Unlimited saved searches
- Alerts — notified when person speaks, topic discussed, vote recorded
- Query history
- Person cross-reference pages — full profile across every data source

**Pro — $30/mo**
- Everything in Researcher
- 20 deep research queries/day
- Unlimited clips/day, 2 concurrent generations
- Priority clip queue — ahead of Free/Community/Researcher
- API access — rate-limited programmatic access
- Bulk export — full datasets
- Custom watchlists with email or webhook alerts

**Unlimited — $50/mo**
- Everything in Pro
- 15 deep research per 4-hour rolling window (no daily hard cap)
- Unlimited clips, 3 concurrent generations
- Bumped API rate limits
- Webhook alerts (push, not just email)
- Early access to new features

### Launch strategy
- **Day one**: wide open, no auth, no limits — maximum discoverability
- **Cost control during open mode** (pick one or both):
  - Cap total cloud LLM API calls per day (e.g. 500/day across all users)
  - Route all queries to home Ollama only — zero cloud API cost, still full LLM value
  - If home Ollama is down during open mode, fall back to cloud LLM with a separate lower cap (TBD — e.g. 50–100 cloud queries/day as a fallback floor, not a full replacement)
- **Transition to gated**: flip the toggle when ready — no code change, just config
- Open mode can be re-enabled any time (press coverage, events, etc.)

### Open mode toggle
- Env var or `platform_settings` DB row: `open_mode=true`, `open_mode_expires=YYYY-MM-DD`
- `llm_open_mode_backend=home_only|cloud|both` — controls which LLM is used during open mode
- `llm_open_mode_daily_cap=500` — optional hard cap on total queries during open mode (0 = unlimited)
- Used for: launch, press events, special occasions
- Auto-expires on set date; admin can re-enable manually
- Protected admin endpoint: `POST /admin/toggle-open-mode`

### Wix integration
- **Wix Members** — sign-up/login, Wix handles identity
- **Wix Pricing Plans** — define paid tier(s), Wix handles billing
- **Wix Headless OAuth** — VPS validates member JWT against Wix public key
- VPS checks plan tier from Wix Identity API → maps to query limit

### Query rate limiting
- Counted per Wix member ID, per rolling 24-hour window
- Stored in Redis (or SQLite `query_usage` table if Redis not running)
- Returns `429` with `X-Queries-Remaining` header so UI can display usage

---

## Open Questions

1. ~~**Clip URL expiry**~~ — resolved: clips are **always available but files expire after 48 hours**.
   Generated clip files are deleted from Wasabi after 48h. If requested again, VPS regenerates
   from the source video (which is permanent). Clips are cached artifacts, not permanent storage.
   A `clips` table tracks `{clip_id, meeting_id, start_sec, end_sec, wasabi_path, generated_at, expires_at}`
   so the system knows whether to serve the existing file or regenerate.
2. **Signal-Desk Facebook exposure**: What gets summarized and when?
3. ~~**Home Ollama model**~~ — resolved: Mission Control handles all model selection. Civic Breakdown calls `POST /router/select` and never specifies a model directly.
4. ~~**Sync frequency**~~ — resolved: **both nightly and on-demand**.
   - Nightly: full batch sync of all spokes (catches anything missed, keeps replica fresh)
   - On-demand: triggered manually from admin UI, or automatically when a meeting finishes processing
   - On-demand sync is scoped — only pushes the changed meeting, not a full rebuild
5. ~~**Public platform branding**~~ — resolved: **Civic Breakdown**
   - Canonical: `civicbreakdown.com` (register now, ~$12/year) → served from VPS
   - Alias: `civic.northstatebreakdown.com` → 301 redirect to civicbreakdown.com
   - SSL: handled on VPS side (Caddy/Let's Encrypt) — Wix won't auto-cert a subdomain pointing away from their platform
   - **Action**: Register civicbreakdown.com before announcing anything
6. ~~**Free tier query limit**~~ — resolved: **3/day**
7. **Paid tier pricing**: What's the monthly price and query cap for paid subscribers?

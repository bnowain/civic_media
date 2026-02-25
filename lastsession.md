# Last Session — 2026-02-25

## What Was Built

### Meeting Votes Pipeline
Full structured vote extraction from minutes OCR text.

**New models** (`app/models.py`):
- `MeetingVote` — one row per motion (outcome, tally, mover, seconder, agenda section, governing body)
- `VoteMember` — one row per supervisor per vote (yes/no/abstain/absent); UNIQUE on (vote_id, member_name)
- `ReferenceDocument` — stores reference law/policy documents (e.g. Brown Act)
- `ReferenceSection` — one row per statutory section for RAG chunking

**New columns on `Document`** (auto-added by `validate_schema_columns` on startup):
- `minutes_parse_status` TEXT — "ok" | "partial" | "empty" | "unrecognized"
- `minutes_parse_notes` TEXT — JSON `{"unmatched_paragraphs": [...]}`

**New files**:
- `app/services/minutes_parser.py` — regex vote extractor; `PARSER_REGISTRY` dict for multi-governing-body support; `ParseResult` dataclass with `votes`, `unmatched_paragraphs`, `parser_used`, `parse_status`; currently has Shasta BOS parser only
- `app/routers/votes.py` — 6 endpoints (see below)
- `scripts/ingest_minutes_votes.py` — idempotent backfill script; `--force`, `--dry-run`, `--retry-failed`, `--show-unmatched` flags; auto-retries `partial`/`unrecognized` meetings on every run
- `scripts/ingest_brown_act.py` — PyMuPDF PDF → `reference_documents` + `reference_sections`; default PDF: `E:\0-Automated-Apps\Brown-Act-2026.pdf`

**New API endpoints**:
- `GET /api/votes/{meeting_id}` — all votes + member breakdown
- `GET /api/votes/{meeting_id}/unmatched` — raw paragraphs parser couldn't match (for review/pattern writing)
- `GET /api/votes/{meeting_id}/parse-status` — parse status of minutes docs
- `GET /api/votes` — cross-meeting search (member, vote_value, outcome, governing_body, start_date, end_date, section, limit)
- `POST /api/votes/backfill` — re-parse all partial/unrecognized meetings in background
- `GET /api/reference/brown-act/sections?q=&limit=` — keyword/section-number search

**Router registered** in `app/main.py`.

### Summary Prompts (docs/summary_prompts.md)
Major overhaul:
- Short: 150–250 words, self-identifying (governing body + month+year), covers 2–4 items, 1–2 Notable Moment lines
- Long: new Meeting Narrative section, agenda-as-framework, closed session guidance, People Present 4-column table with rotating leadership roles
- Notable Moments format: `**[TYPE | ROLE]** [HH:MM:SS] ([Agenda Item]) | [Who]`
- Brown Act Compliance Check block appended to Tag Instructions
- Two-tier tagging rule documented

### Tag Taxonomy (108 tags total, was 98)
New ACTION tags (moment-type): Adversarial, Revelation, Accusation, Testimony, Conduct, Recusal, Legal Warning, Recess — Disruption, Room Cleared, Brown Act
New TOPIC tag: Ethics
Updated in: `seed_tags.py`, `docs/tag_taxonomy.md`, `docs/summary_prompts.md`

## What Still Needs Doing

### Run these scripts
```bash
# From civic_media root — seed new tags (Ethics, Brown Act, Conduct, etc.)
python seed_tags.py

# Ingest Brown Act PDF (requires: pip install pymupdf)
python scripts/ingest_brown_act.py

# Backfill votes from existing minutes OCR text
python scripts/ingest_minutes_votes.py
```

### Still not implemented
- **Summary ingest pipeline** — parsing `---TAGS-*:` footer, creating `tag_assignments`, stripping footer before storing summary text. Currently summaries are stored with the footer still attached. Needs a dedicated ingest endpoint or post-processing step.
- **Brown Act RAG** — sections in DB but not embedded into ChromaDB yet. Needs `reference_sections` added as a source type in Atlas RAG pre-index.
- **Agenda alignment** — matching parsed agenda items to transcript segments.
- **Worker status pill on review.html** (from previous session, still pending)

## Key Design Decisions

- Parser is **BOS-specific and format-explicit** — no generic fallbacks. Unmatched paragraphs surfaced, not dropped.
- Auto-retry: backfill script automatically re-queues `partial`/`unrecognized` meetings every run.
- `POST /api/votes/backfill` is safe to call anytime — idempotent, targets failed meetings only.
- Tag footer stripped by ingest pipeline (not yet built) — stored summaries should contain prose only.

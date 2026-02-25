# TODO — civic_media

## High Priority

### Re-run backfill to pick up skipped transcodes
The 2026-02-24 backfill run used old code. Several meetings were skipped/failed:
- ~8 meetings with stuck `transcode_status = "transcoding"` not reset (stale detection fix wasn't loaded yet)
- 2 meetings (2025-10-21, 2025-08-19) have valid 540p files but DB still says "pending" (WinError 32 crash)
- **Action**: Re-run `python backfill_bos.py --limit 50` after current run finishes. New code will:
  - Detect existing valid 540p via ffprobe duration comparison, skip re-transcoding
  - Reset stale "transcoding" status when progress.json stage doesn't match
  - Retry unlink() with backoff, proceed if file still locked

### Commit backfill_bos.py to git
`backfill_bos.py` is untracked. Has important fixes (stale detection, _probe_duration, auto_process=False).
Decide if it should be tracked or stay as a local utility.

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

### Backfill meeting votes
127+ minutes OCR text files ready to parse. Script is idempotent, safe to run multiple times.
```bash
python scripts/ingest_minutes_votes.py
# After fixing any parser patterns:
python scripts/ingest_minutes_votes.py  # auto-retries partial/unrecognized meetings
```

## High Priority

### Worker status pill on review.html
The worker pill (green/red/yellow) is only on index.html. Review page also dispatches Celery tasks
(confirm speaker -> rerun voiceprints) but has no worker visibility. Add the same pill there.

### Clean up orphaned original video files
Several meetings have BOTH the original (multi-GB) and 540p files because unlink() failed with WinError 32.
New transcoder code handles this going forward, but existing orphans need manual cleanup.
Check media/ dirs for meetings that have both {name}.mp4 and {name}_540p.mp4.

### DuplicateNodenameWarning from Celery
Backfill log shows: "Received multiple replies from node name: celery@Strix-790E-2024"
Happens when ensure_worker() auto-restarts while old worker hasn't fully died.
Consider longer kill wait or unique node names.

### Update Master Schema and Codex
- Update `E:\0-Automated-Apps\MASTER_SCHEMA.md` with new endpoints (worker-health, restart-worker)
- Update `E:\0-Automated-Apps\master_codex.md` with worker health integration details

### Summary ingest pipeline
Parse `---TAGS-*:` tag footer from LLM-generated summaries → create `tag_assignments` records →
strip footer before writing to `summary_short`/`summary_long`. Not yet built.
Needs a `POST /api/meetings/{id}/summary/ingest` endpoint (or extend the existing summary upload endpoint).

### Brown Act RAG embedding
`reference_sections` rows are in the DB but not yet embedded into ChromaDB.
Needs `reference_sections` added as a source type in Atlas `POST /api/rag/pre-index`.

### Minutes parser extension — other governing bodies
Redding City Council minutes format will be different from BOS.
When adding: check `/api/votes/{id}/unmatched`, write new `_parse_*` function,
register in `PARSER_REGISTRY` in `app/services/minutes_parser.py`.

## Low Priority

### WinError 32 root cause investigation
Something holds file handles on downloaded .mp4 files. Retry+proceed is a good workaround but root cause unknown.
- Check if ffmpeg subprocess handles are properly closed
- Check if Windows Defender real-time scanning locks new large files
- Consider subprocess.Popen with explicit handle cleanup

### Add other committees to backfill
Currently only BOS (committee_id=3). Could add Planning Commission, Housing, etc.

### Scheduled re-discovery
Auto-detect new minutes/videos on a schedule instead of manual discovery runs.

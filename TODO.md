# TODO — civic_media

## Before First Test
- [ ] Install Playwright in project venv: `venv/Scripts/pip install playwright && venv/Scripts/playwright install chromium`
- [ ] Start Redis, worker, and API server (`.\start.ps1` or manually)
- [ ] Test discovery: `POST /api/primegov/discover?background=false` — should create ~384 Meeting records
- [ ] Verify meeting cards show in UI with amber "Download" badges

## Testing Checklist
- [ ] Click "Discover BOS" in UI → committee selection → discovery runs → meetings appear
- [ ] Click "Download" on one meeting → video + agenda + minutes download
- [ ] After download → card shows "needs transcode" badge + "540p" button
- [ ] Click "540p" → transcode runs → original deleted → card shows "unprocessed" + Process button
- [ ] Click Process → existing pipeline runs on 540p video
- [ ] Re-run discovery → should report 0 new, 0 updated (idempotent)
- [ ] Re-run discovery after minutes become available → should update records

## Post-Testing
- [ ] Update `E:\0-Automated-Apps\MASTER_SCHEMA.md` with new columns and endpoints
- [ ] Update `E:\0-Automated-Apps\master_codex.md` with PrimeGov integration details
- [ ] Consider auto-transcode option (skip manual step for batch downloads)
- [ ] Consider adding other committees beyond BOS (Planning Commission, etc.)
- [ ] Consider scheduled re-discovery (cron-style) to auto-detect new minutes/videos

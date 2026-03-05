# Civic Media — UI Feature Backlog

Persistent record of requested UI changes. Items stay here until **confirmed
implemented and visible in the browser**. Agent rule: never delete an entry
without the user explicitly confirming it's working.

**Format:**
- `[ ]` — requested, not yet built
- `[~]` — partially built or in progress
- `[x]` — confirmed working by user

---

## Main Page (index.html / index.js)

- [~] **Meetings tab — sort by governing body.** The sidebar already filters by governing body; the main grid should also be sortable by it (e.g. a sort toggle or dropdown in the toolbar: Date ↕ / Governing Body).
- [~] **Audio tab — sort by show name.** Toolbar sort toggle "Date ↓ / By Show" implemented. Awaiting user confirmation.
- [~] **News tab — sort by news program name.** Sort toggle "Date ↓ / By Program" added to toolbar. Awaiting user confirmation.
- [~] **Add "Web Shows" tab** — 4th tab (renamed from "Web Series"), `category=web_series`, sortable by show, sidebar shows show names. YouTube/Facebook/Rumble scope. Awaiting user confirmation.
- [~] **Audio sidebar shows show names** — sidebar now server-side filtered via `GET /api/governing-bodies/?type=show` (2026-02-27). Meetings tab uses `?type=government`. No more client-side extraction from items. Awaiting user confirmation.
- [ ] **News tab label** — user may want a different label (currently "News"). Confirm before changing.
- [~] **Backfill link in header nav** — added between Clips and + New in top bar. Awaiting user confirmation.
- [~] **Sidebar section label changes by tab** — "Governing Bodies" / "Shows" / "Shows" now updates dynamically per tab. Awaiting user confirmation.

---

## Review Page (review.html / review.js)

<!-- Add items here -->

---

## Backfill Page (backfill.html)

- [ ] **Dual-worker progress display** — When two workers are running (opposite-end strategy), the backfill page flips back and forth showing status/progress for two different tasks on a single line. Should show two separate progress bars with independent status when both workers are active (W1: oldest-first, W2: newest-first). Single bar when only one worker is running.

---

## Agent Rules

1. At session start, read this file if the task touches any UI page listed above.
2. When a user describes a UI request, add it here **before** implementing.
3. When implementation is complete, mark `[~]` and note what was done.
4. Only mark `[x]` after the user confirms it looks correct in the browser.
5. Never delete an entry — move stale ones to the "Dropped" section at the bottom
   if the user explicitly cancels them.

---

## Dropped (explicitly cancelled)

<!-- Items the user decided not to build -->

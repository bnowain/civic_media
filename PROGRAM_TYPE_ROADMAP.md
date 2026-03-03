# program_type — Rollout Roadmap

The `program_type` column now exists in `meetings` and is fully populated.
`category` is unchanged — existing code continues to work.

This document lists every change needed to make the rest of the app use
`program_type` as the canonical content classifier.

**Work order:** Do Steps 1–4 in the same session. Steps 5–7 are follow-on.

---

## Values

| `program_type`       | Meaning                                                      | Populated from `category` |
|----------------------|--------------------------------------------------------------|---------------------------|
| `governing_meeting`  | Official government body meeting (BOS, City Council, etc.)  | `meeting`                 |
| `media_broadcast`    | Radio show, podcast, AM/FM talk program                      | `audio`                   |
| `news_broadcast`     | TV or radio news segments / newscasts                        | `news`                    |
| `web_show`           | Online/streamed web show (auto-ingest or one-off)            | `web_series`              |

---

## Step 1 — `app/models.py`

Add `program_type` column to the `Meeting` ORM model so SQLAlchemy knows about it.

**File:** `app/models.py`

After the existing `category` column (line ~70), add:

```python
program_type = Column(String, nullable=True)
# 'governing_meeting' | 'media_broadcast' | 'news_broadcast' | 'web_show'
# Set by migrate_add_program_type.py; replaces category for semantic filtering.
# category column is kept for backward compatibility.
```

`validate_schema_columns()` in `database.py` will auto-detect this nullable column
on startup and not raise an error even before the model is updated — but adding it
to the ORM model is required for SQLAlchemy queries to use it cleanly.

---

## Step 2 — `app/schemas.py`

Add `program_type` to the three Meeting Pydantic models.

**File:** `app/schemas.py`

### `MeetingCreate` (line ~52)
```python
program_type: Optional[str] = None
# Inferred from category on create if not supplied — see router note below.
```

### `MeetingUpdate` (line ~79)
```python
program_type: Optional[str] = None
```

### `MeetingOut` (line ~105)
```python
program_type: Optional[str] = None
```

---

## Step 3 — `app/routers/meetings.py`

### 3a. Add `?program_type=` filter to `GET /api/meetings/`

The existing `?category=` filter stays — backwards compatible.
Add `program_type` alongside it:

```python
@router.get("/", response_model=list[schemas.MeetingOut])
def list_meetings(
    category: Optional[str] = Query(None),
    program_type: Optional[str] = Query(None),   # ADD THIS
    group_id: Optional[str] = Query(None),
    meeting_type: Optional[str] = Query(None),
    ...
):
    ...
    if program_type:
        q = q.filter(models.Meeting.program_type == program_type)
    ...
```

### 3b. Auto-derive `program_type` on create if not supplied

In `POST /api/meetings/`, after resolving `data`, add:

```python
_CATEGORY_TO_PROGRAM_TYPE = {
    "meeting":    "governing_meeting",
    "audio":      "media_broadcast",
    "news":       "news_broadcast",
    "web_series": "web_show",
}
if not data.get("program_type") and data.get("category"):
    data["program_type"] = _CATEGORY_TO_PROGRAM_TYPE.get(data["category"])
```

This means any new meeting created via the API automatically gets a `program_type`
without callers needing to change anything.

---

## Step 4 — `app/services/primegov/discovery.py`

Primegov pulls and creates governing meetings. It currently filters and creates with
`category='meeting'`. Add `program_type` to the filter and to the record created.

**File:** `app/services/primegov/discovery.py`

Line ~74 (existing meetings filter):
```python
# Before:
.filter(Meeting.category == "meeting")
# After: keep category filter for safety, also check program_type
.filter(Meeting.program_type == "governing_meeting")
```

Line ~170 (creating new meeting record):
```python
# Add alongside existing category="meeting":
program_type="governing_meeting",
```

---

## Step 5 — `app/services/ingest/` (audio ingest pipeline)

The audio ingest pipeline (`__init__.py` and `downloader.py`) uses `category='audio'`
to identify radio/podcast meetings for download and processing.

**File:** `app/services/ingest/__init__.py` — lines 73, 98, 190, 202
**File:** `app/services/ingest/downloader.py` — lines 66, 114

In each place, add `program_type='media_broadcast'` to the filter alongside the
existing `category='audio'` check (use OR, or just switch to `program_type` once
you've confirmed all audio rows have been migrated). Also set `program_type` when
creating new audio meeting records (line ~114 in downloader.py):

```python
category="audio",
program_type="media_broadcast",   # ADD
```

---

## Step 6 — `app/main.py` (startup group tagging)

Startup logic tags Groups as government vs. show based on `category`. Update to use
`program_type` instead.

**File:** `app/main.py`

Line ~49–53 (tag government groups):
```python
# Before:
.filter(_M.category == "meeting", _M.group_id.isnot(None))
# After:
.filter(_M.program_type == "governing_meeting", _M.group_id.isnot(None))
```

Line ~66 (tag show groups):
```python
# Before:
.filter(_M.category.in_(["audio", "web_series"]), _M.group_id.isnot(None))
# After:
.filter(_M.program_type.in_(["media_broadcast", "web_show"]), _M.group_id.isnot(None))
```

Line ~80 (show group membership):
```python
# Before:
_M.category.in_(["audio", "web_series"]),
# After:
_M.program_type.in_(["media_broadcast", "web_show"]),
```

---

## Step 7 — `app/routers/library.py`

The library router derives `media_type` from `category`. Update to use `program_type`.

**File:** `app/routers/library.py`, line ~33:

```python
# Before:
media_type="audio" if m.category == "audio" else "meeting",
# After:
media_type="audio" if m.program_type in ("media_broadcast", "web_show", "news_broadcast") else "meeting",
```

---

## Step 8 — `app/routers/people.py`

The people appearances endpoint passes `category=meeting.category` to the appearance
response. Update to include `program_type` as well so callers get it.

**File:** `app/routers/people.py`, line ~159:

```python
# Add alongside existing:
program_type=meeting.program_type,
```

Also add `program_type: Optional[str] = None` to the `PersonAppearance` schema if it
doesn't already have it (in `schemas.py`).

---

## What Does NOT Need to Change

| File | Reason |
|------|--------|
| `app/routers/segments.py` | Segments are attached to meetings by FK — `program_type` is on the meeting, not the segment |
| `app/routers/backfill.py` | Backfill uses `category` internally for processing logic — leave it; the filter params on the API endpoints can add `program_type` optionally when needed |
| `app/services/pipeline.py` | Uses `file_type='audio'` (MediaFile format), not meeting category/program_type |
| `app/routers/media.py` | Works with MediaFile `file_type` (video/audio format), not meeting program_type |
| Knowledge.db ingest | `mc meeting ingest` script will filter `program_type='governing_meeting'` when querying civic_media — no civic_media change needed |
| Atlas search tools | Atlas reads `program_type` from the API response — no civic_media change needed |

---

## After All Steps Complete

The canonical query for "give me governing meetings only" becomes:

```
GET /api/meetings/?program_type=governing_meeting&date_from=2026-01-01
```

No exclusion lists. No hardcoded group names. No comments saying
"# skip Kevin Crye Show". The data is self-describing.

New content types (e.g., school board meetings, planning commission meetings,
city council from a new jurisdiction) are added by:
1. Setting `program_type='governing_meeting'` when creating the meeting record
2. Everything downstream picks them up automatically

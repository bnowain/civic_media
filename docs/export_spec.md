# Transcript Export Spec

Endpoint: `GET /api/segments/{meeting_id}/export?format=srt|txt|json|pdf`

All four formats share a common set of helpers in `app/routers/segments.py`.
The PDF generator lives in `app/services/pdf_export.py` and imports those
helpers directly.

---

## Shared Helpers (`app/routers/segments.py`)

These functions are used by multiple export formats. Changes here affect
every format that uses them.

| Helper | Used by | Purpose |
|--------|---------|---------|
| `_speaker_name(seg)` | SRT, TXT, JSON, PDF | Resolve best speaker name: confirmed person &rarr; predicted person &rarr; `raw_speaker_label` &rarr; `"Unknown"`. `"Ignore"` renders as `"unnamed speaker"`. |
| `_group_by_speaker(segments)` | TXT, PDF | Group consecutive segments by same speaker into turn blocks. |
| `_is_trivial(text)` | TXT, PDF | True if segment text is just noise (punctuation, single char). Threshold: `len(stripped) < 2`. |
| `_fmt_display_time(seconds)` | TXT, PDF | Format seconds as `M:SS` or `H:MM:SS` (no leading zero on hours). |
| `_fmt_srt_time(seconds)` | SRT | Format seconds as `HH:MM:SS,mmm`. |
| `_clean_fillers(text)` | TXT, PDF | Remove standalone `uh`/`um` (case-insensitive, whole word, optional trailing comma). |
| `_capitalize_first(text)` | TXT, PDF | Capitalize first letter of a string. |
| `_capitalize_sentences(text)` | TXT, PDF | Capitalize first letter after `.?!` + whitespace. |
| `_format_phone_numbers(text)` | TXT, PDF | Reassemble spaced-out digits into `NNN-NNN-NNNN` or `NNN-NNNN` format. |
| `_split_paragraphs(text, target)` | TXT, PDF | Split long text at sentence boundary closest to `target` chars. |

### Shared Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `_PARA_TARGET_CHARS` | `500` | Ideal paragraph length for splitting |
| `_SHORT_CONTINUATION` | `60` | Segments shorter than this join without a period |
| `_INCOMPLETE_TRAIL_WORDS` | ~50 function words | If a turn ends on one of these, no trailing period is added |

---

## Format: SRT

**Formatter**: `_to_srt()` in `app/routers/segments.py`
**Content-Type**: `text/plain; charset=utf-8`
**Text processing**: None &mdash; raw segment text, no cleaning or joining.

### Structure

```
1
00:00:05,120 --> 00:00:12,340
[Kevin Crye] Good morning everyone, welcome to the meeting.

2
00:00:13,000 --> 00:00:25,100
[Mary Rickert] Thank you, I'd like to call this meeting to order.
```

### Rules

- Sequential counter starting at 1
- Timestamps in `HH:MM:SS,mmm` format (comma separator per SRT standard)
- Speaker name in square brackets, prefixed to segment text
- Blank line between entries
- Each segment is its own subtitle entry (no grouping)

---

## Format: TXT

**Formatter**: `_to_txt()` in `app/routers/segments.py`
**Content-Type**: `text/plain; charset=utf-8`
**Text processing**: Full pipeline (see below).

### Output Structure

```
================================================================================
Board of Supervisors Regular Meeting
Date: 2024-11-19
Speakers: Kevin Crye, Mary Rickert, Tim Garman
================================================================================

[Kevin Crye] 0:05
Good morning everyone, welcome to the meeting. I'd like to start by...

[Mary Rickert] 3:42
Thank you. I would like to call this meeting to order and begin with
the consent calendar.
```

### Header

- `=` rule (80 chars), title, date, speaker list, `=` rule, blank line
- Speakers listed in order of first appearance
- `"Unknown"` and `"unnamed speaker"` excluded from speaker list

### Speaker Labels

- Format: `[Speaker Name] M:SS` or `[Speaker Name] H:MM:SS`
- `"Ignore"` person renders as `[unnamed speaker]`

### Text Processing Pipeline

Applied in this order:

1. **Group by speaker** &mdash; consecutive same-speaker segments merged into turns
2. **Filter trivial** &mdash; drop noise segments (single chars, bare punctuation)
3. **Natural paragraph breaks** &mdash; >2s silence gap between segments starts a new paragraph within the turn
4. **Segment joining with punctuation logic**:
   - Previous ends with `.?!` &rarr; space + capitalize next
   - Next segment &ge;60 chars AND accumulated text &ge;30 chars &rarr; add `.` + space + capitalize
   - Otherwise &rarr; space only (continuation)
5. **Filler removal** &mdash; `uh`/`um` stripped
6. **First-letter capitalization** &mdash; each joined group capitalized
7. **Sentence capitalization** &mdash; capitalize after `.?!` + whitespace
8. **Phone number formatting** &mdash; spaced digits &rarr; `NNN-NNN-NNNN`
9. **Paragraph splitting** &mdash; groups &gt;500 chars split at nearest sentence boundary
10. **Trailing period** &mdash; added only if final word is a content word (not in `_INCOMPLETE_TRAIL_WORDS`)
11. **Word wrap** &mdash; `textwrap.fill()` at 80 chars

---

## Format: JSON

**Formatter**: `_to_json()` in `app/routers/segments.py`
**Content-Type**: `application/json; charset=utf-8`
**Text processing**: None &mdash; raw segment text, no cleaning or joining.

### Structure

```json
[
  {
    "segment_id": "abc123",
    "start": 5.12,
    "end": 12.34,
    "speaker": "Kevin Crye",
    "text": "Good morning everyone, welcome to the meeting.",
    "verified": true
  }
]
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `segment_id` | string | Unique segment identifier |
| `start` | float | Start time in seconds |
| `end` | float | End time in seconds |
| `speaker` | string | Resolved speaker name (same as `_speaker_name()`) |
| `text` | string | Raw segment text, stripped |
| `verified` | boolean | Whether the assignment is human-verified |

### Rules

- Array of objects, one per segment, ordered by start time
- `indent=2`, `ensure_ascii=False`
- No grouping by speaker
- No text processing applied

---

## Format: PDF

**Generator**: `generate_transcript_pdf()` in `app/services/pdf_export.py`
**Content-Type**: `application/pdf`
**Library**: reportlab (platypus high-level layout)
**Text processing**: Same pipeline as TXT (shared helpers imported from `segments.py`).

### Page Layout

- **Page size**: US Letter (8.5" x 11")
- **Margins**: 1" left/right/top, 0.85" bottom
- **Font family**: Helvetica (built into reportlab, no font files needed)

### First-Page Header

| Element | Style |
|---------|-------|
| Meeting title | 16pt Helvetica-Bold, black |
| Date + governing body | 10pt Helvetica, gray (#737373) |
| Speaker list | 10pt Helvetica-Oblique, gray |
| Horizontal rule | 0.5pt, light gray |

### Speaker Turns

| Element | Style |
|---------|-------|
| Speaker name | 12pt Helvetica-Bold, black |
| Timestamp (same line as name) | 10pt, gray (#737373) |
| Body paragraphs | 11pt Helvetica, 14pt leading |
| Paragraph spacing (within turn) | 6pt |
| Turn spacing | 16pt before each speaker line |

### Footer (Every Page)

| Element | Position |
|---------|----------|
| `Page N` | Centered, 8pt Helvetica, light gray |
| `Generated by Civic Media` | Right-aligned, 7pt Helvetica, light gray |

### Text Processing

Uses the same logic as TXT via `_build_turn_paragraphs()`:

1. Group by speaker, filter trivial
2. Natural paragraph breaks at >2s gaps
3. Segment joining with punctuation logic
4. Filler removal, capitalization, phone formatting
5. Paragraph splitting at ~500 chars
6. Trailing period logic

**Difference from TXT**: No word wrap (reportlab handles line breaking internally via paragraph flowables).

---

## Speaker Name Resolution (All Formats)

Applied by `_speaker_name()`, used by every format:

1. If segment has a confirmed/predicted person &rarr; use `canonical_name`
2. If that name is `"Ignore"` &rarr; render as `"unnamed speaker"`
3. Fall back to `raw_speaker_label` (e.g. `SPEAKER_00`)
4. Final fallback: `"Unknown"`

---

## Known Limitations

- **Intra-segment sentence detection**: Long Whisper segments often lack periods entirely. Breaking these at sentence boundaries would need NLP.
- **Proper noun capitalization**: `"kevin cry"`, `"shasta county"` stay lowercase. Could use vocab hints for post-processing correction.
- **Filler removal scope**: Only `uh`/`um`. Could add optional toggle for `"you know"`, `"I mean"`, `"like"`.
- **Whisper misspellings**: `"kevin cry"` vs `"Kevin Crye"`. Addressable with `config/vocab_hints.yml` post-processing.
- **Trailing word set**: `_INCOMPLETE_TRAIL_WORDS` may need expansion as edge cases surface.
- **Paragraph target**: Fixed at 500 chars. Could be configurable or adaptive.
- **PDF fonts**: Helvetica only (no Unicode beyond Latin-1). Non-Latin text would need font embedding.

# TXT Export Formatting Spec

Location: `app/routers/segments.py` — `_to_txt()` and helper functions.

This document describes the text processing pipeline applied when exporting
transcripts in TXT format. SRT and JSON exports remain raw/unprocessed.

---

## Pipeline Overview

```
Raw segments → Group by speaker → Join with punctuation logic
  → Clean fillers → Capitalize → Format numbers
  → Split paragraphs at sentence boundaries → Trailing period logic
  → Word wrap → Assemble with header
```

---

## 1. Header Block

Added at the top of every TXT export when a meeting object is available.

```
================================================================================
Meeting Title
Date: YYYY-MM-DD
Speakers: Name1, Name2, Name3
================================================================================
```

- Speakers listed in order of first appearance.
- "Unknown" and "unnamed speaker" excluded from the speaker list.

## 2. Speaker Labels

Format: `[Speaker Name] M:SS` or `[Speaker Name] H:MM:SS`

- Person with `canonical_name = "Ignore"` renders as `[unnamed speaker]`.
- Segments with no assignment fall back to `raw_speaker_label` or `"Unknown"`.

## 3. Segment Grouping

Consecutive segments assigned to the same speaker are merged into one turn.
Within a turn, segments are joined into flowing text rather than listed as
separate indented lines.

## 4. Segment Joining — Punctuation at Boundaries

Each Whisper segment is a natural sentence/phrase boundary. When joining:

| Condition | Action |
|-----------|--------|
| Previous text already ends with `.?!` | Space + capitalize next |
| Next segment is "substantial" (≥60 chars) AND previous accumulated text ≥30 chars | Add `.` + space + capitalize next |
| Next segment is short (<60 chars) or previous text is short (<30 chars) | Space only (continuation) |

**Why**: Short fragments like "yes", "and she's", "right" are continuations
or affirmations — adding periods between them creates awkward "Yes. And she's."
artifacts. Only substantial new segments get sentence-break treatment.

Constants:
- `_SHORT_CONTINUATION = 60` — threshold for "substantial" segment
- Accumulated text threshold: `30` chars (hardcoded in join loop)

## 5. Natural Paragraph Breaks (>2s gaps)

Within a speaker turn, if there is a >2 second silence gap between the end of
one segment and the start of the next, a paragraph break is inserted. These
represent real pauses (commercial breaks, thinking pauses, etc.).

## 6. Long-Text Paragraph Splitting

After joining, if a paragraph exceeds ~500 characters, it is split at the
**sentence boundary closest to the target** (above or below). This avoids
arbitrary mid-sentence breaks.

Implementation: `_split_paragraphs(text, target=500)`
- Uses regex to find all `[.?!]\s+` positions.
- Picks the break point with minimum distance to `target` chars from start.
- If no sentence boundary exists, text is kept as-is.
- Remaining text under `target + 100` chars is not split further.

## 7. Trailing Period Logic

A period is appended to the end of a speaker turn **only if** the final word
is a content word (complete thought). If the turn trails off on a function
word (conjunction, preposition, article, pronoun), no period is added — the
speaker was interrupted mid-sentence.

Word set: `_INCOMPLETE_TRAIL_WORDS` — includes conjunctions (and, but, so, or),
prepositions (to, of, in, on, at, by, for, from, with, about), articles
(a, an, the), pronouns (i, you, he, she, it, we, they), contractions
(i'm, it's, she's, that's, we're, they're), and linking verbs (is, was, are).

Examples:
- `"...the shasta county republican central committee"` → adds `.` (content word)
- `"...shasta gop.com and"` → no period (conjunction, interrupted)
- `"...it she's"` → no period (contraction, mid-sentence)

## 8. Filler Word Removal

Standalone `uh` and `um` are removed (case-insensitive, with optional trailing
comma). Matched as whole words only (`\buh\b`) to avoid affecting words like
"uh-huh".

Not removed: "you know", "like", "I mean" — too contextual, sometimes
intentional. Could be revisited as an optional toggle.

## 9. Sentence Capitalization

Applied after filler removal:
1. First letter of each speaker turn is capitalized.
2. First letter after `.`, `?`, `!` followed by whitespace is capitalized.

Limitation: Whisper often produces all-lowercase text without sentence-ending
punctuation within a single segment. Intra-segment sentence detection is not
currently attempted (would require NLP). Capitalization only fires at explicit
punctuation marks.

## 10. Phone Number Formatting

Regex reassembles spaced-out digit sequences that Whisper produces:
- `5 3 0 2 2 1 14 0 2` → `530-221-1402`
- Only applied when concatenated digits total exactly 7 or 10 (phone number lengths).

## 11. Trivial Segment Filtering

Segments with text that is just punctuation or a single character (after
stripping `.!?,;:`) are dropped entirely. Catches noise like `.` or `,`
that Whisper sometimes emits.

Threshold: `len(stripped) < 2`

## 12. Word Wrap

All paragraphs are wrapped at 80 characters using `textwrap.fill()`.

---

## Known Limitations / Future Improvements

- **Intra-segment sentence detection**: Long Whisper segments often lack
  periods entirely. Breaking these at sentence boundaries would require
  an NLP sentence tokenizer or heuristic pause detection.
- **Proper noun capitalization**: "kevin cry", "shasta county" remain lowercase.
  Could be improved with a local names dictionary or NER.
- **Filler removal aggressiveness**: Currently conservative (only uh/um).
  Could offer a "clean" toggle that also strips "you know", "I mean", "like".
- **Whisper misspellings**: "kevin cry" vs "Kevin Crye", "reading" vs "Redding".
  Could be addressed with a post-processing vocabulary correction pass
  using `config/vocab_hints.yml`.
- **Trailing incomplete words**: The `_INCOMPLETE_TRAIL_WORDS` set may need
  expansion as more edge cases are found.
- **Paragraph target length**: Currently 500 chars. May want to make this
  configurable or adapt based on total transcript length.

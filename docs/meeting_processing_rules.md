# Meeting Transcript Processing Rules

**Version:** 1.0 — 2026-03-02
**Implementation:** `Mission_Control/scripts/ingest_knowledge_meetings.py`

---

## Purpose and Audience

This document is the canonical specification for processing government meeting transcripts
in the Shasta County Civic Accountability ecosystem. It is written for LLMs and agents
that need to understand how transcripts are parsed, speaker identities inferred, agenda
items detected, summaries generated, and summaries validated for factual accuracy.

**Read this before:**
- Building or modifying any meeting summary pipeline
- Implementing speaker attribution from an audio or text transcript
- Writing prompts that generate government meeting summaries
- Understanding what the `ingest_knowledge_meetings.py` script does

**This document describes rules, not regex.** The Python implementation is authoritative
for exact pattern behavior; this document explains the intent and design so any LLM
can reason about and extend the system correctly.

---

## Related Documents

| Document | Location | Purpose |
|----------|----------|---------|
| Summary prompt templates | `civic_media/docs/summary_prompts.md` | Short/long summary prompts, NOTABLE MOMENT types, Brown Act compliance, anti-hallucination guidance |
| Tag taxonomy | `civic_media/docs/tag_taxonomy.md` | 107 canonical tags across 5 dimensions (TOPIC, AGENCY, ACTION, MONEY, PLACE) |
| Diarization codex | `civic_media/docs/diarization_codex.md` | Audio/voiceprint pipeline (speaker ID from audio waveforms, not text) |
| Script implementation | `Mission_Control/scripts/ingest_knowledge_meetings.py` | Python source — authoritative for exact behavior |

---

## 1. Transcript Format Recognition

### 1.1 Format Types

The pipeline handles three diarization levels emitted by the pyannote/SpeechBrain pipeline:

**Named** — Speakers fully identified by voiceprinting or manual confirmation:
```
[Mike Littau (Councilmember)] (17:17 - 18:27)
  This evening we'll be discussing the triathlon MOU...

[Mary Bradfield] (24:20 - 24:44)
  Thank you for the opportunity to speak on this matter.
```

**Anonymous** — Speakers labeled by pyannote cluster only, not yet voiceprinted:
```
[Speaker 1] (00:00 - 01:15)
  The meeting will come to order. Today's date is...

[Speaker 14]
  My name is Carol Jensen. I'm a resident of District 3.
```

**Mixed** — Combination of both. Typically occurs mid-meeting as voiceprint confidence
varies, or when some officials have confirmed voiceprints but public commenters do not.

### 1.2 Diarization Quality Classification

The pipeline classifies quality at ingest time:
- **named**: ≥ 80% of turns have confirmed speaker names → `diarization_quality = "named"`
- **anonymous**: ≤ 20% named → `diarization_quality = "anonymous"`
- **mixed**: 20%–80% named → `diarization_quality = "mixed"`

This classification affects speaker confidence scoring and whether LLM inference runs.

### 1.3 Header Format

Each speaker turn begins with a bracketed header line. Two formats are accepted:

**With timestamp:**
```
[Name or Label] (HH:MM - HH:MM)
```
or
```
[Name or Label] (MM:SS - MM:SS)
```

**Without timestamp:**
```
[Speaker 14]
[Mary Bradfield]
[ignore]
```

**With role parenthetical:**
```
[Mike Littau (Councilmember)] (17:17 - 18:27)
```
The role is extracted from the parenthetical inside the brackets (not the timestamp
parenthetical after the brackets).

### 1.4 Skip Labels

Certain speaker labels carry no speech content and are hard-skipped (never parsed):
- `no_speech`, `overlap`, `noise`, `music`, `silence`, `laughter`, `crosstalk`, `background`

The `ignore` label is a **soft skip** — content is included but marked `low_relevance=True`.
In government meetings, `ignore` typically means:
- Invocation / opening-prayer speakers (real speech, low civic relevance)
- Speakers not voice-matched with ambiguous content
- Very brief interjections ("Aye", "Second", unnamed audience reactions)

Summarization prompts de-prioritize low-relevance segments but do not exclude them entirely.

---

## 2. Speaker Inference Pipeline

### 2.1 Three-Pass Architecture

For anonymous or mixed transcripts, a three-pass pipeline resolves `Speaker N` labels to
human-readable names or role descriptions. The passes run in order; each pass feeds its
partial results to the next.

| Pass | Method | When it runs | Python function |
|------|--------|--------------|-----------------|
| **Pass 1** | Regex name extraction (~20 pattern groups) | Always, no LLM required | `infer_speaker_names_regex()` |
| **Pass 2** | LLM-assisted inference (small local model) | Only for speakers still unresolved after Pass 1 | `infer_speaker_names_llm()` |
| **Pass 3** | Role fallback assignment (content signal scan) | All speakers unresolved after Pass 1+2 | `assign_role_fallbacks()` |

The final output is a `speaker_map` dict: `{"Speaker 1": "Mike Littau", "Speaker 3": "the clerk", ...}`.

---

### 2.2 Pass 1 — Regex Name Extraction

**Purpose:** Extract names from the text itself using patterns that fire reliably without
needing a language model. Runs on every turn in document order. Stateful: a speaker queue
accumulates names announced by the clerk for future assignment.

Cross-reference: `ingest_knowledge_meetings.py § 2.2` (regex pass constants block)

#### Rule: Clerk Queue Announcement
**Pattern constant:** `_CLERK_QUEUE_RE`, `_BARE_QUEUE_RE`, `_QUEUE_SEP_RE`, `_INLINE_SKIP_RE`
**Direction:** Forward — names extracted here are assigned to the NEXT N anonymous speaker turns.

When the clerk announces multiple speakers at once, all queued names are extracted
into a `deque`. Each subsequent anonymous speaker turn pops one name from the front.

**Example:**
```
[Speaker 3]: First up we have Carol, followed by Christian Gardner, Beverly,
             then Dolores Lucero. Three-minute limit applies.
```
→ Queue is loaded: `["Carol", "Christian Gardner", "Beverly", "Dolores Lucero"]`

Next four anonymous speaker turns are assigned Carol, Christian Gardner, Beverly, Dolores Lucero
in order. The clerk's turn itself does NOT consume a queue slot.

**Guard:** Every new queue announcement replaces the remaining queue entirely. If the clerk
re-announces mid-public-comment ("Next up, Beverly, followed by Dolores..."), the stale
queue (which might still contain Carol) is superseded by the fresh listing.

**Inline skip signals:** Within a queue announcement, "she's not here" or "I don't see him"
immediately after a name inserts a `None` slot. That slot is skipped when assigning to turns.

**Standalone skip signals:** `_CLERK_SKIP_RE` handles a separate turn where the clerk reports
a speaker absent ("I don't see them"). This pops the front slot or finds and removes the
named person from the queue by name match.

---

#### Rule: Self-Introduction
**Pattern constant:** `_SELF_INTRO_RE`
**Direction:** Current speaker — resolves the turn where the phrase appears.

Phrases: "My name is X", "I am X", "I'm X", "This is X"

**Example:**
```
[Speaker 14]: My name is Carol Jensen. I'm a resident of District 3...
```
→ `Speaker 14` → `"Carol Jensen"`

**Guard:** Candidate must have ≥ 2 words and pass `_looks_like_name()` validation
(title-case words, no stop words, ≤ 20 chars per word).

---

#### Rule: Chair Addressing Next Speaker (Forward-Only)
**Pattern constant:** `_CHAIR_ADDRESS_RE`
**Direction:** Forward — resolves 1 of the next 3 anonymous turns.

Phrases that reliably signal the chair is giving the floor to someone:
- "Please welcome X"
- "You're recognized, X"
- "I'll call on X" / "I'll recognize X"
- "Please come up, X"
- "Go ahead, X"
- "The chair recognizes X"
- "You're up, X"

**Example:**
```
[Speaker 1]: Go ahead, Ms. Bradfield. You have three minutes.
[Speaker 9]:  Good afternoon, board members. I'm here regarding item 4...
```
→ `Speaker 9` → `"Bradfield"` (then roster normalization may expand to full name)

**Important:** "Thank you, X" is intentionally excluded from this pattern. That phrase
means X just finished speaking (backward direction), not that X is about to speak.
Mixing forward and backward in one pattern causes identity scrambling.

---

#### Rule: Thank You (Backward — Public Comment Only)
**Pattern constant:** `_THANK_YOU_RE`
**Direction:** Backward — resolves 1 of the previous 3 anonymous turns.
**Active only in:** `public_comment` and `oral_comm` blocks.

When a chair thanks a speaker by name after they finish, the named person just spoke.

**Example (public_comment block):**
```
[Speaker 9]:  I'm very concerned about the proposed development on...
[Speaker 1]: Thank you, Ms. Reyes. Next speaker.
```
→ `Speaker 9` → `"Reyes"` (then roster normalization may expand to full name)

**Guard — Role-title strip (`_ROLE_TITLE_STRIP_RE`):**
"Thank you, Supervisor Plummer" strips "Supervisor" before validation.
If the original capture had a leading role title (Supervisor, Councilmember, Mayor, etc.),
the backward mapping is SKIPPED entirely during public comment blocks. The chair is
thanking an official (who spoke in an official capacity), not a public commenter —
there is no queued anonymous turn to label.

**Guard — Block type:** During debate or closed session, "thank you, X" is a courtesy
phrase that does not reliably indicate X just finished speaking. This rule fires only
inside `public_comment` and `oral_comm` blocks. The current block type is tracked by
scanning boundary patterns on each turn as they are processed.

---

#### Rule: Role-Title Address (Bidirectional — Context-Sensitive)
**Pattern:** `role_addr` (inline regex, not a named constant)
**Direction:** Forward or backward depending on context.

Matches: `"Supervisor Jones"`, `"Councilmember Smith"`, `"Mayor Littau"`, `"Commissioner Davis"`

**Direction heuristic:** Look at the 80 characters before and after the matched name.
- If "you have", "go ahead", "please", "the floor", "your turn", "would you", "can you"
  appear → **FORWARD** (current speaker is giving the floor to the named person).
- Otherwise → **BACKWARD** (current speaker is addressing/responding to the named person
  who just spoke).

**Example (forward):**
```
[Speaker 1]: Supervisor Jones, you have the floor.
[Speaker 4]:  Thank you, Chair. I'd like to move that we...
```
→ `Speaker 4` → `"Jones"` (forward, giving floor)

**Example (backward):**
```
[Speaker 4]:  Supervisor Price, I must disagree with your characterization of...
[Speaker 5]: [unresolved]
```
→ The most recent prior unresolved turn → `"Price"` (backward, responding to)

**Guard — "Thank you" prefix:**
If "thank you" appears within 30 characters before the role-title match, the entire
role_addr pattern is skipped. "Thank you, Supervisor Plummer" is a courtesy phrase;
`_THANK_YOU_RE` handles the actual backward mapping.

---

#### Rule: Debate Respondent Address
**Pattern constant:** `_RESPONDENT_ADDRESS_RE`
**Direction:** Backward — the named person was just speaking and is being addressed/challenged.
**Position guard:** Only fires when the phrase appears in the first 60 characters of the turn.

Trigger phrases (near the start of a turn):
- "With respect, [Name]..."
- "With all due respect, [Name]..."
- "I disagree, Supervisor..."
- "That's not correct, Jones..."
- "If I may, Supervisor..."
- "No offense, but..."
- "Look, Supervisor..."
- "Come on, Jones..."
- "I understand your position, Supervisor..."

**Example:**
```
[Speaker 3]: Supervisor Kellstrom, I must respectfully disagree with the
             interpretation you've put forward...
```
→ Most recent prior unresolved turn → `"Kellstrom"` (or roster-normalized full name)

---

#### Rule: Acting Chair Detection
**Pattern constant:** `_ACTING_CHAIR_RE`
**Direction:** Updates `role_map["chair"]` for all subsequent turns.

When both the elected Chair and Vice Chair are absent, an acting chair is announced.
This overrides the `role_map["chair"]` so all subsequent `_CHAIR_ROLE_RE` assignments
go to the correct person for the rest of the meeting.

**Matches:**
- "Supervisor Harmon will serve as acting chair today."
- "I'll serve as acting chair in the absence of Chair Kelstrom and Vice Chair Crye."
- "Acting chair for today is Supervisor Long."

---

#### Rule: Presiding Officer Phrases
**Pattern constant:** `_CHAIR_ROLE_RE`
**Direction:** Current speaker — resolves to the current chair/vice-chair from `role_map`.

Phrases that only a presiding officer would say:
- "Do we have a second?" / "Seeing a lack of a second"
- "It dies" (motion dies for lack of second)
- "We will recess into closed session" / "Returning from closed session"
- "That concludes this meeting"
- "Today is a special meeting of..."
- "All those in favor" / "Any those opposed"
- "The motion carries/fails/passes/dies"
- "Is there a motion?" / "Is there a second?"
- "The ayes have it"
- "Please call the roll" / "The roll call vote"
- "The chair recognizes/calls on..."

When a turn contains one of these phrases and the speaker is still unresolved, the
speaker is assigned `role_map.get("chair") or role_map.get("vice_chair")`.

---

#### Rule: District Reference
**Pattern:** Inline regex `\bDistrict\s+(\d)\b` with roster `district_map`
**Direction:** Forward or backward based on context.

When the roster is available and a "District N" reference is found, the official
representing that district is inferred.

- "District 3, you have the floor" / "I recognize District 3" → **FORWARD**
- "As District 3 said" / "District 3's position is" → **BACKWARD**

Only applies when the `district_map` has been populated from the roster.

---

#### Rule: Clerk Detection (Post-Loop)
**Pattern constant:** `_CLERK_ROLE_SIGNALS`
**Timing:** After the per-turn loop, scanning ALL turns per speaker.

Clerk-specific signals that fire across multiple turns (not just the first):
- "Next up, Beverly" / "First up, Carol"
- "If I called your name, please..."
- "This is our last speaker"
- "Please get in queue"

The clerk's first turn often looks like generic staff speech — "Through the chair, I would
like to remind everyone..." — so a single-turn scan would misclassify them. Scanning
ALL turns catches queue-announcement signals on later turns.

**Priority:** County counsel takes priority over clerk. A speaker matching both signals
(e.g., clerk who also reports a board vote) is classified as county counsel.

---

#### Rule: County Counsel Detection (Post-Loop)
**Pattern constant:** `_COUNTY_COUNSEL_SIGNALS`
**Timing:** Post-loop, ALL turns per speaker.

Closed-session report phrasing distinctive to county counsel:
- "Item [N] was heard in closed session"
- "The board voted [four/three/etc.] to [one/two/etc.]"
- "As your county counsel"
- "We do, Mr. Chairman" (opener)

These phrases are specific enough that a single occurrence in any turn confidently
identifies the speaker's role.

---

### 2.3 Name Validation — `_looks_like_name()`

Before any regex-extracted candidate is used, it must pass name validation:

1. **All words title-case** — every word must start with an uppercase letter.
2. **No stop words** — no word (lowercased) may appear in `_NAME_STOP_WORDS` (40 words,
   see §2.3.1 below).
3. **No bare honorifics** — if the candidate is exactly one word and that word is an
   honorific ("Mr", "Mrs", "Ms", "Dr", "Prof", "Sir", "Rev", "Hon"), reject it.
4. **Max 20 characters per word** — prevents ASR artifacts from being treated as names.

#### 2.3.1 Stop Words List (`_NAME_STOP_WORDS`)

Grouped by category for readability:

| Category | Words |
|----------|-------|
| Articles/prepositions | the, a, an, to, is, it, and, or, but, not, no, so, as, if, in, on, at, for |
| Pronouns | my, your, their, we, i, you, he, she, they, his, her, its |
| Common verbs | are, was, be, been, going, saying, have, will, would, could, should, do, can |
| Demonstratives | this, that, these, those, with, from, about |
| Position/time | here, there, now, then, today, morning, afternoon, evening |
| Common false positives | yes, please, thank, good, all, see, up, next, first, last, any, some, also, one, just, sir, public, meeting, who, how, when, where, what, why, our |
| Colloquial | very, still, again, right, well, okay, ok |

---

### 2.4 External Data Sources for Name Resolution

Two database tables augment the regex pass:

**`civic_officials`** — Roster of elected and appointed officials with:
- `official_person_name` — canonical spelling
- `official_aliases` — JSON array of alternate names / ASR transcription variants
- `official_district` — district number for "District N" reference inference
- `is_chair`, `is_vice_chair` — flags for the current term

**`civic_frequent_speakers`** — Recurring public speakers with:
- `canonical_name` — confirmed spelling
- `speaker_aliases` — known ASR variants
- `appearance_count` — higher count = higher priority when aliases could conflict

**Alias normalization** examples:
- "Aaron Resner" → "Erin Resner" (ASR mishear of first name)
- "Kellstrom" → "Chris Kelstrom" (last-name-only → full canonical)
- "Christian Gardner" → "Christian Gardinier" (ASR mishear of surname)

**Normalization priority:** Officials with higher `appearance_count` are processed first
in the alias map. When two people share a last name or similar alias, the more frequent
speaker's canonical name wins.

---

### 2.5 Pass 2 — LLM-Assisted Inference

**Python function:** `infer_speaker_names_llm()`
**Model:** `qwen2.5:7b` (configurable via `--infer-speakers` and `--summarize-model`)
**Runs only when:** `--infer-speakers` flag is passed AND speakers remain unresolved after Pass 1.

#### What the LLM Receives

1. **Already-resolved speakers** — JSON dict of Pass 1 results so the LLM doesn't
   re-guess what is already known.
2. **Unresolved speakers and turn counts** — how many turns each anonymous speaker has.
   More turns = more evidence the LLM can use.
3. **Debate/crosstalk clusters** — if the transcript has rapid back-and-forth exchanges
   (sliding window: 8 turns, ≥ 2 distinct speakers, avg ≤ 40 words/turn), those sequences
   are highlighted. They contain strong positional clues: respondent addresses, position
   statements, direct rebuttals.
4. **Known officials roster** — if provided, a "Known officials" block shows expected
   attendees with roles and districts. The LLM may only use a name if it appears in the
   transcript (Rule 1 still applies).
5. **Transcript excerpt** — up to 60 turns from unresolved speakers + 20 turns from
   resolved speakers for context.

#### LLM Output Rules (injected as prompt rules)

1. **Only use names spoken in the transcript.** Do not draw on training knowledge.
2. **Assign a NAME only when clearly confirmed** — self-introduction, direct address, or
   unambiguous evidence.
3. **If role is identifiable but not name**, use an exact label from:
   `"the chair"`, `"the vice chair"`, `"a supervisor"`, `"a councilmember"`,
   `"a commissioner"`, `"the mayor"`, `"county staff"`, `"a staff member"`,
   `"the CEO"`, `"county counsel"`, `"the clerk"`, `"a member of the public"`
4. **If neither name nor role is identifiable** — OMIT that speaker entirely.
5. **Speakers with 1–2 turns:** prefer a role label. Too little evidence for a name.
6. **In arguments or crosstalk:** use `"a supervisor"` — do not guess a name.

#### LLM Response Validation

The response is parsed for a JSON object. Each value is validated:
- **Role labels** — must exactly match one of the valid roles listed above.
- **Names** — 2–4 words, first word uppercase, no digits, no all-caps words (rejects
  acronyms and ASR artifacts).
- Anything else is silently rejected. A hallucinated long sentence or a single word
  that isn't a valid role is dropped, not accepted.

---

### 2.6 Pass 3 — Role Fallback Assignment

**Python function:** `assign_role_fallbacks()`
**Runs for:** All speakers still unresolved after Pass 1 and Pass 2.

Goal: if we can't name someone, give them a useful role label that is honest about
uncertainty. "a member of the public" is more useful than "Speaker 14".

#### Pre-Pass: Clerk Detection

Before the main fallback logic, scan ALL turns of every unresolved speaker for
`_CLERK_ROLE_SIGNALS`. The clerk's first turn often looks like staff speech —
only multiple turns across the meeting reliably identify them.

Speakers matching clerk signals are promoted to `"the clerk"` at the start of fallback,
taking priority over the content-signal scan for that speaker.

#### Priority Chain

| Priority | Role Label | Source Signal |
|----------|-----------|---------------|
| 1 | "the clerk" | `_CLERK_ROLE_SIGNALS` across multiple turns (pre-pass) |
| 2 | "county counsel" | `_COUNTY_COUNSEL_SIGNALS` (closed-session report phrases) |
| 3 | "a supervisor" | `_BOARD_MEMBER_SIGNALS` (motion/second phrases) |
| 4 | "a member of the public" | `_PUBLIC_COMMENT_OPENERS` OR timeline majority (≥65% turns in public_comment/oral_comm) |
| 5 | "county staff" | `_STAFF_SIGNALS` |
| 6 | "a speaker" | Final fallback — honest uncertainty |

Content signals (items 2–5) can override timeline signals. The timeline majority
(what kind of agenda block the speaker appears in most) is used as a secondary signal.

#### Content Signal Patterns

**Board member signals (`_BOARD_MEMBER_SIGNALS`):**
- "I will move to..." / "I'd like to move that..." / "I'll move that..."
- "So moved"
- "I make a motion to..."
- "I will second that" / "I'll second"

Only board/council members make formal motions. This prevents motion-makers from
being misclassified as public commenters even if they appear during a public comment block.

**Public comment openers (`_PUBLIC_COMMENT_OPENERS`):**
- "Good morning/afternoon/evening, board/chair/supervisor/council"
- "Through the chair"
- "My name is" (+ must follow public comment pattern, not a self-intro near start of meeting)
- "I live / I work / I am a resident"
- "I'm here to speak/comment/oppose/support"
- "Thank you for the opportunity"

**Staff signals (`_STAFF_SIGNALS`):**
- "Through the chair, staff would..."
- "Staff recommends/is requesting..."
- "The department is/has/would..."
- "Pursuant to..."
- "As your county counsel/CEO/administrator..."

#### Interruption Safety Guard

If a speaker issued procedural order / chamber-clearing commands on **2 or more turns**
across the meeting (phrases like "Order!", "Clear the chamber", "I will warn the audience"),
the "a member of the public" timeline label is suppressed even if ≥65% of their turns
are in public comment blocks. A presiding officer may speak during public comment when
managing disruptions; they are not the public.

One occurrence of procedural language is insufficient to trigger this guard — a public
commenter might mention "when the chamber was cleared" while addressing the board.

#### Timeline Minimum Requirements

For the timeline majority signal to apply:
- ≥ 65% of that speaker's turns must be in a single boundary type
- Minimum 2 turns (a single turn is too little to establish a pattern)
- Only `public_comment` and `oral_comm` boundary types trigger the "a member of the public"
  timeline label. Other boundary types (debate, consent, etc.) serve as weak hints only —
  they can't distinguish a supervisor from a staff member without content signals.

---

## 3. Agenda Boundary Detection

Cross-reference: `ingest_knowledge_meetings.py § 3` (boundary detection constants)

### 3.1 Boundary Pattern Categories

The pipeline recognizes 7 boundary types from verbal announcements in the transcript.
These become the `boundary_type` field on `AgendaItem` objects:

| Type | Trigger phrases | Examples |
|------|----------------|---------|
| `transition` | "move/moving/proceed to", "next up", "next item", "next on the agenda" | "We'll move on to R3", "Next up we have public comment" |
| `consent` | "the consent calendar", "the consent agenda" | "Now our consent calendar" |
| `public_comment` | "public comment" | "Public comment period is now open" |
| `closed_session` | "closed session" | "We'll recess into closed session" |
| `adjournment` | "adjournment", "adjourned", "concludes this meeting" | "We are adjourned", "That concludes this meeting" |
| `oral_comm` | "oral communications" | "Moving to oral communications" |

### 3.2 Boundary Confirmation Requirements

To prevent false positives (a speaker mentioning "public comment" while already in public
comment, or a member discussing an item that references "consent calendar"), a boundary
announcement requires at least one of:

1. **An item number reference** — e.g., "R3", "item 5", "item 12.2b" (`_ITEM_NUM_RE`)
2. **A "strong" section keyword** — the boundary type is `consent`, `public_comment`,
   `closed_session`, `adjournment`, or `oral_comm` (these are unambiguous enough alone)
3. **Spoken by the presiding officer** — based on the turn's `is_presiding` property
   (role contains "mayor", "chair", "chairman", "chairwoman", "president", "supervisor",
   "clerk", or "moderator") OR in anonymous transcripts the speaker label matches
   `Speaker 0` or `Speaker 1` (first/second cluster is usually the chair)

**Suppression:** Boundary types `public_comment` and `closed_session` suppress re-firing
when already inside that block type. A speaker mentioning "public comment" while already
in public comment is content, not a boundary.

### 3.3 Item Number Extraction

Item numbers are extracted from boundary turn text using `_ITEM_NUM_RE`:

**Pattern:** `R` or `item [number]` followed by digits, optionally with dot-notation and letter suffix.

**Examples extracted:**
- "R3" → `"R3"`
- "item 5" → `"5"`
- "item 12.2b" → `"12.2b"`
- "item number 4" → `"4"`

### 3.4 Consent Calendar Handling

The entire consent block (public comment on consent items + vote) is one `AgendaItem`
with `is_consent=True`. Within consent blocks, "pulled" items are tracked:

**Consent pull patterns (`_CONSENT_PULL_RE`):**
- "pull item 4"
- "remove item 3a from consent"
- "I'd like to pull 5B"
- "item 7 off the consent calendar"

Pulled item numbers are recorded in `consent_pulled_items`. A pull is notable even if
the item is later re-added or deferred.

### 3.5 Short-Item Merge Rule

Items with fewer than **30 words** are merged into the preceding item. This prevents
brief procedural exchanges ("So moved. Seconded. All those in favor. Carried.") from
creating their own agenda items when they belong with the substantive discussion that
preceded them.

---

## 4. Summary Generation Rules

For full prompt templates, see `civic_media/docs/summary_prompts.md`.

### 4.1 Two-Pass Synthesis Architecture

The pipeline generates summaries in two passes to ensure quality and consistency:

**Pass A — Long summary first** (synthesis model, e.g., `qwen2.5:32b`):
- Input: item-by-item summaries (NOT raw transcript), meeting header, confirmed speaker names
- Uses the long summary prompt from `summary_prompts.md`
- Produces: structured markdown with Executive Summary, Key Decisions, Notable Moments,
  People Present, Public Comment Highlights, Brown Act compliance notes
- Tags are parsed from `---TAGS-*` lines appended to the long summary output

**Pass B — Short summary second** (synthesis model):
- Input: the long summary (not raw input again)
- Uses the short summary prompt from `summary_prompts.md`
- Compresses the long analysis into 150–250 words of flowing prose
- Tags are NOT re-extracted from the short — they come from Pass A which had more context

**Why long first:** The long analysis does the deep work (Notable Moments taxonomy, full
Public Comment breakdown, Brown Act compliance). Compressing known content in Pass B
produces better word-count compliance than re-analyzing from scratch.

### 4.2 Anti-Hallucination Injection

The `_FAITHFULNESS` text is injected immediately before the transcript in both passes
so it is the last instruction the model reads before generating content:

> IMPORTANT — FACTUAL ACCURACY: Your summary must describe ONLY the events, votes, and
> discussions documented in the item summaries below. Do NOT add information, decisions,
> vote counts, or topics that do not appear in the provided summaries. If an item was not
> detailed, acknowledge it briefly rather than inventing content. Accuracy over completeness.

Local models (qwen2.5:32b) are prone to supplementing sparse input with plausible-sounding
but invented BOS meeting content. This instruction makes faithfulness the top-priority
constraint.

### 4.3 Anonymous Transcript Disclaimer

When `diarization_quality == "anonymous"`, the meeting header includes:

> Note: Speaker labels are generic (Speaker N) — attribute by role when possible (the
> chair, the ROV, county counsel).
> Note in the summary: 'Speaker assignments not confirmed in this transcript.'

The LLM is instructed to include this disclaimer in the summary when speaker attribution
is uncertain.

### 4.4 Confirmed Speaker Names Block

For named or mixed transcripts, up to 30 confirmed speaker names (from voiceprinting)
are injected into both prompts as a `CONFIRMED SPEAKER NAMES` block:

> CONFIRMED SPEAKER NAMES (these come from voiceprint identification — use these exact
> spellings only, do not abbreviate or alter them):
>   - Mike Littau (Councilmember)
>   - Mary Bradfield

This prevents the model from altering spellings ("Kelstrom" → "Kellstrom") or using
abbreviated forms that don't match the official record.

### 4.5 Per-Item vs. Meeting-Level Summarization

**Per-item summaries** (`summarize_item()`): 2–3 sentence factual summaries of each
agenda item. Uses the smaller summarize model (default `qwen2.5:7b`). Input is
raw transcript text (up to 6,000 chars per item).

**Meeting synthesis** (`synthesize_meeting()`): Full short and long meeting summaries.
Input is item summaries — NOT raw transcript. This keeps synthesis input within any
local model's context window regardless of meeting length.

---

## 5. Tagging Rules

For the full 107-tag canonical taxonomy, see `civic_media/docs/tag_taxonomy.md`.
For the tag output format and prompt instructions, see `civic_media/docs/summary_prompts.md § Tag Instructions`.

### 5.1 Tag Output Format

Tags are appended by the LLM at the end of summary output using pipe-delimited lines:

```
---TAGS-TOPIC: Election Administration, Recall Elections
---TAGS-AGENCY: Board of Supervisors, Register of Voters
---TAGS-ACTION: Denied, Closed Session
---TAGS-MONEY: Contract, Sole Source
---TAGS-PLACE: District 3, Unincorporated
```

Omit any `---TAGS-*` line that has no applicable tags.

### 5.2 Tag Parsing (`_parse_tag_lines()`)

The ingest pipeline:
1. Parses `---TAGS-*` lines to build a `tags_dict` by dimension
2. Strips everything from the first `---TAGS-` line onward before writing the summary to DB
3. Filters non-informative values: "N/A", "None", "n/a", "not applicable", "-", "—", ""

**Robustness rules:**
- Values that look like tag keys themselves are stripped (handles models running multiple
  categories on one line)
- `### Tags` or `### Tag Instructions` headings echoed by the model are also stripped
- A trailing `TAGS: [...]` summary line the model sometimes appends is also stripped

### 5.3 When Tags Are Captured

Tags are captured from the **long summary only** (Pass A). The short summary prompt does
not include the tag instructions block, and any `---TAGS-*` lines in the short output are
parsed but discarded. This ensures tag coverage from the more complete analysis.

---

## 6. Grounding and Anti-Hallucination

Cross-reference: `ingest_knowledge_meetings.py § 6` (grounding functions)

### 6.1 Entity-Grounding Check (Fast, No LLM)

**Python function:** `grade_summary_grounding()`
**Runs always** after short summary generation.

For each entity extracted from the short summary, verify it appears in the item summaries
that were fed to the synthesis model. Source material is the item-level summaries, not
the raw transcript.

**Entity types extracted:**
- **Proper names:** Two or more consecutive Title-Case words (e.g., "Kathy Darling-Allen",
  "Board of Supervisors")
- **Vote tallies:** "3-2", "4-1", "5-0", "3 to 2", "unanimously"
- **Dollar amounts:** "$1.5M", "$53,000", "$1.2 billion"
- **Agenda item references:** "R3", "C-12", "Item 5"

**Score formula:**
```
grounding_score = 1.0 - (ungrounded_entity_count / total_entity_count)
```

A `grounding_score` of 1.0 means every extracted entity appears verbatim in the source
material (case-insensitive comparison against the concatenated item summaries).

### 6.2 LLM Grounding Check (Triggered at Score < 0.70)

**Python function:** `llm_grounding_check()`
**Model:** Configurable (default: `qwen2.5:7b`, the smaller fast model)
**Triggered when:** Entity grounding score < 0.70

The LLM reviews the short summary against the item summaries and identifies sentences
or claims that state specific facts not supported by the source material.

**LLM fact-checker prompt structure:**
1. Source material: up to 12 item summaries, each truncated to 600 chars, labeled `[Item N]`
2. Generated summary: the short summary to be checked (up to 2,000 chars)
3. Task: identify sentences/claims where a specific fact (name, vote tally, dollar
   amount, decision, event) does not appear in the source

**Response format:** Bullet list of unsupported claims. If all claims are grounded,
the LLM responds with exactly `(all claims grounded)`.

### 6.3 Grounding Warnings in Output

Grounding warnings (from both entity check and LLM check) are stored in the output
file and logged at ingest time. They are NOT written to the DB summary text — the
summary is written as-is, with warnings surfaced to the operator for review.

A summary with grounding warnings is still written to the DB. The grounding score is
available in the output file and can inform downstream quality filtering.

---

## 7. Delta Analysis (Minutes vs. Transcript)

**Python function:** `delta_analysis()`
**Input:** Long summary (transcript record) + official minutes text

The transcript is treated as **ground truth** — the authoritative account of what was
actually said and done. The official minutes are the governing body's formal record —
often sanitized, compressed, or selectively presented to avoid controversy.

**Purpose:** Surface what the official record leaves out.

### 7.1 Five-Section Analysis Structure

The delta analysis LLM prompt produces a five-section accountability document:

**1. Context Lost from Official Record** — Substantive discussions, arguments, public
comments, or notable moments documented in the transcript that the minutes omit, compress,
or sanitize. Be specific: quote or paraphrase what was said, name who said it, reference
item numbers.

**2. Public Comment Coverage** — Did the minutes accurately represent the volume and
content of public comment? Or did they bury specific testimony under generic language
like "public comment received"?

**3. Dissent and Minority Positions** — For split votes, did the minutes record the
reasoning of dissenting supervisors? Was the debate captured, or only the outcome?

**4. Potential Errors in the Minutes** — Factual discrepancies: wrong vote tallies,
wrong mover/seconder, wrong item descriptions, or events characterized differently
than the transcript record shows.

**5. Transparency Rating** — HIGH / MEDIUM / LOW:
- **HIGH** = Minutes faithfully reflect what happened including debate and dissent
- **MEDIUM** = Key decisions recorded but discussion/context omitted
- **LOW** = Only outcomes recorded; significant events hidden or sanitized

### 7.2 Delta Analysis Model Choice

Delta analysis uses the `--synthesis-model` (default `qwen2.5:32b`). This is the most
demanding analysis task: comparing two documents, finding discrepancies, and generating
a structured accountability report. The larger model is preferred for reasoning quality.

---

## 8. Meeting Package Structure

### 8.1 Package ID Format

All documents from the same meeting share a `meeting_package_id`:
```
{meeting_type}_{jurisdiction_slug}_{date}
```

**Examples:**
- `city_council_redding_ca_2026-02-17`
- `board_of_supervisors_shasta_ca_2026-01-14`

The jurisdiction slug lowercases the jurisdiction string, replaces ", " with "_", and
replaces remaining spaces with "-".

### 8.2 Document Types in `knowledge.db`

All documents are stored in `knowledge.db` under `section='civic'` with these subsections:

| Subsection | Description |
|-----------|-------------|
| `transcript_chunk` | Raw agenda item turns, one row per detected item |
| `agenda_item_summary` | LLM 2–3 sentence summary of each agenda item |
| `action_items` | Motions, votes, and assignments extracted from each item |
| `meeting_summary` | Short (150–250 word) prose meeting narrative |
| `meeting_summary_long` | Full structured meeting analysis in markdown |
| `agenda_basic` | Basic agenda items list (from PDF-converted basic agenda) |
| `agenda_full` | Full agenda item with staff report text |
| `minutes` | Official meeting minutes (full text, one row per meeting or per item) |
| `delta_analysis` | LLM comparison: transcript reality vs. official minutes |

### 8.3 Pre-Processing Required for PDFs

PDF agendas and minutes must be converted to plain text before ingestion:
```bash
pdftotext -layout agenda.pdf agenda.txt
# or:
python -c "import pdfplumber; p=pdfplumber.open('f.pdf'); \
    print('\n'.join(pg.extract_text() for pg in p.pages))" > out.txt
```

---

## 9. Quick Reference Checklist

Use this checklist when processing a government meeting transcript end-to-end.

### Step 1: Parse Transcript
- [ ] Detect diarization quality (named/anonymous/mixed)
- [ ] Parse all speaker turns from `[Header] (timestamp)` format
- [ ] Hard-skip noise labels; soft-include `ignore` as `low_relevance=True`
- [ ] Confirm timestamps extracted where present

### Step 2: Infer Speakers (if anonymous/mixed)
- [ ] **Pass 1 (Regex):** Run `infer_speaker_names_regex()` with roster and frequent speakers
  - [ ] Load `civic_officials` and `civic_frequent_speakers` from DB
  - [ ] Build alias_map, district_map, role_map from roster
  - [ ] Run per-turn loop: queue → self-intro → chair-address → thank-you (pc only) → role-title → respondent → district → acting-chair → chair-role
  - [ ] Post-loop: clerk and county counsel detection across ALL turns
- [ ] **Pass 2 (LLM):** Run `infer_speaker_names_llm()` for remaining unresolved speakers
  - [ ] Only when `--infer-speakers` flag is set
  - [ ] Only if Ollama is available
  - [ ] Validate LLM response: reject non-role strings that don't match 2–4 word title-case name format
- [ ] **Pass 3 (Fallback):** Run `assign_role_fallbacks()` for all still-unresolved speakers
  - [ ] Pre-pass: clerk detection across all turns
  - [ ] Priority: clerk > county counsel > board member > public comment > staff > "a speaker"

### Step 3: Detect Agenda Items
- [ ] Run `detect_agenda_items()` over all turns
- [ ] Confirm boundary requires: item number OR strong section type OR presiding officer
- [ ] Check for consent pulls within consent blocks
- [ ] Merge items with < 30 words into preceding item

### Step 4: Summarize Each Item
- [ ] Run `summarize_item()` per agenda item using fast model (`qwen2.5:7b`)
- [ ] Input: raw transcript text (up to 6,000 chars)
- [ ] Output: 2–3 sentence factual summary

### Step 5: Extract Action Items
- [ ] Check for motion/vote keywords before calling LLM (skip if none present)
- [ ] Run `extract_action_items()` for items with votes/motions
- [ ] Parse bullet list from LLM response

### Step 6: Synthesize Meeting Summary
- [ ] Run `synthesize_meeting()` using synthesis model (`qwen2.5:32b`)
- [ ] **Long first**: use long prompt from `summary_prompts.md`, inject `_FAITHFULNESS`
- [ ] Parse `---TAGS-*` lines from long output for all 5 tag dimensions
- [ ] **Short second**: compress long analysis using short prompt from `summary_prompts.md`
- [ ] Input: item summaries, NOT raw transcript

### Step 7: Grounding Check
- [ ] Run `grade_summary_grounding()` — extract entities, check against source
- [ ] If score < 0.70: run `llm_grounding_check()` with fast model
- [ ] Log all warnings; write to output file

### Step 8: Tagging
- [ ] Verify tags extracted from long summary (5 dimensions: topic, agency, action, money, place)
- [ ] Strip `---TAGS-*` footer from summary text before DB write
- [ ] Filter non-informative values (N/A, None, etc.)
- [ ] See `tag_taxonomy.md` for canonical tag values

### Step 9: Delta Analysis (if minutes available)
- [ ] Run `delta_analysis()` comparing long summary against official minutes text
- [ ] Use synthesis model for reasoning quality
- [ ] Store as `subsection=delta_analysis` in knowledge.db

### Step 10: Write to Database
- [ ] All documents share `meeting_package_id` in `knowledge_meta`
- [ ] Transcript chunks: one row per agenda item (`subsection=transcript_chunk`)
- [ ] Summaries, actions, delta: separate rows with appropriate subsections
- [ ] `page_url` populated if document has a web-accessible source URL (Rule 3)

---

## Appendix A: Constants Quick Reference

| Constant | Purpose | Section |
|----------|---------|---------|
| `_HARD_SKIP_LABELS` | Acoustic noise labels — never include | §1.4 |
| `_LOW_RELEVANCE_LABELS` | Soft skip: include as low_relevance | §1.4 |
| `_ANON_RE` | Detects "Speaker N" / "SPEAKER_08" labels | §1.1 |
| `_HEADER_TS_RE` | Parses `[Name] (HH:MM - HH:MM)` | §1.3 |
| `_HEADER_ONLY_RE` | Parses `[Name]` without timestamp | §1.3 |
| `_BOUNDARY_PATTERNS` | 7 boundary type regex list | §3.1 |
| `_ITEM_NUM_RE` | Extracts "R3", "item 5", "12.2b" from text | §3.3 |
| `_CONSENT_PULL_RE` | Detects "pull item N" in consent blocks | §3.4 |
| `_PRESIDING_ROLES` | Role words that identify presiding officers | §3.2 |
| `_SELF_INTRO_RE` | "My name is X", "I am X", "I'm X" | §2.2 |
| `_CHAIR_ADDRESS_RE` | "Go ahead, X", "You're recognized, X" (forward) | §2.2 |
| `_THANK_YOU_RE` | "Thank you, X" (backward, pc only) | §2.2 |
| `_ROLE_TITLE_STRIP_RE` | Strips "Supervisor/Councilmember/Mayor" prefix | §2.2 |
| `_CLERK_QUEUE_RE` | "First up we have X" / "Next up is X" | §2.2 |
| `_BARE_QUEUE_RE` | "X, followed by Y" (no "first up" prefix) | §2.2 |
| `_QUEUE_SEP_RE` | ", followed by", ", then", etc. | §2.2 |
| `_INLINE_SKIP_RE` | "she's not here" within queue text | §2.2 |
| `_CLERK_SKIP_RE` | "I don't see Carol" (standalone skip turn) | §2.2 |
| `_RESPONDENT_ADDRESS_RE` | "With respect, Jones, I disagree" (backward) | §2.2 |
| `_ACTING_CHAIR_RE` | "Supervisor Harmon will serve as acting chair" | §2.2 |
| `_CHAIR_ROLE_RE` | "All those in favor", "Do we have a second" | §2.2 |
| `_NAME_STOP_WORDS` | 40 words that disqualify name candidates | §2.3 |
| `_NAME_HONORIFICS` | Bare honorifics rejected as single-word names | §2.3 |
| `_PROCEDURAL_ORDER_RE` | "Order!", "Clear the chamber" | §2.6 |
| `_PUBLIC_COMMENT_OPENERS` | Public commenter opener phrases | §2.6 |
| `_STAFF_SIGNALS` | Staff speech patterns | §2.6 |
| `_COUNTY_COUNSEL_SIGNALS` | Closed-session report phrases | §2.2, §2.6 |
| `_CLERK_ROLE_SIGNALS` | "Next up,", "If I called your name" | §2.2, §2.6 |
| `_BOARD_MEMBER_SIGNALS` | "I move to", "I'll second" | §2.6 |

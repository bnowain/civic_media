# Summary Prompt Templates

Reference templates for generating summaries with an external LLM (Claude, ChatGPT, etc.).
Copy the relevant prompt, paste it along with the transcript/document text, and upload
the result back via the review page or API.

**For tagging:** After either prompt, append the **Tag Instructions** block at the bottom of this file.

---

## Short Summary Prompt

```
You are summarizing a civic government meeting for a local accountability journalism database.
Your audience is researchers, journalists, and engaged citizens who want a fast read on what
happened and whether it matters.

If an agenda is provided alongside the transcript, use it to understand what the meeting was
officially convened to address — then summarize what actually happened.

Given the following transcript (and agenda if provided), produce a SHORT SUMMARY following
these rules:

FORMAT:
- Flowing prose only — no markdown, no bullet points, no headers
- Do NOT open with "The [Body] held a [type] meeting on [date]" — start with what happened
  or what made this meeting notable. BUT work the governing body name and the month and year
  (e.g., "February 2026") into the text naturally — this summary must be self-identifying
  when retrieved without other context.
- If nothing controversial or significant happened, a straightforward summary is fine.
  But if there were contentious votes, adversarial exchanges, accusations, or significant
  public comment, lead with that.
- Name names. "Supervisor Crye questioned..." is more useful than "A supervisor questioned..."
- Include vote outcomes and dissenters for any significant vote.
  Format: "passed 3-2 (Jones, Crye dissenting)" or "failed 2-3 (Garmon, Drummond dissenting)"
- Cover 2–4 significant agenda items, not just the single top headline. A researcher reading
  only this text — with no access to the long summary — should understand the meeting's main
  business, the key players, and whether anything newsworthy occurred.
- End with one or two NOTABLE MOMENT lines for the most significant confrontation,
  accusation, revelation, conduct moment, or disruption. Use two only if both are
  genuinely distinct and significant — don't manufacture a second one to fill the space.
  Format: "Notable: [Speaker] [what they said/did, in one sentence]."

LENGTH: 150–250 words. Dense but readable. This text serves as a RAG retrieval unit — it
must stand alone as a complete, self-sufficient account of the meeting.

EXAMPLES OF GOOD SHORT SUMMARIES:
  "The Shasta County Board of Supervisors approved a $1.5M election infrastructure grant from
  the Center for Tech and Civic Life 3-2 (Jones, Crye dissenting) over Republican Central
  Committee objections that the funds were 'Zuck Bucks' tied to Zuckerberg. ROV Kathy
  Darling-Allen proposed using the grant to purchase an election department building rather
  than equipment, arguing it would reduce political optics. The board also approved routine
  road maintenance contracts and a proclamation for ACES Awareness Month, both unanimously.
  The grant controversy consumed nearly two hours of a three-hour session — unusual for an
  item initially framed as a straightforward funding acceptance. Notable: Supervisor Crye
  pressed County Counsel to confirm that CTCL could demand repayment at its sole discretion,
  calling the organization 'judge, jury, and executioner' over county funds."

  "The Redding City Council approved the Ironman Triathlon MOU ($53K annual commitment) but
  split sharply on funding source — Councilmember Audette pushed to offset the cost against
  the Visit Redding contract rather than draw from reserves, and the vote passed only with
  direction to staff to return with alternative options. The council also approved three
  consent items covering utility maintenance and a personnel policy update, all unanimously,
  and heard a downtown streetscape presentation with no action taken. The meeting ran
  approximately 90 minutes and was otherwise routine except for the triathlon funding dispute.
  Notable: Audette raised the city's unresolved $3.2M structural deficit mid-debate,
  prompting Mayor Littau to cut off discussion."
```

---

## Long Summary Prompt

```
You are summarizing a civic government meeting for a local accountability journalism
database. Your audience is researchers and journalists doing long-term tracking of
local officials, votes, spending, and power dynamics.

Given the following transcript (and agenda if provided), produce a LONG SUMMARY
in structured markdown following the format below.

Guiding principles:
- Name names. If a speaker is identified, use their name — not "a supervisor" or "a council member."
- Call out controversy clearly. If there was an adversarial exchange, name who was involved
  and what the substance of the dispute was.
- Record vote outcomes with dissenters: "passed 3-2 (Jones, Crye dissenting)"
- Public comment is often where the most important accountability moments happen.
  Include any public speaker who made a factual accusation, revealed new information,
  challenged board conduct, or generated a board response.
- If speaker assignments are unclear (transcript uses [Speaker N] labels without names),
  attribute by role when possible: "the chair," "the ROV," "the county counsel."
- If timestamps are in the transcript (format: HH:MM or MM:SS), include them in
  Notable Moments. If not, use the agenda item reference or approximate position.
- Use the agenda as the meeting's structural backbone. Understand what the body was
  officially convened to address, then compare that to what actually happened. Items
  pulled from consent, items deferred from a prior meeting, or routine items that
  consumed outsized time are often where the real story is. Name these gaps explicitly.
- Closed session: California's Brown Act permits public comment on closed session items
  before the board retires — capture any public speakers who commented on closed session
  items as you would any other public comment. Note the announced topic(s) going in.
  Note the report-out when the board returned: Brown Act requires public reporting of
  certain outcomes (litigation authorizations, settlement approvals, personnel actions).
  If the board reported no reportable action, note that — silence is a data point.
- Tell the story, not just the record. The structured sections below are your evidence.
  The Meeting Narrative is where you synthesize them into a coherent account of what
  this meeting meant and how it unfolded.

---

### Metadata
- **Type**: [Board of Supervisors Regular Meeting / City Council / Planning Commission / Radio Show / etc.]
- **Body**: [Governing body name]
- **Date**: [YYYY-MM-DD]
- **Duration**: [approximate, if determinable from timestamps]
- **Location**: [if stated]
- **Quorum / Members Present**: [names and titles if stated]

---

### Executive Summary
2–3 sentences. Lead with the most significant thing that happened, not with procedural
boilerplate. Name names. Include vote outcomes for major items.

---

### Meeting Narrative
4–8 sentences of flowing, interpretive prose. Synthesize the arc of the meeting: What was
this body grappling with? What was the dominant theme or tension running through the session?
How did the meeting unfold — was there a turning point, a moment where the discussion became
heated or took an unexpected direction? How did it resolve, or what remains unresolved?

If the agenda and the actual proceedings diverged — an item consumed far more time than its
description suggested, a consent item was pulled, off-agenda topics arose, or public comment
shifted the direction of a vote — describe that gap. This section should give a researcher
who hasn't read the transcript a clear mental model of what kind of meeting this was and
what it means in the context of this body's ongoing work.

---

### Agenda Overview
A quick-scan table of all items with outcomes. Use N/A for items with no decision.

| Item | Topic | Outcome |
|------|-------|---------|
| R1 | ACES Awareness Month Proclamation | Approved 5-0 |
| R3 | CTCL Election Grant $1.5M | Approved 3-2 (Jones, Crye dissenting) |
| ... | ... | ... |

---

### Key Decisions
For each significant vote or decision, one focused paragraph:
- What was decided (include dollar amounts, contract terms, policy changes)
- Who voted which way — by name
- Key arguments made for and against
- Any conditions, amendments, or staff direction attached

Do not include routine consent items unless they were contested.

---

### Notable Moments
This section is critical for accountability research. Identify moments in the meeting
that represent: adversarial exchanges between officials, accusations or revelations in
public comment, significant procedural disputes, moments of board conduct that
journalists would find newsworthy, or anything that departs from routine procedure.

TIMESTAMP NOTE: Our system always stores start_time/end_time for every transcript
segment. Summaries generated from our processed transcripts should always include a
timestamp. Summaries generated from an external transcript without embedded timestamps
should fall back to the agenda item reference. In ALL cases, include the agenda item
reference — it provides context that a timestamp alone cannot.

Format each moment as:
**[TYPE | ROLE]** [HH:MM:SS] ([Agenda Item]) | [Who]
Brief description — what was said, what the stakes were, how the other party responded.

If timestamp is not available: **[TYPE | ROLE]** ([Agenda Item]) | [Who] | ...

MOMENT TYPES:
- ADVERSARIAL — direct conflict between two or more officials
- CONTROVERSIAL — a vote, statement, or action likely to generate public criticism
- REVELATION — new information disclosed for the first time (cost, timeline, relationship, etc.)
- ACCUSATION — a direct accusation against a named official or entity by name
- PROCEDURAL — dispute about process, Brown Act compliance, public comment rules, etc.
- TESTIMONY — public speaker or expert makes a significant factual statement affecting a decision
- CONDUCT — derogatory, demeaning, or improper remark or behavior by an official; hot mic
  moments; anything that reflects on the character or conduct of an elected or appointed
  official regardless of whether it was part of a formal agenda item
- RECUSAL — official declares a conflict of interest and steps away from a vote; note
  who recused, on what item, and the resulting vote math (a recusal can shift a majority)
- LEGAL WARNING — county counsel or legal advisor formally warns the board that a proposed
  action is legally risky (Brown Act, open meeting law, liability, etc.)
- RECESS — DISRUPTION — presiding officer suspends the meeting due to audience disorder
- ROOM CLEARED — presiding officer orders the gallery/audience removed; meeting may
  continue without the public present
- WALKOUT / DISRUPTION — voluntary walkout by officials or organized audience disruption
  not resulting in a formal recess or clearing

SPEAKER ROLES (use the most specific role that applies):
- PUBLIC — member of the public making comment
- BOARD — elected official speaking in board/council deliberation
- STAFF — department head, county counsel, or staff presenter
- EXPERT — technical expert, consultant, or invited presenter
- CHAIR — the presiding officer exercising procedural control (chair/mayor/president)

Examples:
**[ADVERSARIAL | BOARD]** 1:23:14 (R3 — CTCL Election Grant) | Supervisor Crye vs. County Counsel Ross
Crye pressed Ross on whether CTCL could unilaterally demand repayment at its "sole
judgment," calling the arrangement "strings attached" and characterizing CTCL as
"judge, jury, and executioner" over county funds. Ross acknowledged the language was
broad but maintained the agreement was lawful. Crye voted no.

**[REVELATION | PUBLIC]** 0:09:42 (R1 — ACES Proclamation) | Suzanne Barrymore, public commenter
Barrymore disclosed the county had gone 299 days without a permanent health officer,
linking the vacancy directly to the board's governance instability. No supervisor
responded directly to the claim.

**[ACCUSATION | PUBLIC]** 0:36:17 (Public Comment) | Delores Lucero (Shasta County Watchdog)
Lucero accused Council Member Danuka of a conflict of interest in his effort to attract
a medical school while serving on multiple health-related boards, and stated she would
pursue action against his medical license. Mayor Littau ended her comment without
board response.

---

### Public Comment Highlights
Named public speakers who made substantive arguments, accusations, or testimony.
Skip routine supportive or ceremonial comments. Include:
- Speaker name (and affiliation if stated)
- What they said in 1–2 sentences
- Board response (if any)

---

### People Present / Named in Record
List every named individual who spoke, voted, or was referenced by name.
For elected officials, include their seat/district and governing body. Note leadership
positions (Chair, Vice Chair, Mayor, Mayor Pro Tem) — these rotate annually and are
meeting-specific. Format: "Supervisor, District 5; Chair (2025)" so the record reflects
who held the role at the time of this meeting.

Also note: members absent from a significant vote (their absence may affect the outcome),
and any member who recused themselves (note the item and the resulting vote math).

| Name | Role / Title | Governing Body | Notes |
|------|-------------|----------------|-------|

---

### Staff & Counsel Appearances
List any department heads, county counsel, or subject-matter staff who presented
or answered questions, and what they addressed.

---

[Append Tag Instructions block here]
```

---

## Notes on Transcript Formats

### Standard format (from our civic_media pipeline)
Our system attaches `start_time` and `end_time` (in seconds) to every segment.
When a transcript is exported from our DB, each segment has a timestamp.
Use exact timestamps for Notable Moments. Always include the agenda item too.

### Named speakers with inline timestamps (e.g., external export)
```
[Mike Littau (Councilmember)] (28:03 - 29:13)
Thank you so much...
```
Use name directly. Use the inline timestamp for Notable Moments + agenda item reference.

### Generic speaker labels, no timestamps (older external format)
```
[Speaker 1]
All right. Good morning, everyone...
```
Attribute by role when possible using context: "the chair," "the ROV," "county counsel."
For Notable Moments, omit the timestamp and reference the agenda item only.
Note in the summary: "Speaker assignments not confirmed in this transcript."

### Transcripts without an agenda provided
Infer items from procedural language: "moving on to R3," "the next item,"
"public comment period," etc. Use those as the agenda item reference in Notable Moments.

---

## Tag Instructions
*(Append this block to the bottom of either prompt above)*

```
---

## Tag Instructions

Apply tags from the approved Shasta County taxonomy below. Output tags at the
very end of your response using this exact format:

---TAGS-TOPIC: [comma-separated topic tags, or omit line if none apply]
---TAGS-AGENCY: [comma-separated agency tags, or omit line if none apply]
---TAGS-ACTION: [comma-separated action tags, or omit line if none apply]
---TAGS-MONEY: [comma-separated money/admin tags, or omit line if none apply]
---TAGS-PLACE: [comma-separated place tags, or omit line if none apply]

The ingest pipeline will parse these lines, create tag_assignment records in the
database, then strip everything from the first ---TAGS- line onward before storing
the summary text. The summary field in the database will contain only the prose —
no tag footer.

Tagging rules:
- TOPIC / AGENCY / MONEY / PLACE: Apply conservatively — only when clearly and directly
  evidenced as a primary subject. Do NOT tag incidental mentions or background references.
- ACTION outcomes (Approved, Denied, Tabled, etc.): tag confirmed decisions only.
- ACTION moment types (Adversarial, Revelation, Accusation, Testimony, Conduct): tag if
  the moment occurred — brevity is NOT a reason to skip. A one-sentence hot mic remark or
  a derogatory comment that lasted five seconds still gets tagged. If it was notable enough
  to appear in Notable Moments, it gets the corresponding ACTION tag.
- AGENCY: tag the department/body that OWNS or PRESENTS the item; not every agency mentioned
- MONEY: use the most specific type (RFP / Sole Source / Change Order) before falling back to Contract
- PLACE: City of Redding = the city government as actor; Redding = geographic area
- Conduct tag: use for hot mic moments, derogatory or demeaning remarks by officials,
  improper behavior, or any off-agenda conduct by an elected or appointed official that
  would be newsworthy regardless of whether it was part of a formal agenda item.

### TOPIC — What it's about
Healthcare, Mental Health, Substance Use, Homelessness, Housing, Law Enforcement,
In-Custody Deaths, Jail / Detention, Fire / Emergency Services, Wildfire Recovery,
Child Welfare, Veterans Services, Education, School Board Politics, Elections,
Election Administration, Hand-Count / Election Integrity, Charter County,
Recall Elections, Political Retaliation, Outside Donor Influence,
Secession / State of Jefferson, Church-State / Religious Political Influence,
Tribal Affairs, Environment, Water, Economic Development,
Transportation / Infrastructure, Immigration / Sanctuary,
Civil Rights / Civil Liberties, Transparency / Accountability, Whistleblower,
Hospital / Healthcare Workers, Utility Rates, Budget / Finance, Grants, Ethics

### AGENCY — Who owns it
Board of Supervisors, Redding City Council, Register of Voters, Grand Jury,
HHSA, Public Health Dept, Behavioral Health Dept, RPD, Sheriff's Office,
Probation, District Attorney, Public Defender, County Counsel, Clerk / ROV,
Public Works, Planning Dept, Code Enforcement, Animal Services, City Attorney,
Redding Electric Utility, SRMC, CAL FIRE / USFS, FPPC

### ACTION — What happened
Approved, Denied, Tabled, Continued, No Action Taken, Public Hearing,
Public Comment, Ordinance, Resolution, Closed Session, Settlement,
Litigation, Eminent Domain, Recall Filed, Recall Qualified,
Adversarial, Revelation, Accusation, Testimony, Conduct,
Recusal, Legal Warning, Recess — Disruption, Room Cleared, Brown Act

### MONEY — How it's funded
Budget, Mid-Year Budget, Contract, RFP, Sole Source, Change Order, Audit,
Fees / Rate Increase, Tax, Capital Project, Grants, Settlement

### PLACE — Where it applies
City of Redding, Redding, Anderson, Shasta Lake, Unincorporated,
District 1, District 2, District 3, District 4, District 5,
Shasta Dam, Redding Riverfront

---

## Brown Act Compliance Check

After completing the summary and tags, briefly scan the meeting record for potential
open-meetings-law issues. This is a lightweight flag — raise it if the evidence in the
transcript or agenda warrants concern. You are NOT rendering a legal opinion.

Check for these common violations. If none are found, skip this section entirely.

**Sections to watch:**
- §54952.2 — Serial meeting prohibition: Did a quorum appear to confer outside the public
  meeting (phone, email, intermediary) to reach consensus on an agenda item?
- §54954.2 — 72-hour agenda notice: Was an item heard that was not on the posted agenda,
  or was the agenda amended with less than 72 hours' notice without an emergency finding?
- §54954.3 — Public comment rights: Was public comment on a non-closed-session agenda item
  cut short, denied, or limited in a way not authorized by the body's adopted rules?
- §54956.9 — Closed session — litigation: Did the board retreat to closed session on a
  litigation item without the required agenda description (case name or factual basis)?
- §54957.95 — Disruption / removal: Was anyone removed from the meeting? If so, did the
  presiding officer give the required warning(s) before removal?
- §54963 — Closed session confidentiality: Did any member disclose the substance of a
  closed session discussion in open session without authorization?

**Output format (only if a potential issue exists):**
```
## Potential Brown Act Issue
Section: §54954.2 — Inadequate agenda notice
Summary: Item 7 (emergency contract award) was added to the agenda without a 4/5
majority vote declaring an emergency, and no emergency finding was stated on the record.
Severity: Moderate — this is a procedural irregularity, not a confirmed violation.
```

If a potential issue is flagged, add `Brown Act` to `---TAGS-ACTION:`.
```

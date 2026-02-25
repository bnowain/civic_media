# Shasta County Civic Accountability Tag Taxonomy

Canonical reference for the `tags` table in civic_media. These tags are seeded by
`seed_tags.py` and used by the LLM summary pipeline to categorize meetings, documents,
and audio content.

## Naming Conventions

- Tags are stored as **Title Case display names** (e.g., `Election Administration`, `Board of Supervisors`)
- The `tag_type` field classifies each tag into one of five categories (see below)
- Tag matching is **case-insensitive** at parse time
- Tag names are unique across all categories — no two tags share the same name

## Tag Output Format (Summary Footer)

When an LLM generates a summary, tags are embedded at the very end using this format:

```
---TAGS-TOPIC: Election Administration, Recall Elections
---TAGS-AGENCY: Board of Supervisors, Register of Voters
---TAGS-ACTION: Denied, Closed Session
---TAGS-MONEY: Contract, Sole Source
---TAGS-PLACE: District 3, Unincorporated
```

- Omit any `---TAGS-*` line that has no applicable tags
- Tags within a line are comma-separated
- The ingest pipeline:
  1. Parses these lines and creates `tag_assignments` records with `source="llm"`
  2. Strips everything from the first `---TAGS-` line onward before writing to the
     `summary_short` or `summary_long` column — the stored summary contains prose only

## Categories

### TOPIC — What it's about (`tag_type = "topic"`)

General subject matter tags. Apply when the topic is a *primary* item, not
background context.

```
Healthcare
Mental Health
Substance Use
Homelessness
Housing
Law Enforcement
In-Custody Deaths
Jail / Detention
Fire / Emergency Services
Wildfire Recovery
Child Welfare
Veterans Services
Education
School Board Politics
Elections
Election Administration
Hand-Count / Election Integrity
Charter County
Recall Elections
Political Retaliation
Outside Donor Influence
Secession / State of Jefferson
Church-State / Religious Political Influence
Tribal Affairs
Environment
Water
Economic Development
Transportation / Infrastructure
Immigration / Sanctuary
Civil Rights / Civil Liberties
Transparency / Accountability
Whistleblower
Hospital / Healthcare Workers
Utility Rates
Budget / Finance
Grants
Ethics
```

`Ethics` — FPPC complaints, conflict of interest allegations, official misconduct claims,
ethics violation filings; use when ethics is the *subject* of discussion, not just context.

### AGENCY — Who owns it (`tag_type = "agency"`)

The government body, department, or organization that *owns* or *presents* the
agenda item. Not every agency mentioned — only the primary responsible party.

```
Board of Supervisors
Redding City Council
Register of Voters
Grand Jury
HHSA
Public Health Dept
Behavioral Health Dept
RPD
Sheriff's Office
Probation
District Attorney
Public Defender
County Counsel
Clerk / ROV
Public Works
Planning Dept
Code Enforcement
Animal Services
City Attorney
Redding Electric Utility
SRMC
CAL FIRE / USFS
FPPC
```

### ACTION — What happened (`tag_type = "action"`)

The formal outcome, procedural event, or notable meeting moment type.

Two sub-groups:
- **Outcome tags** (Approved, Denied, etc.): apply to confirmed decisions only
- **Moment-type tags** (Adversarial, Revelation, Accusation, Testimony, Conduct): apply
  if the moment occurred — brevity is NOT a reason to skip. A five-second hot mic remark
  still gets `Conduct`. If it appeared in Notable Moments, it gets the corresponding tag.

```
Approved
Denied
Tabled
Continued
No Action Taken
Public Hearing
Public Comment
Ordinance
Resolution
Closed Session
Settlement
Litigation
Eminent Domain
Recall Filed
Recall Qualified
Adversarial
Revelation
Accusation
Testimony
Conduct
Recusal
Legal Warning
Recess — Disruption
Room Cleared
Brown Act
```

Moment-type notes:
- `Conduct` — derogatory, demeaning, or improper remark or behavior by an official; hot mic moments; anything reflecting on character or conduct regardless of agenda item
- `Recusal` — official declares conflict of interest and steps away from a vote; note item and resulting vote math
- `Legal Warning` — county counsel or legal advisor formally warns the board a proposed action is legally risky
- `Recess — Disruption` — presiding officer suspends the meeting due to audience disorder
- `Room Cleared` — presiding officer orders gallery/audience removed; meeting may continue without the public
- `Brown Act` — a potential or apparent Brown Act violation was identified or alleged: serial meeting concerns, inadequate agenda notice, improper closed session, denial of public comment rights, or similar open-meetings-law issues

### MONEY — How it's funded (`tag_type = "money"`)

Financial and procurement classification. `Grants` appears in both TOPIC and MONEY —
use TOPIC when grants are the subject of discussion; MONEY when a specific grant
action is taken.

```
Budget
Mid-Year Budget
Contract
RFP
Sole Source
Change Order
Audit
Fees / Rate Increase
Tax
Capital Project
Grants
Settlement
```

> **Procurement hierarchy:** Tag with the most specific type that applies
> (RFP, Sole Source, Change Order). Only use `Contract` as a catch-all when
> none of the specific types fit.

### PLACE — Where it applies (`tag_type = "place"`)

Geographic or jurisdictional scope. `City of Redding` = the city government as
an institution or party. `Redding` = the geographic area when a county action
applies there.

```
City of Redding
Redding
Anderson
Shasta Lake
Unincorporated
District 1
District 2
District 3
District 4
District 5
Shasta Dam
Redding Riverfront
```

---

## Adding New Tags

1. Add the tag name and type to this file under the appropriate category
2. Add it to the `TAXONOMY` dict in `seed_tags.py`
3. Run `python seed_tags.py` from the civic_media project root
4. Update the prompt appendix in `summary_prompts.md` so the LLM knows the new tag exists
5. Note the addition in `E:\0-Automated-Apps\master_codex.md` (section 7.7)

## Rationale

Sources consulted when building this taxonomy:
- NorthStateBreakdown (northstatebreakdown.com) — recurring beats and agenda previews
- A News Cafe (anewscafe.com) — local civic accountability coverage
- Shasta Scout (shastascout.org) — investigative local journalism
- Shasta County Board of Supervisors agenda archives

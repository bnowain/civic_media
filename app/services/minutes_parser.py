"""
minutes_parser.py — Parse meeting minutes OCR text into structured vote records.

IMPORTANT: This parser is format-specific. Different governing bodies and different
time periods may produce minutes in different formats. See PARSER REGISTRY below.

Currently implemented:
  - Board of Supervisors (Shasta County) — "shasta_bos"

Adding a new format:
  1. Write a function `parse_<body>(text, ...) -> ParseResult`
  2. Register it in PARSER_REGISTRY keyed by a group_name string or alias
  3. The dispatch in `parse_minutes()` selects it automatically

Graceful degradation:
  When a paragraph looks like it contains a vote but doesn't match any known pattern,
  it is added to ParseResult.unmatched_paragraphs rather than silently skipped.
  The ingest script records these in Document.minutes_parse_notes so they can be
  reviewed and new patterns added.
"""

import difflib
import json as _json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Vote outcome constants ────────────────────────────────────────────────────

OUTCOME_UNANIMOUS = "Unanimously Carried"
OUTCOME_CARRIED   = "Carried"
OUTCOME_FAILED    = "Failed"
OUTCOME_NO_SECOND = "Failed — No Second"
OUTCOME_WITHDRAWN = "Withdrawn"


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ParsedVote:
    vote_id:           str = field(default_factory=lambda: str(uuid.uuid4()))
    meeting_id:        str = ""
    document_id:       Optional[str] = None
    meeting_date:      Optional[str] = None
    group_name:    Optional[str] = None
    agenda_section:    Optional[str] = None
    item_description:  str = ""
    resolution_number: Optional[str] = None
    outcome:           str = ""
    vote_tally:        Optional[str] = None
    mover:             Optional[str] = None
    seconder:          Optional[str] = None
    yes_members:       list = field(default_factory=list)
    no_members:        list = field(default_factory=list)
    absent_members:    list = field(default_factory=list)


@dataclass
class ParseResult:
    """
    Container returned by every parser. Always check unmatched_paragraphs
    when votes is empty or smaller than expected — it means paragraphs
    looked vote-like but didn't match any known pattern for this format.
    """
    votes:                list[ParsedVote]
    unmatched_paragraphs: list[str]       # vote-like paragraphs that didn't parse
    parser_used:          str             # e.g. "shasta_bos"
    parse_status:         str             # "ok" | "partial" | "empty" | "unrecognized"

    @property
    def parse_notes_json(self) -> Optional[str]:
        """Serialise unmatched paragraphs for storage in Document.minutes_parse_notes."""
        if not self.unmatched_paragraphs:
            return None
        import json
        return json.dumps(self.unmatched_paragraphs)


# ── Common helpers ────────────────────────────────────────────────────────────

_PARA_BREAK = re.compile(r'\n{2,}')
_LINE_BREAK = re.compile(r'\n')
_RESOLUTION = re.compile(r'Resolution\s+No\.\s+([\d-]+)', re.IGNORECASE)


def _normalise(text: str) -> str:
    paras = _PARA_BREAK.split(text)
    return "\n\n".join(_LINE_BREAK.sub(" ", p).strip() for p in paras)


def _clean_name(raw: str) -> str:
    return raw.strip().rstrip(",.")


def _parse_multi_names(raw: str) -> list[str]:
    raw = re.sub(r'\s+and\b', ',', raw)  # handles trailing "and" too
    names = [_clean_name(n) for n in raw.split(',') if n.strip()]
    cleaned = []
    for n in names:
        n = re.sub(r'^Supervisor\s+', '', n, flags=re.IGNORECASE)
        n = re.sub(r'\s+(?:abstaining|recusing|recused|absent)\s*$', '', n, flags=re.IGNORECASE).strip()
        if n and n.lower() != 'none':
            cleaned.append(n)
    return cleaned


def _extract_resolution(text: str) -> Optional[str]:
    m = _RESOLUTION.search(text)
    return m.group(1) if m else None


def _truncate(text: str, max_chars: int = 1000) -> str:
    text = text.strip()
    return text[:max_chars].rstrip() + "…" if len(text) > max_chars else text


_TALLY_PAT = re.compile(r'\b(\d+-\d+(?:-\d+)?)\b')


def _extract_tally(seg: str, group_tally: Optional[str], search_window: int = 80) -> Optional[str]:
    """Return the vote tally string.

    Prefers the explicitly-captured regex group.  Falls back to searching the
    first `search_window` chars of *seg* for a N-N or N-N-N pattern — needed
    when the tally is embedded inside 'by a 3-2 roll call vote' and not captured
    by the standalone tally group.
    """
    if group_tally:
        return group_tally
    m = _TALLY_PAT.search(seg[:search_window])
    return m.group(1) if m else None


# ── Supervisor roster — OCR name correction ───────────────────────────────────
# Loaded once at module import from config/supervisors.json.
# Used to fuzzy-correct OCR-mangled mover/seconder/member names at parse time.

_SUPERVISORS_FILE = Path(__file__).parent.parent.parent / "config" / "supervisors.json"

# [(last_name, start_date_str, end_date_str_or_None), ...]
_SUPERVISOR_ROSTER: list[tuple[str, str, Optional[str]]] = []
# all unique last names across all eras, for widened fuzzy matching
_ALL_SUPERVISOR_NAMES: list[str] = []


def _load_supervisor_roster() -> None:
    if not _SUPERVISORS_FILE.exists():
        return
    try:
        data = _json.loads(_SUPERVISORS_FILE.read_text(encoding="utf-8"))
        seen: set[str] = set()
        for entry in data.get("district_supervisors", []):
            full  = entry.get("supervisor", "")
            last  = full.rsplit(" ", 1)[-1]   # last word = surname
            start = entry.get("start_date") or "1900-01-01"
            end   = entry.get("end_date")     # None = still active
            _SUPERVISOR_ROSTER.append((last, start, end))
            if last not in seen:
                seen.add(last)
                _ALL_SUPERVISOR_NAMES.append(last)
    except Exception:
        pass  # degrade gracefully if file missing or malformed


_load_supervisor_roster()


def _supervisors_for_date(meeting_date: Optional[str]) -> list[str]:
    """Return last names of supervisors active on meeting_date (all names if date unknown)."""
    if not meeting_date or not _SUPERVISOR_ROSTER:
        return _ALL_SUPERVISOR_NAMES
    active = [
        last for last, start, end in _SUPERVISOR_ROSTER
        if start <= meeting_date and (end is None or meeting_date < end)
    ]
    return active if active else _ALL_SUPERVISOR_NAMES


def _correct_supervisor_name(name: str, meeting_date: Optional[str] = None) -> str:
    """Fuzzy-correct an OCR-mangled supervisor last name using the roster.

    Returns the corrected name if a close match (≥ 0.8 similarity) is found.
    First tries supervisors active on meeting_date; widens to all known names
    if no match found there.  Returns the original name if no close match found.
    """
    if not name or not _ALL_SUPERVISOR_NAMES:
        return name
    # Exact match (case-insensitive) — nothing to fix
    if any(n.lower() == name.lower() for n in _ALL_SUPERVISOR_NAMES):
        return name
    candidates = _supervisors_for_date(meeting_date)
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.75)
    if not matches:
        matches = difflib.get_close_matches(name, _ALL_SUPERVISOR_NAMES, n=1, cutoff=0.75)
    return matches[0] if matches else name


def _fix_supervisor_names(v: "ParsedVote", meeting_date: Optional[str]) -> None:
    """Correct OCR-mangled supervisor names in-place using the roster."""
    if v.mover:
        v.mover = _correct_supervisor_name(v.mover, meeting_date)
    if v.seconder:
        v.seconder = _correct_supervisor_name(v.seconder, meeting_date)
    v.yes_members    = [_correct_supervisor_name(n, meeting_date) for n in v.yes_members]
    v.no_members     = [_correct_supervisor_name(n, meeting_date) for n in v.no_members]
    v.absent_members = [_correct_supervisor_name(n, meeting_date) for n in v.absent_members]


# ── Vote-like detection (format-agnostic) ─────────────────────────────────────
# Used to flag paragraphs that look like votes but didn't match any pattern.

_VOTE_SIGNALS = re.compile(
    r'motion\s+made|seconded|unanimously\s+carried|roll\s+call\s+vote|'
    r'motion\s+failed|lack\s+of\s+a\s+second|withdrew\s+(?:his|her|their|the)\s+motion|'
    r'carried\s+\d-\d|failed\s+\d-\d',
    re.IGNORECASE,
)

# Outcome signals — distinguishes real vote outcomes from supplementary descriptions.
# Supplementary descriptions ("A motion was made (X/Y) to [action]" with no outcome)
# look vote-like but should NOT be counted as unmatched — their outcome appears in a
# separate segment.  Only flag as unmatched if an outcome signal is present.
_OUTCOME_SIGNALS = re.compile(
    r'unanimously\s+carried|carried\s+unanimously|(?:and|which)\s+(?:carried|failed)|'
    r'[Tt]he\s+(?:substitute\s+)?motion\s+(?:was\s+)?(?:approved|passed|carried|failed|denied|mooted|withdrawn)|'
    r'[Mm]otion\s+(?:was\s+)?withdrawn|[Mm]otion\s+failed|'
    r'lack\s+of\s+(?:a\s+)?(?:second|vote)|withdrew\s+(?:his|her|their|the)\s+motion|'
    r'ruled\s+out\s+of\s+order|AYES\s*:|NOES\s*:',
    re.IGNORECASE,
)


def _looks_like_vote(para: str) -> bool:
    return bool(_VOTE_SIGNALS.search(para))


def _has_outcome(para: str) -> bool:
    """Return True if the paragraph contains outcome language (not just a description)."""
    return bool(_OUTCOME_SIGNALS.search(para))


# ── OCR artifact normalization (older scanned minutes) ───────────────────────
# EasyOCR misreads '/' between supervisor names in parentheticals as 'M', 'l',
# or space (e.g. "BaughMMoty", "MotylKehoe", "Baugh Moty").
# Semicolons OCR'd instead of commas break pattern separators.
# Applied once per document before pattern matching so regex patterns are unchanged.

def _normalize_ocr_artifacts(text: str) -> str:
    """Normalize EasyOCR misreads common in older scanned BOS minutes."""

    def _fix_paren(m: re.Match) -> str:
        p = m.group()
        # Slash misread as single char between lowercase and uppercase names
        # (e.g. BaughMMoty → Baugh/Moty, MotylKehoe → Moty/Kehoe, BaughfMoty → Baugh/Moty)
        p = re.sub(r'(?<=[a-z])[Mlf1I](?=[A-Z])', '/', p)
        if '/' not in p:
            # Space as separator: "(Baugh Moty)" → "(Baugh/Moty)"
            p = re.sub(
                r'(?<=\()([A-Z][a-z]{2,14})\s+([A-Z][a-z]{2,14})(?=\))',
                r'\1/\2', p,
            )
        if '/' not in p:
            # No separator at all: "(MotyBaugh)" → "(Moty/Baugh)" — split at case boundary
            p = re.sub(
                r'(?<=\()([A-Z][a-z]{2,14})([A-Z][a-z]{2,14})(?=\))',
                r'\1/\2', p,
            )
        return p

    text = re.sub(r'\([^)]+\)', _fix_paren, text)

    # Strip date+page-number headers OCR'd at page boundaries, e.g. "October 11,2016 409"
    # MUST run first so subsequent lookaheads (semicolon, etc.) can see through page breaks.
    _months = (r'(?:January|February|March|April|May|June|July|August|September|'
               r'October|November|December)')
    text = re.sub(
        rf'(?m)^{_months}\s+\d{{1,2}},\s*\d{{4}}\s+\d+\s*',
        '', text, flags=re.IGNORECASE,
    )

    # Semicolons used as commas in critical vote-phrase positions
    text = re.sub(
        r'\s*;\s*(?=seconded|and\s+(?:unanimously\s+)?(?:carried|failed)|the\s+Board)',
        ', ', text, flags=re.IGNORECASE,
    )

    # "ad unanimously" → "and unanimously" (OCR misread of "and")
    text = re.sub(r'\bad\s+(?=unanimously)', 'and ', text, flags=re.IGNORECASE)

    # Period used as comma after closing paren before vote outcome
    # e.g. "seconded (Kehoe/Baugh). and unanimously carried" → "...), and unanimously carried"
    text = re.sub(
        r'\)\.\s+(?=and\s+(?:unanimously\s+)?(?:carried|failed))',
        '), ', text, flags=re.IGNORECASE,
    )

    # Collapse runs of spaces to single space (EasyOCR double-spaces within words)
    text = re.sub(r' {2,}', ' ', text)

    # IHSS uses "the IHSS Public Authority Governing Board" where BOS minutes say
    # "the Board of Supervisors" — normalize so patterns match
    text = re.sub(
        r'the\s+IHSS\s+Public\s+Authority\s+(?:Governing\s+)?Board',
        'the Board of Supervisors',
        text, flags=re.IGNORECASE,
    )

    return text


# ═══════════════════════════════════════════════════════════════════════════════
# SHASTA COUNTY BOARD OF SUPERVISORS PARSER
# ═══════════════════════════════════════════════════════════════════════════════

_MEMBER_HEADER = re.compile(
    r'District\s+No\.\s+\d\s+-\s+Supervisor\s+(\w+)', re.IGNORECASE
)

_SECTION_HEADERS = re.compile(
    r'^(CONSENT\s+CALENDAR|REGULAR\s+CALENDAR|BOARD\s+MATTERS|'
    r'CLOSED\s+SESSION|PUBLIC\s+COMMENT\s+PERIOD|'
    r'COUNTY\s+ADMINIST\w+\s+OFFICE|HEALTH\s+AND\s+HUMAN\s+SERVICES)',
    re.IGNORECASE | re.MULTILINE,
)

# Marks where a new vote action starts in the text — used to split into segments.
# Handles "By [substitute|amended] motion made" (standard) and "A [substitute] motion was made" (alt form).
_VOTE_START_PAT = re.compile(
    r'(?:By\s+(?:(?:substitute|amended)\s+)?motion\s+made|A(?:\s+substitute)?\s+motion\s+was\s+made)',
    re.IGNORECASE,
)

# Unanimous — "By [substitute] motion made" form (with or without "by roll call vote").
# Handles both word orders: "unanimously carried" and "carried unanimously".
# Also handles 2016 format with extra comma: "seconded, (Moty/Baugh)".
# Also handles "as amended" suffix: "unanimously carried as amended".
# Also handles optional tally before "by [a] roll call vote": "unanimously carried 5-0 by a roll call vote".
# Also handles optional recusal/abstention clause: "unanimously carried, with Supervisor X recusing himself".
# "(" before mover/seconder is optional to handle OCR artifacts.
_BOS_UNANIMOUS = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[.,\s]+'
    r'and\s+(?:unanimously carried|carried unanimously)(?:\s+as\s+amended)?'
    r'(?:\s+\d+-\d+(?:-\d+)?)?(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'(?:,\s*with\s+Supervisor\w*\s+\S+\s+(?:recus|abstain)\w+[^,]*)?'
    r'[,\s]+the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Unanimous — alternate "A [substitute] motion was made [and] seconded" form (2024+).
# "and" before "seconded" is optional to handle "was made, seconded" variant.
_BOS_ALT_UNANIMOUS = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made[,\s]+(?:and\s+)?seconded[,\s]+\(([^/]+)/([^)]+)\)[,\s]+'
    r'(?:.*?\s+)?(?:and\s+)?(?:unanimously carried|carried unanimously)(?:\s+by\s+(?:a\s+)?roll\s+call\s+vote)?[,\s]+'
    r'(?:the\s+Board(?:\s+of\s+Supervisors)?\s+)?(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Split — single dissenter ("By [substitute] motion made" form).
# Tally optional. Roll call optional and can appear before or after tally.
# Accepts "voting no/against", "opposed", or "dissenting" as dissent language.
# "(" before mover/seconder is optional to handle OCR artifacts.
_BOS_SPLIT_SINGLE = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'(?:and|which)\s+(?:carried|failed)'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'(?:\s+(\d+-\d+(?:-\d+)?))?'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'[,\s]+with\s+(?:Supervisors?\s+)?(\w+)\s+(?:voting\s+(?:no|against)|opposed|dissenting)[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Combined abstention + no vote — e.g. "carried 3-1-1 ... with Supervisor X abstaining
# and Supervisor Y voting no".  Must fire BEFORE _BOS_SPLIT_MULTI to avoid the lazy
# (.+?) in that pattern swallowing "X abstaining and Supervisor Y" as dissenter names.
# Groups: mover, seconder, tally, abstaining supervisors, no-voting supervisors, description.
_BOS_ABSTAIN_AND_NO = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'(?:and|which)\s+(?:carried|failed)'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'(?:\s+(\d+-\d+(?:-\d+)?))?'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'[,\s]+with\s+Supervisors?\s+(.+?)\s+abstaining\s+and\s+Supervisors?\s+(.+?)\s+'
    r'(?:voting\s+(?:no|against)|opposed|dissenting)[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Split — multiple dissenters ("By [substitute] motion made" form).
# Tally optional. Roll call optional and can appear before or after tally.
# Accepts "voting no/against", "opposed", or "dissenting" as dissent language.
# "(" before mover/seconder is optional to handle OCR artifacts.
_BOS_SPLIT_MULTI = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'(?:and|which)\s+(?:carried|failed)'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'(?:\s+(\d+-\d+(?:-\d+)?))?'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'[,\s]+with\s+(?:Supervisors?\s+)?(.+?)\s+(?:voting\s+(?:no|against)|opposed|dissenting)[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Carried/failed with 1+ supervisors absent, recused, or abstaining (no dissenters).
# "By [substitute] motion made" form. Handles single and multiple supervisors.
# Roll call optional, can appear before or after tally.
# e.g. "carried 4-0 with Supervisor Kelstrom recused" or "carried 3-0 with Supervisors Garman and Rickert abstaining"
_BOS_EXCUSED = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'(?:and|which)\s+(?:carried|unanimously\s+carried|carried\s+unanimously|failed)'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'(?:\s+(\d+-\d+(?:-\d+)?))?'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'[,\s]+with\s+Supervisors?\s+(.+?)\s+(?:absent|recused|abstaining|recusing)[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Single dissenter plus one supervisor excused — both qualifiers in the same vote.
# Roll call optional, can appear before or after tally. "which" alternative for "and".
# e.g. "carried by roll call vote 3-2 with Supervisor Jones voting no and Supervisor Kelstrom abstaining"
# Groups: mover, seconder, tally, no_name, excused_name (optional), description
_BOS_SPLIT_WITH_EXCUSED = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'(?:and|which)\s+(?:carried|failed)'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'(?:\s+(\d+-\d+(?:-\d+)?))?'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'[,\s]+with\s+(?:Supervisors?\s+)?(\w+)\s+(?:voting\s+(?:no|against)|opposed|dissenting)'
    r'(?:\s+and\s+(?:with\s+)?(?:Supervisors?\s+)?(\w+)\s+(?:absent|recused|abstaining))?[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Carried/failed with tally only — no dissenter names, "by roll call [vote]" optional.
# e.g. "carried 4-1, the Board" or "carried 5-0 by roll call vote, the Board"
# "(" before mover/seconder is optional to handle OCR artifacts.
_BOS_SPLIT_TALLY_ONLY = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'(?:and|which)\s+(?:carried|failed)\s+(\d+-\d+(?:-\d+)?)[,\s]+'
    r'(?:by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?[,\s]+)?'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Consent-calendar exception: "carried (except for the item concerning X where Supervisor Y voted no/recused)"
# The main vote is carried; the exception item is noted inline.
# Also handles "carried by roll call vote (except ...)" (2021 format).
# Groups: mover, seconder, exception_text, description
_BOS_CONSENT_EXCEPTION = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'and (?:unanimously\s+)?carried(?:\s+by\s+roll\s+call\s+vote)?\s*'
    r'\(except\s+for\s+(?:the\s+item\s+)?(?:concerning\s+)?(.+?)\)'
    r'[,\s]+the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Simple "carried by roll call vote" without tally or dissenter names.
# Used in 2021 format where explicit "unanimously" was sometimes omitted.
# Treated as a carried vote with no breakdown (tally left null).
# Groups: mover, seconder, description
_BOS_CARRIED_ROLLCALL = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'and\s+carried(?:\s+by\s+roll\s+call\s+vote)?[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Pre-outcome abstention — "By motion made, seconded (X/Y), with Supervisors A and B abstaining, and carried"
# Used when some supervisors abstain; outcome is CARRIED (not unanimous).
# Groups: mover, seconder, abstaining supervisors text, description
_BOS_ABSTAIN = re.compile(
    r'By\s+(?:(?:substitute|amended)\s+)?motion\s+made[,\s]+seconded[,\s]+\(?([^/]+)/([^)]+)\)[,\s]+'
    r'with\s+Supervisors?\s+([\w,\s]+?)\s+abstaining[,\s]+'
    r'and\s+carried[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Alt form — split single dissenter.
# "A [substitute] motion was made[,] [and] seconded (X/Y), and carried/failed [N-N] with Supervisor X voting no"
# Description is the trailing text (the action being voted on).
_BOS_ALT_SPLIT_SINGLE = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made[,\s]+(?:and\s+)?seconded[,\s]+\(([^/]+)/([^)]+)\)[,\s]+'
    r'and\s+(?:carried|failed)(?:\s+(\d+-\d+(?:-\d+)?))?[,\s]+'
    r'(?:by\s+(?:a\s+)?roll\s+call\s+vote[,\s]+)?'
    r'with\s+Supervisor\s+(\w+)\s+(?:voting\s+(?:no|against)|opposed|dissenting)[,\s]+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Alt form — split multiple dissenters. "and" before "seconded" optional.
# Tally optional. "by roll call vote" optional.
# e.g. "A motion was made, seconded (X/Y), and failed 2-3 with Supervisors Crye, Jones, and Kelstrom voting no"
_BOS_ALT_SPLIT_MULTI = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made[,\s]+(?:and\s+)?seconded[,\s]+\(([^/]+)/([^)]+)\)[,\s]+'
    r'and\s+(?:carried|failed)(?:\s+(\d+-\d+(?:-\d+)?))?[,\s]+'
    r'(?:by\s+(?:a\s+)?roll\s+call\s+vote[,\s]+)?'
    r'with\s+Supervisors?\s+(.+?)\s+(?:voting\s+(?:no|against)|opposed|dissenting)[,\s]+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Alt form — supervisors excused (absent/recused/abstaining), no dissenters.
# "and" before "seconded" optional.
_BOS_ALT_EXCUSED = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made[,\s]+(?:and\s+)?seconded[,\s]+\(([^/]+)/([^)]+)\)[,\s]+'
    r'and\s+(?:carried|failed|unanimously\s+carried|carried\s+unanimously)(?:\s+(\d+-\d+(?:-\d+)?))?[,\s]+'
    r'(?:by\s+(?:a\s+)?roll\s+call\s+vote[,\s]+)?'
    r'with\s+Supervisors?\s+(.+?)\s+(?:absent|recused|abstaining)[,\s]+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Alt form with inline AYES/NOES (2022+ split votes recorded without an explicit outcome line).
# "and" before "seconded" optional.
# e.g. "A motion was made and seconded (Baugh/Jones) to place on next agenda...
#       AYES: Supervisors Jones and Baugh  NOES: Supervisors Chimenti, Moty, and Rickert"
# Outcome is derived from member counts (majority = carried, minority = failed).
_BOS_ALT_AYES_NOES = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made[,\s]+(?:and\s+)?seconded[,\s]+\(([^/]+)/([^)]+)\)'
    r'(.*?)'
    r'AYES\s*:\s*((?:Supervisors?\s+)?[\w,\s]+?)'
    r'\s*NOES\s*:\s*((?:Supervisors?\s+)?[\w,\s]+?)'
    r'(?=\s+(?:ABSENT|ABSTAIN|[A-Z]{4,}\b)|[\s,.]*$)',
    re.IGNORECASE | re.DOTALL,
)

# Failed — no second, named mover ("A motion was made by Supervisor X to ...").
# Uses loose .*? between clauses so OCR page numbers inserted between sentences
# don't break the match.  Also handles "which failed", "lack of second" (no "a").
_BOS_NO_SECOND_NAMED = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made(?:\s+by)?\s+Supervisor\s+(\w+)\s+to\s+'
    r'(.*?)'
    r'(?:The\s+)?[Mm]otion(?:\s+which)?.*?'
    r'(?:died|failed).*?'
    r'lack\s+of\s+(?:a\s+)?(?:second|vote)',
    re.IGNORECASE | re.DOTALL,
)

# Failed — no second, paired motion ("A motion was made and seconded (X/Y) to ...").
# Used when the mover/seconder are in the parenthetical rather than named inline.
_BOS_NO_SECOND_PAIRED = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made[,\s]+(?:and\s+)?seconded[,\s]+\(([^/]+)/([^)]+)\)'
    r'\s+to\s+(.*?)'
    r'(?:The\s+)?[Mm]otion(?:\s+which)?.*?'
    r'(?:died|failed).*?'
    r'lack\s+of\s+(?:a\s+)?(?:second|vote)',
    re.IGNORECASE | re.DOTALL,
)

# Alt form — withdrawn motion when the parenthetical names mover/seconder.
# "A motion was made and seconded (X/Y) to [action]. Motion was withdrawn."
# Groups: mover, seconder
_BOS_ALT_WITHDRAWN = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made[,\s]+(?:and\s+)?seconded[,\s]+\(([^/]+)/([^)]+)\)'
    r'.*?'
    r'[Mm]otion\s+(?:was\s+)?withdrawn',
    re.IGNORECASE | re.DOTALL,
)

# Alt form — deferred outcome: description comes first, then "The motion [was] carried/failed".
# Covers the pattern: "A motion was made (X/Y) to [action]. [discussion.] The motion failed 2-3..."
# The description text (group 3) includes everything from the parenthetical to "The motion".
# Groups: mover, seconder, desc_and_discussion, tally, no_names (optional), absent_names (optional)
_BOS_ALT_DEFERRED_OUTCOME = re.compile(
    r'A(?:\s+substitute)?\s+motion\s+was\s+made[,\s]+(?:and\s+)?seconded[,\s]+\(([^/]+)/([^)]+)\)'
    r'(.*?)'
    r'(?:The\s+(?:substitute\s+)?)?[Mm]otion'
    r'(?:\s+was)?'
    r'\s+(?:approved|passed|carried|failed|denied|rejected|mooted)(?:\s+unanimously)?'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'(?:\s+(\d+-\d+(?:-\d+)?))?'
    r'(?:\s+by\s+(?:a\s+)?(?:\d+-\d+(?:-\d+)?\s+)?roll\s+call(?:\s+vote)?)?'
    r'(?:[,\s]+with\s+Supervisors?\s+([\w,\s]+?)\s+(?:voting\s+(?:no|against)|opposed|dissenting))?'
    r'(?:[,\s]+(?:and\s+)?(?:with\s+)?Supervisors?\s+([\w,\s]+?)\s+(?:absent|recused|abstaining))?',
    re.IGNORECASE | re.DOTALL,
)

# Withdrawn — "Supervisor X withdrew his/her/their/the motion" or plural form.
_BOS_WITHDRAWN = re.compile(
    r'Supervisors?\s+(\w+)(?:\s+and\s+\w+)?\s+withdrew\s+(?:his|her|their|the)\s+motion',
    re.IGNORECASE,
)

# Explicit AYES / NOES roll-call block (2022+ split votes recorded separately)
# Matches "AYES: Supervisors Jones and Baugh NOES: Supervisors Chimenti, Moty, and Rickert"
_BOS_AYES_NOES = re.compile(
    r'AYES\s*:\s*((?:Supervisors?\s+)?[\w,\s]+?)'
    r'\s+NOES\s*:\s*((?:Supervisors?\s+)?[\w,\s]+?)'
    r'(?=\s+(?:ABSENT|ABSTAIN|[A-Z]{4,}\b)|$)',
    re.IGNORECASE,
)


def _bos_current_section(text_before: str) -> str:
    matches = list(_SECTION_HEADERS.finditer(text_before))
    return matches[-1].group(0).strip().title() if matches else "Regular Calendar"


def _parse_supervisor_names(raw: str) -> list[str]:
    """Parse 'Supervisors Jones, Plummer and Baugh' -> ['Jones', 'Plummer', 'Baugh']."""
    raw = re.sub(r'^Supervisors?\s+', '', raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r'\s+and\b', ',', raw)  # handles mid-list "and" AND trailing "and"
    names = [_clean_name(n) for n in raw.split(',') if n.strip()]
    return [n for n in names if n.lower() != 'none']


def _parse_shasta_bos(
    ocr_text: str,
    meeting_id: str,
    document_id: Optional[str],
    meeting_date: Optional[str],
    group_name: Optional[str],
) -> ParseResult:
    members_present = [m.group(1) for m in _MEMBER_HEADER.finditer(ocr_text)]
    normalised = _normalise(ocr_text)
    normalised = _normalize_ocr_artifacts(normalised)  # fix scanned-PDF misreads

    votes: list[ParsedVote] = []
    unmatched: list[str] = []

    # ── Phase 1: scan for vote-action segments ────────────────────────────────
    # Split the full text at every "By motion made" / "A motion was made" boundary
    # so each segment starts with vote language.  This handles:
    #   - OCR that produces very few paragraph breaks (all votes in one big block)
    #   - Multiple votes within a single paragraph
    #   - Vote language that doesn't start at the beginning of a paragraph
    vote_starts = [m.start() for m in _VOTE_START_PAT.finditer(normalised)]
    vote_starts.append(len(normalised))  # sentinel

    matched_positions: set[int] = set()  # phase-1 starts that produced a successful parse

    for i in range(len(vote_starts) - 1):
        start = vote_starts[i]
        end   = vote_starts[i + 1]
        seg   = normalised[start:end]
        section = _bos_current_section(normalised[:start])
        v = None

        m = _BOS_UNANIMOUS.match(seg)
        if m:
            v = ParsedVote(
                meeting_id=meeting_id, document_id=document_id,
                meeting_date=meeting_date, group_name=group_name,
                agenda_section=section,
                item_description=_truncate(m.group(3)),
                resolution_number=_extract_resolution(seg),
                outcome=OUTCOME_UNANIMOUS, vote_tally="5-0",
                mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                yes_members=list(members_present),
            )

        if v is None:
            m = _BOS_SPLIT_SINGLE.match(seg)
            if m:
                no_name  = m.group(4).strip()
                yes_list = [n for n in members_present if n.lower() != no_name.lower()]
                tally    = _extract_tally(seg, m.group(3)) or f"{len(yes_list)}-1"
                outcome  = OUTCOME_FAILED if 'failed' in seg[:80].lower() else OUTCOME_CARRIED
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(5)),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=[no_name],
                )

        if v is None:
            m = _BOS_ABSTAIN_AND_NO.match(seg)
            if m:
                abstain_names = _parse_supervisor_names(m.group(4))
                no_names = _parse_multi_names(m.group(5))
                exclude = {n.lower() for n in abstain_names + no_names}
                yes_list = [n for n in members_present if n.lower() not in exclude]
                tally = _extract_tally(seg, m.group(3)) or (
                    f"{len(yes_list)}-{len(no_names)}-{len(abstain_names)}")
                outcome = OUTCOME_FAILED if 'failed' in seg[:80].lower() else OUTCOME_CARRIED
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(6)),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=no_names, absent_members=abstain_names,
                )

        if v is None:
            m = _BOS_SPLIT_MULTI.match(seg)
            if m:
                no_names = _parse_multi_names(m.group(4))
                no_lower = {n.lower() for n in no_names}
                yes_list = [n for n in members_present if n.lower() not in no_lower]
                tally    = _extract_tally(seg, m.group(3)) or f"{len(yes_list)}-{len(no_names)}"
                outcome  = OUTCOME_FAILED if 'failed' in seg[:80].lower() else OUTCOME_CARRIED
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(5)),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=no_names,
                )

        if v is None:
            m = _BOS_SPLIT_WITH_EXCUSED.match(seg)
            if m:
                no_name      = m.group(4).strip()
                excused_name = m.group(5).strip() if m.group(5) else None
                outcome      = OUTCOME_FAILED if 'failed' in seg[:80].lower() else OUTCOME_CARRIED
                excused_set  = {excused_name.lower()} if excused_name else set()
                yes_list     = [n for n in members_present
                                if n.lower() != no_name.lower() and n.lower() not in excused_set]
                tally        = _extract_tally(seg, m.group(3)) or f"{len(yes_list)}-1"
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(6)),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=[no_name],
                    absent_members=[excused_name] if excused_name else [],
                )

        if v is None:
            m = _BOS_EXCUSED.match(seg)
            if m:
                excused_list = _parse_supervisor_names(m.group(4))
                tally_str    = _extract_tally(seg, m.group(3))
                excused_set  = {n.lower() for n in excused_list}
                yes_list     = [n for n in members_present if n.lower() not in excused_set]
                if tally_str:
                    tally = tally_str
                elif members_present:
                    tally = f"{len(yes_list)}-0"
                else:
                    tally = f"{5 - len(excused_list)}-0"
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(5)),
                    resolution_number=_extract_resolution(seg),
                    outcome=OUTCOME_UNANIMOUS, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, absent_members=excused_list,
                )

        if v is None:
            m = _BOS_SPLIT_TALLY_ONLY.match(seg)
            if m:
                tally   = m.group(3)
                outcome = OUTCOME_FAILED if 'failed' in seg[:80].lower() else OUTCOME_CARRIED
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(4)),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                )

        if v is None:
            m = _BOS_CONSENT_EXCEPTION.match(seg)
            if m:
                exception_text = m.group(3)
                diss_m  = re.search(r'Supervisor\s+(\w+)\s+(?:voted|voting)\s+no', exception_text, re.IGNORECASE)
                recus_m = re.search(r'Supervisor\s+(\w+)\s+recus', exception_text, re.IGNORECASE)
                if diss_m:
                    no_name  = diss_m.group(1)
                    no_names = [no_name]
                    yes_list = [n for n in members_present if n.lower() != no_name.lower()]
                    tally    = f"{len(yes_list)}-1"
                    outcome  = OUTCOME_CARRIED
                    absent_l = []
                elif recus_m:
                    rec_name = recus_m.group(1)
                    yes_list = [n for n in members_present if n.lower() != rec_name.lower()]
                    tally    = f"{len(yes_list)}-0"
                    outcome  = OUTCOME_UNANIMOUS
                    no_names = []
                    absent_l = [rec_name]
                else:
                    yes_list = list(members_present)
                    tally    = "5-0"
                    outcome  = OUTCOME_UNANIMOUS
                    no_names = []
                    absent_l = []
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(4)),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=no_names, absent_members=absent_l,
                )

        if v is None:
            m = _BOS_CARRIED_ROLLCALL.match(seg)
            if m:
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(3)),
                    resolution_number=_extract_resolution(seg),
                    outcome=OUTCOME_CARRIED, vote_tally=None,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=list(members_present),
                )

        if v is None:
            m = _BOS_ABSTAIN.match(seg)
            if m:
                abstain_names = _parse_supervisor_names(m.group(3))
                yes_list = [n for n in members_present
                            if not any(n.lower() == a.lower() for a in abstain_names)]
                tally = f"{len(yes_list)}-0"
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(4)),
                    resolution_number=_extract_resolution(seg),
                    outcome=OUTCOME_CARRIED, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, absent_members=abstain_names,
                )

        if v is None:
            m = _BOS_ALT_UNANIMOUS.match(seg)
            if m:
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(3)),
                    resolution_number=_extract_resolution(seg),
                    outcome=OUTCOME_UNANIMOUS, vote_tally="5-0",
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=list(members_present),
                )

        if v is None:
            m = _BOS_ALT_SPLIT_SINGLE.match(seg)
            if m:
                no_name  = m.group(4).strip()
                yes_list = [n for n in members_present if n.lower() != no_name.lower()]
                tally    = _extract_tally(seg, m.group(3)) or f"{len(yes_list)}-1"
                outcome  = OUTCOME_FAILED if 'failed' in seg[:80].lower() else OUTCOME_CARRIED
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(5)),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=[no_name],
                )

        if v is None:
            m = _BOS_ALT_SPLIT_MULTI.match(seg)
            if m:
                no_names = _parse_multi_names(m.group(4))
                no_lower = {n.lower() for n in no_names}
                yes_list = [n for n in members_present if n.lower() not in no_lower]
                tally    = _extract_tally(seg, m.group(3)) or f"{len(yes_list)}-{len(no_names)}"
                outcome  = OUTCOME_FAILED if 'failed' in seg[:80].lower() else OUTCOME_CARRIED
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(5)),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=no_names,
                )

        if v is None:
            m = _BOS_ALT_EXCUSED.match(seg)
            if m:
                excused_list = _parse_supervisor_names(m.group(4))
                tally_str    = m.group(3)
                excused_set  = {n.lower() for n in excused_list}
                yes_list     = [n for n in members_present if n.lower() not in excused_set]
                tally        = tally_str or f"{len(yes_list)}-0"
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(5)),
                    resolution_number=_extract_resolution(seg),
                    outcome=OUTCOME_UNANIMOUS, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, absent_members=excused_list,
                )

        if v is None:
            m = _BOS_ALT_AYES_NOES.match(seg)
            if m:
                yes_names = _parse_supervisor_names(m.group(4))
                no_names  = _parse_supervisor_names(m.group(5))
                # Filter out "None" placeholder
                no_names  = [n for n in no_names if n.lower() != 'none']
                if not no_names:
                    outcome = OUTCOME_UNANIMOUS
                elif len(yes_names) > len(no_names):
                    outcome = OUTCOME_CARRIED
                else:
                    outcome = OUTCOME_FAILED
                tally = f"{len(yes_names)}-{len(no_names)}"
                desc  = m.group(3).strip().lstrip(',').strip()
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(desc),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_names if yes_names else list(members_present),
                    no_members=no_names,
                )

        if v is None:
            m = _BOS_NO_SECOND_NAMED.search(seg)
            if m:
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(2)),
                    resolution_number=_extract_resolution(seg),
                    outcome=OUTCOME_NO_SECOND,
                    mover=m.group(1).strip(),
                )

        if v is None:
            m = _BOS_NO_SECOND_PAIRED.search(seg)
            if m:
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(m.group(3)),
                    resolution_number=_extract_resolution(seg),
                    outcome=OUTCOME_NO_SECOND,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                )

        if v is None:
            m = _BOS_ALT_WITHDRAWN.match(seg)
            if m:
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(seg[:400]),
                    resolution_number=_extract_resolution(seg),
                    outcome=OUTCOME_WITHDRAWN,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                )

        if v is None:
            m = _BOS_ALT_DEFERRED_OUTCOME.match(seg)
            if m:
                # Group 3 is the description + discussion text (up to "The motion...")
                raw_desc = m.group(3).strip()
                # Strip leading "to " connector
                desc = re.sub(r'^\s*to\s+', '', raw_desc)
                # Truncate at first paragraph break (limit to the action description)
                desc = re.split(r'\n{2,}', desc)[0].strip()
                tally_str = m.group(4)
                no_raw    = (m.group(5) or "").strip()
                abs_raw   = (m.group(6) or "").strip()
                no_names  = _parse_supervisor_names(no_raw) if no_raw else []
                abs_names = _parse_supervisor_names(abs_raw) if abs_raw else []
                # Determine outcome from the text after the description
                after_desc = seg[m.end(3):]
                if re.search(r'\b(?:failed|denied|rejected)\b', after_desc, re.IGNORECASE):
                    outcome = OUTCOME_FAILED
                elif no_names:
                    outcome = OUTCOME_CARRIED
                else:
                    outcome = OUTCOME_UNANIMOUS if re.search(r'unanimously', after_desc, re.IGNORECASE) else OUTCOME_CARRIED
                no_set   = {n.lower() for n in no_names}
                abs_set  = {n.lower() for n in abs_names}
                yes_list = [n for n in members_present
                            if n.lower() not in no_set and n.lower() not in abs_set]
                if not tally_str and members_present:
                    tally_str = f"{len(yes_list)}-{len(no_names)}"
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, group_name=group_name,
                    agenda_section=section,
                    item_description=_truncate(desc),
                    resolution_number=_extract_resolution(seg),
                    outcome=outcome, vote_tally=tally_str,
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=no_names, absent_members=abs_names,
                )

        if v is not None:
            _fix_supervisor_names(v, meeting_date)
            votes.append(v)
            matched_positions.add(start)
        elif _looks_like_vote(seg) and _has_outcome(seg):
            # Only flag as unmatched if the segment has actual outcome language.
            # Pure supplementary descriptions ("A motion was made (X/Y) to [action]"
            # with no outcome) are not missing votes — they're preambles whose outcome
            # is captured in the following segment.
            # Skip segments where Phase 2 will handle a withdrawal.
            if _BOS_WITHDRAWN.search(seg):
                pass  # Phase 2 finditer will add the withdrawn vote
            else:
                unmatched.append(seg[:300])

    # ── Phase 2: withdrawn motions (don't start with vote-action markers) ─────
    for m in _BOS_WITHDRAWN.finditer(normalised):
        section = _bos_current_section(normalised[:m.start()])
        wv = ParsedVote(
            meeting_id=meeting_id, document_id=document_id,
            meeting_date=meeting_date, group_name=group_name,
            agenda_section=section,
            item_description=_truncate(normalised[max(0, m.start() - 200):m.end() + 100]),
            outcome=OUTCOME_WITHDRAWN,
            mover=m.group(1).strip(),
        )
        _fix_supervisor_names(wv, meeting_date)
        votes.append(wv)

    # ── Phase 3: explicit AYES/NOES roll-call blocks (2022+ split votes) ──────
    # These appear when split votes are recorded without a preceding "By motion made"
    # outcome line.  Only add as a vote record if it wasn't already captured in Phase 1
    # by a SUCCESSFULLY matched pattern (matched_positions).  If Phase 1 triggered but
    # failed to match (unmatched), Phase 3 should still try to extract something here.

    for m in _BOS_AYES_NOES.finditer(normalised):
        # Skip if a nearby phase-1 segment was SUCCESSFULLY parsed (avoid double-count).
        nearby = any(abs(m.start() - p) < 400 for p in matched_positions
                     if p < m.start())
        if nearby:
            continue
        yes_names = _parse_supervisor_names(m.group(1))
        no_names  = _parse_supervisor_names(m.group(2))
        section   = _bos_current_section(normalised[:m.start()])
        tally     = f"{len(yes_names)}-{len(no_names)}"
        av = ParsedVote(
            meeting_id=meeting_id, document_id=document_id,
            meeting_date=meeting_date, group_name=group_name,
            agenda_section=section,
            item_description=_truncate(normalised[max(0, m.start() - 300):m.start()]),
            resolution_number=_extract_resolution(normalised[max(0, m.start() - 500):m.end()]),
            outcome=OUTCOME_CARRIED,
            vote_tally=tally,
            yes_members=yes_names,
            no_members=no_names,
        )
        _fix_supervisor_names(av, meeting_date)
        votes.append(av)

    if not votes and not unmatched:
        status = "empty"
    elif unmatched and not votes:
        status = "unrecognized"
    elif unmatched:
        status = "partial"
    else:
        status = "ok"

    return ParseResult(
        votes=votes,
        unmatched_paragraphs=unmatched,
        parser_used="shasta_bos",
        parse_status=status,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PARSER REGISTRY — add new governing body parsers here
# ═══════════════════════════════════════════════════════════════════════════════
#
# Each entry maps one or more governing body name substrings (lowercase) to
# a parser function with signature:
#   fn(ocr_text, meeting_id, document_id, meeting_date, group_name) -> ParseResult
#
# The dispatch in parse_minutes() checks group_name.lower() for each key.

PARSER_REGISTRY: dict[str, callable] = {
    "board of supervisors": _parse_shasta_bos,
    "shasta county board": _parse_shasta_bos,
    # Future entries:
    # "redding city council": _parse_redding_city_council,
    # "planning commission":  _parse_planning_commission,
}

_DEFAULT_PARSER = _parse_shasta_bos  # fallback if no registry match


# ── Public API ────────────────────────────────────────────────────────────────

def parse_minutes(
    ocr_text: str,
    meeting_id: str,
    document_id: Optional[str] = None,
    meeting_date: Optional[str] = None,
    group_name: Optional[str] = None,
) -> ParseResult:
    """
    Parse meeting minutes OCR text into structured vote records.

    Selects the appropriate parser based on group_name. Falls back to the
    Shasta BOS parser if no match is found — check parse_result.parser_used and
    parse_result.parse_status to detect mismatches.

    Args:
        ocr_text:       Raw OCR text from a minutes document.
        meeting_id:     UUID of the meeting in the DB.
        document_id:    UUID of the source Document record (nullable).
        meeting_date:   ISO date string "YYYY-MM-DD".
        group_name: Name of the group (government body or show).

    Returns:
        ParseResult with votes, unmatched paragraphs, and parse status.
    """
    parser = _DEFAULT_PARSER
    if group_name:
        gb_lower = group_name.lower()
        for key, fn in PARSER_REGISTRY.items():
            if key in gb_lower:
                parser = fn
                break

    return parser(
        ocr_text=ocr_text,
        meeting_id=meeting_id,
        document_id=document_id,
        meeting_date=meeting_date,
        group_name=group_name,
    )

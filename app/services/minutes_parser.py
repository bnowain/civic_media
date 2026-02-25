"""
minutes_parser.py — Parse meeting minutes OCR text into structured vote records.

IMPORTANT: This parser is format-specific. Different governing bodies and different
time periods may produce minutes in different formats. See PARSER REGISTRY below.

Currently implemented:
  - Board of Supervisors (Shasta County) — "shasta_bos"

Adding a new format:
  1. Write a function `parse_<body>(text, ...) -> ParseResult`
  2. Register it in PARSER_REGISTRY keyed by a governing_body string or alias
  3. The dispatch in `parse_minutes()` selects it automatically

Graceful degradation:
  When a paragraph looks like it contains a vote but doesn't match any known pattern,
  it is added to ParseResult.unmatched_paragraphs rather than silently skipped.
  The ingest script records these in Document.minutes_parse_notes so they can be
  reviewed and new patterns added.
"""

import re
import uuid
from dataclasses import dataclass, field
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
    governing_body:    Optional[str] = None
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
    raw = raw.replace(" and ", ", ")
    return [_clean_name(n) for n in raw.split(",") if n.strip()]


def _extract_resolution(text: str) -> Optional[str]:
    m = _RESOLUTION.search(text)
    return m.group(1) if m else None


def _truncate(text: str, max_chars: int = 1000) -> str:
    text = text.strip()
    return text[:max_chars].rstrip() + "…" if len(text) > max_chars else text


# ── Vote-like detection (format-agnostic) ─────────────────────────────────────
# Used to flag paragraphs that look like votes but didn't match any pattern.

_VOTE_SIGNALS = re.compile(
    r'motion\s+made|seconded|unanimously\s+carried|roll\s+call\s+vote|'
    r'motion\s+failed|lack\s+of\s+a\s+second|withdrew\s+(?:his|her|their|the)\s+motion|'
    r'carried\s+\d-\d|failed\s+\d-\d',
    re.IGNORECASE,
)


def _looks_like_vote(para: str) -> bool:
    return bool(_VOTE_SIGNALS.search(para))


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

# Unanimous (with or without explicit "by roll call vote")
_BOS_UNANIMOUS = re.compile(
    r'By motion made,?\s+seconded\s+\(([^/]+)/([^)]+)\)[,\s]+'
    r'and unanimously carried(?:\s+by roll call vote)?[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Split — single dissenter
_BOS_SPLIT_SINGLE = re.compile(
    r'By motion made,?\s+seconded\s+\(([^/]+)/([^)]+)\)[,\s]+'
    r'and carried\s+(\d-\d)\s+by roll call vote\s+'
    r'with\s+Supervisor\s+(\w+)\s+voting no[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Split — multiple dissenters
_BOS_SPLIT_MULTI = re.compile(
    r'By motion made,?\s+seconded\s+\(([^/]+)/([^)]+)\)[,\s]+'
    r'and carried\s+(\d-\d)\s+by roll call vote\s+'
    r'with\s+Supervisors?\s+(.+?)\s+voting no[,\s]+'
    r'the\s+Board(?:\s+of\s+Supervisors)?\s+(.*)',
    re.IGNORECASE | re.DOTALL,
)

# Failed — no second (named mover form)
_BOS_NO_SECOND_NAMED = re.compile(
    r'A motion was made by\s+Supervisor\s+(\w+)\s+to\s+(.*?)\.\s*'
    r'(?:The\s+)?[Mm]otion\s+failed\s+(?:due\s+to\s+|for\s+)lack\s+of\s+a\s+second',
    re.IGNORECASE | re.DOTALL,
)

# Withdrawn
_BOS_WITHDRAWN = re.compile(
    r'Supervisor\s+(\w+)\s+withdrew\s+(?:his|her|their|the)\s+motion',
    re.IGNORECASE,
)


def _bos_current_section(text_before: str) -> str:
    matches = list(_SECTION_HEADERS.finditer(text_before))
    return matches[-1].group(0).strip().title() if matches else "Regular Calendar"


def _parse_shasta_bos(
    ocr_text: str,
    meeting_id: str,
    document_id: Optional[str],
    meeting_date: Optional[str],
    governing_body: Optional[str],
) -> ParseResult:
    members_present = [m.group(1) for m in _MEMBER_HEADER.finditer(ocr_text)]
    normalised = _normalise(ocr_text)
    paras = normalised.split("\n\n")

    votes: list[ParsedVote] = []
    unmatched: list[str] = []
    offset = 0

    for para in paras:
        section = _bos_current_section(normalised[:offset])
        offset += len(para) + 2
        v = None

        m = _BOS_UNANIMOUS.match(para)
        if m:
            v = ParsedVote(
                meeting_id=meeting_id, document_id=document_id,
                meeting_date=meeting_date, governing_body=governing_body,
                agenda_section=section,
                item_description=_truncate(m.group(3)),
                resolution_number=_extract_resolution(para),
                outcome=OUTCOME_UNANIMOUS, vote_tally="5-0",
                mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                yes_members=list(members_present),
            )

        if v is None:
            m = _BOS_SPLIT_SINGLE.match(para)
            if m:
                no_name  = m.group(4).strip()
                yes_list = [n for n in members_present if n.lower() != no_name.lower()]
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, governing_body=governing_body,
                    agenda_section=section,
                    item_description=_truncate(m.group(5)),
                    resolution_number=_extract_resolution(para),
                    outcome=OUTCOME_CARRIED, vote_tally=m.group(3),
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=[no_name],
                )

        if v is None:
            m = _BOS_SPLIT_MULTI.match(para)
            if m:
                no_names = _parse_multi_names(m.group(4))
                no_lower = {n.lower() for n in no_names}
                yes_list = [n for n in members_present if n.lower() not in no_lower]
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, governing_body=governing_body,
                    agenda_section=section,
                    item_description=_truncate(m.group(5)),
                    resolution_number=_extract_resolution(para),
                    outcome=OUTCOME_CARRIED, vote_tally=m.group(3),
                    mover=_clean_name(m.group(1)), seconder=_clean_name(m.group(2)),
                    yes_members=yes_list, no_members=no_names,
                )

        if v is None:
            m = _BOS_NO_SECOND_NAMED.search(para)
            if m:
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, governing_body=governing_body,
                    agenda_section=section,
                    item_description=_truncate(m.group(2)),
                    resolution_number=_extract_resolution(para),
                    outcome=OUTCOME_NO_SECOND,
                    mover=m.group(1).strip(),
                )

        if v is None:
            m = _BOS_WITHDRAWN.search(para)
            if m:
                v = ParsedVote(
                    meeting_id=meeting_id, document_id=document_id,
                    meeting_date=meeting_date, governing_body=governing_body,
                    agenda_section=section,
                    item_description=_truncate(para),
                    outcome=OUTCOME_WITHDRAWN,
                    mover=m.group(1).strip(),
                )

        if v is not None:
            votes.append(v)
        elif _looks_like_vote(para):
            # Paragraph looked vote-like but didn't match any known pattern
            unmatched.append(para[:300])

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
#   fn(ocr_text, meeting_id, document_id, meeting_date, governing_body) -> ParseResult
#
# The dispatch in parse_minutes() checks governing_body.lower() for each key.

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
    governing_body: Optional[str] = None,
) -> ParseResult:
    """
    Parse meeting minutes OCR text into structured vote records.

    Selects the appropriate parser based on governing_body. Falls back to the
    Shasta BOS parser if no match is found — check parse_result.parser_used and
    parse_result.parse_status to detect mismatches.

    Args:
        ocr_text:       Raw OCR text from a minutes document.
        meeting_id:     UUID of the meeting in the DB.
        document_id:    UUID of the source Document record (nullable).
        meeting_date:   ISO date string "YYYY-MM-DD".
        governing_body: Name of the governing body.

    Returns:
        ParseResult with votes, unmatched paragraphs, and parse status.
    """
    parser = _DEFAULT_PARSER
    if governing_body:
        gb_lower = governing_body.lower()
        for key, fn in PARSER_REGISTRY.items():
            if key in gb_lower:
                parser = fn
                break

    return parser(
        ocr_text=ocr_text,
        meeting_id=meeting_id,
        document_id=document_id,
        meeting_date=meeting_date,
        governing_body=governing_body,
    )

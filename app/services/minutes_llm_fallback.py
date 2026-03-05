"""
minutes_llm_fallback.py — LLM-assisted vote extraction for unmatched paragraphs.

Called by ingest_minutes_votes.py when the regex parser leaves unmatched_paragraphs.
Uses the Anthropic API (claude-haiku-4-5) to:
  1. Extract structured vote data from text the regex couldn't parse.
  2. Suggest a Python regex pattern that would match the format in future.

Suggestions are appended to scripts/minutes_parser_suggestions.txt for review
and potential addition to app/services/minutes_parser.py.

Requirements:
  ANTHROPIC_API_KEY env var (or in .env at project root).
  pip install anthropic python-dotenv

If the API key is not present the function returns an empty list and logs a warning —
it never raises so the ingest pipeline continues without it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SUGGESTIONS_FILE = Path(__file__).parent.parent.parent / "scripts" / "minutes_parser_suggestions.txt"

_SYSTEM_PROMPT = """\
You are a structured data extractor for Shasta County Board of Supervisors meeting minutes.
Given a paragraph of minutes text, extract vote information and return ONLY a JSON array.

Each element in the array represents one vote action found in the text. Fields:
  mover:            string — supervisor last name only, or null
  seconder:         string — supervisor last name only, or null
  outcome:          one of: "Unanimously Carried" | "Carried" | "Failed" | "Failed — No Second" | "Withdrawn" | "Unknown"
  vote_tally:       string like "4-1" or "5-0", or null if not stated
  item_description: string — what was being voted on, max 200 chars
  yes_members:      array of last names (empty array if unknown)
  no_members:       array of last names (empty array if none)
  suggested_regex:  a Python regex pattern (raw string, no re.compile) that would match
                    this text format so the parser can be updated to handle it without LLM.
                    Use named groups where helpful. null if no clean pattern exists.

Rules:
- If the outcome is not stated, use "Unknown"
- If only one vote is in the paragraph, return a single-element array
- Return [] if the text contains no vote action at all
- Return ONLY valid JSON. No markdown fences, no explanation.
"""


def _load_api_key() -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        return key
    # Try .env at project root
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _append_suggestion(
    meeting_date: Optional[str],
    paragraph: str,
    extracted: list[dict],
) -> None:
    """Append regex suggestions from this extraction to the suggestions file."""
    suggestions = [
        e.get("suggested_regex") for e in extracted
        if e.get("suggested_regex")
    ]
    if not suggestions:
        return
    _SUGGESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _SUGGESTIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n# {datetime.now().strftime('%Y-%m-%d %H:%M')}  meeting={meeting_date}\n")
        f.write(f"# Source text: {paragraph[:200].replace(chr(10), ' ')}\n")
        for s in suggestions:
            f.write(f"r'{s}'\n")


def extract_votes_llm(
    unmatched_paragraphs: list[str],
    meeting_id: str,
    document_id: Optional[str],
    meeting_date: Optional[str],
    group_name: Optional[str],
    members_present: list[str],
) -> list:
    """
    Send unmatched vote paragraphs to Claude and return a list of ParsedVote objects.

    Returns [] if the API key is missing or if any error occurs.
    Suggestions are always written to scripts/minutes_parser_suggestions.txt on success.
    """
    if not unmatched_paragraphs:
        return []

    api_key = _load_api_key()
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set — skipping LLM fallback for %d unmatched paragraphs. "
            "Add ANTHROPIC_API_KEY to .env to enable.",
            len(unmatched_paragraphs),
        )
        return []

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping LLM fallback")
        return []

    from app.services.minutes_parser import (
        ParsedVote,
        OUTCOME_UNANIMOUS, OUTCOME_CARRIED, OUTCOME_FAILED,
        OUTCOME_NO_SECOND, OUTCOME_WITHDRAWN,
    )

    _OUTCOME_MAP = {
        "unanimously carried": OUTCOME_UNANIMOUS,
        "carried":             OUTCOME_CARRIED,
        "failed":              OUTCOME_FAILED,
        "failed — no second":  OUTCOME_NO_SECOND,
        "failed - no second":  OUTCOME_NO_SECOND,
        "withdrawn":           OUTCOME_WITHDRAWN,
        "unknown":             OUTCOME_CARRIED,  # default to carried if unknown
    }

    client = anthropic.Anthropic(api_key=api_key)
    votes_out = []

    for para in unmatched_paragraphs:
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                temperature=0.1,
                system=_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": f"Extract votes from:\n\n{para}"}
                ],
            )
            content = response.content[0].text.strip()

            # Strip markdown fences if model added them
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

            extracted = json.loads(content)
            if not isinstance(extracted, list):
                extracted = [extracted]

            _append_suggestion(meeting_date, para, extracted)

            for item in extracted:
                if not isinstance(item, dict):
                    continue
                outcome_raw = (item.get("outcome") or "unknown").lower()
                outcome = _OUTCOME_MAP.get(outcome_raw, OUTCOME_CARRIED)
                tally = item.get("vote_tally") or None
                if tally is None and outcome == OUTCOME_UNANIMOUS:
                    tally = "5-0"

                yes_members = item.get("yes_members") or []
                no_members  = item.get("no_members") or []

                # If no member lists supplied but we know who's present and it's unanimous
                if not yes_members and outcome == OUTCOME_UNANIMOUS:
                    yes_members = list(members_present)

                votes_out.append(ParsedVote(
                    vote_id=str(uuid.uuid4()),
                    meeting_id=meeting_id,
                    document_id=document_id,
                    meeting_date=meeting_date,
                    group_name=group_name,
                    item_description=(item.get("item_description") or "")[:1000],
                    outcome=outcome,
                    vote_tally=tally,
                    mover=item.get("mover") or None,
                    seconder=item.get("seconder") or None,
                    yes_members=yes_members,
                    no_members=no_members,
                ))

            logger.info(
                "LLM fallback: extracted %d vote(s) from unmatched paragraph (meeting %s)",
                len(extracted), meeting_date,
            )

        except json.JSONDecodeError as e:
            logger.warning("LLM fallback: JSON parse error for paragraph: %s", e)
        except Exception as e:
            logger.warning("LLM fallback: error processing paragraph: %s", e)

    return votes_out

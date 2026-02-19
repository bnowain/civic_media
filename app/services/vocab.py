"""
Vocabulary hints loader.

Reads config/vocab_hints.yml and builds a compact initial_prompt string
for Whisper. Feeding proper nouns as an initial prompt significantly
improves recognition of names, places, and acronyms that Whisper hasn't
seen frequently in training.

Prompt format:
  "Public meeting transcript. Speakers and terms include: Term1, Term2, ..."

The prompt is capped at MAX_PROMPT_CHARS to stay safely within Whisper's
context window (224 tokens ≈ ~900 chars of English text). We log the
exact prompt and a hash for reproducibility.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Absolute cap on initial_prompt length (characters).
# Whisper's context prefix is 224 tokens; ~4 chars/token = ~896 chars.
# We use 850 to leave headroom for the fixed prefix text.
MAX_PROMPT_CHARS = 850

# Path to vocab hints file, relative to project root.
_VOCAB_FILE = Path(__file__).parents[2] / "config" / "vocab_hints.yml"

# Category order — terms are included in this priority order until cap is hit
_CATEGORY_ORDER = ["people", "places", "agencies", "acronyms", "programs", "other"]


def load_initial_prompt() -> str:
    """
    Load active vocab terms from config/vocab_hints.yml and build
    a Whisper initial_prompt string capped at MAX_PROMPT_CHARS.

    Returns an empty string if the file doesn't exist or has no active terms.
    """
    terms = _load_terms()

    if not terms:
        logger.info("Vocab hints: no active terms found, using no initial_prompt.")
        return ""

    prefix = "Public meeting transcript. Speakers and terms include: "
    prompt = prefix + ", ".join(terms)

    # Truncate at word boundary if over cap
    if len(prompt) > MAX_PROMPT_CHARS:
        truncated = prompt[:MAX_PROMPT_CHARS].rsplit(",", 1)[0]
        included  = truncated[len(prefix):]
        included_count = included.count(",") + 1
        logger.warning(
            "Vocab prompt truncated to %d chars (%d/%d terms fit).",
            len(truncated), included_count, len(terms),
        )
        prompt = truncated

    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
    logger.info(
        "Vocab prompt: %d terms, %d chars, hash=%s",
        len(terms), len(prompt), prompt_hash,
    )
    logger.debug("Vocab prompt text: %s", prompt)

    return prompt


def _load_terms() -> list[str]:
    """
    Parse vocab_hints.yml and return a deduplicated list of active terms
    in category-priority order.
    """
    if not _VOCAB_FILE.exists():
        logger.warning("Vocab hints file not found: %s", _VOCAB_FILE)
        return []

    try:
        import yaml
    except ImportError:
        logger.warning(
            "PyYAML not installed — vocab hints disabled. "
            "Run: pip install pyyaml"
        )
        return []

    try:
        with _VOCAB_FILE.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        logger.warning("Could not parse vocab hints file: %s", exc)
        return []

    if not data or "global_terms" not in data:
        return []

    seen: set[str] = set()
    ordered: list[str] = []

    global_terms = data["global_terms"] or {}

    for category in _CATEGORY_ORDER:
        entries = global_terms.get(category) or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            term   = entry.get("term", "").strip()
            active = entry.get("active", True)
            if term and active and term not in seen:
                seen.add(term)
                ordered.append(term)

    return ordered

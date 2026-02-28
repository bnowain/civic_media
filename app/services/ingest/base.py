"""
Base classes for the ingest system.

ScrapedEpisode — data container for a scraped episode before download.
BaseScraper    — abstract base class all scrapers implement.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScrapedEpisode:
    """Represents a single scraped episode ready for download."""
    title: str
    audio_url: str                          # direct URL to the audio file
    episode_date: str                       # YYYY-MM-DD
    source_type: str                        # "kcnr" | "securenet" | "freedominaction"
    show_name: str = ""                     # e.g. "Kevin Crye Show"
    description: Optional[str] = None       # episode notes / guest list
    thumbnail_url: Optional[str] = None     # cover art
    page_url: Optional[str] = None         # human-browsable source page URL
    duration_hint: Optional[float] = None   # seconds, if available from metadata
    extra: dict = field(default_factory=dict)  # source-specific metadata


class BaseScraper(abc.ABC):
    """Abstract base for all ingest scrapers."""

    @abc.abstractmethod
    def scrape(self, show_cutoffs: dict[str, str] | None = None) -> list[ScrapedEpisode]:
        """
        Scrape the source and return a list of ScrapedEpisode objects.

        show_cutoffs: mapping of show_name -> newest episode date already in DB
            (YYYY-MM-DD).  Scrapers use this to stop paginating once they reach
            already-known content.  None = no cutoff (full historical scrape).

        Should be idempotent — callers handle deduplication.
        """
        ...

    @property
    @abc.abstractmethod
    def source_type(self) -> str:
        """Return the source type identifier (e.g. 'kcnr')."""
        ...

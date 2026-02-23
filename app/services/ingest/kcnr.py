"""
KCNR archive scraper.

Scrapes the KCNR podcast archive pages at apps.kcnr1460.com.
Each page lists episodes with dates (h2/h3 headings) and direct MP3 links.
Pagination is via ?page=N query params.

Two archive paths:
  - /Show/archive/kevin_crye — Kevin Crye Show episodes
  - /Show/archive/poke — alternates between Kevin Crye Show and Poke the
    Hornets Nest episodes (all imported as Kevin Crye Show; user sorts manually)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapedEpisode

logger = logging.getLogger(__name__)

# Ordinal suffix patterns (1st, 2nd, 3rd, 22nd, etc.)
_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)")

# Archive paths on KCNR. The "poke" archive alternates between Kevin Crye Show
# and Poke the Hornets Nest episodes — we import all as Kevin Crye Show and
# the user manually reassigns the Poke episodes.
KCNR_SHOWS = [
    {
        "slug": "kevin_crye",
        "path": "/Show/archive/kevin_crye",
        "show_name": "Kevin Crye Show",
        "archive_label": "kevin_crye",
    },
    {
        "slug": "poke",
        "path": "/Show/archive/poke",
        "show_name": "Kevin Crye Show",
        "archive_label": "poke",
    },
]


def _parse_date(text: str) -> str | None:
    """
    Parse a date heading like 'Sunday, February 22nd, 2026' into YYYY-MM-DD.
    Returns None if parsing fails.
    """
    # Strip ordinal suffixes: "22nd" -> "22"
    cleaned = _ORDINAL_RE.sub(r"\1", text.strip())
    # Try multiple formats
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%A %B %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


class KCNRScraper(BaseScraper):
    """Scrapes KCNR podcast archive pages for MP3 episodes."""

    def __init__(self, config: dict):
        self.base_url = config.get("base_url", "https://apps.kcnr1460.com")
        self.max_pages = config.get("max_pages", 20)
        self.shows = config.get("shows", None)  # if None, scrape all

    @property
    def source_type(self) -> str:
        return "kcnr"

    def scrape(self) -> list[ScrapedEpisode]:
        episodes = []
        for show in KCNR_SHOWS:
            if self.shows and show["slug"] not in self.shows:
                continue
            episodes.extend(self._scrape_show(show))
        return episodes

    def _scrape_show(self, show: dict) -> list[ScrapedEpisode]:
        """Scrape all pages for a single show."""
        episodes = []
        show_name = show["show_name"]
        archive_label = show.get("archive_label", show["slug"])

        for page in range(1, self.max_pages + 1):
            url = f"{self.base_url}{show['path']}?page={page}"
            logger.info("  Scraping %s page %d: %s", show_name, page, url)

            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
            except Exception:
                logger.warning("  Failed to fetch page %d for %s", page, show_name)
                break

            page_episodes = self._parse_page(resp.text, show_name, archive_label)
            if not page_episodes:
                logger.info("  No more episodes on page %d", page)
                break

            episodes.extend(page_episodes)

            # Check if there's a next page
            if not self._has_next_page(resp.text):
                break

        logger.info("  Total %d episodes from %s", len(episodes), show_name)
        return episodes

    def _parse_page(self, html: str, show_name: str, archive_label: str) -> list[ScrapedEpisode]:
        """Parse a single archive page and extract episodes."""
        soup = BeautifulSoup(html, "html.parser")
        episodes = []

        # Date headings are h2 or h3 tags
        current_date = None

        for tag in soup.find_all(["h2", "h3", "a"]):
            if tag.name in ("h2", "h3"):
                parsed = _parse_date(tag.get_text(strip=True))
                if parsed:
                    current_date = parsed
            elif tag.name == "a":
                href = tag.get("href", "")
                if not href or ".mp3" not in href.lower():
                    continue

                # Normalize URL
                if href.startswith("/"):
                    href = self.base_url + href
                elif not href.startswith("http"):
                    href = self.base_url + "/" + href

                # Skip duplicates within this page (each episode listed twice)
                if any(ep.audio_url == href for ep in episodes):
                    continue

                date = current_date or "1970-01-01"
                title = f"{show_name} — {date}"

                episodes.append(ScrapedEpisode(
                    title=title,
                    audio_url=href,
                    episode_date=date,
                    source_type="kcnr",
                    show_name=show_name,
                    description=f"Source: KCNR (archive: {archive_label})",
                ))

        return episodes

    def _has_next_page(self, html: str) -> bool:
        """Check if the page has a [next] pagination link."""
        return "?page=" in html and "[next]" in html.lower() or ">next<" in html.lower()

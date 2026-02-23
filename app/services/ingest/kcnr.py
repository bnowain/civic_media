"""
KCNR archive scraper.

Scrapes the KCNR podcast archive pages at apps.kcnr1460.com.
Each page lists episodes with direct MP3 links whose filenames contain
dates (e.g. kevin_crye_2026-02-22.mp3, poke_2023-02-12.mp3).
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

# Date embedded in MP3 filenames: kevin_crye_2026-02-22.mp3, poke_2023-02-12.mp3
_FILENAME_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.mp3", re.IGNORECASE)
# Date in URL path: /media/2025/04/20/FILENAME.mp3
_URL_PATH_DATE_RE = re.compile(r"/media/(\d{4})/(\d{2})/(\d{2})/")

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
        self.cutoff_date = config.get("cutoff_date")  # YYYY-MM-DD: skip episodes >= this date

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

        # Track heading dates as fallback
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

                # Extract date from filename (e.g. kevin_crye_2026-02-22.mp3)
                date_match = _FILENAME_DATE_RE.search(href)
                if date_match:
                    date = date_match.group(1)
                else:
                    # Fallback: date from URL path (e.g. /media/2025/04/20/)
                    path_match = _URL_PATH_DATE_RE.search(href)
                    if path_match:
                        date = f"{path_match.group(1)}-{path_match.group(2)}-{path_match.group(3)}"
                    else:
                        date = current_date or "1970-01-01"

                # Skip episodes at or after the cutoff date (moved to KQMS)
                if self.cutoff_date and date >= self.cutoff_date:
                    continue

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

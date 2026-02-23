"""
Freedom in Action metadata scraper.

freedominactionradio.com is a metadata-only source (blog posts with show
descriptions, guest names, etc.). The actual audio lives on KQMS's SecureNet
infrastructure and is picked up by the SecureNet scraper.

This scraper attempts to enrich SecureNet episodes with metadata from the blog.
If the blog is unreachable (it's been flaky), it returns an empty list
and logs a warning — no episodes are lost since SecureNet handles audio.

Strategy:
1. Scrape the blog for post titles, dates, and descriptions
2. Return ScrapedEpisode objects with metadata but NO audio_url
3. The orchestrator uses these to enrich existing SecureNet-sourced meetings
   that match on date + show name
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapedEpisode

logger = logging.getLogger(__name__)


class FreedomInActionScraper(BaseScraper):
    """
    Scrapes Freedom in Action blog for metadata.

    Since the blog has no audio, this scraper returns empty episodes
    (audio_url="") that the orchestrator uses for metadata enrichment only.
    The SecureNet scraper handles the actual audio discovery.
    """

    def __init__(self, config: dict):
        self.blog_url = config.get("blog_url", "https://www.freedominactionradio.com/blog")
        self.max_pages = config.get("max_pages", 5)

    @property
    def source_type(self) -> str:
        return "freedominaction"

    def scrape(self) -> list[ScrapedEpisode]:
        """
        Attempt to scrape blog metadata. Returns empty list on failure
        (graceful degradation — audio comes from SecureNet).
        """
        episodes = []

        for page in range(1, self.max_pages + 1):
            url = self.blog_url if page == 1 else f"{self.blog_url}?page={page}"
            logger.info("  Fetching Freedom in Action blog page %d: %s", page, url)

            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code == 404:
                    logger.info("  Blog page %d returned 404 — stopping", page)
                    break
                resp.raise_for_status()
            except Exception:
                logger.warning(
                    "  Freedom in Action blog unreachable (page %d). "
                    "This is expected — audio is handled by SecureNet scraper.",
                    page,
                )
                break

            page_episodes = self._parse_blog_page(resp.text)
            if not page_episodes:
                break
            episodes.extend(page_episodes)

        logger.info("  Found %d blog entries from Freedom in Action", len(episodes))
        return episodes

    def _parse_blog_page(self, html: str) -> list[ScrapedEpisode]:
        """Parse a blog page for episode metadata."""
        soup = BeautifulSoup(html, "html.parser")
        episodes = []

        # Squarespace blog structure: look for article elements or blog-item classes
        # Also try generic article/post selectors
        articles = (
            soup.select("article.blog-item")
            or soup.select("article")
            or soup.select(".blog-item")
            or soup.select(".post")
        )

        for article in articles:
            # Extract title
            title_el = (
                article.select_one("h1 a, h2 a, h3 a, .blog-title a, .entry-title a")
                or article.select_one("h1, h2, h3, .blog-title, .entry-title")
            )
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            # Extract date
            date_el = article.select_one("time, .blog-date, .entry-date, .post-date")
            date_str = ""
            if date_el:
                date_str = date_el.get("datetime", "") or date_el.get_text(strip=True)

            episode_date = self._parse_date(date_str)

            # Extract description/excerpt
            desc_el = article.select_one(
                ".blog-excerpt, .entry-summary, .post-excerpt, p"
            )
            description = desc_el.get_text(strip=True) if desc_el else ""

            # Extract thumbnail
            img_el = article.select_one("img")
            thumbnail = img_el.get("src", "") if img_el else ""

            episodes.append(ScrapedEpisode(
                title=f"Freedom in Action — {title}",
                audio_url="",  # No audio on blog — SecureNet handles this
                episode_date=episode_date,
                source_type="freedominaction",
                show_name="Freedom in Action",
                description=description[:500] if description else None,
                thumbnail_url=thumbnail or None,
            ))

        return episodes

    def _parse_date(self, date_str: str) -> str:
        """Try various date formats and return YYYY-MM-DD."""
        if not date_str:
            return "1970-01-01"

        # ISO format already
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            return date_str[:10]

        # Try common blog date formats
        for fmt in (
            "%B %d, %Y",   # "February 22, 2026"
            "%b %d, %Y",   # "Feb 22, 2026"
            "%m/%d/%Y",    # "02/22/2026"
            "%m/%d/%y",    # "02/22/26"
        ):
            try:
                return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

        return "1970-01-01"

"""
Freedom in Action Squarespace scraper.

The blog at freedominactionradio.com/blog-news hosts audio directly via
Squarespace audio embeds (div.sqs-audio-embed). Each blog post corresponds
to one hour-long segment:

  - Blog post h1 title  → guest / topic name (e.g. "Dr Mohammed Khan")
  - data-url            → direct Squarespace CDN MP3 URL
  - data-duration-in-ms → duration in milliseconds
  - <time datetime="">  → episode date (YYYY-MM-DD)

Pagination uses ?offset= (not ?page=).  Each Saturday show publishes 2
blog posts (Hour One and Hour Two), each with its own audio file.
"""

from __future__ import annotations

import logging
import re

import requests
from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapedEpisode

logger = logging.getLogger(__name__)

_HOUR_RE = re.compile(r'hour\s+(one|two|1|2)', re.IGNORECASE)
_HOUR_MAP = {"one": "Hour One", "1": "Hour One", "two": "Hour Two", "2": "Hour Two"}


def _extract_hour_label(audio_title: str) -> str:
    """Return 'Hour One' / 'Hour Two' from the audio embed's data-title."""
    m = _HOUR_RE.search(audio_title)
    if m:
        return _HOUR_MAP.get(m.group(1).lower(), "")
    return ""


class FreedomInActionScraper(BaseScraper):
    """
    Scrapes Freedom in Action blog for audio episodes.

    Each blog post embeds a Squarespace audio player pointing to a direct
    MP3 file on Squarespace's CDN.  Returns real ScrapedEpisode objects
    with audio_url set so they go through the normal download pipeline.
    """

    def __init__(self, config: dict):
        self.blog_url = config.get(
            "blog_url", "https://www.freedominactionradio.com/blog-news"
        )
        self.max_pages = config.get("max_pages", 10)

    @property
    def source_type(self) -> str:
        return "freedominaction"

    def scrape(self, show_cutoffs: dict[str, str] | None = None) -> list[ScrapedEpisode]:
        stop_after_date = (show_cutoffs or {}).get("Freedom in Action")

        episodes: list[ScrapedEpisode] = []
        url: str | None = self.blog_url
        pages_fetched = 0

        while url and pages_fetched < self.max_pages:
            logger.info("  Fetching Freedom in Action blog (page %d): %s", pages_fetched + 1, url)
            try:
                resp = requests.get(url, timeout=20)
                if resp.status_code == 404:
                    logger.info("  Blog returned 404 — stopping")
                    break
                resp.raise_for_status()
            except Exception:
                logger.warning("  Freedom in Action blog unreachable: %s", url, exc_info=True)
                break

            page_episodes, next_path = self._parse_page(resp.text)
            pages_fetched += 1

            if not page_episodes:
                break  # empty page — stop paginating

            # Early stop: keep episodes on or after the cutoff date; stop when
            # we see content strictly before it.  Using >= (not >) so that shows
            # publishing multiple episodes per day (Hour One / Hour Two) don't
            # lose a second episode whose date equals the cutoff.  URL-based
            # dedup in run_ingest() will skip anything already downloaded.
            if stop_after_date:
                new_episodes = [ep for ep in page_episodes if ep.episode_date >= stop_after_date]
                episodes.extend(new_episodes)
                if len(new_episodes) < len(page_episodes):
                    logger.info(
                        "  Early stop on page %d: %d new, %d before cutoff %s",
                        pages_fetched, len(new_episodes),
                        len(page_episodes) - len(new_episodes), stop_after_date,
                    )
                    break
            else:
                episodes.extend(page_episodes)

            if next_path:
                # next_path is relative: "/blog-news?offset=..."
                base = self.blog_url.split("/blog-news")[0]
                url = base + next_path if not next_path.startswith("http") else next_path
            else:
                break

        logger.info("  Found %d audio episodes from Freedom in Action", len(episodes))
        return episodes

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse_page(self, html: str) -> tuple[list[ScrapedEpisode], str | None]:
        """Parse one blog listing page. Returns (episodes, next_page_path)."""
        soup = BeautifulSoup(html, "html.parser")
        episodes: list[ScrapedEpisode] = []

        for article in soup.select("article"):
            # ---- blog post title ----
            title_el = article.select_one("h1.entry-title a, h1 a, h1")
            blog_title = title_el.get_text(strip=True) if title_el else ""
            # Don't skip posts with empty titles — use date as fallback below

            # ---- date ----
            time_el = article.select_one("time.dt-published")
            date_str = (time_el.get("datetime", "") or "")[:10] if time_el else ""
            if not date_str:
                date_str = "1970-01-01"

            # ---- page URL ----
            dateline_link = article.select_one("time.dt-published a")
            post_path = dateline_link.get("href", "") if dateline_link else ""
            if post_path and not post_path.startswith("http"):
                base = self.blog_url.split("/blog-news")[0]
                post_url = base + post_path
            elif post_path:
                post_url = post_path
            else:
                post_url = self.blog_url

            # ---- audio embeds (usually one per article) ----
            for embed in article.select("div.sqs-audio-embed[data-url]"):
                audio_url = embed.get("data-url", "").strip()
                if not audio_url or not audio_url.startswith("http"):
                    continue

                audio_data_title = embed.get("data-title", "").strip()
                duration_ms_str = embed.get("data-duration-in-ms", "")
                try:
                    duration_secs = float(duration_ms_str) / 1000.0 if duration_ms_str else None
                except (ValueError, TypeError):
                    duration_secs = None

                # Build episode title
                hour_label = _extract_hour_label(audio_data_title)
                # Use blog title, fall back to date + audio data-title for older posts
                if blog_title:
                    if hour_label:
                        full_title = f"Freedom in Action — {blog_title} ({hour_label})"
                    else:
                        full_title = f"Freedom in Action — {blog_title}"
                elif audio_data_title:
                    full_title = f"Freedom in Action — {audio_data_title}"
                else:
                    full_title = f"Freedom in Action — {date_str}"

                episodes.append(ScrapedEpisode(
                    title=full_title,
                    audio_url=audio_url,
                    episode_date=date_str,
                    source_type="freedominaction",
                    show_name="Freedom in Action",
                    description=f"Guest/Topic: {blog_title}",
                    thumbnail_url=None,
                    page_url=post_url,
                    duration_hint=duration_secs,
                ))

        # ---- pagination: "Older Posts" link ----
        next_link = soup.select_one("a[rel='next']")
        next_path = next_link.get("href") if next_link else None
        return episodes, next_path

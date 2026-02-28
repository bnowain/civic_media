"""
Podbean RSS feed scraper.

Fetches episodes from a Podbean-hosted podcast via RSS/XML feed.
Used as a backup source for Kevin Crye Show episodes that don't
always appear on KQMS SecureNet.

Feed URL pattern: https://feed.podbean.com/{slug}/feed.xml
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests

from .base import BaseScraper, ScrapedEpisode

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = (15, 60)


def _parse_duration(raw: str | None) -> float | None:
    """Parse itunes:duration — could be seconds or HH:MM:SS."""
    if not raw:
        return None
    raw = raw.strip()
    # Pure seconds
    if raw.isdigit():
        return float(raw)
    # HH:MM:SS or MM:SS
    parts = raw.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (ValueError, TypeError):
        pass
    return None


class PodbeamScraper(BaseScraper):
    """Fetches episodes from a Podbean RSS feed."""

    def __init__(self, config: dict):
        self.feed_url = config.get(
            "feed_url", "https://feed.podbean.com/admind4/feed.xml"
        )
        self.show_name = config.get("show_name", "Kevin Crye Show")

    @property
    def source_type(self) -> str:
        return "podbean"

    def scrape(self, show_cutoffs: dict[str, str] | None = None) -> list[ScrapedEpisode]:
        logger.info("  Fetching Podbean RSS feed: %s", self.feed_url)
        try:
            resp = requests.get(self.feed_url, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
        except Exception:
            logger.exception("  Failed to fetch Podbean feed")
            return []

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            logger.exception("  Failed to parse Podbean RSS XML")
            return []

        # Namespace map for itunes tags
        ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}

        channel = root.find("channel")
        if channel is None:
            logger.warning("  No <channel> in RSS feed")
            return []

        # Channel-level thumbnail (fallback)
        channel_image = None
        img_el = channel.find("itunes:image", ns)
        if img_el is not None:
            channel_image = img_el.get("href")

        stop_after_date = (show_cutoffs or {}).get(self.show_name)

        episodes = []
        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()

            # Audio URL from enclosure
            enclosure = item.find("enclosure")
            if enclosure is None:
                continue
            audio_url = enclosure.get("url", "")
            if not audio_url:
                continue

            # Parse date from pubDate (RFC 2822)
            pub_date_str = item.findtext("pubDate", "")
            episode_date = "1970-01-01"
            if pub_date_str:
                try:
                    dt = parsedate_to_datetime(pub_date_str)
                    episode_date = dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    pass

            # Duration
            duration_raw = item.findtext("itunes:duration", None, ns)
            duration = _parse_duration(duration_raw)

            # Description
            description = (
                item.findtext("description", "")
                or item.findtext("itunes:summary", "", ns)
            ).strip()
            # Strip HTML tags from description (Podbean often wraps in <p>)
            if description.startswith("<"):
                import re
                description = re.sub(r"<[^>]+>", "", description).strip()

            # Episode thumbnail
            ep_image = None
            ep_img_el = item.find("itunes:image", ns)
            if ep_img_el is not None:
                ep_image = ep_img_el.get("href")
            thumbnail = ep_image or channel_image

            # Per-episode page URL from RSS <link>
            episode_link = (item.findtext("link") or "").strip() or None

            # RSS is sorted newest-first — stop as soon as we hit the cutoff.
            if stop_after_date and episode_date <= stop_after_date:
                logger.info(
                    "  Early stop: episode date %s at/before cutoff %s",
                    episode_date, stop_after_date,
                )
                break

            episodes.append(ScrapedEpisode(
                title=title,
                audio_url=audio_url,
                episode_date=episode_date,
                source_type="podbean",
                show_name=self.show_name,
                description=description or None,
                thumbnail_url=thumbnail,
                page_url=episode_link,
                duration_hint=duration,
                extra={"feed_url": self.feed_url},
            ))

        logger.info("  Parsed %d episodes from Podbean feed", len(episodes))
        return episodes

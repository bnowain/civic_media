"""
Granicus scraper — City of Redding public meeting archive.

Parses the ViewPublisher HTML listing to enumerate all clip entries, then
fetches each MediaPlayer page to extract authoritative title/date/MP4 URL.
Follows Granicus viewer redirects to resolve actual PDF download URLs.

No authentication required — all public portal endpoints.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, parse_qs

import httpx

logger = logging.getLogger(__name__)

GRANICUS_BASE = "https://reddingca.granicus.com"
VIEW_ID = 4
CITY_COUNCIL_NAME = "Redding City Council"

# MP4 URL pattern (appears in downloadLinks JS variable in MediaPlayer HTML)
_MP4_PATTERN = re.compile(
    r'https://archive-video\.granicus\.com/reddingca/[^"\'>\s]+\.mp4'
)

# AgendaViewer link: AgendaViewer.php?view_id=4&clip_id=NNN
_AGENDA_HREF_PATTERN = re.compile(
    r'AgendaViewer\.php\?[^"\']*clip_id=(\d+)[^"\']*'
)

# MinutesViewer link (includes doc_id UUID): MinutesViewer.php?view_id=4&clip_id=NNN&doc_id=UUID
_MINUTES_HREF_PATTERN = re.compile(
    r'(MinutesViewer\.php\?[^"\']*clip_id=(\d+)[^"\']*doc_id=[^"\']+)'
)

# MediaPlayer page <title> typically reads:
#   "Jan 24, 2022 5:15 PM - Redding City Council - Special Meeting"
_TITLE_TAG_PATTERN = re.compile(r'<title[^>]*>([^<]+)</title>', re.IGNORECASE)

# After splitting on first " - ": date portion format
_DATE_FORMATS = ["%b %d, %Y %I:%M %p", "%b %d, %Y %H:%M"]


@dataclass
class GranicusClip:
    """All metadata for a single Granicus meeting clip."""
    clip_id: int
    title: str                        # Human-readable: "Redding City Council - Regular Meeting"
    meeting_date: str                 # ISO: YYYY-MM-DD
    group_name: str                   # "Redding City Council"
    meeting_type: str                 # "Regular Meeting" | "Special Meeting" | …
    mp4_url: Optional[str] = None     # Direct MP4 at archive-video.granicus.com
    agenda_url: Optional[str] = None  # Resolved DocumentViewer.php PDF URL
    minutes_url: Optional[str] = None # Resolved DocumentViewer.php PDF URL
    page_url: str = ""                # Human-browsable MediaPlayer URL


class GranicusScraper:
    """Scraper for the City of Redding Granicus portal (view_id=4)."""

    def __init__(
        self,
        base_url: str = GRANICUS_BASE,
        request_delay: float = 0.5,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.delay = request_delay
        self.timeout = timeout

    def _get(self, url: str, follow_redirects: bool = True) -> httpx.Response:
        """HTTP GET with rate-limit delay."""
        time.sleep(self.delay)
        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=follow_redirects,
            headers={"User-Agent": "CivicMediaArchive/1.0"},
        ) as client:
            return client.get(url)

    # ── ViewPublisher: enumerate all clip IDs ─────────────────────────────────

    def list_clip_ids(self) -> dict[int, dict]:
        """
        Scrape ViewPublisher HTML to get all clip_ids with their viewer URLs.

        Returns:
            {clip_id: {"agenda_viewer_url": str|None, "minutes_viewer_url": str|None}}
        """
        url = f"{self.base_url}/ViewPublisher.php?view_id={VIEW_ID}"
        logger.info("Fetching ViewPublisher: %s", url)
        r = self._get(url)
        r.raise_for_status()
        html = r.text

        result: dict[int, dict] = {}

        # Every AgendaViewer link defines a clip_id that has documents
        for match in _AGENDA_HREF_PATTERN.finditer(html):
            href = match.group(0)
            clip_id = int(match.group(1))
            if clip_id not in result:
                result[clip_id] = {
                    "agenda_viewer_url": f"{self.base_url}/{href}",
                    "minutes_viewer_url": None,
                }

        # MinutesViewer links (not all clips have them — posted ~3-4 weeks later)
        for match in _MINUTES_HREF_PATTERN.finditer(html):
            href = match.group(1)
            clip_id = int(match.group(2))
            if clip_id in result:
                result[clip_id]["minutes_viewer_url"] = f"{self.base_url}/{href}"

        logger.info("ViewPublisher: %d clip entries found", len(result))
        return result

    # ── MediaPlayer: get title, date, MP4 URL ─────────────────────────────────

    def get_clip_details(self, clip_id: int) -> Optional[GranicusClip]:
        """
        Fetch the MediaPlayer page for a clip.

        Extracts meeting title, date, group name, meeting type, and MP4 URL
        from the page's <title> tag and embedded downloadLinks JS variable.
        Returns None if the clip doesn't exist or title can't be parsed.
        """
        page_url = f"{self.base_url}/MediaPlayer.php?view_id={VIEW_ID}&clip_id={clip_id}"
        try:
            r = self._get(page_url)
        except httpx.HTTPError as exc:
            logger.warning("clip %d: HTTP error: %s", clip_id, exc)
            return None

        if r.status_code != 200:
            logger.debug("clip %d: HTTP %d — skipping", clip_id, r.status_code)
            return None

        html = r.text

        # MP4 URL from downloadLinks JS variable
        mp4_match = _MP4_PATTERN.search(html)
        mp4_url = mp4_match.group(0) if mp4_match else None

        # Title from <title> tag
        title_match = _TITLE_TAG_PATTERN.search(html)
        raw_title = title_match.group(1).strip() if title_match else ""

        # Granicus <title> format:
        #   "Jan 24, 2022 5:15 PM - Redding City Council - Special Meeting"
        #   "Mar 1, 2022 6:00 PM - Redding City Council - Regular Meeting"
        # Split on first " - " to separate datetime from the rest
        if " - " not in raw_title:
            logger.debug("clip %d: unexpected title %r", clip_id, raw_title)
            return None

        date_part, rest = raw_title.split(" - ", 1)
        date_part = date_part.strip()
        rest = rest.strip()

        # rest is "Redding City Council - Regular Meeting" or "Redding City Council - Special Meeting"
        if " - " in rest:
            group_name, meeting_type = rest.split(" - ", 1)
        else:
            group_name = rest
            meeting_type = "Meeting"

        group_name = group_name.strip()
        meeting_type = meeting_type.strip()

        # Parse date from date_part (e.g. "Jan 24, 2022 5:15 PM")
        meeting_date = None
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(date_part, fmt)
                meeting_date = dt.strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

        if not meeting_date:
            logger.debug("clip %d: cannot parse date %r", clip_id, date_part)
            return None

        return GranicusClip(
            clip_id=clip_id,
            title=f"{group_name} - {meeting_type}",
            meeting_date=meeting_date,
            group_name=group_name,
            meeting_type=meeting_type,
            mp4_url=mp4_url,
            page_url=page_url,
        )

    # ── Document URL resolution ───────────────────────────────────────────────

    def resolve_document_url(self, viewer_url: str) -> Optional[str]:
        """
        Follow the Granicus viewer redirect to get the actual PDF URL.

        AgendaViewer/MinutesViewer → 302 → Google viewer with encoded URL
        → extract inner DocumentViewer.php URL (this serves the PDF directly).

        Returns the DocumentViewer URL or None if resolution fails.
        """
        try:
            r = self._get(viewer_url, follow_redirects=False)
        except httpx.HTTPError as exc:
            logger.warning("resolve_document_url: HTTP error for %s: %s", viewer_url, exc)
            return None

        if r.status_code not in (301, 302):
            # Some clips return the PDF directly without redirect
            if "pdf" in r.headers.get("content-type", "").lower():
                return viewer_url
            logger.debug("resolve_document_url: non-redirect %d for %s", r.status_code, viewer_url)
            return None

        location = r.headers.get("location", "")

        # Google viewer pattern: https://docs.google.com/gview?url=<encoded_granicus_url>
        if "docs.google.com/gview" in location or "google.com/gview" in location:
            try:
                parsed = urlparse(location)
                qs = parse_qs(parsed.query)
                if "url" in qs:
                    return qs["url"][0]  # parse_qs URL-decodes automatically
            except Exception as exc:
                logger.warning("Could not parse Google viewer URL %r: %s", location, exc)
            return None

        # Direct redirect to a PDF
        if location.endswith(".pdf") or ".pdf?" in location:
            if location.startswith("http"):
                return location
            return f"{self.base_url}{location}"

        # Any other absolute redirect
        if location.startswith("http"):
            return location

        logger.debug("resolve_document_url: unrecognized redirect %r", location)
        return None

    # ── High-level discovery ──────────────────────────────────────────────────

    def discover(
        self,
        min_date: str = "2022-01-01",
        group_filter: str = CITY_COUNCIL_NAME,
    ) -> list[GranicusClip]:
        """
        Full discovery: enumerate clips, filter to City Council since min_date,
        resolve agenda and minutes PDF URLs.

        Returns list of GranicusClip sorted by meeting_date ascending.
        """
        # Step 1: Get clip_id → viewer URLs from ViewPublisher (one request)
        clip_map = self.list_clip_ids()
        logger.info("Total clips in ViewPublisher: %d", len(clip_map))

        clips: list[GranicusClip] = []
        total = len(clip_map)

        for idx, (clip_id, urls) in enumerate(sorted(clip_map.items()), 1):
            # Step 2: Fetch MediaPlayer for metadata (one request per clip)
            logger.info("[%d/%d] Checking clip %d…", idx, total, clip_id)
            clip = self.get_clip_details(clip_id)

            if clip is None:
                continue

            # Filter: group name
            if group_filter and group_filter not in clip.group_name:
                logger.debug("clip %d: skip — group=%r", clip_id, clip.group_name)
                continue

            # Filter: date range
            if clip.meeting_date < min_date:
                logger.debug("clip %d: skip — date=%s < %s", clip_id, clip.meeting_date, min_date)
                continue

            # Step 3: Resolve document URLs (one request each, only when needed)
            if urls.get("agenda_viewer_url"):
                clip.agenda_url = self.resolve_document_url(urls["agenda_viewer_url"])
                if not clip.agenda_url:
                    logger.warning("clip %d: could not resolve agenda URL", clip_id)

            if urls.get("minutes_viewer_url"):
                clip.minutes_url = self.resolve_document_url(urls["minutes_viewer_url"])

            logger.info(
                "  → %s %s | agenda=%s | minutes=%s | mp4=%s",
                clip.meeting_date,
                clip.meeting_type,
                "✓" if clip.agenda_url else "✗",
                "✓" if clip.minutes_url else "✗",
                "✓" if clip.mp4_url else "✗",
            )
            clips.append(clip)

        clips.sort(key=lambda c: c.meeting_date)
        logger.info(
            "Discovery complete: %d City Council meetings since %s", len(clips), min_date
        )
        return clips

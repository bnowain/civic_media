"""
SecureNet on-demand audio scraper.

Parses the inline JavaScript `onDemandQueue` array from SecureNet radio
player pages (e.g. radio.securenetsystems.net/v5/KQMS).

The JS format is:
    var streamRoot = "https://ice42.securenetsystems.net/media";
    var stationCallSign = "KQMS";
    onDemandQueue[0] = new Object();
    onDemandQueue[0].fileId = 3041794;
    onDemandQueue[0].fileName = streamRoot+"/"+stationCallSign+"/ondemand/"+"file.m4a";
    onDemandQueue[0].title = "Show Name - Sunday 2/22/2026";
    onDemandQueue[0].duration = "3599.929";
    onDemandQueue[0].coverArt = "https://...jpg";
    ...

We parse this with regex — no JS engine needed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import requests

from .base import BaseScraper, ScrapedEpisode

logger = logging.getLogger(__name__)

# Regex patterns for parsing the JS
_STREAM_ROOT_RE = re.compile(r'var\s+streamRoot\s*=\s*"([^"]+)"')
_STATION_RE = re.compile(r'var\s+stationCallSign\s*=\s*"([^"]+)"')
_NEW_OBJ_RE = re.compile(r'onDemandQueue\[(\d+)\]\s*=\s*new\s+Object')
_FIELD_RE = re.compile(
    r'onDemandQueue\[(\d+)\]\.(\w+)\s*=\s*'
    r'(?:'
    r'(?:streamRoot\s*\+\s*"/"\s*\+\s*stationCallSign\s*\+\s*"/ondemand/"\s*\+\s*"([^"]*)")'  # fileName with streamRoot
    r'|"([^"]*)"'  # plain string
    r'|(\d+(?:\.\d+)?)'  # number
    r'|(true|false)'  # boolean
    r"|'([^']*)'"  # single-quoted string
    r')'
)

# Date patterns found in SecureNet titles
_DATE_PATTERNS = [
    # "Sunday 2/22/2026", "Saturday 1/10/24"
    re.compile(r'(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat)\w*\s+(\d{1,2})/(\d{1,2})/(\d{2,4})'),
    # "2-22-2026", "1-25-24"
    re.compile(r'(\d{1,2})-(\d{1,2})-(\d{2,4})'),
]

# Also try extracting from filenames like "Hornetsnest-2026-02-22.m4a"
_FILENAME_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})\.\w+$')
# Filename with short dates: "Kevin-Crye-2-15-24.m4a"
_FILENAME_SHORT_DATE_RE = re.compile(r'(\d{1,2})-(\d{1,2})-(\d{2,4})\.\w+$')


def _normalize_year(y: str) -> int:
    """Convert 2-digit year to 4-digit."""
    yr = int(y)
    if yr < 100:
        yr += 2000
    return yr


def _extract_date(title: str, filename: str) -> str:
    """Try to extract a YYYY-MM-DD date from title or filename."""
    # Try title patterns first
    for pat in _DATE_PATTERNS:
        m = pat.search(title)
        if m:
            month, day, year = m.group(1), m.group(2), m.group(3)
            yr = _normalize_year(year)
            return f"{yr:04d}-{int(month):02d}-{int(day):02d}"

    # Try filename ISO date
    m = _FILENAME_DATE_RE.search(filename)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # Try filename short date
    m = _FILENAME_SHORT_DATE_RE.search(filename)
    if m:
        month, day, year = m.group(1), m.group(2), m.group(3)
        yr = _normalize_year(year)
        return f"{yr:04d}-{int(month):02d}-{int(day):02d}"

    return "1970-01-01"


def _guess_show_name(title: str, filename: str) -> str:
    """Guess the show name from the title or filename."""
    lower_title = title.lower()
    lower_file = filename.lower()

    if "hornet" in lower_title or "hornet" in lower_file:
        return "Poke the Hornets Nest"
    elif "kevin crye" in lower_title or "kevin-crye" in lower_file:
        return "Kevin Crye Show"
    elif "freedom" in lower_title or "kelly frost" in lower_title:
        return "Freedom in Action"
    elif "global alert" in lower_title:
        return "Global Alert News"
    else:
        return ""


class SecureNetScraper(BaseScraper):
    """Parses SecureNet radio player pages for on-demand audio entries."""

    def __init__(self, config: dict):
        self.station_id = config.get("station_id", "KQMS")
        self.station_url = config.get(
            "station_url",
            f"https://radio.securenetsystems.net/v5/{self.station_id}",
        )
        self.target_shows = [s.lower() for s in config.get("shows", [])]

    @property
    def source_type(self) -> str:
        return "securenet"

    def scrape(self) -> list[ScrapedEpisode]:
        logger.info("  Fetching SecureNet page: %s", self.station_url)
        try:
            resp = requests.get(self.station_url, timeout=30)
            resp.raise_for_status()
        except Exception:
            logger.exception("  Failed to fetch SecureNet page")
            return []

        html = resp.text

        # Extract streamRoot and stationCallSign
        stream_root_m = _STREAM_ROOT_RE.search(html)
        station_m = _STATION_RE.search(html)

        stream_root = stream_root_m.group(1) if stream_root_m else "https://ice42.securenetsystems.net/media"
        station = station_m.group(1) if station_m else self.station_id

        # Parse all onDemandQueue entries
        entries: dict[int, dict] = {}
        for m in _NEW_OBJ_RE.finditer(html):
            idx = int(m.group(1))
            entries[idx] = {}

        for m in _FIELD_RE.finditer(html):
            idx = int(m.group(1))
            field = m.group(2)
            # Value is in one of the capture groups
            if m.group(3) is not None:
                # fileName with streamRoot interpolation
                value = f"{stream_root}/{station}/ondemand/{m.group(3)}"
            elif m.group(4) is not None:
                value = m.group(4)
            elif m.group(5) is not None:
                value = float(m.group(5)) if "." in m.group(5) else int(m.group(5))
            elif m.group(6) is not None:
                value = m.group(6) == "true"
            elif m.group(7) is not None:
                value = m.group(7)
            else:
                continue

            if idx not in entries:
                entries[idx] = {}
            entries[idx][field] = value

        logger.info("  Parsed %d on-demand entries from SecureNet", len(entries))

        # Convert to ScrapedEpisode objects, filtering by target shows
        episodes = []
        for idx in sorted(entries.keys()):
            entry = entries[idx]
            filename = entry.get("fileName", "")
            title = entry.get("title", "")
            duration_str = entry.get("duration", "")
            cover_art = entry.get("coverArt", "")
            file_dsp = entry.get("fileDsp", "")

            if not filename or not filename.startswith("http"):
                continue

            show_name = _guess_show_name(title, filename)

            # Filter by target shows if configured
            if self.target_shows:
                if not any(t in show_name.lower() for t in self.target_shows):
                    continue

            date = _extract_date(title, filename)

            try:
                duration = float(duration_str) if duration_str else None
            except (ValueError, TypeError):
                duration = None

            episodes.append(ScrapedEpisode(
                title=title or file_dsp or filename.split("/")[-1],
                audio_url=filename,
                episode_date=date,
                source_type="securenet",
                show_name=show_name,
                description=f"Source: {station} (SecureNet on-demand)",
                thumbnail_url=cover_art or None,
                page_url=self.station_url,
                duration_hint=duration,
                extra={"file_id": entry.get("fileId"), "station": station},
            ))

        logger.info("  %d episodes after show filter", len(episodes))
        return episodes

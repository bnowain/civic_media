"""
Radio show ingest system.

Orchestrates scraping, downloading, and creating unprocessed Meeting records
from external radio show sources.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.config import BASE_DIR
from .base import ScrapedEpisode
from .downloader import download_episode

logger = logging.getLogger(__name__)

PROGRESS_PATH = BASE_DIR / "media" / "ingest_progress.json"


def _write_progress(**kwargs):
    """Write ingest progress to a JSON file for polling."""
    PROGRESS_PATH.write_text(json.dumps(kwargs))


def _clear_progress():
    if PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()


def _get_scraper(source: models.IngestSource):
    """Instantiate the correct scraper for a source."""
    config = json.loads(source.config_json) if source.config_json else {}

    if source.source_type == "kcnr":
        from .kcnr import KCNRScraper
        return KCNRScraper(config)
    elif source.source_type == "securenet":
        from .securenet import SecureNetScraper
        return SecureNetScraper(config)
    elif source.source_type == "freedominaction":
        from .freedominaction import FreedomInActionScraper
        return FreedomInActionScraper(config)
    else:
        raise ValueError(f"Unknown source type: {source.source_type}")


def _enrich_from_metadata(db: Session, metadata_episodes: list[ScrapedEpisode]):
    """
    Enrich existing meetings with metadata from blog-only sources.
    Matches by date + show name (Freedom in Action).
    """
    for ep in metadata_episodes:
        if ep.episode_date == "1970-01-01":
            continue

        # Find matching meetings by date that look like Freedom in Action
        matches = (
            db.query(models.Meeting)
            .filter(
                models.Meeting.meeting_date == ep.episode_date,
                models.Meeting.category == "audio",
            )
            .all()
        )

        for meeting in matches:
            title_lower = (meeting.title or "").lower()
            if "freedom" in title_lower or "kelly frost" in title_lower:
                if ep.description and not meeting.description:
                    meeting.description = ep.description
                if ep.thumbnail_url and not meeting.thumbnail_url:
                    meeting.thumbnail_url = ep.thumbnail_url

    db.commit()


def run_ingest(db: Session, source_id: str | None = None):
    """
    Run the ingest pipeline for one or all enabled sources.

    Steps:
    1. Load source configs from DB
    2. Scrape each source for episodes
    3. Deduplicate against existing meetings (by source_url)
    4. Download audio and create unprocessed Meeting + MediaFile records
    """
    if source_id:
        sources = [db.query(models.IngestSource).filter_by(
            source_id=source_id, enabled=True
        ).first()]
        sources = [s for s in sources if s]
    else:
        sources = db.query(models.IngestSource).filter_by(enabled=True).all()

    if not sources:
        logger.warning("No enabled ingest sources found")
        _write_progress(running=False, error="No enabled sources")
        return

    total_found = 0
    total_downloaded = 0

    for source in sources:
        logger.info("Ingesting source: %s (%s)", source.name, source.source_type)
        _write_progress(
            running=True,
            source_id=source.source_id,
            stage=f"Scraping {source.name}...",
            episodes_found=total_found,
            episodes_downloaded=total_downloaded,
        )

        try:
            scraper = _get_scraper(source)
            episodes = scraper.scrape()
            logger.info("  Found %d episodes from %s", len(episodes), source.name)
        except Exception:
            logger.exception("  Failed to scrape %s", source.name)
            continue

        # Separate episodes with audio from metadata-only (Freedom in Action blog)
        audio_episodes = [ep for ep in episodes if ep.audio_url]
        metadata_episodes = [ep for ep in episodes if not ep.audio_url]

        # Deduplicate audio episodes against existing source_urls
        existing_urls = set()
        if audio_episodes:
            urls = [ep.audio_url for ep in audio_episodes]
            rows = (
                db.query(models.Meeting.source_url)
                .filter(models.Meeting.source_url.in_(urls))
                .all()
            )
            existing_urls = {r[0] for r in rows}

        new_episodes = [ep for ep in audio_episodes if ep.audio_url not in existing_urls]
        total_found += len(audio_episodes)

        logger.info("  %d new episodes (skipping %d duplicates)",
                     len(new_episodes), len(audio_episodes) - len(new_episodes))

        # Download new audio episodes
        for i, episode in enumerate(new_episodes):
            _write_progress(
                running=True,
                source_id=source.source_id,
                stage=f"Downloading {i + 1}/{len(new_episodes)}: {episode.title[:60]}",
                episodes_found=total_found,
                episodes_downloaded=total_downloaded,
            )

            try:
                download_episode(db, episode)
                total_downloaded += 1
            except Exception:
                logger.exception("  Failed to download: %s", episode.audio_url)

        # Enrich existing meetings with metadata-only episodes (cross-reference by date)
        if metadata_episodes:
            _enrich_from_metadata(db, metadata_episodes)
            logger.info("  Enriched meetings with %d metadata entries", len(metadata_episodes))

        # Update source metadata
        source.last_scraped_at = datetime.utcnow()
        source.last_scraped_count = len(episodes)
        db.commit()

    _write_progress(
        running=False,
        episodes_found=total_found,
        episodes_downloaded=total_downloaded,
        stage="Complete",
    )
    logger.info("Ingest complete: found %d, downloaded %d", total_found, total_downloaded)

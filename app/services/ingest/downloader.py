"""
Episode downloader.

Downloads audio from a ScrapedEpisode URL and creates a Meeting + MediaFile
record in the database. Does NOT trigger processing — that's manual.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session

from app import models
from app.config import MEDIA_DIR
from .base import ScrapedEpisode

logger = logging.getLogger(__name__)

# Timeout for downloads (connect, read) in seconds
DOWNLOAD_TIMEOUT = (15, 300)


def _guess_extension(url: str, content_type: str | None = None) -> str:
    """Guess file extension from URL or content-type header."""
    path = urlparse(url).path.lower()
    if path.endswith(".mp3"):
        return ".mp3"
    elif path.endswith(".m4a"):
        return ".m4a"
    elif path.endswith(".wav"):
        return ".wav"
    elif path.endswith(".aac"):
        return ".aac"
    elif path.endswith(".ogg"):
        return ".ogg"
    elif path.endswith(".flac"):
        return ".flac"

    # Fall back to content-type
    if content_type:
        ct = content_type.lower()
        if "mpeg" in ct or "mp3" in ct:
            return ".mp3"
        elif "mp4" in ct or "m4a" in ct:
            return ".m4a"
        elif "wav" in ct:
            return ".wav"
        elif "ogg" in ct:
            return ".ogg"

    return ".mp3"  # safe default


def download_episode(db: Session, episode: ScrapedEpisode) -> models.Meeting:
    """
    Download an episode's audio and create database records.

    Creates:
    - Meeting record (category="audio", no processed_at)
    - MediaFile record (file_type="audio")
    - Audio file on disk at media/{meeting_id}/audio_source.{ext}

    Returns the created Meeting.
    """
    meeting_id = str(uuid.uuid4())
    meeting_dir = MEDIA_DIR / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)

    # Download the audio file
    logger.info("Downloading: %s", episode.audio_url)
    resp = requests.get(episode.audio_url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type")
    ext = _guess_extension(episode.audio_url, content_type)
    audio_filename = f"audio_source{ext}"
    audio_path = meeting_dir / audio_filename

    with audio_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    file_size = audio_path.stat().st_size
    logger.info("  Saved %s (%.1f MB)", audio_path, file_size / 1_048_576)

    # Build title: use show_name prefix if available
    title = episode.title
    if episode.show_name and not title.lower().startswith(episode.show_name.lower()):
        title = f"{episode.show_name} — {title}"

    # Create Meeting record
    meeting = models.Meeting(
        meeting_id=meeting_id,
        governing_body=episode.show_name or "",
        meeting_type="",
        meeting_date=episode.episode_date,
        title=title,
        category="audio",
        description=episode.description,
        source_url=episode.audio_url,
        thumbnail_url=episode.thumbnail_url,
        media_directory=str(meeting_dir),
    )
    db.add(meeting)

    # Create MediaFile record
    media = models.MediaFile(
        meeting_id=meeting_id,
        file_type="audio",
        file_path=str(audio_path),
        duration=episode.duration_hint,
    )
    db.add(media)

    db.commit()
    logger.info("  Created meeting %s: %s", meeting_id, title)
    return meeting

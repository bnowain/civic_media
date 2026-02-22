"""
Synchronous HTTP client for Atlas API.

Used to send transcript text to Atlas for LLM-powered story segmentation
and tagging. Gracefully handles Atlas being unavailable.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from app.config import ATLAS_API_URL

logger = logging.getLogger(__name__)

# Timeout for Atlas API calls (seconds)
ATLAS_TIMEOUT = 120


def segment_and_tag(
    transcript: str,
    content_type: str = "tv_news",
    metadata: Optional[dict] = None,
) -> Optional[dict]:
    """
    Send transcript text to Atlas for story segmentation and tagging.

    Args:
        transcript: Full transcript text.
        content_type: Type of content being processed.
        metadata: Optional metadata (station, air_date, etc.).

    Returns:
        Atlas response dict with segments, tags, mentions.
        None if Atlas is unavailable or request fails.
    """
    url = f"{ATLAS_API_URL}/tools/segment_and_tag"
    payload = {
        "transcript": transcript,
        "content_type": content_type,
        "metadata": metadata or {},
    }

    try:
        r = requests.post(url, json=payload, timeout=ATLAS_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        logger.info(
            "Atlas segment_and_tag: %d segments, %d tags, %d mentions",
            len(data.get("segments", [])),
            len(data.get("tags", [])),
            len(data.get("mentions", [])),
        )
        return data
    except requests.ConnectionError:
        logger.warning("Atlas unavailable at %s — skipping segmentation", ATLAS_API_URL)
        return None
    except requests.Timeout:
        logger.warning("Atlas timed out after %ds", ATLAS_TIMEOUT)
        return None
    except requests.HTTPError as e:
        logger.warning("Atlas returned error: %s", e)
        return None
    except Exception as e:
        logger.warning("Atlas client error: %s", e)
        return None


def tag_content(
    content_type: str,
    content_id: str,
    text: str,
    metadata: Optional[dict] = None,
) -> Optional[dict]:
    """
    Send content to Atlas for tagging only (no segmentation).

    Returns:
        Atlas response dict with tags and mentions.
        None if Atlas is unavailable.
    """
    url = f"{ATLAS_API_URL}/tools/tag_content"
    payload = {
        "content_type": content_type,
        "content_id": content_id,
        "text": text,
        "metadata": metadata or {},
    }

    try:
        r = requests.post(url, json=payload, timeout=ATLAS_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
        logger.warning("Atlas tag_content failed: %s", e)
        return None
    except Exception as e:
        logger.warning("Atlas client error: %s", e)
        return None

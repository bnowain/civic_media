"""
Pydantic schemas for API request and response validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


# ── Venues ───────────────────────────────────────────────────────────────────

class VenueCreate(BaseModel):
    name: str
    acoustic_type: str = "indoor_dry"
    notes: Optional[str] = None


class VenueOut(BaseModel):
    venue_id: str
    name: str
    acoustic_type: str
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Groups ───────────────────────────────────────────────────────────────────

class GroupCreate(BaseModel):
    name: str
    display_name: Optional[str] = None
    group_type: Optional[str] = None


class GroupOut(BaseModel):
    group_id: str
    name: str
    display_name: Optional[str]
    group_type: Optional[str] = None
    default_venue_id: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Meetings ─────────────────────────────────────────────────────────────────

class MeetingCreate(BaseModel):
    group_name: str = ""
    meeting_type: str = ""
    meeting_date: str   # YYYY-MM-DD
    title: str
    category: str = "meeting"
    program_type: Optional[str] = None  # "governing_meeting" | "media_broadcast" | "news_broadcast" | "web_show"
    group_id: Optional[str] = None
    description: Optional[str] = None
    source_url: Optional[str] = None
    page_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    primegov_id: Optional[int] = None
    video_url: Optional[str] = None
    agenda_url: Optional[str] = None
    minutes_url: Optional[str] = None
    packet_url: Optional[str] = None
    venue_id: Optional[str] = None

    @field_validator("meeting_date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("meeting_date must be YYYY-MM-DD")
        return v


class MeetingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    meeting_date: Optional[str] = None
    group_name: Optional[str] = None
    meeting_type: Optional[str] = None
    program_type: Optional[str] = None
    venue_id: Optional[str] = None
    summary_short: Optional[str] = None
    summary_long: Optional[str] = None

    @field_validator("meeting_date")
    @classmethod
    def validate_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("meeting_date must be YYYY-MM-DD")
        return v


class SummaryUpload(BaseModel):
    summary_short: Optional[str] = None
    summary_long: Optional[str] = None


class MeetingOut(MeetingCreate):
    meeting_id: str
    media_directory: Optional[str]
    processed_at: Optional[datetime] = None
    created_at: datetime
    summary_short: Optional[str] = None
    summary_long: Optional[str] = None
    summary_updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Media Files ──────────────────────────────────────────────────────────────

class MediaFileOut(BaseModel):
    media_id: str
    meeting_id: str
    file_type: str
    file_path: str
    duration: Optional[float]
    transcode_status: Optional[str] = None

    model_config = {"from_attributes": True}


# ── Documents ────────────────────────────────────────────────────────────────

class DocumentOut(BaseModel):
    document_id: str
    meeting_id: str
    document_type: str
    file_path: str
    ocr_text: Optional[str]
    created_at: datetime
    summary_short: Optional[str] = None
    summary_long: Optional[str] = None
    summary_updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── People ───────────────────────────────────────────────────────────────────

class PersonAppearance(BaseModel):
    meeting_id: str
    title: Optional[str]
    group_name: Optional[str]
    meeting_type: Optional[str]
    meeting_date: Optional[str]
    category: str
    segment_count: int

    model_config = {"from_attributes": True}


class PersonCreate(BaseModel):
    canonical_name: str


class PersonOut(PersonCreate):
    person_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PersonSummary(PersonOut):
    voiceprint_count: int = 0


# ── Assignments ──────────────────────────────────────────────────────────────

class AssignmentOut(BaseModel):
    assignment_id: str
    segment_id: str
    predicted_person_id: Optional[str]
    similarity_score: Optional[float]
    verified: bool
    tagged: bool = False

    model_config = {"from_attributes": True}


class ConfirmAssignment(BaseModel):
    person_id: str


class TagAssignment(BaseModel):
    person_id: str


# ── Transcript Segments ─────────────────────────────────────────────────────

class SegmentOut(BaseModel):
    segment_id: str
    meeting_id: str
    start_time: float
    end_time: float
    text: str
    raw_speaker_label: Optional[str]
    source_type: str = "in_person"
    overlap_ratio: Optional[float] = None
    assignment: Optional[AssignmentOut]

    model_config = {"from_attributes": True}


class SegmentEdit(BaseModel):
    """Payload for manually correcting segment text."""
    text: str


class SourceTypeUpdate(BaseModel):
    """Payload for changing a segment's source type (in_person vs telephone)."""
    source_type: str


# ── Pipeline Status ──────────────────────────────────────────────────────────

class PipelineStatus(BaseModel):
    meeting_id: str
    segment_count: int
    task_id: Optional[str]
    status: str              # "pending" | "processing" | "complete" | "error"
    stage: Optional[str]     # Human-readable current step
    progress_pct: Optional[int]   # 0-100
    detail: Optional[str]         # Extra info e.g. "1200/1800 segments"
    error: bool = False
    transcode_status: Optional[str] = None  # null | "pending" | "transcoding" | "transcoded"


# ── TV News ──────────────────────────────────────────────────────────────────

class TVNewscastCreate(BaseModel):
    title: str
    station: str = ""
    air_date: Optional[str] = None
    air_time: Optional[str] = None


class TVNewscastOut(BaseModel):
    newscast_id: str
    title: str
    station: str
    air_date: Optional[str]
    air_time: Optional[str]
    source_file: Optional[str]
    cleaned_file: Optional[str]
    duration_seconds: Optional[float]
    status: str
    error_detail: Optional[str]
    processed_at: Optional[datetime]
    created_at: datetime
    updated_at: Optional[datetime]

    model_config = {"from_attributes": True}


class TVNewsSegmentOut(BaseModel):
    segment_id: str
    newscast_id: str
    title: Optional[str]
    summary: Optional[str]
    start_time: Optional[float]
    end_time: Optional[float]
    story_type: Optional[str]
    transcript: Optional[str]
    last_tagged_at: Optional[datetime]
    tagging_version: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class TVNewsChunkOut(BaseModel):
    chunk_id: str
    newscast_id: str
    segment_id: Optional[str]
    start_time: Optional[float]
    end_time: Optional[float]
    text: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Tags ─────────────────────────────────────────────────────────────────────

class TagCreate(BaseModel):
    name: str
    parent_id: Optional[str] = None
    tag_type: Optional[str] = None
    description: Optional[str] = None


class TagOut(BaseModel):
    tag_id: str
    name: str
    parent_id: Optional[str]
    tag_type: Optional[str]
    description: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class TagAssignmentCreate(BaseModel):
    tag_id: str
    content_type: str
    content_id: str
    source: str = "manual"
    confidence: Optional[float] = None
    llm_context: Optional[str] = None


class TagAssignmentOut(BaseModel):
    assignment_id: str
    tag_id: str
    content_type: str
    content_id: str
    source: Optional[str]
    confidence: Optional[float]
    llm_context: Optional[str]
    created_at: datetime
    tagged_at: Optional[datetime]
    tag: Optional[TagOut] = None

    model_config = {"from_attributes": True}


class TagDenyRequest(BaseModel):
    tag_id: str
    content_type: str
    content_id: str


# ── People Mentions ──────────────────────────────────────────────────────────

class PeopleMentionCreate(BaseModel):
    person_id: str
    content_type: str
    content_id: str
    source: str = "manual"
    confidence: Optional[float] = None
    context: Optional[str] = None


class PeopleMentionOut(BaseModel):
    mention_id: str
    person_id: str
    content_type: str
    content_id: str
    source: Optional[str]
    confidence: Optional[float]
    context: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class PeopleMentionDenyRequest(BaseModel):
    person_id: str
    content_type: str
    content_id: str


# ── Clips ───────────────────────────────────────────────────────────────

class ClipCreate(BaseModel):
    source_type: str       # "meeting" | "newscast"
    source_id: str
    start_time: float
    end_time: float
    title: str = "Untitled clip"
    notes: Optional[str] = None


class ClipUpdate(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None


class ClipOut(BaseModel):
    clip_id: str
    source_type: str
    source_id: str
    source_media_path: str
    media_type: str
    start_time: float
    end_time: float
    duration: float
    title: str
    notes: Optional[str]
    thumbnail_path: Optional[str]
    export_path: Optional[str]
    export_status: Optional[str]
    export_error: Optional[str]
    cover_image_path: Optional[str]
    downloaded_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Library ──────────────────────────────────────────────────────────────────

class RecentMediaItem(BaseModel):
    id: str
    title: str
    media_type: str       # "meeting" | "audio" | "news"
    date: Optional[str]
    processed_at: Optional[datetime]
    created_at: datetime
    status: Optional[str] = None
    subtitle: Optional[str] = None


# ── Ingest Sources ──────────────────────────────────────────────────────────

class IngestSourceOut(BaseModel):
    source_id: str
    name: str
    source_type: str
    config_json: Optional[str]
    last_scraped_at: Optional[datetime]
    last_scraped_count: Optional[float]
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class IngestRunResponse(BaseModel):
    task_id: str
    status: str
    message: str


class IngestStatusResponse(BaseModel):
    running: bool
    source_id: Optional[str] = None
    episodes_found: int = 0
    episodes_downloaded: int = 0
    stage: Optional[str] = None
    error: Optional[str] = None


# ── News Articles ─────────────────────────────────────────────────────────────

class NewsArticleCreate(BaseModel):
    canonical_url: str
    source_slug: str
    capture_method: str = "rss"
    headline: Optional[str] = None
    author: Optional[str] = None
    published_at: Optional[str] = None           # ISO datetime or date string
    article_text: Optional[str] = None
    description: Optional[str] = None
    preview_image_url: Optional[str] = None
    image_urls: Optional[list[str]] = None       # inline image URLs
    embedded_links: Optional[list[dict]] = None  # [{text, href}, ...]
    source_tags: Optional[list[str]] = None      # tags from RSS feed
    section: Optional[str] = None                # 'local', 'politics', 'crime-courts', 'opinion'
    article_type: Optional[str] = None           # 'news', 'opinion', 'letter', 'analysis'
    source_modified_at: Optional[str] = None     # article:modified_time from source meta
    word_count: Optional[int] = None
    filter_reason: Optional[str] = None          # which relevance rule passed
    fetch_status: Optional[str] = None           # 'full', 'partial', 'metadata_only'
    external_document_urls: Optional[list[str]] = None  # linked PDFs / gov docs


class NewsArticleOut(BaseModel):
    article_id: str
    canonical_url: str
    source_slug: str
    capture_method: str
    headline: Optional[str]
    author: Optional[str]
    published_at: Optional[str]
    article_text: Optional[str]
    description: Optional[str]
    preview_image_url: Optional[str]
    image_urls: Optional[list[str]]
    embedded_links: Optional[list[dict]]
    source_tags: Optional[list[str]]
    section: Optional[str]
    article_type: Optional[str]
    source_modified_at: Optional[str]
    word_count: Optional[int]
    filter_reason: Optional[str]
    fetch_status: Optional[str]
    external_document_urls: Optional[list[str]]
    created_at: datetime
    updated_at: Optional[datetime]

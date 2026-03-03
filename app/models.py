"""
ORM table definitions for Civic Media.

Tables: meetings, governing_bodies, media_files, documents,
transcript_segments, people, voiceprints, segment_assignments,
tv_newscasts, tv_news_segments, tv_news_transcription_chunks,
tags, tag_assignments, tag_denials, people_mentions, people_mention_denials,
clips, news_articles.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer,
    LargeBinary, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def _gen_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ── Venues ───────────────────────────────────────────────────────────────────

class Venue(Base):
    __tablename__ = "venues"

    venue_id      = Column(String, primary_key=True, default=_gen_id)
    name          = Column(String, nullable=False)
    acoustic_type = Column(String, nullable=False, default="indoor_dry")
    notes         = Column(Text)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Groups ───────────────────────────────────────────────────────────────────

class Group(Base):
    __tablename__ = "groups"

    group_id         = Column(String, primary_key=True, default=_gen_id)
    name             = Column(String, nullable=False, unique=True)
    display_name     = Column(String)
    group_type       = Column(String, nullable=True)  # "government" | "show"
    default_venue_id = Column(String, ForeignKey("venues.venue_id"), nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    meetings      = relationship("Meeting", back_populates="group_ref")
    default_venue = relationship("Venue")


# ── Meetings ─────────────────────────────────────────────────────────────────

class Meeting(Base):
    __tablename__ = "meetings"

    meeting_id   = Column(String, primary_key=True, default=_gen_id)
    group_name   = Column(String, nullable=False)
    meeting_type = Column(String, nullable=False)
    meeting_date = Column(String, nullable=False)   # ISO date string YYYY-MM-DD
    title        = Column(String, nullable=False)
    category     = Column(String, nullable=False, default="meeting")  # "meeting" | "audio" | "news" | "web_series"
    program_type = Column(String, nullable=True)  # "governing_meeting" | "media_broadcast" | "news_broadcast" | "web_show"
    media_directory = Column(String)
    group_id     = Column(String, ForeignKey("groups.group_id"), nullable=True)
    description       = Column(Text, nullable=True)       # episode title, guest names
    source_url        = Column(Text, nullable=True)        # original audio URL (dedup key)
    thumbnail_url     = Column(Text, nullable=True)        # cover art URL from source
    venue_id          = Column(String, ForeignKey("venues.venue_id"), nullable=True)
    primegov_id       = Column(Integer, nullable=True, unique=True, index=True)
    video_url         = Column(Text, nullable=True)        # Swagit video page URL
    page_url          = Column(Text, nullable=True)        # human-browsable source page URL
    agenda_url        = Column(Text, nullable=True)        # PrimeGov agenda PDF URL
    minutes_url       = Column(Text, nullable=True)        # PrimeGov minutes PDF URL
    packet_url        = Column(Text, nullable=True)         # PrimeGov meeting packet PDF URL
    processed_at       = Column(DateTime, nullable=True)
    created_at         = Column(DateTime, default=datetime.utcnow)
    summary_short      = Column(Text, nullable=True)
    summary_long       = Column(Text, nullable=True)
    summary_updated_at = Column(DateTime, nullable=True)

    group_ref = relationship("Group", back_populates="meetings")
    venue     = relationship("Venue")
    media_files = relationship("MediaFile", back_populates="meeting",
                               cascade="all, delete-orphan")
    documents   = relationship("Document",  back_populates="meeting",
                               cascade="all, delete-orphan")
    segments    = relationship("TranscriptSegment", back_populates="meeting",
                               cascade="all, delete-orphan")
    votes       = relationship("MeetingVote", back_populates="meeting",
                               cascade="all, delete-orphan")


class MediaFile(Base):
    __tablename__ = "media_files"
    __table_args__ = (
        # One primary source (video or non-extracted audio) per meeting.
        # Extracted WAVs (path ends in _extracted.wav) are excluded so the
        # pipeline can create and recreate them freely on rerun.
        Index(
            "uq_media_primary_source",
            "meeting_id", "file_type",
            unique=True,
            sqlite_where=text("file_path NOT LIKE '%_extracted.wav'"),
        ),
        Index('ix_media_files_meeting_id', 'meeting_id'),
    )

    media_id         = Column(String, primary_key=True, default=_gen_id)
    meeting_id       = Column(String, ForeignKey("meetings.meeting_id"), nullable=False)
    file_type        = Column(String, nullable=False)   # "video" | "audio"
    file_path        = Column(String, nullable=False)
    duration         = Column(Float)                    # seconds, populated after extraction
    transcode_status = Column(String, nullable=True)    # null | "pending" | "transcoding" | "transcoded"

    meeting = relationship("Meeting", back_populates="media_files")


class Document(Base):
    __tablename__ = "documents"

    document_id   = Column(String, primary_key=True, default=_gen_id)
    meeting_id    = Column(String, ForeignKey("meetings.meeting_id"), nullable=False)
    document_type = Column(String, nullable=False)  # agenda | minutes | supplemental
    file_path          = Column(String, nullable=False)
    ocr_text           = Column(Text)
    created_at         = Column(DateTime, default=datetime.utcnow)
    summary_short      = Column(Text, nullable=True)
    summary_long       = Column(Text, nullable=True)
    summary_updated_at = Column(DateTime, nullable=True)

    minutes_parse_status = Column(String, nullable=True)   # "ok" | "partial" | "empty" | "unrecognized"
    minutes_parse_notes  = Column(Text, nullable=True)     # unmatched vote-like paragraphs, JSON

    meeting = relationship("Meeting", back_populates="documents")
    votes   = relationship("MeetingVote", back_populates="document")


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        Index('ix_transcript_segments_meeting_id', 'meeting_id'),
    )

    segment_id        = Column(String, primary_key=True, default=_gen_id)
    meeting_id        = Column(String, ForeignKey("meetings.meeting_id"), nullable=False)
    start_time        = Column(Float, nullable=False)   # seconds
    end_time          = Column(Float, nullable=False)
    text              = Column(Text,  nullable=False)
    raw_speaker_label = Column(String)                  # e.g. "SPEAKER_00"
    embedding         = Column(LargeBinary)             # serialised numpy array
    source_type       = Column(String, nullable=False, default="in_person")
    overlap_ratio     = Column(Float, nullable=True, default=0.0)

    avg_logprob    = Column(Float, nullable=True)
    no_speech_prob = Column(Float, nullable=True)

    meeting    = relationship("Meeting", back_populates="segments")
    assignment = relationship(
        "SegmentAssignment", back_populates="segment",
        uselist=False, cascade="all, delete-orphan"
    )


class Person(Base):
    __tablename__ = "people"

    person_id      = Column(String, primary_key=True, default=_gen_id)
    canonical_name = Column(String, nullable=False, unique=True)
    created_at     = Column(DateTime, default=datetime.utcnow)

    voiceprints = relationship("Voiceprint",        back_populates="person",
                               cascade="all, delete-orphan")
    assignments = relationship("SegmentAssignment", back_populates="predicted_person")
    mentions    = relationship("PeopleMention",     back_populates="person",
                               cascade="all, delete-orphan")


class Voiceprint(Base):
    __tablename__ = "voiceprints"

    voiceprint_id    = Column(String, primary_key=True, default=_gen_id)
    person_id        = Column(String, ForeignKey("people.person_id"), nullable=False)
    embedding        = Column(LargeBinary, nullable=False)   # serialised numpy
    source_segment_id = Column(String, nullable=True)        # segment that created this VP
    venue_id         = Column(String, ForeignKey("venues.venue_id"), nullable=True)
    source_type      = Column(String, nullable=False, default="in_person")
    source_duration  = Column(Float, nullable=True)          # seconds of source audio
    created_at       = Column(DateTime, default=datetime.utcnow)

    person = relationship("Person", back_populates="voiceprints")
    venue  = relationship("Venue")


class SegmentAssignment(Base):
    __tablename__ = "segment_assignments"

    assignment_id       = Column(String, primary_key=True, default=_gen_id)
    segment_id          = Column(
        String, ForeignKey("transcript_segments.segment_id"),
        nullable=False, unique=True
    )
    predicted_person_id = Column(String, ForeignKey("people.person_id"))
    similarity_score    = Column(Float)
    verified            = Column(Boolean, default=False)
    tagged              = Column(Boolean, default=False)

    segment          = relationship("TranscriptSegment", back_populates="assignment")
    predicted_person = relationship("Person",            back_populates="assignments")


# ── TV News ──────────────────────────────────────────────────────────────────

class TVNewscast(Base):
    __tablename__ = "tv_newscasts"

    newscast_id      = Column(String, primary_key=True, default=_gen_id)
    title            = Column(String, nullable=False)
    station          = Column(String, nullable=False, default="")
    air_date         = Column(String)    # YYYY-MM-DD
    air_time         = Column(String)    # HH:MM
    source_file      = Column(String)
    cleaned_file     = Column(String)
    duration_seconds = Column(Float)
    status           = Column(String, nullable=False, default="queued")
    error_detail     = Column(Text)
    processed_at     = Column(DateTime)
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    segments = relationship("TVNewsSegment", back_populates="newscast",
                            cascade="all, delete-orphan")
    chunks   = relationship("TVNewsTranscriptionChunk", back_populates="newscast",
                            cascade="all, delete-orphan")


class TVNewsSegment(Base):
    __tablename__ = "tv_news_segments"

    segment_id      = Column(String, primary_key=True, default=_gen_id)
    newscast_id     = Column(String, ForeignKey("tv_newscasts.newscast_id"), nullable=False)
    title           = Column(String)
    summary         = Column(Text)
    start_time      = Column(Float)
    end_time        = Column(Float)
    story_type      = Column(String)
    transcript      = Column(Text)
    last_tagged_at  = Column(DateTime)
    tagging_version = Column(String)
    created_at      = Column(DateTime, default=datetime.utcnow)

    newscast = relationship("TVNewscast", back_populates="segments")
    chunks   = relationship("TVNewsTranscriptionChunk", back_populates="segment")


class TVNewsTranscriptionChunk(Base):
    __tablename__ = "tv_news_transcription_chunks"

    chunk_id    = Column(String, primary_key=True, default=_gen_id)
    newscast_id = Column(String, ForeignKey("tv_newscasts.newscast_id"), nullable=False)
    segment_id  = Column(String, ForeignKey("tv_news_segments.segment_id"), nullable=True)
    start_time  = Column(Float)
    end_time    = Column(Float)
    text        = Column(Text)
    created_at  = Column(DateTime, default=datetime.utcnow)

    newscast = relationship("TVNewscast", back_populates="chunks")
    segment  = relationship("TVNewsSegment", back_populates="chunks")


# ── Tags & Mentions ──────────────────────────────────────────────────────────

class Tag(Base):
    __tablename__ = "tags"

    tag_id      = Column(String, primary_key=True, default=_gen_id)
    name        = Column(String, nullable=False, unique=True)
    parent_id   = Column(String, ForeignKey("tags.tag_id"), nullable=True)
    tag_type    = Column(String)     # e.g. "topic", "entity", "category"
    description = Column(Text)
    created_at  = Column(DateTime, default=datetime.utcnow)

    parent      = relationship("Tag", remote_side=[tag_id], backref="children")
    assignments = relationship("TagAssignment", back_populates="tag",
                               cascade="all, delete-orphan")
    denials     = relationship("TagDenial", back_populates="tag",
                               cascade="all, delete-orphan")


class TagAssignment(Base):
    __tablename__ = "tag_assignments"
    __table_args__ = (
        UniqueConstraint("tag_id", "content_type", "content_id"),
    )

    assignment_id = Column(String, primary_key=True, default=_gen_id)
    tag_id        = Column(String, ForeignKey("tags.tag_id"), nullable=False)
    content_type  = Column(String, nullable=False)   # "meeting" | "tv_news_segment" | etc.
    content_id    = Column(String, nullable=False)
    source        = Column(String)                   # "llm" | "manual"
    confidence    = Column(Float)
    llm_context   = Column(Text)
    created_at    = Column(DateTime, default=datetime.utcnow)
    tagged_at     = Column(DateTime, default=datetime.utcnow)

    tag = relationship("Tag", back_populates="assignments")


class TagDenial(Base):
    __tablename__ = "tag_denials"
    __table_args__ = (
        UniqueConstraint("tag_id", "content_type", "content_id"),
    )

    denial_id    = Column(String, primary_key=True, default=_gen_id)
    tag_id       = Column(String, ForeignKey("tags.tag_id"), nullable=False)
    content_type = Column(String, nullable=False)
    content_id   = Column(String, nullable=False)
    denied_at    = Column(DateTime, default=datetime.utcnow)

    tag = relationship("Tag", back_populates="denials")


class PeopleMention(Base):
    __tablename__ = "people_mentions"
    __table_args__ = (
        UniqueConstraint("person_id", "content_type", "content_id"),
    )

    mention_id   = Column(String, primary_key=True, default=_gen_id)
    person_id    = Column(String, ForeignKey("people.person_id"), nullable=False)
    content_type = Column(String, nullable=False)
    content_id   = Column(String, nullable=False)
    source       = Column(String)                    # "llm" | "manual"
    confidence   = Column(Float)
    context      = Column(Text)
    created_at   = Column(DateTime, default=datetime.utcnow)

    person = relationship("Person", back_populates="mentions")


class Clip(Base):
    __tablename__ = "clips"

    clip_id           = Column(String, primary_key=True, default=_gen_id)
    source_type       = Column(String, nullable=False)       # "meeting" | "newscast"
    source_id         = Column(String, nullable=False)       # plain string, no FK
    source_media_path = Column(String, nullable=False)
    media_type        = Column(String, nullable=False)       # "video" | "audio"
    start_time        = Column(Float, nullable=False)
    end_time          = Column(Float, nullable=False)
    duration          = Column(Float, nullable=False)
    title             = Column(String, nullable=False, default="Untitled clip")
    notes             = Column(Text)
    thumbnail_path    = Column(String)
    export_path       = Column(String)
    export_status     = Column(String)                       # pending|exporting|ready|error|cleaned
    export_error      = Column(Text)
    cover_image_path  = Column(String)
    downloaded_at     = Column(DateTime)
    created_at        = Column(DateTime, default=datetime.utcnow)


# ── Ingest Sources ──────────────────────────────────────────────────────────

class IngestSource(Base):
    __tablename__ = "ingest_sources"

    source_id          = Column(String, primary_key=True, default=_gen_id)
    name               = Column(String, nullable=False, unique=True)
    source_type        = Column(String, nullable=False)  # "kcnr" | "securenet" | "freedominaction" | "podbean"
    config_json        = Column(Text, nullable=True)     # JSON blob of source-specific params
    last_scraped_at    = Column(DateTime, nullable=True)
    last_scraped_count = Column(Float, nullable=True)    # episodes found last run
    enabled            = Column(Boolean, default=True)
    created_at         = Column(DateTime, default=datetime.utcnow)


class PeopleMentionDenial(Base):
    __tablename__ = "people_mention_denials"
    __table_args__ = (
        UniqueConstraint("person_id", "content_type", "content_id"),
    )

    denial_id    = Column(String, primary_key=True, default=_gen_id)
    person_id    = Column(String, ForeignKey("people.person_id"), nullable=False)
    content_type = Column(String, nullable=False)
    content_id   = Column(String, nullable=False)
    denied_at    = Column(DateTime, default=datetime.utcnow)


# ── Meeting Votes (parsed from minutes) ──────────────────────────────────────

# outcome values: "Carried" | "Unanimously Carried" | "Failed" |
#                 "Failed — No Second" | "Withdrawn"

class MeetingVote(Base):
    __tablename__ = "meeting_votes"

    vote_id           = Column(String, primary_key=True, default=_gen_id)
    meeting_id        = Column(String, ForeignKey("meetings.meeting_id"),
                               nullable=False, index=True)
    document_id       = Column(String, ForeignKey("documents.document_id"),
                               nullable=True, index=True)
    meeting_date = Column(String, nullable=True)    # YYYY-MM-DD, denormalized
    group_name   = Column(String, nullable=True)    # denormalized
    agenda_section    = Column(String, nullable=True)    # "Consent Calendar" etc.
    item_description  = Column(Text, nullable=False)     # what was decided
    resolution_number = Column(String, nullable=True)    # "2026-008"
    outcome           = Column(String, nullable=False)
    vote_tally        = Column(String, nullable=True)    # "4-1", "3-2"
    mover             = Column(String, nullable=True)
    seconder          = Column(String, nullable=True)    # null if failed-no-second
    source            = Column(String, nullable=False, default="minutes_parsed")
    created_at        = Column(DateTime, default=datetime.utcnow)

    meeting  = relationship("Meeting",  back_populates="votes")
    document = relationship("Document", back_populates="votes")
    members  = relationship("VoteMember", back_populates="vote",
                            cascade="all, delete-orphan")


class VoteMember(Base):
    __tablename__ = "vote_members"
    __table_args__ = (
        UniqueConstraint("vote_id", "member_name"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    vote_id     = Column(String, ForeignKey("meeting_votes.vote_id"),
                         nullable=False, index=True)
    member_name = Column(String, nullable=False)
    vote_value  = Column(String, nullable=False)  # "yes" | "no" | "abstain" | "absent"

    vote = relationship("MeetingVote", back_populates="members")


# ── Reference Documents (laws, policies, guidelines) ─────────────────────────

class ReferenceDocument(Base):
    __tablename__ = "reference_documents"

    ref_doc_id  = Column(String, primary_key=True, default=_gen_id)
    name        = Column(String, nullable=False, unique=True)  # "Brown Act 2026"
    doc_type    = Column(String, nullable=False)               # "law" | "policy" | "guidelines"
    year        = Column(Integer, nullable=True)
    source_file = Column(String, nullable=True)                # path to original PDF/file
    full_text   = Column(Text, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sections = relationship("ReferenceSection", back_populates="document",
                            cascade="all, delete-orphan")


class ReferenceSection(Base):
    """One row per statutory section — the unit for RAG chunking."""
    __tablename__ = "reference_sections"
    __table_args__ = (
        UniqueConstraint("ref_doc_id", "section_num"),
    )

    section_id  = Column(String, primary_key=True, default=_gen_id)
    ref_doc_id  = Column(String, ForeignKey("reference_documents.ref_doc_id"),
                         nullable=False, index=True)
    section_num = Column(String, nullable=True)   # "54954.2"
    title       = Column(String, nullable=True)   # short human-readable descriptor
    text        = Column(Text, nullable=False)
    seq         = Column(Integer, nullable=False)  # ordering within document

    document = relationship("ReferenceDocument", back_populates="sections")


# ── Processing Jobs ───────────────────────────────────────────────────────────

class ProcessingJob(Base):
    """
    One row per queued/running/completed processing task.

    Replaces filesystem-based progress.json as the authoritative state store
    for backfill tracking. progress.json is still written for backward compat
    with existing endpoints (media status, etc.), but the DB is the source of
    truth for the backfill API and SSE stream.

    stage:  'download' | 'transcode' | 'process'
    status: 'queued' | 'running' | 'done' | 'error'
    """
    __tablename__ = "processing_jobs"
    __table_args__ = (
        Index('ix_processing_jobs_meeting_id', 'meeting_id'),
        Index('ix_processing_jobs_status', 'status'),
        Index('ix_processing_jobs_meeting_stage', 'meeting_id', 'stage'),
    )

    job_id         = Column(String, primary_key=True, default=_gen_id)
    meeting_id     = Column(String, ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False)
    stage          = Column(String, nullable=False)       # 'download' | 'transcode' | 'process'
    status         = Column(String, nullable=False, default="queued")  # queued|running|done|error
    celery_task_id = Column(String, nullable=True)
    pct            = Column(Integer, default=0)
    stage_label    = Column(String, nullable=True)        # "Transcribing", "Diarizing speakers"
    detail         = Column(String, nullable=True)        # "Segment 50 of 120"
    error_msg      = Column(Text, nullable=True)
    queued_at      = Column(DateTime, default=datetime.utcnow)
    started_at     = Column(DateTime, nullable=True)
    completed_at   = Column(DateTime, nullable=True)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    meeting = relationship("Meeting", backref="processing_jobs")


# ── News Articles (RSS + authenticated scrape) ────────────────────────────────

class NewsArticle(Base):
    """
    Text articles ingested from RSS feeds or authenticated scrapers.

    Deduplication key: canonical_url (query-string-stripped URL).
    source_slug identifies the outlet (e.g. 'shasta-scout', 'record-searchlight').
    capture_method: 'rss' | 'authenticated' | 'stealth'
    JSON columns (image_urls, embedded_links, source_tags) stored as TEXT.
    """
    __tablename__ = "news_articles"
    __table_args__ = (
        Index('ix_news_articles_source_slug', 'source_slug'),
        Index('ix_news_articles_published_at', 'published_at'),
    )

    article_id        = Column(String,   primary_key=True, default=_gen_id)
    canonical_url     = Column(Text,     nullable=False, unique=True)
    source_slug       = Column(String,   nullable=False)   # e.g. 'shasta-scout'
    capture_method    = Column(String,   nullable=False, default='rss')
    headline          = Column(Text,     nullable=True)
    author            = Column(String,   nullable=True)
    published_at      = Column(String,   nullable=True)    # ISO datetime string
    article_text      = Column(Text,     nullable=True)    # full body
    description       = Column(Text,     nullable=True)    # lead / summary
    preview_image_url = Column(Text,     nullable=True)
    image_urls        = Column(Text,     nullable=True)    # JSON list of inline img URLs
    embedded_links    = Column(Text,     nullable=True)    # JSON list of {text, href}
    source_tags       = Column(Text,     nullable=True)    # JSON list from feed

    # ── Additional metadata ──────────────────────────────────────────────────
    section               = Column(String,   nullable=True)  # 'local', 'politics', 'crime-courts', 'opinion', etc.
    article_type          = Column(String,   nullable=True)  # 'news', 'opinion', 'letter', 'analysis', 'press_release'
    source_modified_at    = Column(String,   nullable=True)  # article:modified_time from OG meta
    word_count            = Column(Integer,  nullable=True)  # len(article_text.split())
    filter_reason         = Column(String,   nullable=True)  # relevance filter rule: 'trusted-source', 'keyword:budget'
    fetch_status          = Column(String,   nullable=True)  # 'full', 'partial', 'metadata_only'
    external_document_urls = Column(Text,    nullable=True)  # JSON list of linked doc URLs (.pdf, .docx, gov portals)

    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

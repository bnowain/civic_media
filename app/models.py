"""
ORM table definitions for Civic Media.

Tables: meetings, governing_bodies, media_files, documents,
transcript_segments, people, voiceprints, segment_assignments,
tv_newscasts, tv_news_segments, tv_news_transcription_chunks,
tags, tag_assignments, tag_denials, people_mentions, people_mention_denials.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def _gen_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ── Governing Bodies ─────────────────────────────────────────────────────────

class GoverningBody(Base):
    __tablename__ = "governing_bodies"

    governing_body_id = Column(String, primary_key=True, default=_gen_id)
    name              = Column(String, nullable=False, unique=True)
    display_name      = Column(String)
    created_at        = Column(DateTime, default=datetime.utcnow)

    meetings = relationship("Meeting", back_populates="governing_body_ref")


# ── Meetings ─────────────────────────────────────────────────────────────────

class Meeting(Base):
    __tablename__ = "meetings"

    meeting_id        = Column(String, primary_key=True, default=_gen_id)
    governing_body    = Column(String, nullable=False)
    meeting_type      = Column(String, nullable=False)
    meeting_date      = Column(String, nullable=False)   # ISO date string YYYY-MM-DD
    title             = Column(String, nullable=False)
    category          = Column(String, nullable=False, default="meeting")  # "meeting" | "audio"
    media_directory   = Column(String)
    governing_body_id = Column(String, ForeignKey("governing_bodies.governing_body_id"), nullable=True)
    processed_at      = Column(DateTime, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)

    governing_body_ref = relationship("GoverningBody", back_populates="meetings")
    media_files = relationship("MediaFile", back_populates="meeting",
                               cascade="all, delete-orphan")
    documents   = relationship("Document",  back_populates="meeting",
                               cascade="all, delete-orphan")
    segments    = relationship("TranscriptSegment", back_populates="meeting",
                               cascade="all, delete-orphan")


class MediaFile(Base):
    __tablename__ = "media_files"

    media_id   = Column(String, primary_key=True, default=_gen_id)
    meeting_id = Column(String, ForeignKey("meetings.meeting_id"), nullable=False)
    file_type  = Column(String, nullable=False)   # "video" | "audio"
    file_path  = Column(String, nullable=False)
    duration   = Column(Float)                    # seconds, populated after extraction

    meeting = relationship("Meeting", back_populates="media_files")


class Document(Base):
    __tablename__ = "documents"

    document_id   = Column(String, primary_key=True, default=_gen_id)
    meeting_id    = Column(String, ForeignKey("meetings.meeting_id"), nullable=False)
    document_type = Column(String, nullable=False)  # agenda | minutes | supplemental
    file_path     = Column(String, nullable=False)
    ocr_text      = Column(Text)
    created_at    = Column(DateTime, default=datetime.utcnow)

    meeting = relationship("Meeting", back_populates="documents")


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    segment_id        = Column(String, primary_key=True, default=_gen_id)
    meeting_id        = Column(String, ForeignKey("meetings.meeting_id"), nullable=False)
    start_time        = Column(Float, nullable=False)   # seconds
    end_time          = Column(Float, nullable=False)
    text              = Column(Text,  nullable=False)
    raw_speaker_label = Column(String)                  # e.g. "SPEAKER_00"
    embedding         = Column(LargeBinary)             # serialised numpy array

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
    created_at       = Column(DateTime, default=datetime.utcnow)

    person = relationship("Person", back_populates="voiceprints")


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

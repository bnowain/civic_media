"""
ORM table definitions — exactly the schema specified in Phase 1.
No additional tables.
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


class Meeting(Base):
    __tablename__ = "meetings"

    meeting_id      = Column(String, primary_key=True, default=_gen_id)
    governing_body  = Column(String, nullable=False)
    meeting_type    = Column(String, nullable=False)
    meeting_date    = Column(String, nullable=False)   # ISO date string YYYY-MM-DD
    title           = Column(String, nullable=False)
    media_directory = Column(String)
    created_at      = Column(DateTime, default=datetime.utcnow)

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


class Voiceprint(Base):
    __tablename__ = "voiceprints"

    voiceprint_id = Column(String, primary_key=True, default=_gen_id)
    person_id     = Column(String, ForeignKey("people.person_id"), nullable=False)
    embedding     = Column(LargeBinary, nullable=False)   # serialised numpy
    created_at    = Column(DateTime, default=datetime.utcnow)

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

    segment          = relationship("TranscriptSegment", back_populates="assignment")
    predicted_person = relationship("Person",            back_populates="assignments")

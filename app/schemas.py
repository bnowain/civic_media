"""
Pydantic schemas for API request and response validation.
Kept minimal — exactly what each endpoint needs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


# ── Meetings ──────────────────────────────────────────────────────────────────

class MeetingCreate(BaseModel):
    governing_body: str
    meeting_type: str
    meeting_date: str   # YYYY-MM-DD
    title: str

    @field_validator("meeting_date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("meeting_date must be YYYY-MM-DD")
        return v


class MeetingOut(MeetingCreate):
    meeting_id: str
    media_directory: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Media Files ───────────────────────────────────────────────────────────────

class MediaFileOut(BaseModel):
    media_id: str
    meeting_id: str
    file_type: str
    file_path: str
    duration: Optional[float]

    model_config = {"from_attributes": True}


# ── Documents ─────────────────────────────────────────────────────────────────

class DocumentOut(BaseModel):
    document_id: str
    meeting_id: str
    document_type: str
    file_path: str
    ocr_text: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── People ────────────────────────────────────────────────────────────────────

class PersonCreate(BaseModel):
    canonical_name: str


class PersonOut(PersonCreate):
    person_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Assignments ───────────────────────────────────────────────────────────────

class AssignmentOut(BaseModel):
    assignment_id: str
    segment_id: str
    predicted_person_id: Optional[str]
    similarity_score: Optional[float]
    verified: bool

    model_config = {"from_attributes": True}


class ConfirmAssignment(BaseModel):
    person_id: str


# ── Transcript Segments ───────────────────────────────────────────────────────

class SegmentOut(BaseModel):
    segment_id: str
    meeting_id: str
    start_time: float
    end_time: float
    text: str
    raw_speaker_label: Optional[str]
    assignment: Optional[AssignmentOut]

    model_config = {"from_attributes": True}


# ── Pipeline Status ───────────────────────────────────────────────────────────

class PipelineStatus(BaseModel):
    meeting_id: str
    segment_count: int
    task_id: Optional[str]
    status: str         # "pending" | "processing" | "complete" | "error"
    stage: Optional[str]    # Human-readable current step
    progress_pct: Optional[int]  # 0-100
    detail: Optional[str]        # Extra info e.g. "1200/1800 segments"

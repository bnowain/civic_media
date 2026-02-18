"""
Celery task definitions.

Three tasks:
  - process_video_task:     Full video ingestion pipeline.
  - process_pdf_task:       PDF text extraction (native + OCR fallback).
  - rerun_voiceprints_task: Background voiceprint re-evaluation after confirmation.

All tasks use their own DB sessions (never share across task boundaries).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.worker import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.process_video",
    max_retries=1,
    default_retry_delay=15,
)
def process_video_task(self, meeting_id: str, media_id: str) -> dict:
    """
    Run the full video ingestion pipeline for a single video file.
    Returns a summary dict with segment count.
    """
    from app.database import SessionLocal
    from app.services.pipeline import run_video_pipeline

    db = SessionLocal()
    try:
        run_video_pipeline(db, meeting_id, media_id)

        from app.models import TranscriptSegment
        count = db.query(TranscriptSegment).filter_by(meeting_id=meeting_id).count()
        return {"meeting_id": meeting_id, "segment_count": count, "status": "complete"}

    except Exception as exc:
        db.rollback()
        logger.exception("process_video_task failed for meeting %s", meeting_id)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="tasks.process_pdf",
    max_retries=1,
    default_retry_delay=10,
)
def process_pdf_task(self, document_id: str) -> dict:
    """
    Extract text from a PDF document and store it in the database.
    Also writes a .txt file to ocr_text/{meeting_id}/.
    """
    from app.database import SessionLocal
    from app.models import Document
    from app.services.pdf_ingestor import extract_text
    from app.config import OCR_TEXT_DIR

    db = SessionLocal()
    try:
        doc = db.query(Document).filter_by(document_id=document_id).first()
        if not doc:
            logger.error("Document %s not found", document_id)
            return {"error": "not_found"}

        text = extract_text(doc.file_path)
        doc.ocr_text = text
        db.commit()

        ocr_dir = OCR_TEXT_DIR / doc.meeting_id
        ocr_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(doc.file_path).stem
        out_file = ocr_dir / f"{stem}.txt"
        out_file.write_text(text, encoding="utf-8")

        logger.info("PDF processed: %s (%d chars)", doc.file_path, len(text))
        return {"document_id": document_id, "char_count": len(text), "status": "complete"}

    except Exception as exc:
        db.rollback()
        logger.exception("process_pdf_task failed for document %s", document_id)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    name="tasks.rerun_voiceprints",
    max_retries=0,
)
def rerun_voiceprints_task(meeting_id: str) -> dict:
    """
    Re-evaluate all unverified segments in a meeting against current voiceprints.
    Runs in the background after a human confirmation so the HTTP response
    returns immediately without blocking on 1700+ segment re-evaluations.
    """
    from app.database import SessionLocal
    from app.services import voiceprint as vp_service

    db = SessionLocal()
    try:
        count = vp_service.rerun_unverified_segments(db, meeting_id)
        logger.info(
            "rerun_voiceprints_task complete: %d segments re-evaluated for meeting %s",
            count, meeting_id,
        )
        return {"meeting_id": meeting_id, "segments_reprocessed": count}
    except Exception:
        db.rollback()
        logger.exception("rerun_voiceprints_task failed for meeting %s", meeting_id)
        raise
    finally:
        db.close()

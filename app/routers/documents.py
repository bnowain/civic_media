"""
Document (PDF) upload endpoints.

POST /api/documents/{meeting_id}/upload — save PDF, enqueue OCR.
GET  /api/documents/{meeting_id}        — list documents with extracted text.
GET  /api/documents/{meeting_id}/{doc_id} — single document detail.
"""

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import DOCUMENTS_DIR
from app.database import get_db

router = APIRouter(prefix="/api/documents", tags=["documents"])

_VALID_DOC_TYPES = {"agenda", "minutes", "supplemental"}


@router.post(
    "/{meeting_id}/upload",
    response_model=schemas.DocumentOut,
    status_code=202,
)
async def upload_document(
    meeting_id: str,
    document_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a PDF and queue text extraction."""
    meeting = db.query(models.Meeting).filter_by(meeting_id=meeting_id).first()
    if not meeting:
        raise HTTPException(404, "Meeting not found")

    if document_type not in _VALID_DOC_TYPES:
        raise HTTPException(
            400,
            f"Invalid document_type '{document_type}'. "
            f"Must be one of: {', '.join(sorted(_VALID_DOC_TYPES))}",
        )

    original_name = Path(file.filename or f"{document_type}.pdf")
    if original_name.suffix.lower() != ".pdf":
        raise HTTPException(400, "Only PDF files are accepted.")

    dest_dir = DOCUMENTS_DIR / meeting_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Use document_type as filename so agenda/minutes are clearly labelled
    dest_path = dest_dir / f"{document_type}.pdf"

    with dest_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    doc = models.Document(
        meeting_id=meeting_id,
        document_type=document_type,
        file_path=str(dest_path),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    try:
        from app.tasks import process_pdf_task
        process_pdf_task.delay(doc.document_id)
    except Exception:
        # Celery/worker not available — run OCR directly
        try:
            from app.services.pdf_ingestor import extract_text
            text = extract_text(str(dest_path))
            if text:
                doc.ocr_text = text
                db.commit()
                db.refresh(doc)
        except Exception:
            pass

    return doc


@router.get("/{meeting_id}", response_model=list[schemas.DocumentOut])
def list_documents(meeting_id: str, db: Session = Depends(get_db)):
    return (
        db.query(models.Document)
        .filter_by(meeting_id=meeting_id)
        .order_by(models.Document.created_at)
        .all()
    )


@router.get("/{meeting_id}/{document_id}", response_model=schemas.DocumentOut)
def get_document(meeting_id: str, document_id: str, db: Session = Depends(get_db)):
    doc = (
        db.query(models.Document)
        .filter_by(meeting_id=meeting_id, document_id=document_id)
        .first()
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


@router.get("/{meeting_id}/{document_id}/file")
def serve_document_file(meeting_id: str, document_id: str, db: Session = Depends(get_db)):
    """Serve the actual PDF file for inline viewing."""
    doc = (
        db.query(models.Document)
        .filter_by(meeting_id=meeting_id, document_id=document_id)
        .first()
    )
    if not doc:
        raise HTTPException(404, "Document not found")

    pdf_path = Path(doc.file_path)
    if not pdf_path.exists():
        raise HTTPException(404, "PDF file not found on disk")

    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )

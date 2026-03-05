#!/usr/bin/env python3
"""
ocr_backfill.py — Run OCR on documents that have a PDF on disk but no extracted text.

Idempotent: skips documents that already have ocr_text unless --force is passed.
Uses the same three-stage extraction pipeline as the main ingest path
(pdfplumber → Tesseract → EasyOCR).

Usage:
    # Process all minutes missing OCR
    python scripts/ocr_backfill.py --document-type minutes

    # All document types
    python scripts/ocr_backfill.py --document-type all

    # Single meeting
    python scripts/ocr_backfill.py --meeting-id <uuid>

    # Dry run — show what would be processed
    python scripts/ocr_backfill.py --document-type minutes --dry-run

    # Re-OCR even if text already exists
    python scripts/ocr_backfill.py --document-type minutes --force
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import models
from app.services.pdf_ingestor import extract_text
from app.config import OCR_TEXT_DIR


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


def _save_ocr_text(db: Session, doc: models.Document, text: str) -> None:
    """Persist OCR text to DB and write a sidecar .txt file."""
    doc.ocr_text = text
    db.commit()

    # Write sidecar .txt next to the PDF for easy inspection
    if doc.file_path:
        stem = Path(doc.file_path).stem
        txt_dir = OCR_TEXT_DIR / doc.meeting_id
        txt_dir.mkdir(parents=True, exist_ok=True)
        (txt_dir / f"{stem}.txt").write_text(text, encoding="utf-8")


def process_document(
    db: Session,
    doc: models.Document,
    meeting_date: str,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    """
    OCR a single document.

    Returns (processed: bool, status_message: str).
    """
    if not doc.file_path:
        return False, "skip — no file_path"

    if not os.path.exists(doc.file_path):
        return False, f"skip — file missing: {doc.file_path}"

    already_done = doc.ocr_text and len(doc.ocr_text.strip()) > 50
    if already_done and not force:
        return False, "skip — already has OCR text"

    if dry_run:
        file_size = os.path.getsize(doc.file_path)
        return True, f"dry-run — would OCR ({file_size // 1024} KB)"

    logger.info("OCR-ing %s (%s)", Path(doc.file_path).name, meeting_date)
    text = extract_text(doc.file_path)
    char_count = len(text.strip())

    if char_count < 50:
        return False, f"warn — extraction yielded only {char_count} chars"

    _save_ocr_text(db, doc, text)
    return True, f"ok — {char_count:,} chars"


def get_documents(
    db: Session,
    document_type: str | None,
    meeting_id: str | None,
    force: bool,
):
    """Return the documents to process."""
    q = db.query(models.Document, models.Meeting.meeting_date) \
          .join(models.Meeting,
                models.Meeting.meeting_id == models.Document.meeting_id)

    if meeting_id:
        q = q.filter(models.Document.meeting_id == meeting_id)

    if document_type and document_type != "all":
        q = q.filter(models.Document.document_type == document_type)

    if not force:
        q = q.filter(
            (models.Document.ocr_text.is_(None)) |
            (models.Document.ocr_text == "") |
            (models.Document.ocr_text.ilike(""))
        )
        # Also include very short (failed) OCR results
        q = q.filter(
            models.Document.file_path.isnot(None)
        )

    q = q.filter(models.Document.file_path.isnot(None))
    q = q.order_by(models.Meeting.meeting_date)
    return q.all()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill OCR text for documents with PDF files on disk"
    )
    parser.add_argument(
        "--document-type",
        default="minutes",
        choices=["minutes", "agenda", "packet", "all"],
        help="Which document type to process (default: minutes)",
    )
    parser.add_argument("--meeting-id", help="Process a single meeting UUID only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be processed without writing")
    parser.add_argument("--force", action="store_true",
                        help="Re-OCR even if ocr_text already exists")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = get_documents(
            db,
            document_type=args.document_type if not args.meeting_id else None,
            meeting_id=args.meeting_id,
            force=args.force,
        )

        # Filter out docs that already have text unless --force
        if not args.force:
            rows = [
                (doc, date) for doc, date in rows
                if not (doc.ocr_text and len(doc.ocr_text.strip()) > 50)
            ]

        print(f"Found {len(rows)} document(s) to process.")

        processed = skipped = warnings = 0
        for doc, meeting_date in rows:
            label = f"{meeting_date}  {doc.document_type:10}  {Path(doc.file_path).name[:55]}"
            ok, status = process_document(
                db, doc, meeting_date,
                dry_run=args.dry_run,
                force=args.force,
            )
            marker = "!" if status.startswith("warn") else (" " if ok else "-")
            print(f"  {marker} {label}: {status}")
            if ok:
                processed += 1
            elif status.startswith("warn"):
                warnings += 1
            else:
                skipped += 1

        action = "would process" if args.dry_run else "processed"
        print(f"\nDone.  {action.capitalize()}: {processed}  "
              f"Skipped: {skipped}  Warnings: {warnings}")

    except Exception as exc:
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

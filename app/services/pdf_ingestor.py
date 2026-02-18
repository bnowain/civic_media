"""
PDF text extraction service.

Strategy:
  1. Attempt native text extraction with pdfplumber.
  2. If extracted text is too sparse (< 50 chars after stripping),
     fall back to Tesseract OCR via pdf2image.

No summarisation. No indexing. Raw text only.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Minimum characters of native text before we assume the PDF is image-only
_MIN_NATIVE_CHARS = 50


def extract_text(pdf_path: str) -> str:
    """
    Extract all text from a PDF.

    Args:
        pdf_path: Absolute path to the PDF file.

    Returns:
        Extracted text as a single string.
        Empty string if extraction completely fails.
    """
    path = Path(pdf_path)
    if not path.exists():
        logger.error("PDF not found: %s", pdf_path)
        return ""

    text = _native_extract(pdf_path)

    if len(text.strip()) < _MIN_NATIVE_CHARS:
        logger.info(
            "Native text too sparse (%d chars), falling back to OCR: %s",
            len(text.strip()), path.name,
        )
        text = _ocr_extract(pdf_path)

    return text


# ── Native extraction ─────────────────────────────────────────────────────────

def _native_extract(pdf_path: str) -> str:
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages.append(page_text)
        result = "\n\n".join(pages)
        logger.info("Native PDF extraction: %d chars from %s", len(result), pdf_path)
        return result
    except Exception as exc:
        logger.warning("pdfplumber failed on %s: %s", pdf_path, exc)
        return ""


# ── OCR fallback ──────────────────────────────────────────────────────────────

def _ocr_extract(pdf_path: str) -> str:
    try:
        from pdf2image import convert_from_path
        import pytesseract

        logger.info("Running Tesseract OCR on %s ...", pdf_path)
        pages = convert_from_path(pdf_path, dpi=300)
        texts = []
        for i, page_img in enumerate(pages):
            page_text = pytesseract.image_to_string(page_img)
            texts.append(page_text)
            logger.debug("OCR page %d: %d chars", i + 1, len(page_text))

        result = "\n\n".join(texts)
        logger.info("OCR complete: %d chars from %s", len(result), pdf_path)
        return result

    except ImportError as exc:
        logger.error(
            "OCR dependencies missing (%s). "
            "Install: pip install pdf2image pytesseract && "
            "apt install tesseract-ocr poppler-utils",
            exc,
        )
        return ""
    except Exception as exc:
        logger.error("OCR failed on %s: %s", pdf_path, exc)
        return ""

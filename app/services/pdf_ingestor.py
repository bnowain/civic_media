"""
PDF text extraction service.

Strategy (three-stage fallback):
  1. Native text extraction with pdfplumber (fast, for digital PDFs).
  2. Tesseract OCR via pdf2image (for scanned printed text).
  3. EasyOCR with GPU (for handwriting, cursive, mixed content).

No summarisation. No indexing. Raw text only.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Minimum characters before triggering the next fallback stage
_MIN_NATIVE_CHARS = 50
_MIN_TESSERACT_CHARS = 50


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

    # Stage 1: Native text extraction (pdfplumber)
    text = _native_extract(pdf_path)

    if len(text.strip()) < _MIN_NATIVE_CHARS:
        logger.info(
            "Native text too sparse (%d chars), falling back to Tesseract: %s",
            len(text.strip()), path.name,
        )
        # Stage 2: Tesseract OCR
        text = _ocr_extract(pdf_path)

    if len(text.strip()) < _MIN_TESSERACT_CHARS:
        logger.info(
            "Tesseract text too sparse (%d chars), falling back to EasyOCR: %s",
            len(text.strip()), path.name,
        )
        # Stage 3: EasyOCR (handles handwriting, cursive, mixed content)
        easyocr_text = _easyocr_extract(pdf_path)
        if len(easyocr_text.strip()) > len(text.strip()):
            text = easyocr_text

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


# ── Tesseract OCR fallback ────────────────────────────────────────────────────

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
            logger.debug("Tesseract page %d: %d chars", i + 1, len(page_text))

        result = "\n\n".join(texts)
        logger.info("Tesseract complete: %d chars from %s", len(result), pdf_path)
        return result

    except ImportError as exc:
        logger.error(
            "Tesseract dependencies missing (%s). "
            "Install: pip install pdf2image pytesseract",
            exc,
        )
        return ""
    except Exception as exc:
        logger.error("Tesseract failed on %s: %s", pdf_path, exc)
        return ""


# ── EasyOCR fallback (handwriting, cursive, mixed) ───────────────────────────

# Cache the EasyOCR reader — loading the model is expensive (~2s)
_easyocr_reader = None


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        try:
            import easyocr
            logger.info("Initializing EasyOCR reader (GPU)...")
            _easyocr_reader = easyocr.Reader(["en"], gpu=True)
            logger.info("EasyOCR reader ready")
        except Exception as exc:
            logger.error("EasyOCR init failed: %s", exc)
            try:
                import easyocr
                logger.info("Retrying EasyOCR with CPU...")
                _easyocr_reader = easyocr.Reader(["en"], gpu=False)
            except Exception:
                pass
    return _easyocr_reader


def _easyocr_extract(pdf_path: str) -> str:
    try:
        from pdf2image import convert_from_path
        import numpy as np

        reader = _get_easyocr_reader()
        if reader is None:
            logger.error("EasyOCR not available")
            return ""

        logger.info("Running EasyOCR on %s ...", pdf_path)
        pages = convert_from_path(pdf_path, dpi=300)
        texts = []

        for i, page_img in enumerate(pages):
            # EasyOCR expects numpy array
            img_array = np.array(page_img)
            results = reader.readtext(img_array, detail=0, paragraph=True)
            page_text = "\n".join(results)
            texts.append(page_text)
            logger.debug("EasyOCR page %d: %d chars", i + 1, len(page_text))

        result = "\n\n".join(texts)
        logger.info("EasyOCR complete: %d chars from %s", len(result), pdf_path)
        return result

    except ImportError as exc:
        logger.error("EasyOCR dependencies missing (%s). Install: pip install easyocr", exc)
        return ""
    except Exception as exc:
        logger.error("EasyOCR failed on %s: %s", pdf_path, exc)
        return ""

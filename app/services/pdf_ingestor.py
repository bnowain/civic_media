"""
PDF text extraction service.

Strategy (two-stage):
  1. Native text extraction with pdfplumber (fast, for digital PDFs).
  2. Surya OCR (PyTorch + CUDA) — transformer-based foundation model for scanned pages.

PDF rasterization for stage 2 uses PyMuPDF (fitz) — no Poppler required.

No summarisation. No indexing. Raw text only.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Minimum characters before triggering Surya OCR fallback
_MIN_NATIVE_CHARS = 50

# Cached Surya predictors — loading models is expensive (~2–5s on first call)
_surya_foundation = None
_surya_rec = None
_surya_det = None


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

    # Stage 1: Native text extraction (pdfplumber — instant for digital PDFs)
    text = _native_extract(pdf_path)

    if len(text.strip()) < _MIN_NATIVE_CHARS:
        logger.info(
            "Native text too sparse (%d chars), falling back to Surya OCR: %s",
            len(text.strip()), path.name,
        )
        # Stage 2: Surya OCR (PyTorch + CUDA, transformer-based)
        text = _surya_extract(pdf_path)

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


# ── PDF rasterizer ────────────────────────────────────────────────────────────

def _pdf_to_images(pdf_path: str, dpi: int = 300) -> list:
    """Rasterize PDF pages to PIL Image objects using PyMuPDF (no Poppler needed)."""
    import fitz  # PyMuPDF
    from PIL import Image

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    doc = fitz.open(pdf_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        images.append(img)
    doc.close()
    return images


# ── Surya OCR (PyTorch + CUDA) ────────────────────────────────────────────────

def _get_surya_predictors():
    """Load and cache Surya foundation, recognition, and detection predictors."""
    global _surya_foundation, _surya_rec, _surya_det
    if _surya_rec is None:
        try:
            from surya.foundation import FoundationPredictor
            from surya.recognition import RecognitionPredictor
            from surya.detection import DetectionPredictor

            logger.info("Initializing Surya OCR predictors (GPU)...")
            _surya_foundation = FoundationPredictor()
            _surya_rec = RecognitionPredictor(foundation_predictor=_surya_foundation)
            _surya_det = DetectionPredictor()
            logger.info("Surya OCR predictors ready")
        except Exception as exc:
            logger.error("Surya OCR init failed: %s", exc)
            _surya_foundation = _surya_rec = _surya_det = None
    return _surya_rec, _surya_det


def _surya_extract(pdf_path: str) -> str:
    try:
        rec, det = _get_surya_predictors()
        if rec is None:
            logger.error("Surya OCR not available")
            return ""

        logger.info("Running Surya OCR on %s ...", pdf_path)
        images = _pdf_to_images(pdf_path, dpi=300)

        if not images:
            return ""

        predictions = rec(images, det_predictor=det)

        texts = []
        for i, pred in enumerate(predictions):
            page_text = "\n".join(line.text for line in pred.text_lines)
            texts.append(page_text)
            logger.debug("Surya page %d: %d chars", i + 1, len(page_text))

        result = "\n\n".join(texts)
        logger.info("Surya OCR complete: %d chars from %s", len(result), pdf_path)
        return result

    except Exception as exc:
        logger.error("Surya OCR failed on %s: %s", pdf_path, exc)
        return ""

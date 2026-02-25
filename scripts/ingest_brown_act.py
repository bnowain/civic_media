#!/usr/bin/env python3
"""
ingest_brown_act.py — Extract text from the Brown Act PDF and populate
the reference_documents and reference_sections tables.

Each statutory section (§54950, §54952.2, etc.) becomes one ReferenceSection
row — the unit of chunking for RAG embedding.

Usage:
    python scripts/ingest_brown_act.py
    python scripts/ingest_brown_act.py --pdf E:/path/to/Brown-Act-2026.pdf
    python scripts/ingest_brown_act.py --force   # re-ingest if already exists
"""

import argparse
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_PDF = r"E:\0-Automated-Apps\Brown-Act-2026.pdf"

# ── Text extraction ───────────────────────────────────────────────────────────

def extract_pdf_text(pdf_path: str) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("PyMuPDF not installed. Run: pip install pymupdf")

    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        pages.append(page.get_text())
    return "\n".join(pages)


# ── Section splitting ─────────────────────────────────────────────────────────

# Match section headers like "54950." or "54952.2." at the start of a paragraph
_SECTION_START = re.compile(
    r'(?:^|\n)(5495\d[\d.]*)\.\s{1,4}',
    re.MULTILINE,
)

# Known short titles for key compliance sections (extend as needed)
_SECTION_TITLES = {
    "54950":    "Legislative findings — open government",
    "54950.5":  "Short title — Ralph M. Brown Act",
    "54951":    "Definition — local agency",
    "54952":    "Definition — legislative body",
    "54952.1":  "Elect-to-serve members bound by Brown Act",
    "54952.2":  "Definition — meeting; serial meeting prohibition",
    "54952.6":  "Definition — action taken",
    "54952.7":  "Requirement to provide copy of Brown Act to members",
    "54953":    "Open and public meetings; teleconference requirements",
    "54953.3":  "No registration required as condition of attendance",
    "54953.4":  "Agenda translation; remote access; public participation",
    "54954":    "Regular meeting location and time",
    "54954.1":  "Agenda distribution to requesters",
    "54954.2":  "72-hour agenda posting requirement; item descriptions",
    "54954.3":  "Public comment — right to address legislative body",
    "54954.4":  "Public comment — teleconference",
    "54954.5":  "Closed session subject matter — permissible exceptions",
    "54955":    "Adjourned meetings; continuances",
    "54955.1":  "Continuance of public hearings",
    "54956":    "Special meetings",
    "54956.5":  "Emergency meetings",
    "54956.6":  "Closed session — compensation of officers/employees",
    "54956.7":  "Closed session — license applicants with criminal history",
    "54956.75": "Closed session — audit discussions",
    "54956.8":  "Closed session — real property negotiations",
    "54956.81": "Closed session — investments",
    "54956.86": "Closed session — standing committee on audit",
    "54956.9":  "Closed session — pending/threatened litigation",
    "54956.95": "Closed session — liability claims",
    "54956.96": "Closed session — JPA exposure to litigation",
    "54956.97": "Closed session — insurance pooling",
    "54957":    "Closed session — personnel matters; public employee appointments",
    "54957.1":  "Report of closed session action",
    "54957.2":  "Minutes of closed session",
    "54957.5":  "Public inspection of agenda materials",
    "54957.6":  "Closed session — labor negotiations",
    "54957.7":  "Disclosure statement for closed session",
    "54957.8":  "Closed session — multijurisdictional drug enforcement",
    "54957.9":  "Disorderly conduct; adjournment",
    "54957.95": "Disruption of meeting; removal; warning requirements",
    "54957.96": "Disruption via teleconference or electronic means",
    "54958":    "Chapter applicability",
    "54959":    "Misdemeanor — unauthorized closed session",
    "54960":    "Civil action to stop/prevent violations; demand to cure",
    "54960.1":  "Action to determine applicability of chapter",
    "54960.2":  "Cure and correct — settlement procedure",
    "54960.5":  "Injunction; costs and attorney fees",
    "54961":    "Prohibition on excluding persons based on protected class",
    "54962":    "Prohibition on secret ballot",
    "54963":    "Confidentiality of closed session information",
}


def split_into_sections(full_text: str) -> list[tuple[str, str, str]]:
    """
    Split full Brown Act text into sections.
    Returns list of (section_num, title, text).
    Deduplicates by section_num — keeps the longest text (body over TOC entry).
    """
    matches = list(_SECTION_START.finditer(full_text))
    if not matches:
        return [("", "Full Text", full_text)]

    raw = []
    for i, match in enumerate(matches):
        section_num = match.group(1)
        start = match.start(1)
        end = matches[i + 1].start(1) if i + 1 < len(matches) else len(full_text)
        text = full_text[start:end].strip()
        title = _SECTION_TITLES.get(section_num, "")
        raw.append((section_num, title, text))

    # Deduplicate: for each section_num keep the entry with the most text
    seen: dict[str, tuple[str, str, str]] = {}
    for entry in raw:
        num = entry[0]
        if num not in seen or len(entry[2]) > len(seen[num][2]):
            seen[num] = entry

    # Preserve original ordering (first occurrence's position)
    order = []
    for entry in raw:
        num = entry[0]
        if seen.get(num) == entry and num not in [o[0] for o in order]:
            order.append(seen[num])

    return order


# ── DB ingest ─────────────────────────────────────────────────────────────────

def ingest(pdf_path: str, force: bool = False) -> None:
    from app.database import SessionLocal
    from app import models

    db = SessionLocal()
    try:
        existing = db.query(models.ReferenceDocument) \
                     .filter(models.ReferenceDocument.name == "Brown Act 2026") \
                     .first()

        if existing and not force:
            print(f"Brown Act already ingested (ref_doc_id={existing.ref_doc_id}). "
                  f"Use --force to re-ingest.")
            return

        if existing and force:
            db.delete(existing)
            db.commit()
            print("Deleted existing Brown Act record, re-ingesting…")

        print(f"Extracting text from {pdf_path}…")
        full_text = extract_pdf_text(pdf_path)
        sections  = split_into_sections(full_text)
        print(f"Found {len(sections)} sections.")

        ref_doc = models.ReferenceDocument(
            ref_doc_id=str(uuid.uuid4()),
            name="Brown Act 2026",
            doc_type="law",
            year=2026,
            source_file=pdf_path,
            full_text=full_text,
        )
        db.add(ref_doc)
        db.flush()

        for seq, (section_num, title, text) in enumerate(sections):
            db.add(models.ReferenceSection(
                section_id=str(uuid.uuid4()),
                ref_doc_id=ref_doc.ref_doc_id,
                section_num=section_num or None,
                title=title or None,
                text=text,
                seq=seq,
            ))

        db.commit()
        print(f"Done. Inserted 1 reference document + {len(sections)} sections.")
        print(f"  ref_doc_id: {ref_doc.ref_doc_id}")
        print(f"\nNext step: embed sections into ChromaDB via Atlas RAG pre-index.")

    except Exception as exc:
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Brown Act PDF into reference DB")
    parser.add_argument("--pdf", default=DEFAULT_PDF,
                        help=f"Path to Brown Act PDF (default: {DEFAULT_PDF})")
    parser.add_argument("--force", action="store_true",
                        help="Re-ingest even if already exists")
    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        sys.exit(f"PDF not found: {args.pdf}")

    ingest(args.pdf, force=args.force)


if __name__ == "__main__":
    main()

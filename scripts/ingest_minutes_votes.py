#!/usr/bin/env python3
"""
ingest_minutes_votes.py — Backfill and ongoing ingest of meeting votes
parsed from minutes OCR text.

Idempotent: skips meetings whose votes are already ingested unless --force.
Records parse status and unmatched paragraphs in Document.minutes_parse_status
and Document.minutes_parse_notes so format gaps are surfaced, not silently lost.

Usage:
    # Full backfill — all meetings with minutes OCR text
    python scripts/ingest_minutes_votes.py

    # Single meeting
    python scripts/ingest_minutes_votes.py --meeting-id <uuid>

    # Dry run (parse but don't write)
    python scripts/ingest_minutes_votes.py --dry-run

    # Re-process even if already ingested
    python scripts/ingest_minutes_votes.py --force

    # Show only meetings with partial/unrecognized parse status
    python scripts/ingest_minutes_votes.py --show-unmatched
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import func
from sqlalchemy.orm import Session

import json

from app.database import SessionLocal
from app import models
from app.services.minutes_parser import ParseResult, parse_minutes


# ── Error log setup ───────────────────────────────────────────────────────────
# Unmatched vote-like paragraphs (parse failures) are written here for review
# and future regex pattern development.

_LOG_DIR  = Path(__file__).parent.parent / "logs"
_ERR_LOG  = _LOG_DIR / "minutes_parse_errors.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


def _log_unmatched(meeting_date: str, title: str, unmatched: list[str]) -> None:
    """Append unmatched vote paragraphs to the error log for later review."""
    if not unmatched:
        return
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _ERR_LOG.open("a", encoding="utf-8") as f:
        f.write(
            f"\n{'='*72}\n"
            f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  "
            f"meeting={meeting_date}  {title[:55]}\n"
            f"  {len(unmatched)} unmatched paragraph(s):\n"
            f"{'='*72}\n"
        )
        for i, para in enumerate(unmatched, 1):
            f.write(f"\n[{i}] {para[:500]}\n")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _already_ingested(db: Session, meeting_id: str) -> bool:
    count = db.query(func.count(models.MeetingVote.vote_id)) \
               .filter(models.MeetingVote.meeting_id == meeting_id) \
               .scalar()
    return count > 0


def _has_failed_parse(db: Session, meeting_id: str) -> bool:
    """Return True if any minutes document has a partial or unrecognized parse status.

    These meetings are automatically re-queued on every backfill run so that
    fixing a parser pattern picks them up without needing --force.
    """
    count = (
        db.query(func.count(models.Document.document_id))
          .filter(
              models.Document.meeting_id == meeting_id,
              models.Document.document_type == "minutes",
              models.Document.minutes_parse_status.in_(["partial", "unrecognized"]),
          )
          .scalar()
    )
    return count > 0


def _build_person_lookup(db: Session) -> dict[str, str]:
    """Build last-name → person_id mapping from roster + people table."""
    roster_path = Path(__file__).parent.parent / "config" / "supervisors.json"
    if not roster_path.exists():
        return {}
    try:
        data = json.loads(roster_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    last_to_full: dict[str, str] = {}
    for entry in data.get("district_supervisors", []):
        full = entry.get("supervisor", "")
        last = full.rsplit(" ", 1)[-1]
        if last not in last_to_full:
            last_to_full[last] = full
    # Resolve to person_ids
    people = {p.canonical_name: p.person_id for p in db.query(models.Person).all()}
    return {last: people[full] for last, full in last_to_full.items() if full in people}


_person_cache: dict[str, str] | None = None


def _get_person_id(db: Session, last_name: str | None) -> str | None:
    global _person_cache
    if not last_name:
        return None
    if _person_cache is None:
        _person_cache = _build_person_lookup(db)
    return _person_cache.get(last_name)


def _save_result(db: Session, result: ParseResult, doc: models.Document) -> int:
    saved = 0
    for v in result.votes:
        row = models.MeetingVote(
            vote_id=v.vote_id,
            meeting_id=v.meeting_id,
            document_id=v.document_id,
            meeting_date=v.meeting_date,
            group_name=v.group_name,
            agenda_section=v.agenda_section,
            item_description=v.item_description,
            resolution_number=v.resolution_number,
            outcome=v.outcome,
            vote_tally=v.vote_tally,
            mover=v.mover,
            seconder=v.seconder,
            mover_person_id=_get_person_id(db, v.mover),
            seconder_person_id=_get_person_id(db, v.seconder),
        )
        db.add(row)
        for name in v.yes_members:
            db.add(models.VoteMember(vote_id=v.vote_id, member_name=name, vote_value="yes",
                                     person_id=_get_person_id(db, name)))
        for name in v.no_members:
            db.add(models.VoteMember(vote_id=v.vote_id, member_name=name, vote_value="no",
                                     person_id=_get_person_id(db, name)))
        for name in v.absent_members:
            db.add(models.VoteMember(vote_id=v.vote_id, member_name=name, vote_value="absent",
                                     person_id=_get_person_id(db, name)))
        saved += 1

    doc.minutes_parse_status = result.parse_status
    doc.minutes_parse_notes  = result.parse_notes_json
    db.commit()
    return saved


def _delete_votes(db: Session, meeting_id: str) -> None:
    for row in db.query(models.MeetingVote).filter(
            models.MeetingVote.meeting_id == meeting_id).all():
        db.delete(row)
    db.commit()


# ── Core ingest ───────────────────────────────────────────────────────────────

def ingest_meeting(
    db: Session,
    meeting: models.Meeting,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[int, str]:
    minutes_docs = [
        d for d in meeting.documents
        if d.document_type == "minutes" and d.ocr_text
    ]

    if not minutes_docs:
        return 0, "skip — no minutes with OCR text"

    already_done = _already_ingested(db, meeting.meeting_id)
    needs_retry  = _has_failed_parse(db, meeting.meeting_id)

    if already_done and not force and not needs_retry:
        return 0, "skip — already ingested"

    if (force or needs_retry) and not dry_run:
        _delete_votes(db, meeting.meeting_id)

    total = 0
    statuses = []
    for doc in minutes_docs:
        result = parse_minutes(
            ocr_text=doc.ocr_text,
            meeting_id=meeting.meeting_id,
            document_id=doc.document_id,
            meeting_date=meeting.meeting_date,
            group_name=meeting.group_name,
        )
        statuses.append(result.parse_status)
        if result.unmatched_paragraphs:
            _log_unmatched(
                meeting.meeting_date or "unknown",
                meeting.title or "",
                result.unmatched_paragraphs,
            )
        if dry_run:
            total += len(result.votes)
            if result.unmatched_paragraphs:
                print(f"    [DRY-RUN unmatched] {len(result.unmatched_paragraphs)} "
                      f"vote-like paragraphs didn't parse:")
                for p in result.unmatched_paragraphs[:3]:
                    print(f"      • {p[:120]}")
        else:
            total += _save_result(db, result, doc)

    combined = "partial" if "partial" in statuses or "unrecognized" in statuses else statuses[0]
    action = "dry-run" if dry_run else ("retried" if needs_retry and not force else "ingested")
    return total, f"{action} [{combined}]"


# ── Entry point ───────────────────────────────────────────────────────────────

def get_failed_meetings(db) -> list:
    """Return all meetings whose minutes have a partial or unrecognized parse status."""
    return (
        db.query(models.Meeting)
          .join(models.Document,
                (models.Document.meeting_id == models.Meeting.meeting_id) &
                (models.Document.document_type == "minutes") &
                (models.Document.ocr_text.isnot(None)) &
                (models.Document.minutes_parse_status.in_(["partial", "unrecognized"])))
          .distinct()
          .order_by(models.Meeting.meeting_date)
          .all()
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest meeting votes from minutes OCR text"
    )
    parser.add_argument("--meeting-id",     help="Process a single meeting UUID")
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--force",          action="store_true",
                        help="Re-process even if votes already exist")
    parser.add_argument("--retry-failed",   action="store_true",
                        help="Re-process all meetings with partial or unrecognized parse "
                             "status (implies --force for those meetings)")
    parser.add_argument("--show-unmatched", action="store_true",
                        help="Show only meetings with unmatched/partial parse")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.meeting_id:
            meetings = db.query(models.Meeting) \
                         .filter(models.Meeting.meeting_id == args.meeting_id).all()
        elif args.retry_failed:
            meetings = get_failed_meetings(db)
            print(f"Found {len(meetings)} meeting(s) with partial/unrecognized parse status.")
            # --retry-failed always forces re-processing
            args.force = True
        else:
            meetings = (
                db.query(models.Meeting)
                  .join(models.Document,
                        (models.Document.meeting_id == models.Meeting.meeting_id) &
                        (models.Document.document_type == "minutes") &
                        (models.Document.ocr_text.isnot(None)))
                  .distinct()
                  .order_by(models.Meeting.meeting_date)
                  .all()
            )

        print(f"Processing {len(meetings)} meeting(s)…")
        total_votes = ingested = skipped = partial = 0

        for meeting in meetings:
            count, status = ingest_meeting(
                db, meeting, dry_run=args.dry_run, force=args.force
            )
            label = f"{meeting.meeting_date} — {meeting.title[:55]}"

            is_problem = "partial" in status or "unrecognized" in status
            if args.show_unmatched and not is_problem:
                continue

            marker = "!" if is_problem else " "
            print(f"  {marker} {label}: {status} ({count} votes)")
            total_votes += count
            if "skip" in status:
                skipped += 1
            else:
                ingested += 1
            if is_problem:
                partial += 1

        action = "parsed" if args.dry_run else "saved"
        print(f"\nDone.  Processed: {ingested}  Skipped: {skipped}  "
              f"Partial/unrecognized: {partial}  "
              f"Total votes {action}: {total_votes}")
        if partial:
            print("  Run with --show-unmatched to see problematic meetings.")
            print("  Unmatched paragraphs are stored in Document.minutes_parse_notes.")

    except Exception as exc:
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

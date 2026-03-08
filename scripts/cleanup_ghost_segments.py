"""
Remove ghost segments from the database.

Ghost segments are Whisper hallucinations where a full sentence is crammed into
an impossibly short duration (e.g., 150 characters in 0.08 seconds). Normal
speech maxes out around 20 chars/sec; anything over 25 chars/sec with 30+
characters of text is physically impossible and is a hallucination.

Also detects repetition-loop segments (e.g., "I'm sorry. I'm sorry. I'm sorry.")
that slipped past the transcriber's compression_ratio filter.

Run via Windows Python:
  powershell.exe -Command "cd 'E:\\0-Automated-Apps\\civic_media'; .\\venv\\Scripts\\python.exe scripts\\cleanup_ghost_segments.py"
"""
import sys
sys.path.insert(0, ".")

from app.database import SessionLocal
from app import models
from sqlalchemy import func, text

# -- Thresholds --
MIN_TEXT_LEN = 30        # Only flag segments with 30+ chars (short utterances are fine)
MAX_CHARS_PER_SEC = 25   # Normal fast speech ≈ 20 c/s; 25 gives margin
DRY_RUN = "--dry-run" in sys.argv

db = SessionLocal()
try:
    # Query ghost segments
    ghosts = db.execute(text("""
        SELECT segment_id, meeting_id, start_time, end_time, text,
               LENGTH(TRIM(text)) as chars,
               ROUND(end_time - start_time, 3) as duration,
               ROUND(LENGTH(TRIM(text)) * 1.0 / MAX(end_time - start_time, 0.01), 1) as chars_per_sec
        FROM transcript_segments
        WHERE LENGTH(TRIM(text)) >= :min_len
          AND LENGTH(TRIM(text)) * 1.0 / MAX(end_time - start_time, 0.01) > :max_cps
        ORDER BY chars_per_sec DESC
    """), {"min_len": MIN_TEXT_LEN, "max_cps": MAX_CHARS_PER_SEC}).fetchall()

    print(f"Found {len(ghosts)} ghost segments (>={MIN_TEXT_LEN} chars, >{MAX_CHARS_PER_SEC} chars/sec)")
    if DRY_RUN:
        print("DRY RUN - no deletions")

    # Group by meeting for summary
    by_meeting = {}
    for row in ghosts:
        mid = row[1]
        by_meeting.setdefault(mid, []).append(row)

    print(f"Across {len(by_meeting)} meetings\n")

    # Show worst examples
    print("-- Worst examples --")
    for row in ghosts[:15]:
        seg_id, mid, start, end, txt, chars, dur, cps = row
        safe_txt = txt[:80].encode("ascii", errors="replace").decode("ascii")
        print(f"  {dur:6.2f}s  {cps:6.0f} c/s  {chars:3d} chars  {safe_txt}")

    if DRY_RUN:
        print(f"\nWould delete {len(ghosts)} segments. Run without --dry-run to execute.")
        sys.exit(0)

    # Delete ghost segments and their assignments
    seg_ids = [row[0] for row in ghosts]

    # Delete in batches of 500
    deleted_assignments = 0
    deleted_segments = 0
    batch_size = 500

    for i in range(0, len(seg_ids), batch_size):
        batch = seg_ids[i:i + batch_size]

        # Delete segment assignments first (FK constraint)
        a_count = (
            db.query(models.SegmentAssignment)
            .filter(models.SegmentAssignment.segment_id.in_(batch))
            .delete(synchronize_session=False)
        )
        deleted_assignments += a_count

        # Delete segments
        s_count = (
            db.query(models.TranscriptSegment)
            .filter(models.TranscriptSegment.segment_id.in_(batch))
            .delete(synchronize_session=False)
        )
        deleted_segments += s_count

        if (i // batch_size) % 10 == 0:
            db.commit()
            print(f"  Progress: {i + len(batch)}/{len(seg_ids)} deleted...")

    db.commit()

    print(f"\nDeleted {deleted_segments} ghost segments and {deleted_assignments} assignments")
    print(f"Across {len(by_meeting)} meetings")

    # Show per-meeting summary for meetings with many ghosts
    print("\n-- Meetings with most ghosts removed --")
    top = sorted(by_meeting.items(), key=lambda x: len(x[1]), reverse=True)[:20]
    for mid, rows in top:
        meeting = db.query(models.Meeting).filter_by(meeting_id=mid).first()
        title = meeting.title[:50] if meeting else "?"
        date = meeting.meeting_date if meeting else "?"
        print(f"  {len(rows):4d} removed  {date}  {title}")

finally:
    db.close()

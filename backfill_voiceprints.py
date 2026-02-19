"""
Backfill voiceprints from verified segment assignments.

Run from civic_media root:
    python backfill_voiceprints.py

When segments were confirmed before embeddings existed, the voiceprint
was silently skipped. This script finds all verified assignments where
the segment now has an embedding and adds the missing voiceprints.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app.database import SessionLocal
from app import models

db = SessionLocal()

# Find verified assignments where segment has an embedding but no voiceprint was added
verified = (
    db.query(models.SegmentAssignment)
    .filter_by(verified=True)
    .all()
)

added = 0
skipped_no_embedding = 0
skipped_no_person = 0

for assign in verified:
    segment = db.query(models.TranscriptSegment).filter_by(
        segment_id=assign.segment_id
    ).first()

    if not segment or not segment.embedding:
        skipped_no_embedding += 1
        continue

    if not assign.predicted_person_id:
        skipped_no_person += 1
        continue

    # Check if a voiceprint already exists for this segment's embedding
    # (avoid duplicates if this script is run twice)
    existing = db.query(models.Voiceprint).filter_by(
        person_id=assign.predicted_person_id,
        embedding=segment.embedding,
    ).first()

    if existing:
        continue

    vp = models.Voiceprint(
        person_id=assign.predicted_person_id,
        embedding=segment.embedding,
    )
    db.add(vp)
    added += 1

    person = db.query(models.Person).filter_by(
        person_id=assign.predicted_person_id
    ).first()
    name = person.canonical_name if person else assign.predicted_person_id
    print(f"  Added voiceprint: {name} <- segment {assign.segment_id[:8]}...")

db.commit()

print(f"\nDone.")
print(f"  Voiceprints added:          {added}")
print(f"  Skipped (no embedding):     {skipped_no_embedding}")
print(f"  Skipped (no person linked): {skipped_no_person}")

# Show final state
from sqlalchemy import func
rows = (
    db.query(models.Person.canonical_name,
             func.count(models.Voiceprint.voiceprint_id))
    .join(models.Voiceprint,
          models.Person.person_id == models.Voiceprint.person_id)
    .group_by(models.Person.person_id)
    .all()
)
print("\nVoiceprints per person:")
for name, count in rows:
    print(f"  {name}: {count}")

db.close()
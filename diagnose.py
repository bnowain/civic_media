import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app.database import SessionLocal
from app import models
from sqlalchemy import func

db = SessionLocal()

# How many segments have embeddings?
total    = db.query(models.TranscriptSegment).count()
with_emb = db.query(models.TranscriptSegment).filter(
    models.TranscriptSegment.embedding.isnot(None)
).count()
print(f"Segments: {total} total, {with_emb} with embeddings, {total - with_emb} WITHOUT")

# How many voiceprints exist?
vp_count = db.query(models.Voiceprint).count()
print(f"Voiceprints in DB: {vp_count}")

# Voiceprints per person
rows = (
    db.query(models.Person.canonical_name,
             func.count(models.Voiceprint.voiceprint_id))
    .join(models.Voiceprint,
          models.Person.person_id == models.Voiceprint.person_id)
    .group_by(models.Person.person_id)
    .all()
)
if rows:
    for name, count in rows:
        print(f"  {name}: {count} voiceprints")
else:
    print("  *** NO voiceprints found — this is why matching is doing nothing ***")

# Sample a segment to check embedding shape
seg = db.query(models.TranscriptSegment).filter(
    models.TranscriptSegment.embedding.isnot(None)
).first()
if seg:
    import numpy as np, io
    arr = np.load(io.BytesIO(seg.embedding))
    print(f"\nSample embedding shape: {arr.shape}, dtype: {arr.dtype}")
else:
    print("\n*** No segments have embeddings stored ***")

db.close()
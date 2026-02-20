"""Add tagged column to segment_assignments."""
import sqlite3, pathlib

DB = pathlib.Path("database/civic_media.db")
conn = sqlite3.connect(DB)
cols = [r[1] for r in conn.execute("PRAGMA table_info(segment_assignments)")]
if "tagged" not in cols:
    conn.execute("ALTER TABLE segment_assignments ADD COLUMN tagged BOOLEAN DEFAULT 0")
    conn.commit()
    print("Added 'tagged' column.")
else:
    print("'tagged' column already exists.")
conn.close()

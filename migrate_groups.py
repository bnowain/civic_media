"""
Migration: governing_bodies → groups

Renames:
  TABLE  governing_bodies          → groups
  COLUMN governing_body_id         → group_id        (on governing_bodies/groups)
  COLUMN body_type                 → group_type       (on governing_bodies/groups)
  COLUMN governing_body  (text)    → group_name       (on meetings, meeting_votes)
  COLUMN governing_body_id (FK)    → group_id         (on meetings)

Also backfills group records for every distinct governing_body text value that
doesn't already have a governing_bodies row (Redding City Council, shows, etc.)
and wires up group_id on all meetings.

Run from civic_media project root:
    python migrate_groups.py

The server MUST be stopped before running this. Back up the DB first.
"""

import sys
import uuid
from pathlib import Path

# ── resolve DB path the same way the app does ────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
try:
    from app.config import DATABASE_PATH
    DB = str(DATABASE_PATH)
except Exception:
    DB = str(Path(__file__).parent / "database" / "civic_media.db")

import sqlite3

print(f"Database: {DB}")

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row


def run():
    # ── read current counts for post-migration verification ──────────────────
    gb_count = con.execute("SELECT COUNT(*) FROM governing_bodies").fetchone()[0]
    m_count  = con.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    mv_count = con.execute("SELECT COUNT(*) FROM meeting_votes").fetchone()[0]
    print(f"Before: {gb_count} governing_bodies, {m_count} meetings, {mv_count} meeting_votes")

    # ── build group_type mapping: category → group_type ─────────────────────
    # Derive from meeting category. A body used ONLY for meetings → government.
    # A body used for audio/web_series → show.
    # If a body is used for BOTH (unlikely but handle) → government wins.
    body_categories = {}
    for row in con.execute(
        "SELECT DISTINCT governing_body, category FROM meetings WHERE governing_body != ''"
    ):
        name, cat = row["governing_body"], row["category"]
        if name not in body_categories:
            body_categories[name] = cat
        elif cat == "meeting":
            body_categories[name] = "meeting"  # government wins

    def cat_to_group_type(cat):
        if cat == "meeting":
            return "government"
        return "show"

    # ── also capture existing governing_bodies rows ──────────────────────────
    existing_gb = {
        row["name"]: {
            "group_id":    row["governing_body_id"],
            "display_name": row["display_name"],
            "default_venue_id": row["default_venue_id"],
            "created_at":  row["created_at"],
        }
        for row in con.execute("SELECT * FROM governing_bodies")
    }

    # ── build complete groups map: name → {group_id, group_type, ...} ────────
    # Start from existing governing_bodies, then fill in any names found in meetings
    # that don't have a governing_bodies row yet.
    groups_map = {}

    for name, data in existing_gb.items():
        cat = body_categories.get(name, "meeting")
        groups_map[name] = {
            "group_id":        data["group_id"],
            "display_name":    data["display_name"] or name,
            "group_type":      cat_to_group_type(cat),
            "default_venue_id": data["default_venue_id"],
            "created_at":      data["created_at"],
        }

    for name, cat in body_categories.items():
        if name not in groups_map:
            groups_map[name] = {
                "group_id":        str(uuid.uuid4()),
                "display_name":    name,
                "group_type":      cat_to_group_type(cat),
                "default_venue_id": None,
                "created_at":      "2026-02-27 00:00:00.000000",
            }

    print(f"Groups to create: {len(groups_map)}")
    for name, g in groups_map.items():
        print(f"  {g['group_id'][:8]}... {name!r} -> {g['group_type']}")

    # ── begin transaction ─────────────────────────────────────────────────────
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("BEGIN")

    try:
        # Step 1: Create groups table
        con.execute("""
            CREATE TABLE groups (
                group_id         TEXT NOT NULL,
                name             TEXT NOT NULL,
                display_name     TEXT,
                group_type       TEXT,
                default_venue_id TEXT REFERENCES venues(venue_id),
                created_at       DATETIME,
                PRIMARY KEY (group_id),
                UNIQUE (name)
            )
        """)

        # Step 2: Populate groups
        for name, g in groups_map.items():
            con.execute(
                "INSERT INTO groups (group_id, name, display_name, group_type, default_venue_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (g["group_id"], name, g["display_name"], g["group_type"],
                 g["default_venue_id"], g["created_at"])
            )

        # Step 3: Recreate meetings with new column names
        # Old: governing_body (text), governing_body_id (FK → governing_bodies)
        # New: group_name (text),     group_id (FK → groups)
        con.execute("""
            CREATE TABLE meetings_new (
                meeting_id          VARCHAR NOT NULL,
                group_name          VARCHAR NOT NULL,
                meeting_type        VARCHAR NOT NULL,
                meeting_date        VARCHAR NOT NULL,
                title               VARCHAR NOT NULL,
                media_directory     VARCHAR,
                created_at          DATETIME,
                category            TEXT NOT NULL DEFAULT 'meeting',
                group_id            TEXT REFERENCES groups(group_id),
                processed_at        DATETIME,
                description         TEXT,
                source_url          TEXT,
                thumbnail_url       TEXT,
                primegov_id         INTEGER,
                video_url           TEXT,
                agenda_url          TEXT,
                minutes_url         TEXT,
                packet_url          TEXT,
                venue_id            TEXT REFERENCES venues(venue_id),
                summary_short       TEXT,
                summary_long        TEXT,
                summary_updated_at  TIMESTAMP,
                page_url            TEXT,
                PRIMARY KEY (meeting_id)
            )
        """)

        # Step 4: Copy meetings data — resolve group_id from groups_map by governing_body name
        old_meetings = con.execute("SELECT * FROM meetings").fetchall()
        for m in old_meetings:
            m = dict(m)
            gb_name = m.get("governing_body", "")
            gb_id   = m.get("governing_body_id")
            # Prefer the existing FK if it resolves to a group; otherwise look up by name
            if gb_id and any(g["group_id"] == gb_id for g in groups_map.values()):
                new_group_id = gb_id
            else:
                new_group_id = groups_map[gb_name]["group_id"] if gb_name in groups_map else None

            con.execute("""
                INSERT INTO meetings_new (
                    meeting_id, group_name, meeting_type, meeting_date, title,
                    media_directory, created_at, category, group_id, processed_at,
                    description, source_url, thumbnail_url, primegov_id, video_url,
                    agenda_url, minutes_url, packet_url, venue_id,
                    summary_short, summary_long, summary_updated_at, page_url
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                m["meeting_id"], gb_name, m["meeting_type"], m["meeting_date"], m["title"],
                m.get("media_directory"), m.get("created_at"), m.get("category","meeting"),
                new_group_id, m.get("processed_at"),
                m.get("description"), m.get("source_url"), m.get("thumbnail_url"),
                m.get("primegov_id"), m.get("video_url"),
                m.get("agenda_url"), m.get("minutes_url"), m.get("packet_url"),
                m.get("venue_id"),
                m.get("summary_short"), m.get("summary_long"), m.get("summary_updated_at"),
                m.get("page_url"),
            ))

        # Step 5: Recreate meeting_votes with group_name instead of governing_body
        con.execute("""
            CREATE TABLE meeting_votes_new (
                vote_id           VARCHAR NOT NULL,
                meeting_id        VARCHAR NOT NULL,
                document_id       VARCHAR,
                meeting_date      VARCHAR,
                group_name        VARCHAR,
                agenda_section    VARCHAR,
                item_description  TEXT NOT NULL,
                resolution_number VARCHAR,
                outcome           VARCHAR NOT NULL,
                vote_tally        VARCHAR,
                mover             VARCHAR,
                seconder          VARCHAR,
                source            VARCHAR NOT NULL,
                created_at        DATETIME,
                PRIMARY KEY (vote_id),
                FOREIGN KEY(meeting_id)  REFERENCES meetings(meeting_id),
                FOREIGN KEY(document_id) REFERENCES documents(document_id)
            )
        """)
        con.execute("""
            INSERT INTO meeting_votes_new
            SELECT vote_id, meeting_id, document_id, meeting_date,
                   governing_body, agenda_section, item_description,
                   resolution_number, outcome, vote_tally, mover, seconder,
                   source, created_at
            FROM meeting_votes
        """)

        # Step 6: Drop old tables, rename new ones
        con.execute("DROP TABLE meeting_votes")
        con.execute("ALTER TABLE meeting_votes_new RENAME TO meeting_votes")

        con.execute("DROP TABLE meetings")
        con.execute("ALTER TABLE meetings_new RENAME TO meetings")

        con.execute("DROP TABLE governing_bodies")

        # Step 7: Recreate indexes that were on meetings
        con.execute("""
            CREATE UNIQUE INDEX ix_meetings_primegov_id
            ON meetings(primegov_id)
            WHERE primegov_id IS NOT NULL
        """)

        # ── commit ────────────────────────────────────────────────────────────
        con.execute("COMMIT")
        con.execute("PRAGMA foreign_keys = ON")

        # ── verify ────────────────────────────────────────────────────────────
        fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            print(f"WARNING: {len(fk_errors)} FK violations after migration:")
            for e in fk_errors[:10]:
                print(" ", dict(e))
        else:
            print("FK check: PASSED")

        g_after  = con.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        m_after  = con.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        mv_after = con.execute("SELECT COUNT(*) FROM meeting_votes").fetchone()[0]
        null_gid = con.execute("SELECT COUNT(*) FROM meetings WHERE group_id IS NULL").fetchone()[0]
        print(f"After:  {g_after} groups, {m_after} meetings, {mv_after} meeting_votes")
        print(f"Meetings with NULL group_id: {null_gid}")

        # Sanity: row counts should match
        assert m_after == m_count,  f"Meeting count mismatch: {m_after} != {m_count}"
        assert mv_after == mv_count, f"Vote count mismatch: {mv_after} != {mv_count}"
        print("Migration COMPLETE.")

    except Exception as exc:
        con.execute("ROLLBACK")
        con.execute("PRAGMA foreign_keys = ON")
        print(f"FAILED — rolled back: {exc}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    run()

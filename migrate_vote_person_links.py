#!/usr/bin/env python3
"""
migrate_vote_person_links.py — Add person_id FK columns to meeting_votes and
vote_members, then populate them using the supervisor roster mapping.

Additive-only: adds nullable columns with no defaults. Idempotent.
"""

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "database" / "civic_media.db"
ROSTER_PATH = Path(__file__).parent / "config" / "supervisors.json"


def main() -> None:
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()

    # ── 1. Add columns if missing ────────────────────────────────────────────
    existing_mv = {r[1] for r in cur.execute("PRAGMA table_info(meeting_votes)")}
    existing_vm = {r[1] for r in cur.execute("PRAGMA table_info(vote_members)")}

    if "mover_person_id" not in existing_mv:
        cur.execute("ALTER TABLE meeting_votes ADD COLUMN mover_person_id VARCHAR REFERENCES people(person_id)")
        print("Added meeting_votes.mover_person_id")
    if "seconder_person_id" not in existing_mv:
        cur.execute("ALTER TABLE meeting_votes ADD COLUMN seconder_person_id VARCHAR REFERENCES people(person_id)")
        print("Added meeting_votes.seconder_person_id")
    if "person_id" not in existing_vm:
        cur.execute("ALTER TABLE vote_members ADD COLUMN person_id VARCHAR REFERENCES people(person_id)")
        print("Added vote_members.person_id")

    con.commit()

    # ── 2. Build last-name → person_id mapping from roster + people table ────
    with open(ROSTER_PATH, encoding="utf-8") as f:
        roster = json.load(f)

    last_to_full: dict[str, str] = {}
    for entry in roster["district_supervisors"]:
        full = entry["supervisor"]
        last = full.rsplit(" ", 1)[-1]
        if last not in last_to_full:
            last_to_full[last] = full

    # Resolve full names to person_ids
    full_to_pid: dict[str, str] = {}
    cur.execute("SELECT person_id, canonical_name FROM people")
    for pid, cname in cur.fetchall():
        full_to_pid[cname] = pid

    last_to_pid: dict[str, str] = {}
    for last, full in last_to_full.items():
        pid = full_to_pid.get(full)
        if pid:
            last_to_pid[last] = pid
        else:
            print(f"  WARNING: {full} not in people table")

    print(f"\nRoster mapping: {len(last_to_pid)} last names -> person_ids")

    # ── 3. Populate meeting_votes.mover_person_id / seconder_person_id ───────
    mover_updates = seconder_updates = 0
    cur.execute("SELECT vote_id, mover, seconder FROM meeting_votes")
    for vote_id, mover, seconder in cur.fetchall():
        if mover and mover in last_to_pid:
            cur.execute("UPDATE meeting_votes SET mover_person_id = ? WHERE vote_id = ?",
                        (last_to_pid[mover], vote_id))
            mover_updates += 1
        if seconder and seconder in last_to_pid:
            cur.execute("UPDATE meeting_votes SET seconder_person_id = ? WHERE vote_id = ?",
                        (last_to_pid[seconder], vote_id))
            seconder_updates += 1

    print(f"Updated mover_person_id: {mover_updates}")
    print(f"Updated seconder_person_id: {seconder_updates}")

    # ── 4. Populate vote_members.person_id ───────────────────────────────────
    member_updates = 0
    cur.execute("SELECT id, member_name FROM vote_members")
    for row_id, member_name in cur.fetchall():
        if member_name in last_to_pid:
            cur.execute("UPDATE vote_members SET person_id = ? WHERE id = ?",
                        (last_to_pid[member_name], row_id))
            member_updates += 1

    print(f"Updated vote_members.person_id: {member_updates}")

    con.commit()

    # ── 5. Verify ────────────────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) FROM meeting_votes WHERE mover IS NOT NULL AND mover_person_id IS NULL")
    unlinked_movers = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM vote_members WHERE person_id IS NULL")
    unlinked_members = cur.fetchone()[0]

    print(f"\nUnlinked movers: {unlinked_movers}")
    print(f"Unlinked vote_members: {unlinked_members}")

    if unlinked_movers == 0 and unlinked_members == 0:
        print("All records linked successfully.")
    else:
        if unlinked_movers:
            cur.execute("""
                SELECT DISTINCT mover FROM meeting_votes
                WHERE mover IS NOT NULL AND mover_person_id IS NULL
            """)
            print(f"  Unlinked mover names: {[r[0] for r in cur.fetchall()]}")

    con.close()


if __name__ == "__main__":
    main()

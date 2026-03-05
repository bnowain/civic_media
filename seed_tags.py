#!/usr/bin/env python3
"""Seed the civic_media tags table with the standard Shasta County tag taxonomy.

Idempotent — safe to run multiple times. Skips tags that already exist by name.
Run from the civic_media project root:

    python seed_tags.py

See docs/tag_taxonomy.md for the full taxonomy reference and rationale.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.database import SessionLocal
from app import models

# ---------------------------------------------------------------------------
# Canonical taxonomy — keep in sync with docs/tag_taxonomy.md
# ---------------------------------------------------------------------------

TAXONOMY: dict[str, list[str]] = {
    "topic": [
        "Healthcare",
        "Mental Health",
        "Substance Use",
        "Homelessness",
        "Housing",
        "Law Enforcement",
        "In-Custody Deaths",
        "Jail / Detention",
        "Fire / Emergency Services",
        "Wildfire Recovery",
        "Child Welfare",
        "Veterans Services",
        "Education",
        "School Board Politics",
        "Elections",
        "Election Administration",
        "Hand-Count / Election Integrity",
        "Charter County",
        "Recall Elections",
        "Political Retaliation",
        "Outside Donor Influence",
        "Secession / State of Jefferson",
        "Church-State / Religious Political Influence",
        "Tribal Affairs",
        "Environment",
        "Water",
        "Economic Development",
        "Transportation / Infrastructure",
        "Immigration / Sanctuary",
        "Civil Rights / Civil Liberties",
        "Transparency / Accountability",
        "Whistleblower",
        "Hospital / Healthcare Workers",
        "Utility Rates",
        "Budget / Finance",
        "Ethics",
    ],
    "agency": [
        "Board of Supervisors",
        "Redding City Council",
        "Register of Voters",
        "Grand Jury",
        "HHSA",
        "Public Health Dept",
        "Behavioral Health Dept",
        "RPD",
        "Sheriff's Office",
        "Probation",
        "District Attorney",
        "Public Defender",
        "County Counsel",
        "Clerk / ROV",
        "Public Works",
        "Planning Dept",
        "Code Enforcement",
        "Animal Services",
        "City Attorney",
        "Redding Electric Utility",
        "SRMC",
        "CAL FIRE / USFS",
        "FPPC",
    ],
    "action": [
        "Approved",
        "Denied",
        "Tabled",
        "Continued",
        "No Action Taken",
        "Public Hearing",
        "Public Comment",
        "Ordinance",
        "Resolution",
        "Closed Session",
        "Litigation",
        "Eminent Domain",
        "Recall Filed",
        "Recall Qualified",
        "Adversarial",
        "Revelation",
        "Accusation",
        "Testimony",
        "Conduct",
        "Recusal",
        "Legal Warning",
        "Recess — Disruption",
        "Room Cleared",
        "Brown Act",
    ],
    "money": [
        "Budget",
        "Mid-Year Budget",
        "Contract",
        "RFP",
        "Sole Source",
        "Change Order",
        "Audit",
        "Fees / Rate Increase",
        "Tax",
        "Capital Project",
        "Grants",
        "Settlement",
    ],
    "place": [
        "City of Redding",
        "Redding",
        "Anderson",
        "Shasta Lake",
        "Unincorporated",
        "District 1",
        "District 2",
        "District 3",
        "District 4",
        "District 5",
        "Shasta Dam",
        "Redding Riverfront",
    ],
}


def seed() -> None:
    db = SessionLocal()
    try:
        added = 0
        skipped = 0
        for tag_type, names in TAXONOMY.items():
            for name in names:
                existing = (
                    db.query(models.Tag)
                    .filter(models.Tag.name.ilike(name))
                    .first()
                )
                if existing:
                    skipped += 1
                    continue
                tag = models.Tag(
                    name=name,
                    tag_type=tag_type,
                )
                db.add(tag)
                added += 1

        db.commit()
        print(f"Done. Added: {added}  Skipped (already existed): {skipped}")
    except Exception as exc:
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    seed()

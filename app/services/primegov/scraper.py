"""
PrimeGov API client — fetches meeting metadata from the public REST API.

No authentication required.  Uses httpx for HTTP requests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://shastacounty.primegov.com"

# Known committee IDs (from GetCommitteeesListByShowInPublicPortal)
COMMITTEES = {
    3: "Board of Supervisors",
    2: "Air Pollution Control Hearing Board",
    4: "Housing Authority",
    5: "In-Home Supportive Services",
    6: "Planning Commission",
    7: "Waterworks District #1",
}

# Document template name mapping
DOC_TYPES = {
    "Agenda": "agenda",
    "Minutes": "minutes",
    "Packet": "packet",
    "HTML Packet": "packet",
    "Meeting Packet": "packet",
}


@dataclass
class PrimeGovDocument:
    template_id: int
    template_name: str
    doc_type: str  # "agenda" | "minutes" | "packet"

    @property
    def download_url(self) -> str:
        return (
            f"{BASE_URL}/Public/CompiledDocument"
            f"?meetingTemplateId={self.template_id}&compileOutputType=1"
        )


@dataclass
class PrimeGovMeeting:
    """Parsed meeting record from PrimeGov API."""
    primegov_id: int
    title: str
    date_time: datetime
    committee_id: int
    committee_name: str
    video_url: Optional[str] = None
    swagit_id: Optional[str] = None
    documents: list[PrimeGovDocument] = field(default_factory=list)

    @property
    def meeting_date_iso(self) -> str:
        return self.date_time.strftime("%Y-%m-%d")

    @property
    def agenda_url(self) -> Optional[str]:
        for doc in self.documents:
            if doc.doc_type == "agenda":
                return doc.download_url
        return None

    @property
    def minutes_url(self) -> Optional[str]:
        for doc in self.documents:
            if doc.doc_type == "minutes":
                return doc.download_url
        return None

    @property
    def packet_url(self) -> Optional[str]:
        for doc in self.documents:
            if doc.doc_type == "packet":
                return doc.download_url
        return None


class PrimeGovScraper:
    """Client for the PrimeGov public portal API."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    def get_archived_years(self) -> list[int]:
        """Fetch list of years with archived meetings."""
        with self._client() as client:
            r = client.get("/api/v2/PublicPortal/GetArchivedMeetingYears")
            r.raise_for_status()
            return r.json()

    def get_committees(self) -> list[dict]:
        """Fetch all committees from public portal."""
        with self._client() as client:
            r = client.get(
                "/api/committee/GetCommitteeesListByShowInPublicPortal"
            )
            r.raise_for_status()
            return r.json()

    def get_archived_meetings(
        self, committee_id: int, year: int
    ) -> list[PrimeGovMeeting]:
        """Fetch archived meetings for a committee + year."""
        committee_name = COMMITTEES.get(committee_id, f"Committee {committee_id}")
        with self._client() as client:
            r = client.get(
                "/api/v2/PublicPortal/ListArchivedMeetingsByCommitteeId",
                params={"year": year, "committeeId": committee_id},
            )
            r.raise_for_status()
            return [
                self._parse_meeting(m, committee_id, committee_name)
                for m in r.json()
            ]

    def get_upcoming_meetings(
        self, committee_id: int
    ) -> list[PrimeGovMeeting]:
        """Fetch upcoming meetings for a committee."""
        committee_name = COMMITTEES.get(committee_id, f"Committee {committee_id}")
        with self._client() as client:
            r = client.get(
                "/api/v2/PublicPortal/ListUpcomingMeetingsByCommitteeId",
                params={"committeeId": committee_id},
            )
            r.raise_for_status()
            return [
                self._parse_meeting(m, committee_id, committee_name)
                for m in r.json()
            ]

    def discover(
        self,
        committee_ids: list[int] | None = None,
        years: list[int] | None = None,
        include_upcoming: bool = True,
        min_date: str | None = None,
    ) -> list[PrimeGovMeeting]:
        """
        Full discovery: fetch all meetings across committees and years.

        Args:
            committee_ids: Which committees to fetch. Defaults to [3] (BOS).
            years: Which years to fetch. None = all available years.
            include_upcoming: Also fetch upcoming (future) meetings.
            min_date: YYYY-MM-DD cutoff — skip meetings before this date and
                only fetch years >= this date's year.  None = no cutoff.
        """
        if committee_ids is None:
            committee_ids = [3]

        if years is None:
            available_years = self.get_archived_years()
            if min_date:
                # Only fetch years from min_date's year onward — skip old history
                min_year = int(min_date[:4])
                years = [y for y in available_years if y >= min_year]
                logger.info("Update mode: limiting to years %s (min_date=%s)", years, min_date)
            else:
                years = available_years

        all_meetings: list[PrimeGovMeeting] = []
        seen_ids: set[int] = set()

        for cid in committee_ids:
            committee_name = COMMITTEES.get(cid, f"Committee {cid}")

            # Archived meetings by year
            for year in years:
                try:
                    meetings = self.get_archived_meetings(cid, year)
                    for m in meetings:
                        if m.primegov_id not in seen_ids:
                            seen_ids.add(m.primegov_id)
                            all_meetings.append(m)
                    logger.info(
                        "PrimeGov: %s %d — %d meetings",
                        committee_name, year, len(meetings),
                    )
                except httpx.HTTPError as e:
                    logger.warning(
                        "PrimeGov: failed to fetch %s %d: %s",
                        committee_name, year, e,
                    )

            # Upcoming meetings
            if include_upcoming:
                try:
                    upcoming = self.get_upcoming_meetings(cid)
                    for m in upcoming:
                        if m.primegov_id not in seen_ids:
                            seen_ids.add(m.primegov_id)
                            all_meetings.append(m)
                    logger.info(
                        "PrimeGov: %s upcoming — %d meetings",
                        committee_name, len(upcoming),
                    )
                except httpx.HTTPError as e:
                    logger.warning(
                        "PrimeGov: failed to fetch %s upcoming: %s",
                        committee_name, e,
                    )

        # Apply min_date post-filter (catches any slipthrough from year-level filter)
        if min_date:
            before = len(all_meetings)
            all_meetings = [m for m in all_meetings if m.meeting_date_iso >= min_date]
            logger.info(
                "min_date filter: kept %d of %d meetings (cutoff %s)",
                len(all_meetings), before, min_date,
            )

        # Sort by date descending
        all_meetings.sort(key=lambda m: m.date_time, reverse=True)
        logger.info("PrimeGov discovery complete: %d total meetings", len(all_meetings))
        return all_meetings

    @staticmethod
    def _parse_meeting(
        raw: dict, committee_id: int, committee_name: str
    ) -> PrimeGovMeeting:
        """Parse a raw PrimeGov API meeting object."""
        # Parse documents
        documents = []
        for doc in raw.get("documentList", []):
            template_name = doc.get("templateName", "")
            doc_type = DOC_TYPES.get(template_name, "")
            if doc_type and doc.get("isPublic", True):
                documents.append(PrimeGovDocument(
                    template_id=doc["templateId"],
                    template_name=template_name,
                    doc_type=doc_type,
                ))

        # Parse date
        dt_str = raw.get("dateTime", "")
        try:
            dt = datetime.fromisoformat(dt_str)
        except (ValueError, TypeError):
            dt = datetime.utcnow()

        # Video URL
        video_url = raw.get("videoUrl") or None
        if video_url and not video_url.startswith("http"):
            video_url = None

        return PrimeGovMeeting(
            primegov_id=raw["id"],
            title=raw.get("title", "Untitled Meeting"),
            date_time=dt,
            committee_id=committee_id,
            committee_name=committee_name,
            video_url=video_url,
            swagit_id=raw.get("swagitId") or None,
            documents=documents,
        )

"""Plenary and committee transcripts from the Senedd Record of Proceedings.

This is the highest-value source in the system and the one that replaces the
bulk of a manual weekly briefing. The Senedd publishes the full official Record
as XML under the Open Government Licence v3.0 — a genuinely good piece of
open-data provision that most legislatures do not match.

Verified endpoints (4 August 2026)
----------------------------------
Listing   https://record.senedd.wales/XMLExport
          HTML table of meetings, filterable by ``SelectedCommitteeID``,
          ``Start`` and ``End``. Each row links to the available exports.

Download  https://record.senedd.wales/XMLExport/Download
              ?meetingID=<int>&xmlDownloadType=<type>
          where <type> is one of:
              BilingualTranscript, WelshTranscript, EnglishTranscript,
              QNR, Votes

XML shape (verified against Plenary 15 July 2026, meetingID 16086)
------------------------------------------------------------------
    <dataroot generated="...">
      <XML_Plenary_English>            <-- one element PER CONTRIBUTION
        <Meeting_ID>16086</Meeting_ID>
        <Assembly>7</Assembly>
        <MeetingDate>2026-07-15T13:30:01</MeetingDate>
        <Contribution_ID>768019</Contribution_ID>
        <Contribution_Order_ID>...</Contribution_Order_ID>
        <contribution_type>C</contribution_type>
        <Agenda_Item_ID>260715-2</Agenda_Item_ID>
        <Agenda_item_english>2. Questions to the Cabinet Minister for
            Local Government, Housing and Planning</Agenda_item_english>
        <Member_Id>12172</Member_Id>
        <Member_name_English>Rebeca Phillips</Member_name_English>
        <Member_job_title_English>...</Member_job_title_English>
        <Member_biog_English>https://business.senedd.wales/mgUserInfo.aspx?UID=...
        <Contribution_English><p>...</p></Contribution_English>
        <contribution_translated_seneddTv>http://www.senedd.tv/en/16086?startPos=...
      </XML_Plenary_English>
      ...
    </dataroot>

Two details worth knowing, both learned from the real data rather than the docs:

* The wrapper element name varies with the meeting: ``XML_Plenary_English`` for
  Plenary, ``XML_LocalGovernmentHousingAndPlanningCommittee_English`` for that
  committee. We therefore iterate over whatever children ``dataroot`` has
  rather than looking for a fixed tag.

* ``contribution_type`` ``I`` is boilerplate — the bilingual-column explainer
  and the "[R] indicates a declared interest" note — and appears in every
  transcript. It is filtered out via the taxonomy, not hard-coded, so the
  policy team can change their mind without a code change.

The timestamped Senedd.tv deep link is carried through to the dashboard. It is
strictly better than the "Watch here" link in a manual briefing, because it
jumps to the moment the words were spoken.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from ..models import Item
from ..relevance import Taxonomy
from .base import Collector


LISTING_URL = "https://record.senedd.wales/XMLExport"
DOWNLOAD_URL = "https://record.senedd.wales/XMLExport/Download"

# Committee IDs offered by the listing page's own dropdown, plus Plenary.
# The dropdown only lists forums that already have published records, so this
# will grow through the Senedd term — hence `discover_from_listing`, which reads
# whatever the page actually offers rather than trusting this map.
KNOWN_FORUMS = {
    "908": "Plenary",
    "909": "Business Committee",
    "979": "Constitution, Justice and External Affairs Committee",
    "980": "Culture, Communications, Cymraeg and Sport Committee",
    "984": "Finance Committee",
    "985": "Health and Social Care Committee",
    "986": "Legislation Committee",
    "989": "Public Accounts and Public Administration Committee",
}


def _text(node: ET.Element, tag: str) -> str:
    el = node.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _first_text(node: ET.Element, *tags: str) -> str:
    for tag in tags:
        val = _text(node, tag)
        if val:
            return val
    return ""


def _parse_dt(value: str) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip()[:19], fmt).date()
        except ValueError:
            continue
    return None


class RecordTranscriptCollector(Collector):
    """Collects contributions from Plenary and committee transcripts."""

    name = "senedd_record_transcripts"

    def __init__(self, *args, taxonomy: Taxonomy | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.tax = taxonomy or Taxonomy.load()

    # -- meeting discovery -------------------------------------------------

    def list_meetings(self, start: date | None = None,
                      end: date | None = None,
                      committee_id: str | None = None) -> list[dict]:
        """Return meetings from the XMLExport listing page.

        Parsed from HTML because the Senedd does not expose the meeting index
        itself as data — only the transcripts. The parse is deliberately
        forgiving: it keys off the download links (which carry the meetingID)
        and then reads the surrounding row for the date and forum name, so
        cosmetic template changes will not break it.

        IMPORTANT — verified behaviour of the date filters, 4 August 2026
        ---------------------------------------------------------------
        The listing's ``Start``/``End`` parameters expect ``dd/mm/yyyy`` and
        work correctly **only when a specific committee is selected**:

            SelectedCommitteeID=908&Start=01/07/2026&End=31/07/2026
                -> correct: Plenary sittings of 15 and 14 July 2026

            SelectedCommitteeID=0&Start=15/06/2026&End=04/08/2026
                -> WRONG: returns March 2026 sixth-Senedd meetings

            (no parameters at all)
                -> correct: the 16 most recent meetings, newest first

        So there are two safe modes, and this method supports both:

        * ``committee_id=None`` (default) — request the unfiltered listing and
          filter by date in Python. One request, always correct, and ideal for
          the daily incremental run because the newest meetings are first.
        * ``committee_id="908"`` etc. — send the date range for a targeted
          backfill of one forum.

        Passing ``committee_id="0"`` together with dates is deliberately not
        done anywhere in this codebase, because the server returns the wrong
        answer for that combination and would silently backfill the wrong
        Senedd term.
        """
        params: dict[str, str] = {}
        if committee_id:
            params["SelectedCommitteeID"] = committee_id
            if start:
                params["Start"] = start.strftime("%d/%m/%Y")
            if end:
                params["End"] = end.strftime("%d/%m/%Y")

        html = self.fetcher.get_text(LISTING_URL, params=params or None)
        if not html:
            self.note_error(f"could not fetch meeting listing from {LISTING_URL}")
            return []

        meetings: dict[str, dict] = {}
        for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            ids = re.findall(r"meetingID=(\d+)", row_html)
            if not ids:
                continue
            meeting_id = ids[0]
            cells = [
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S)
            ]
            if len(cells) < 2:
                continue
            available = set(re.findall(r"xmlDownloadType=(\w+)", row_html))
            meetings[meeting_id] = {
                "meeting_id": meeting_id,
                "date": _parse_dt(cells[0]),
                "forum": cells[1],
                "available": available,
            }

        out = sorted(meetings.values(),
                     key=lambda m: (m["date"] or date.min), reverse=True)

        # Client-side date filtering. Always applied, so the caller gets the
        # window it asked for whether or not the server honoured the filter.
        if start or end:
            filtered = []
            for meeting in out:
                when = meeting["date"]
                if when is None:
                    continue
                if start and when < start:
                    continue
                if end and when > end:
                    continue
                filtered.append(meeting)
            out = filtered

        return out

    def list_meetings_across_forums(self, start: date, end: date,
                                    forum_ids: dict[str, str] | None = None
                                    ) -> list[dict]:
        """Backfill helper: query each forum separately for a date range.

        Used for the initial historical load and for catching up after an
        outage. One request per forum, which is why the daily run uses the
        cheaper unfiltered listing instead.

        Note that the Senedd's own committee dropdown is incomplete — on
        4 August 2026 it did not list the Local Government, Housing and
        Planning Committee even though that committee had published a
        transcript for 16 July 2026. We therefore always also include the
        unfiltered listing, so a newly-created committee is never missed just
        because it has not yet appeared in the dropdown.
        """
        forums = forum_ids or KNOWN_FORUMS
        seen: dict[str, dict] = {}

        for cid in forums:
            for meeting in self.list_meetings(start=start, end=end,
                                              committee_id=cid):
                seen[meeting["meeting_id"]] = meeting

        for meeting in self.list_meetings(start=start, end=end):
            seen.setdefault(meeting["meeting_id"], meeting)

        return sorted(seen.values(),
                      key=lambda m: (m["date"] or date.min), reverse=True)

    def discover_from_listing(self, html: str) -> dict[str, str]:
        """Read the committee dropdown so new committees are picked up
        automatically as they start publishing records."""
        match = re.search(r'<select[^>]*id="SelectedCommitteeID".*?</select>',
                          html, re.S)
        if not match:
            return dict(KNOWN_FORUMS)
        found = dict(re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>',
                                match.group(0)))
        return {k: v.strip() for k, v in found.items() if k != "0"}

    # -- transcript parsing ------------------------------------------------

    def fetch_transcript(self, meeting_id: str,
                         download_type: str = "EnglishTranscript") -> bytes | None:
        return self.fetcher.get_bytes(
            DOWNLOAD_URL,
            params={"meetingID": meeting_id, "xmlDownloadType": download_type},
        )

    def parse_transcript(self, xml_bytes: bytes, forum_hint: str = "",
                         source_kind: str = "plenary_transcript"):
        """Yield one Item per substantive contribution."""
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            self.note_error(f"malformed transcript XML: {exc}")
            return

        for node in root:
            ctype = _text(node, "contribution_type")
            if not self.tax.includes_contribution_type(ctype):
                continue

            # Committee housekeeping — "Introductions, apologies", "Papers to
            # note" — appears at every meeting and is never policy content.
            # Filtered here rather than scored down, because it otherwise fills
            # the dashboard's Review section with items nobody can act on.
            if self.tax.is_procedural_agenda_item(_text(node, "Agenda_item_english")):
                continue

            body = _first_text(node, "Contribution_English",
                               "contribution_translated",
                               "contribution_verbatim",
                               "Contribution_Welsh")
            if not body:
                continue

            meeting_id = _text(node, "Meeting_ID")
            item_date = _parse_dt(_first_text(node, "ContributionTime", "MeetingDate"))
            agenda = _text(node, "Agenda_item_english")
            contribution_id = _text(node, "Contribution_ID")

            # Deep link. The Record gives us a Senedd.tv URL with a startPos
            # offset for many contributions, which lets a policy officer jump
            # straight to the moment something was said.
            video = _first_text(node, "contribution_translated_seneddTv",
                                "contribution_spoken_seneddTv")

            # There is no per-contribution permalink on record.senedd.wales, so
            # we link to the meeting's own page and note the contribution ID.
            url = (f"https://record.senedd.wales/Plenary/{meeting_id}"
                   if source_kind == "plenary_transcript"
                   else f"https://record.senedd.wales/Committee/{meeting_id}")

            yield Item(
                source_kind=source_kind,
                source_name=forum_hint or "Senedd Record",
                title=agenda or f"Contribution {contribution_id}",
                body=body,
                url=url,
                item_date=item_date,
                speaker=_text(node, "Member_name_English"),
                speaker_role=_text(node, "Member_job_title_English"),
                speaker_id=_text(node, "Member_Id"),
                forum=forum_hint,
                agenda_item=agenda,
                meeting_id=meeting_id,
                video_url=video,
                raw_ref=(f"{DOWNLOAD_URL}?meetingID={meeting_id}"
                         f"&xmlDownloadType=EnglishTranscript#{contribution_id}"),
            )

    # -- orchestration -----------------------------------------------------

    def collect(self, start: date | None = None, end: date | None = None,
                max_meetings: int = 25, backfill: bool = False):
        if backfill and start and end:
            meetings = self.list_meetings_across_forums(start, end)
        else:
            meetings = self.list_meetings(start=start, end=end)

        if not meetings:
            self.note_error(
                f"no meetings found between {start} and {end}. During recess "
                f"this is correct — the Senedd rose for summer on 17 July 2026 "
                f"and returns on 14 September 2026."
            )
            return

        for meeting in meetings[:max_meetings]:
            forum = meeting["forum"] or ""
            # Forum names carry a term suffix, e.g. "Plenary - Sixth Senedd",
            # so an equality test silently misclassifies every historical
            # sitting as a committee. Substring match instead.
            kind = ("plenary_transcript" if "plenary" in forum.lower()
                    else "committee_transcript")

            # Prefer the English transcript; fall back to bilingual, which
            # contains the English column plus the original-language column.
            payload = None
            for dtype in ("EnglishTranscript", "BilingualTranscript"):
                if meeting["available"] and dtype not in meeting["available"]:
                    continue
                payload = self.fetch_transcript(meeting["meeting_id"], dtype)
                if payload:
                    break

            if not payload:
                self.note_error(
                    f"no transcript retrievable for meeting {meeting['meeting_id']} "
                    f"({forum} {meeting['date']})"
                )
                continue

            yield from self.parse_transcript(payload, forum_hint=forum,
                                             source_kind=kind)

            # Questions not reached in Plenary are published separately and are
            # a genuine blind spot if you only read the transcript — a question
            # can be tabled, never asked aloud, and still be answered in
            # writing. Manual briefings routinely miss these.
            if "QNR" in (meeting["available"] or set()):
                qnr = self.fetch_transcript(meeting["meeting_id"], "QNR")
                if qnr:
                    yield from self.parse_qnr(qnr, forum, meeting["date"])

    def parse_qnr(self, xml_bytes: bytes, forum: str, meeting_date: date | None):
        """Parse the 'questions not reached' export (bilingual, flat shape)."""
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            self.note_error(f"malformed QNR XML: {exc}")
            return

        for node in root:
            body = _first_text(node, "contribution_translated", "contribution_verbatim")
            if not body:
                continue
            meeting_id = _text(node, "Meeting_ID")
            yield Item(
                source_kind="oral_question",
                source_name="Question not reached in Plenary",
                title=_text(node, "Agenda_item_english") or "Question not reached",
                body=body,
                url=f"https://record.senedd.wales/Plenary/{meeting_id}",
                item_date=_parse_dt(_text(node, "MeetingDate")) or meeting_date,
                speaker=_text(node, "Member_name_English"),
                speaker_role=_text(node, "Member_job_title_English"),
                speaker_id=_text(node, "Member_Id"),
                forum=forum or "Plenary",
                agenda_item=_text(node, "Agenda_item_english"),
                meeting_id=meeting_id,
                raw_ref=f"{DOWNLOAD_URL}?meetingID={meeting_id}&xmlDownloadType=QNR",
            )

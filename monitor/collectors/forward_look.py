"""Forward Look — what is scheduled, and what is closing.

The Camlas briefing's "Future Business" table is arguably its most useful page,
because it is the only part that tells you what you can still influence. Rebuilt
here from live data on every run, so it is never a week stale.

The forward look has two halves, from different places:

1. SCHEDULED BUSINESS — Plenary sittings and committee meetings, from the
   Senedd's ModernGov SOAP web service.
2. CLOSING WINDOWS — consultation deadlines, calls for evidence and
   written-question answer dates already captured by other collectors. Derived
   from the archive by `deadlines_from_items`, so every deadline the system has
   ever seen is tracked to its close.

HOW THIS WAS FIXED — read before changing anything
--------------------------------------------------
The first version used the JSON endpoint the Senedd's open-data page documents:

    https://business.senedd.wales/calJson.aspx?fromdate=...&todate=...

It returns HTTP 200 and valid JSON, but it **ignores its own date parameters**
(identical output for 13–18 July and 1 September–1 October) and during recess
contained only visitor events (`type: "50"`, building exhibitions). It produced
no parliamentary business at all, and the forward look was effectively dead.

The working answer is the ModernGov SOAP service, which the same open-data page
documents but does not explain:

    POST https://business.senedd.wales/mgWebService.asmx
    SOAPAction: http://moderngov.co.uk/namespaces/GetAllMeetingsByDate

Two undocumented details make the difference between 0 results and 96, and both
were found by probing rather than from any documentation:

    lCommitteeId = 0   -> returns NOTHING. Zero does not mean "all".
    lCommitteeId = -1  -> returns ALL committees. This is what you want.

    sFromDate/sToDate in dd/mm/yyyy  -> correct, honours the range
    sFromDate/sToDate in ISO         -> silently IGNORES the range and returns
                                        a 5000-row cap of everything

The ISO-date failure is the dangerous one, because it returns a great deal of
plausible data while quietly answering a different question. Verified 4 August
2026: `-1` with `01/09/2026`–`31/10/2026` returned **96 scheduled meetings**,
including the Local Government, Housing and Planning Committee on 1 October 2026.

`GetCommittees` also solves a separate problem. The Record's own committee
dropdown is incomplete — it omitted the Local Government, Housing and Planning
Committee entirely — whereas `GetCommittees` returns the full register of 209
committees across all Senedd terms with their IDs.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

from ..models import Item, _clean
from .base import Collector


MG_SERVICE = "https://business.senedd.wales/mgWebService.asmx"
MG_NAMESPACE = "http://moderngov.co.uk/namespaces"
CAL_JSON = "https://business.senedd.wales/calJson.aspx"

# lCommitteeId sentinel meaning "every committee". Zero means none.
ALL_COMMITTEES = -1

# ModernGov event type codes. Type 50 is visitor events and exhibitions, which
# are not parliamentary business and must never appear in a forward look.
NON_BUSINESS_TYPES = {"50"}

# Committees whose meetings matter most to the NRLA. Used only to prioritise —
# every scheduled meeting is still collected and scored on its own merits.
PRIORITY_FORUMS = (
    "Local Government, Housing and Planning",
    "Plenary",
    "Finance",
    "Legislation",
    "Climate Change, Environment, Sustainability and Rural Affairs",
    "Equality, Human Rights and Social Justice",
    "Petitions",
)


def _soap_envelope(operation: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        f'<{operation} xmlns="{MG_NAMESPACE}">{body}</{operation}>'
        "</soap:Body></soap:Envelope>"
    )


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _elements(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _local(e.tag) == name]


def _fields(node: ET.Element) -> dict[str, str]:
    return {_local(c.tag): (c.text or "").strip() for c in node}


def _uk_date(value: str) -> date | None:
    """Parse the dd/mm/yyyy dates the ModernGov service returns."""
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip()[:16], fmt).date()
        except ValueError:
            continue
    return None


class SeneddCalendarCollector(Collector):
    """Scheduled Senedd business from the ModernGov SOAP web service."""

    name = "senedd_forward_look"
    source_kind = "calendar"

    def _post(self, operation: str, body: str) -> ET.Element | None:
        envelope = _soap_envelope(operation, body)
        try:
            response = self.fetcher.session.post(
                MG_SERVICE,
                data=envelope.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f"{MG_NAMESPACE}/{operation}",
                    "User-Agent": self.fetcher.session.headers.get("User-Agent", ""),
                },
                timeout=self.fetcher.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - network variety
            self.note_error(f"{operation} request failed: {exc}")
            return None

        if response.status_code != 200:
            self.note_error(f"{operation} returned HTTP {response.status_code}")
            return None
        try:
            return ET.fromstring(response.content)
        except ET.ParseError as exc:
            self.note_error(f"{operation} returned malformed XML: {exc}")
            return None

    # -- committee register ------------------------------------------------

    def committees(self) -> dict[str, str]:
        """Full committee register: {committeeid: title}.

        Worth calling occasionally rather than every run — it returns every
        committee across every Senedd term (209 as at August 2026) and changes
        only when a committee is created or dissolved.
        """
        root = self._post("GetCommittees", "")
        if root is None:
            return {}
        out: dict[str, str] = {}
        for node in _elements(root, "committee"):
            data = _fields(node)
            cid, title = data.get("committeeid"), data.get("committeetitle")
            if cid and title:
                out[cid] = title
        return out

    def current_committees(self) -> dict[str, str]:
        """Committees of the current Senedd term.

        Historical committees carry a term suffix ("- Sixth Senedd",
        "- Third Assembly"); current ones do not. Filtering on the absence of a
        suffix is crude but stable, and it is only used to reduce noise.
        """
        return {cid: title for cid, title in self.committees().items()
                if not re.search(r"-\s*(First|Second|Third|Fourth|Fifth|Sixth)\s+"
                                 r"(Assembly|Senedd)\s*$", title, re.I)}

    # -- scheduled meetings ------------------------------------------------

    def meetings(self, start: date, end: date,
                 committee_id: int = ALL_COMMITTEES) -> list[dict]:
        """Scheduled meetings in a date range.

        Dates MUST be dd/mm/yyyy. ISO dates are accepted by the service but
        silently ignored, returning a 5000-row dump of everything — plausible
        data answering the wrong question.
        """
        body = (f"<lCommitteeId>{committee_id}</lCommitteeId>"
                f"<sFromDate>{start.strftime('%d/%m/%Y')}</sFromDate>"
                f"<sToDate>{end.strftime('%d/%m/%Y')}</sToDate>")
        root = self._post("GetAllMeetingsByDate", body)
        if root is None:
            return []

        count_nodes = _elements(root, "meetingscount")
        reported = (count_nodes[0].text or "0").strip() if count_nodes else "0"

        out: list[dict] = []
        for node in _elements(root, "meeting"):
            data = _fields(node)
            when = _uk_date(data.get("meetingdate", ""))
            if when is None:
                continue
            # Belt and braces: filter client-side too, so a future change in
                # the service's date handling cannot silently widen the window.
            if not (start <= when <= end):
                continue
            out.append({
                "date": when,
                "time": data.get("meetingtime", ""),
                "forum": data.get("committeetitle", ""),
                "meeting_id": data.get("meetingid", ""),
                "committee_id": data.get("committeeid", ""),
                "status": data.get("meetingstatus", ""),
                "webcast": data.get("iswebcast", ""),
            })

        if reported not in ("", "0") and not out:
            self.note_error(
                f"GetAllMeetingsByDate reported {reported} meetings but none fell "
                f"inside {start}–{end}. Check the date format is dd/mm/yyyy — "
                f"ISO dates make the service ignore the range."
            )
        return sorted(out, key=lambda m: m["date"])

    # -- collection --------------------------------------------------------

    def collect(self, start: date | None = None, end: date | None = None,
                horizon_days: int = 56):
        start = start or date.today()
        end = end or (start + timedelta(days=horizon_days))

        found = self.meetings(start, end)
        if not found:
            self.note_error(
                f"no scheduled Senedd business between {start} and {end}. "
                f"During a long recess this is correct — the Senedd rose on "
                f"17 July 2026 and returned on 14 September 2026. Outside "
                f"recess, treat this as a failure and check "
                f"GetAllMeetingsByDate directly."
            )
            return

        for meeting in found:
            forum = meeting["forum"]
            priority = any(p.lower() in forum.lower() for p in PRIORITY_FORUMS)

            when = meeting["date"]
            label = f"{forum} — {when.strftime('%d %B %Y')}"
            if meeting["time"]:
                label += f", {meeting['time']}"

            # The body carries the forum name and a plain-English description so
            # the scorer can act on it. A Local Government, Housing and Planning
            # Committee meeting is relevant business in its own right, even
            # before an agenda is published.
            body = "\n".join(filter(None, [
                f"{forum} is scheduled to meet on {when.strftime('%d %B %Y')}"
                + (f" at {meeting['time']}" if meeting["time"] else "") + ".",
                f"Meeting status: {meeting['status']}" if meeting["status"] else "",
                "This is an opportunity to influence: committee papers and any "
                "call for written evidence are normally published in the two "
                "weeks beforehand." if priority else "",
            ]))

            yield Item(
                source_kind="calendar",
                source_name="Senedd forward look",
                title=label,
                body=body,
                url=("https://business.senedd.wales/ieListDocuments.aspx"
                     f"?CId={meeting['committee_id']}&MId={meeting['meeting_id']}"),
                item_date=when,
                forum=forum,
                meeting_id=meeting["meeting_id"],
                # A sitting date IS the window to influence, so it is treated as
                # a deadline and appears in the "closing soon" panel.
                deadline=when,
                raw_ref=f"{MG_SERVICE}#GetAllMeetingsByDate",
            )


class LegacyCalJsonCollector(Collector):
    """The calJson.aspx endpoint. Retained for reference; do not rely on it.

    Kept in the codebase because the Senedd's open-data page documents it, so
    someone will eventually try it again. This class records what it actually
    does: returns HTTP 200 and valid JSON, ignores `fromdate`/`todate`, and
    during recess contains only building exhibitions. Use
    `SeneddCalendarCollector` instead.
    """

    name = "senedd_calendar_json_legacy"
    source_kind = "calendar"

    def collect(self, start: date | None = None, end: date | None = None):
        start = start or date.today()
        end = end or (start + timedelta(days=42))

        text = self.fetcher.get_text(CAL_JSON, params={
            "fromdate": start.isoformat(), "todate": end.isoformat()})
        if not text:
            self.note_error(f"calJson.aspx unavailable at {CAL_JSON}")
            return
        try:
            events = json.loads(text)
        except json.JSONDecodeError as exc:
            self.note_error(f"calJson.aspx returned malformed JSON: {exc}")
            return

        for event in events:
            if str(event.get("type", "")) in NON_BUSINESS_TYPES:
                continue
            when = _uk_date(event.get("date", "")) or None
            if when is None:
                try:
                    when = datetime.strptime(
                        event.get("date", "")[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
            if not (start <= when <= end):
                continue
            title = _clean(event.get("title", ""))
            if not title:
                continue
            yield Item(
                source_kind="calendar",
                source_name="Senedd forward look (legacy calJson)",
                title=title,
                body=f"{title}\n{_clean(event.get('description', ''))}",
                url=_clean(event.get("url", "")),
                item_date=when,
                forum="Senedd",
                deadline=when,
                raw_ref=CAL_JSON,
            )


def deadlines_from_items(items: list[Item], within_days: int = 60,
                         today: date | None = None) -> list[Item]:
    """Derive the 'closing soon' list from everything already collected.

    Any item carrying a deadline — a consultation closing date, a call for
    evidence, a written question's answer-due date, a scheduled committee
    session — is surfaced in date order while the window is still open.

    Expired deadlines are dropped rather than shown as overdue: a policy team
    needs to know what it can still act on.
    """
    today = today or date.today()
    horizon = today + timedelta(days=within_days)
    live = [i for i in items if i.deadline and today <= i.deadline <= horizon]
    return sorted(live, key=lambda i: (i.deadline, -i.score))


# Backwards-compatible alias: the pipeline previously imported this name for the
# HTML-scraping fallback, which the SOAP service makes unnecessary.
ModernGovCalendarCollector = SeneddCalendarCollector

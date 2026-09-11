"""Business that has not happened yet: tabled oral questions and Plenary agendas.

WHY THIS EXISTS
---------------
The first Friday future-business email went out on 11 September 2026 and was
compared, item for item, against the supplier briefing it replaces. Two whole
sections of theirs were empty in ours, and the reason was the same in both
cases: **every Senedd source in this tool reads the Record, and the Record is a
record.** It says what was said. A forward look has to say what is about to be
said, and nothing here could see that.

The most pointed example, from the first sitting of the new term:

    5. David Hughes MS (Pontypridd Cynon Merthyr): Will the First Minister set
       out a timeline for the introduction of new measures to better protect
       renters?

Tabled on 10 September for answer on 15 September. The supplier had it on the
Friday. This tool would have seen it on the Tuesday evening, after it had been
asked — which is exactly one working day too late to brief anyone.

THE TWO SOURCES
---------------
Both are published by the Senedd, openly, with no key and no account.

1. `record.senedd.wales/OrderPaper/OralQuestions/DD-MM-YYYY/`
   The order paper for one sitting: every oral question tabled for it, grouped
   by the minister who has to answer, with the Member, their constituency, the
   OQ number and the date it was tabled. Linked from the Plenary agenda as
   "View Questions", which is how it was found.

2. `business.senedd.wales/ieListDocuments.aspx?CId=908&MId=…`
   The Plenary agenda. Statements and debates appear here with their subjects
   before the sitting — "Statement by the Cabinet Minister for Finance:
   Rebalancing the non-domestic rates system" was on the 15 September agenda,
   and non-domestic rates is core NRLA business.

Plenary meetings are found from the ModernGov monthly calendar, so the dates
come from the Senedd rather than from arithmetic about which weekdays it
usually sits.

A NOTE ON WHAT ZERO MEANS HERE
------------------------------
Asking for an order paper on a day with no sitting, or before questions have
been tabled, returns HTTP 200 with a polite error page and no questions. That
is normal and must not be reported as a fault, or the run would be red every
Monday. What IS a fault is an order paper that identifies itself as an order
paper and yields no questions — that means the markup moved — and the two cases
are told apart by the page title.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from ..models import Item, _clean
from .base import Collector


BUSINESS_ROOT = "https://business.senedd.wales"
RECORD_ROOT = "https://record.senedd.wales"

# Plenary's committee id in the Senedd's ModernGov instance. Verified against
# the September 2026 calendar: every "Meeting of Plenary" links to CId=908.
PLENARY_CID = 908

CALENDAR = f"{BUSINESS_ROOT}/mgCalendarMonthView.aspx"
AGENDA = f"{BUSINESS_ROOT}/ieListDocuments.aspx"
ORDER_PAPER = f"{RECORD_ROOT}/OrderPaper/OralQuestions"

# An order paper announces itself in the page title:
#   "Oral Questions tabled on 10/09/2026 for answer on 15/09/2026"
# The error page returned for a non-sitting day is titled just "Welsh
# Parliament", which is how the two are told apart.
_ORDER_PAPER_TITLE = re.compile(
    r"oral questions tabled on\s*(\d{2}/\d{2}/\d{4})\s*for answer on\s*"
    r"(\d{2}/\d{2}/\d{4})", re.I)

# "Agenda for Plenary on Tuesday, 15 September 2026, 13.30"
_AGENDA_DATE = re.compile(r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})")

# Agenda rows that are machinery rather than business. These appear on every
# single sitting, carry no subject, and would fill the "coming up" list with
# the same four lines every week.
_ROUTINE_ITEMS = re.compile(
    r"^\s*(questions to the|business statement|voting time|motion to elect|"
    r"the record of proceedings|urgent question|topical question)", re.I)


def _dmy(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


class SeneddForthcomingBusinessCollector(Collector):
    """Oral questions tabled for future sittings, and Plenary agendas."""

    name = "senedd_forthcoming"
    source_kind = "oral_question"

    # A three-week window is about as far ahead as the Senedd publishes, and it
    # matches the diary window in the Friday email. The caps below bound a run
    # at roughly thirty requests even if the Senedd publishes an unusual burst.
    MAX_MEETINGS = 12
    MAX_ORDER_PAPERS = 10

    def collect(self, start: date | None = None, end: date | None = None):
        start = start or date.today()
        end = end or (start + timedelta(days=21))

        # The agenda half depends on business.senedd.wales, which returned 403
        # to GitHub Actions on 11 September 2026 — the same datacentre-IP block
        # that www.gov.wales applies, on a host that used to work. The order
        # paper half lives on record.senedd.wales, which does not block, so the
        # two halves must not share a fate: losing the agendas must not also
        # lose the tabled questions, which are the more valuable of the two.
        meeting_ids = self._plenary_meeting_ids(start, end) or []

        sittings: set[date] = set()
        for meeting_id in meeting_ids[:self.MAX_MEETINGS]:
            sitting, items = self._agenda(meeting_id)
            if sitting is None or not (start <= sitting <= end):
                continue
            sittings.add(sitting)
            yield from items

        # Probe the days the Senedd normally sits, whether or not the calendar
        # was readable. The order paper identifies itself in its own title, so
        # a probe on a day with no sitting costs one request and yields
        # nothing — which is cheaper and more robust than depending on a
        # calendar that is currently blocked.
        for candidate in self._likely_sitting_days(start, end):
            sittings.add(candidate)

        for sitting in sorted(sittings)[:self.MAX_ORDER_PAPERS]:
            yield from self._order_paper(sitting)

    @staticmethod
    def _likely_sitting_days(start: date, end: date) -> list[date]:
        """Tuesdays and Wednesdays in the window.

        Plenary has sat on those two days for years. This is a fallback for
        finding candidate dates to ask about, not an assertion that the Senedd
        is sitting — the order paper itself is what confirms that.
        """
        days, cursor = [], start
        while cursor <= end:
            if cursor.weekday() in (1, 2):     # Tuesday, Wednesday
                days.append(cursor)
            cursor += timedelta(days=1)
        return days

    # -- Plenary meetings --------------------------------------------------

    def _plenary_meeting_ids(self, start: date, end: date) -> list[str] | None:
        """Meeting ids for Plenary sittings in the window, newest last.

        Returns None (having noted an error) when the calendar could not be
        read at all, and an empty list when it was read and the Senedd simply
        is not sitting — recess is a real answer, not a fault.
        """
        ids: list[str] = []
        months = self._months_between(start, end)
        readable = False

        for year, month in months:
            html = self.fetcher.get_text(
                CALENDAR, params={"Month": month, "Year": year, "GL": 1, "bcr": 1})
            if html is None:
                continue
            soup = BeautifulSoup(html, "html.parser")
            links = soup.select('a[href*="ieListDocuments"]')
            if links:
                readable = True
            for link in links:
                href = link.get("href") or ""
                if f"CId={PLENARY_CID}" not in href:
                    continue
                if match := re.search(r"MId=(\d+)", href):
                    if match.group(1) not in ids:
                        ids.append(match.group(1))

        if not readable:
            self.note_error(
                "The Senedd meetings calendar could not be read, so SCHEDULED "
                "PLENARY STATEMENTS AND DEBATES are missing — a statement on "
                "non-domestic rates, say, will not be seen until after it is "
                "made. Tabled oral questions are unaffected: they come from "
                "record.senedd.wales, which is read separately. Either "
                f"{CALENDAR} is unreachable from this host — business."
                "senedd.wales returned 403 to GitHub Actions on 11 September "
                "2026, the same datacentre-IP block www.gov.wales uses — or "
                "its markup has changed; the parser looks for links to "
                "ieListDocuments.")
            return None
        return ids

    @staticmethod
    def _months_between(start: date, end: date) -> list[tuple[int, int]]:
        months, cursor = [], date(start.year, start.month, 1)
        while cursor <= end:
            months.append((cursor.year, cursor.month))
            cursor = date(cursor.year + (cursor.month == 12),
                          1 if cursor.month == 12 else cursor.month + 1, 1)
        return months

    # -- the agenda --------------------------------------------------------

    def _agenda(self, meeting_id: str) -> tuple[date | None, list[Item]]:
        html = self.fetcher.get_text(
            AGENDA, params={"CId": PLENARY_CID, "MId": meeting_id})
        if html is None:
            return None, []

        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.get_text(" ", strip=True) if soup.title else "")
        heading = soup.find("h1")
        when = None
        for text in (title, heading.get_text(" ", strip=True) if heading else ""):
            if match := _AGENDA_DATE.search(text or ""):
                for fmt in ("%d %B %Y", "%d %b %Y"):
                    try:
                        when = datetime.strptime(match.group(1), fmt).date()
                        break
                    except ValueError:
                        continue
            if when:
                break
        if when is None:
            return None, []

        items: list[Item] = []
        for row in soup.select("table.mgItemTable tr"):
            title_el = row.select_one("p.mgAiTitleTxt")
            if not title_el:
                continue
            subject = _clean(title_el.get_text(" ", strip=True))
            if not subject or _ROUTINE_ITEMS.match(subject):
                continue
            note_el = row.select_one("div.mgWordPara")
            note = _clean(note_el.get_text(" ", strip=True)) if note_el else ""

            items.append(Item(
                source_kind="calendar",
                source_name="Plenary agenda",
                title=subject,
                body="\n".join(filter(None, [
                    subject,
                    f"Scheduled for Plenary on {when.strftime('%d %B %Y')}.",
                    note])),
                url=f"{AGENDA}?CId={PLENARY_CID}&MId={meeting_id}",
                item_date=when,
                forum="Plenary",
                meeting_id=str(meeting_id),
                # A sitting date is the window in which there is still
                # something to do about it, so it is treated as a deadline —
                # the same choice the committee forward look makes.
                deadline=when,
                raw_ref=f"plenary_agenda:{meeting_id}",
            ))
        return when, items

    # -- the order paper ---------------------------------------------------

    def _order_paper(self, sitting: date):
        url = f"{ORDER_PAPER}/{sitting.strftime('%d-%m-%Y')}/"
        html = self.fetcher.get_text(url)
        if html is None:
            return

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        match = _ORDER_PAPER_TITLE.search(title)
        if not match:
            # No sitting that day, or questions not tabled yet. The Senedd
            # answers 200 with an error page for both. Normal; say nothing.
            return

        tabled_on = _dmy(match.group(1))
        answer_on = _dmy(match.group(2)) or sitting

        questions = soup.select(".itemContent.oralQuestion")
        if not questions:
            self.note_error(
                f"The order paper for {sitting.strftime('%d %B %Y')} says it "
                "carries tabled oral questions, but none could be read from "
                "it — the markup has probably changed. The parser expects "
                "'.itemContent.oralQuestion' per question. Until this is "
                "fixed, tabled questions are missing rather than absent.")
            return

        minister = ""
        for node in soup.select("h2.subheading.orderpaper, .itemContent.oralQuestion"):
            classes = node.get("class") or []
            if "subheading" in classes:
                minister = _clean(node.get_text(" ", strip=True))
                continue
            if item := self._question_to_item(node, minister, tabled_on,
                                              answer_on, url):
                yield item

    def _question_to_item(self, node, minister: str, tabled_on: date | None,
                          answer_on: date, url: str) -> Item | None:
        def text(selector: str) -> str:
            el = node.select_one(selector)
            return _clean(el.get_text(" ", strip=True)) if el else ""

        body = text(".itemContent__content")
        if not body:
            return None
        reference = text("span.title") or "Oral question"
        member = text("span.name")
        area = text("span.area")

        return Item(
            source_kind="oral_question",
            source_name="Oral Question (tabled)",
            title=reference,
            body=body,
            url=url,
            item_date=tabled_on or answer_on,
            speaker=member,
            constituency=area,
            forum="Plenary",
            agenda_item=(f"To the {minister}" if minister else ""),
            # The sitting it is down for. The Friday email treats a future
            # deadline on an oral question as "tabled, not yet asked", which is
            # precisely what this is.
            deadline=answer_on,
            raw_ref=f"orderpaper:{answer_on.isoformat()}:{reference}",
        )

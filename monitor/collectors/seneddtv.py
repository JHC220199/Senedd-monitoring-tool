"""The Senedd's own diary, read from senedd.tv.

WHY THIS EXISTS
---------------
Every committee meeting and Plenary sitting this tool knew about came from
the ModernGov web service at business.senedd.wales. In August 2026 that host
was put behind an Azure Application Gateway WAF that rejects datacentre IPs,
so it answers 403 to GitHub's runners on every request. The "Senedd forward
look" source has failed on every run since, and nobody was told, because the
diary it had already collected — on 4 August — sat in the archive looking
current. The page's "Coming up" section and the Friday email's committee
section were showing a diary six weeks stale.

senedd.tv is the Senedd's broadcast site. It is a different host, it is not
behind that rule, and it carries exactly what the diary needs. Measured from a
cloud host on 23 September 2026:

    GET https://business.senedd.wales/mgCalendarMonthView.aspx   -> 403 (Azure WAF)
    GET https://www.senedd.tv/                                   -> 200
    GET https://www.senedd.tv/Meeting/Index/<guid>               -> 200

The home page lists every broadcast meeting for the next five sitting days,
and each meeting page carries the full agenda — the same agenda the Camlas
morning briefing prints. On 23 September both agreed item for item: EHRSJ and
CCERA Committees at 09.30, Plenary at 13.30 with nine items in order.

WHAT IT DOES NOT SEE
--------------------
Meetings that are not broadcast. A committee meeting held wholly in private is
not on senedd.tv — on 24 September the Finance Committee's "Senedd
Commission: introductory briefing" was in the supplier's email and not here.
For the NRLA that is the right way round, since a private session is not one
anyone can watch or respond to, but it is a difference and it is said here.

It also sees five sitting days ahead, not three weeks. The Friday email's
diary is therefore about a week deep while business.senedd.wales stays
blocked, and the page's "What this page does not cover" panel says so.

HOW ITS ITEMS FIT THE ARCHIVE
-----------------------------
A committee meeting becomes one `calendar` item whose URL is the meeting's
papers page on business.senedd.wales — built from the ModernGov committee and
meeting IDs that senedd.tv itself links to. That is the same URL the blocked
forward look used, so the page's one-row-per-URL rule replaces a stale August
entry with this fresher, fuller one automatically. (The reader's browser, on
an office connection, opens that page normally; only this tool is blocked.)

A Plenary sitting becomes one item *per agenda item*, shaped exactly as the
forthcoming-business collector shapes Plenary agendas, because a sitting is
many unrelated pieces of business and the one worth knowing about — "Statement
by the Cabinet Minister for Finance: Rebalancing the non-domestic rates
system" — must be scored on its own rather than averaged into an afternoon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from bs4 import BeautifulSoup

from ..models import Item, _clean
from .base import Collector
from .forthcoming import AGENDA, PLENARY_CID, _ROUTINE_ITEMS


SENEDD_TV = "https://www.senedd.tv"
MEETING_PAGE = f"{SENEDD_TV}/Meeting/Index"

# "Start time: 09.30"
_START = re.compile(r"(\d{1,2})[.:](\d{2})")

# "23 September 2026"
_LONG_DATE = re.compile(r"\b(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b")

# The papers link on a meeting page:
#   http://www.senedd.assembly.wales/ieListDocuments.aspx?CId=978&MId=16233
_PAPERS = re.compile(r"ieListDocuments\.aspx\?CId=(\d+)&(?:amp;)?MId=(\d+)", re.I)

# The meeting's own ModernGov committee id, carried as a CSS class on the
# schedule card: `item-meeting com-0 com-983`. `com-0` is a placeholder.
_COMMITTEE_CLASS = re.compile(r"^com-(\d+)$")

# Committee agenda rows that are machinery rather than business. Present at
# almost every meeting, and meaningless to a reader: nobody needs to be told
# a committee will note its apologies. Anything here that nonetheless scores
# as NRLA-relevant is kept by the briefing — "Papers to note" can include a
# minister's letter on the Renting Homes Act.
#
# Two shapes. Most machinery opens the line ("Papers to note"). Private
# deliberation usually comes after a colon — "General scrutiny session:
# consideration of evidence", "Annual scrutiny session with Sport Wales:
# Consideration of evidence" — and would read, to a reader scanning for what
# to watch, like a second evidence session. So that phrase matches anywhere.
PROCEDURAL = re.compile(
    r"^\s*(introductions?,? apologies|apologies|declarations? of interest|"
    r"papers? ?\(?s?\)? to note|motion under standing order 17\.42|"
    r"any other business|date of (the )?next meeting|minutes of the previous|"
    r"private session|voting time|forward work programme|"
    r"committee activities|chair'?s update)"
    r"|consideration of (the )?evidence", re.I)


@dataclass
class AgendaEntry:
    number: str
    text: str

    @property
    def is_sub_item(self) -> bool:
        """"3.1" under "3 Papers to note"."""
        return "." in self.number

    @property
    def is_procedural(self) -> bool:
        return bool(PROCEDURAL.search(self.text))


@dataclass
class Meeting:
    """One broadcast meeting, as senedd.tv lists it."""

    guid: str
    name: str
    committee_id: str = ""
    start: str = ""                  # "09.30"
    room: str = ""
    day_index: int = 0               # 1 = today on the home page's tabs
    when: date | None = None         # from the meeting page, authoritative
    meeting_id: str = ""             # ModernGov MId, from the papers link
    agenda: list[AgendaEntry] = field(default_factory=list)

    @property
    def tv_url(self) -> str:
        return f"{MEETING_PAGE}/{self.guid}"

    @property
    def papers_url(self) -> str:
        """The meeting's papers on business.senedd.wales, or senedd.tv.

        Built rather than copied: senedd.tv links to the pre-2020 hostname
        `senedd.assembly.wales` over plain http. The canonical form matches the
        URL the forward look stored, which is what lets the page de-duplicate.
        """
        if self.committee_id and self.meeting_id:
            return (f"{AGENDA}?CId={self.committee_id}"
                    f"&MId={self.meeting_id}")
        return self.tv_url

    @property
    def is_plenary(self) -> bool:
        return (self.committee_id == str(PLENARY_CID)
                or self.name.strip().lower() == "plenary")

    def substantive(self) -> list[AgendaEntry]:
        """Agenda items worth printing, in order.

        A sub-item is dropped with its parent: "3.1 Inter-Institutional
        Relations Agreement" means nothing without "3 Papers to note", and the
        parent is machinery. Callers that want to rescue a relevant procedural
        item should work from `agenda` directly.
        """
        kept: list[AgendaEntry] = []
        parent_dropped = False
        for entry in self.agenda:
            if not entry.is_sub_item:
                parent_dropped = entry.is_procedural
                if not parent_dropped:
                    kept.append(entry)
            elif not parent_dropped and not entry.is_procedural:
                kept.append(entry)
        return kept


_LEADING_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[-–—.]\s*")
_NO_AGENDA = re.compile(r"no agenda items (are )?available", re.I)


def _agenda_entry(row) -> AgendaEntry | None:
    """One agenda row, in either of senedd.tv's two layouts.

    Not yet reached:   <div class="col-xs-1"><b>3</b></div>
                       <div class="col-xs-10"><b>Papers to note</b></div>
    Timestamped:       <a class="agenda-item-time" data-item-number="2">
                         <h5><b>2</b> - General scrutiny of …</h5>
                         <footer>Start time: 10:42</footer>

    Both appear on one page once a meeting is under way — the items already
    reached carry a timestamp, the rest do not.
    """
    text_el = row.select_one(".col-xs-10")
    if text_el is not None:
        num_el = row.select_one(".col-xs-1")
        return AgendaEntry(
            number=_clean(num_el.get_text(" ", strip=True)) if num_el else "",
            text=_clean(text_el.get_text(" ", strip=True)))

    heading = row.select_one("h5") or row.select_one("header")
    if heading is None:
        return None
    raw = _clean(heading.get_text(" ", strip=True))
    number = ""
    if anchor := row.select_one("[data-item-number]"):
        number = (anchor.get("data-item-number") or "").strip()
    if m := _LEADING_NUMBER.match(raw):
        number = number or m.group(1)
        raw = raw[m.end():]
    return AgendaEntry(number=number, text=raw.strip())


class SeneddTVScheduleCollector(Collector):
    """Committee meetings and Plenary sittings for the next five sitting days."""

    name = "senedd_tv_schedule"
    source_kind = "calendar"

    home_html: str = ""

    # The home page lists five days; a sitting week is rarely more than a dozen
    # broadcast meetings. The cap bounds a run even if that changes.
    MAX_MEETINGS = 25

    # -- the home page ------------------------------------------------------

    def schedule(self) -> list[Meeting]:
        """Every meeting on the home page's five day tabs. Never raises."""
        html = self.fetcher.get_text(f"{SENEDD_TV}/")
        if html is None:
            # Loud failure #1: this is now the only diary source the tool has.
            self.note_error(
                "senedd.tv could not be fetched. It is the only source of the "
                "committee and Plenary diary that is reachable from GitHub — "
                "business.senedd.wales blocks this tool — so today's agenda, "
                "the morning briefing and the page's 'Coming up' section are "
                "all empty for the wrong reason until it is back.")
            return []

        self.home_html = html
        meetings = self.parse_schedule(html)
        if not meetings and "id=\"dayTabs\"" not in html and "dayTabs" not in html:
            # Loud failure #2, separately worded. A page with no day tabs at
            # all is a redesign, not a quiet week — a recess still renders the
            # tabs, each saying there are no meetings that day.
            self.note_error(
                "senedd.tv answered but its schedule could not be read — the "
                "markup has probably changed. The parser expects day tabs "
                "(#dayTabs, #day1…#day5) holding '.item-meeting' cards.")
        return meetings

    @staticmethod
    def parse_schedule(html: str) -> list[Meeting]:
        soup = BeautifulSoup(html, "html.parser")
        meetings: list[Meeting] = []
        seen: set[str] = set()

        # The first tab is nested inside a duplicate of itself in the live
        # markup (`#day1` inside `#day1`), so the same card is reachable twice;
        # `seen` is what stops today's meetings being listed twice.
        for pane in soup.select("div.tab-pane[id^=day]"):
            match = re.match(r"day(\d+)$", pane.get("id") or "")
            if not match:
                continue
            day_index = int(match.group(1))

            for card in pane.select("div.item-meeting"):
                link = card.select_one("h4 a[href*='/Meeting/Index/']")
                if not link:
                    continue
                guid = (link.get("href") or "").rstrip("/").rsplit("/", 1)[-1]
                if not guid or guid in seen:
                    continue
                seen.add(guid)

                committee_id = ""
                for cls in card.get("class") or []:
                    if (m := _COMMITTEE_CLASS.match(cls)) and m.group(1) != "0":
                        committee_id = m.group(1)

                start = ""
                if box := card.select_one(".time-box"):
                    if m := _START.search(box.get_text(" ", strip=True)):
                        start = f"{int(m.group(1)):02d}.{m.group(2)}"

                room = ""
                heading = link.find_parent("h4")
                if heading and (para := heading.find_next_sibling("p")):
                    room = _clean(para.get_text(" ", strip=True))

                meetings.append(Meeting(
                    guid=guid,
                    name=_clean(link.get_text(" ", strip=True)),
                    committee_id=committee_id,
                    start=start,
                    room=room,
                    day_index=day_index,
                ))
        return meetings

    @staticmethod
    def parse_recent_dates(html: str) -> list[date]:
        """Dates of the meetings in the home page's "Latest meetings" strip.

        This is how the morning briefing knows when the Senedd last sat
        without keeping any state of its own: the Welsh Government section
        covers everything since the last briefing would have gone out, and a
        briefing goes out on a sitting day. Newest first, de-duplicated.
        """
        soup = BeautifulSoup(html or "", "html.parser")
        dates: set[date] = set()
        for slide in soup.select(".slider-one .slide"):
            for para in slide.select("p"):
                if m := _LONG_DATE.search(para.get_text(" ", strip=True)):
                    try:
                        dates.add(datetime.strptime(m.group(1), "%d %B %Y").date())
                    except ValueError:
                        pass
        return sorted(dates, reverse=True)

    @staticmethod
    def parse_recent_meetings(html: str) -> list[Meeting]:
        """The meetings in the home page's "Latest meetings" strip — the ones
        that have already happened — with the date the strip gives them.

        The debate summaries start here: these are the meetings whose Record
        may now be published. Each still needs its page read (``fill``) for
        the ModernGov meeting id, which is also the Record's meeting id.
        """
        soup = BeautifulSoup(html or "", "html.parser")
        out: list[Meeting] = []
        seen: set[str] = set()
        for slide in soup.select(".slider-one .slide"):
            link = slide.select_one("a[href*='/Meeting/']")
            if link is None:
                continue
            guid = (link.get("href") or "").rstrip("/").rsplit("/", 1)[-1]
            if not guid or guid in seen:
                continue
            seen.add(guid)
            when = None
            name = ""
            for para in slide.select("p"):
                text = para.get_text(" ", strip=True)
                if m := _LONG_DATE.search(text):
                    try:
                        when = datetime.strptime(m.group(1), "%d %B %Y").date()
                    except ValueError:
                        pass
                elif text and not name:
                    name = _clean(text)
            if not name:
                name = _clean(_LONG_DATE.sub("", slide.get_text(" ", strip=True)))
            out.append(Meeting(guid=guid, name=name, when=when))
        return out

    # -- one meeting --------------------------------------------------------

    def fill(self, meeting: Meeting) -> Meeting:
        """Add the date, the ModernGov meeting id and the agenda."""
        html = self.fetcher.get_text(meeting.tv_url)
        if html is None:
            self.note_error(f"senedd.tv meeting page for {meeting.name} "
                            f"could not be fetched ({meeting.tv_url}).")
            return meeting
        self.parse_meeting(html, meeting)
        return meeting

    @staticmethod
    def parse_meeting(html: str, meeting: Meeting) -> Meeting:
        soup = BeautifulSoup(html, "html.parser")

        # The title block. Two layouts, both live on the same day:
        #
        #   before the meeting   .player-title > a > h2, p     (with a dropdown)
        #   once it has ended    .player-title > h2, p         (/Meeting/Archive)
        #
        # /Meeting/Index/<guid> redirects to /Meeting/Archive/<guid> as soon as
        # a meeting finishes. The first version read only the first layout, so
        # a run after 11.30 lost the date of every meeting that had sat that
        # morning — and a meeting with no date is dropped, which made a busy
        # morning look like an empty one.
        #
        # In the first layout the dropdown lists the day's OTHER meetings with
        # their own <h2>s, so only direct children are read, or a Plenary page
        # would be named and dated from whichever committee sat first.
        block = soup.select_one(".player-title")
        title = None
        if block is not None:
            title = block.find("a", recursive=False) or block
        if title is not None:
            if heading := title.find("h2", recursive=False):
                meeting.name = _clean(heading.get_text(" ", strip=True)) or meeting.name
            for para in title.find_all("p", recursive=False):
                if m := _LONG_DATE.search(para.get_text(" ", strip=True)):
                    try:
                        meeting.when = datetime.strptime(m.group(1), "%d %B %Y").date()
                    except ValueError:
                        pass
                    break

        if m := _PAPERS.search(html):
            meeting.committee_id = meeting.committee_id or m.group(1)
            meeting.meeting_id = m.group(2)

        # The agenda is rendered twice, once for phones and once for desktops.
        # The first copy is complete; reading both would list every item twice.
        agenda = soup.select_one("#agenda")
        entries: list[AgendaEntry] = []
        if agenda is not None:
            for row in agenda.select("article.agenda-item"):
                entry = _agenda_entry(row)
                # A sitting whose agenda is not yet published renders one
                # placeholder row: "There are no agenda items available for
                # this video". Kept, it would print as a line of business.
                if entry and entry.text and not _NO_AGENDA.search(entry.text):
                    entries.append(entry)
        meeting.agenda = entries
        return meeting

    def meetings(self) -> list[Meeting]:
        """The schedule, with every meeting's page read. Never raises."""
        listed = self.schedule()[: self.MAX_MEETINGS]
        return [self.fill(m) for m in listed]

    # -- the archive ----------------------------------------------------------

    def collect(self):
        for meeting in self.meetings():
            yield from self.to_items(meeting)

    @staticmethod
    def to_items(meeting: Meeting) -> list[Item]:
        if meeting.when is None:
            # Without a date a meeting cannot be placed in a diary, and a guess
            # from the day tab would be wrong the moment the page is cached.
            return []

        when_text = meeting.when.strftime("%-d %B %Y")
        at = f", {meeting.start}" if meeting.start else ""

        if meeting.is_plenary:
            items = []
            for entry in meeting.agenda:
                if entry.is_procedural or _ROUTINE_ITEMS.match(entry.text):
                    continue
                items.append(Item(
                    source_kind="calendar",
                    source_name="Plenary agenda",
                    title=entry.text,
                    body="\n".join([
                        entry.text,
                        f"Scheduled for Plenary on {when_text}{at}."]),
                    # One URL per agenda item. The page keeps one row per URL
                    # for calendar items, and a sitting's items all share its
                    # papers page — without the fragment every statement but
                    # one would be silently discarded.
                    url=f"{meeting.papers_url}#item-{entry.number or len(items) + 1}",
                    item_date=meeting.when,
                    forum="Plenary",
                    agenda_item=f"Plenary — {when_text}{at}",
                    meeting_id=meeting.meeting_id,
                    video_url=meeting.tv_url,
                    deadline=meeting.when,
                    raw_ref=f"seneddtv:{meeting.guid}:{entry.number}",
                ))
            return items

        substantive = meeting.substantive()
        agenda_lines = [f"{e.number}. {e.text}" if e.number else e.text
                        for e in substantive]
        return [Item(
            source_kind="calendar",
            source_name="Senedd diary (senedd.tv)",
            title=f"{meeting.name} — {when_text}{at}",
            body="\n".join(filter(None, [
                f"{meeting.name} meets on {when_text}{' at ' + meeting.start if meeting.start else ''}"
                f"{' in ' + meeting.room if meeting.room else ''}.",
                "Agenda:" if agenda_lines else "",
                *agenda_lines])),
            url=meeting.papers_url,
            item_date=meeting.when,
            forum=meeting.name,
            agenda_item="; ".join(e.text for e in substantive),
            meeting_id=meeting.meeting_id,
            video_url=meeting.tv_url,
            deadline=meeting.when,
            raw_ref=f"seneddtv:{meeting.guid}",
        )]

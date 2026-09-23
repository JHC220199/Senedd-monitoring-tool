"""The morning briefing — what the Senedd is doing today.

WHAT THIS REPLACES
------------------
Camlas's "Bore da" email, which arrives at about 08.20 on sitting days. Its
structure is the specification, because it is what the directorate already
reads, and the operator chose the full day rather than a filtered one
(23 September 2026):

    On Today's Agenda     every broadcast meeting, with its agenda
    Welsh Government      announcements since the last briefing
    Coming Up Tomorrow    the next sitting day's meetings

What this adds is a mark. Every agenda item, tabled question and announcement
is scored by the same rules as the live page, and anything that passes carries
an **NRLA** tag. The whole day is still there, so nothing is hidden; the tag is
what lets the two things that matter be found among the thirty that do not.

On 23 September that difference was not cosmetic. The supplier's briefing
listed four Welsh Government announcements and omitted a fifth — "Welsh
Government to fund interim alarm measures for leaseholders facing waking watch
costs", published at 18.20 the evening before. It is building safety and
leasehold, it is core NRLA business, and it would have been the first thing in
this email with a tag on it.

WHEN IT IS SENT
---------------
Only when the Senedd is sitting, and that is decided by the Senedd's own diary
rather than by a calendar of weekdays: if senedd.tv lists no broadcast meeting
today, nothing is sent. Recess, a Friday, a bank holiday — all handled without
anyone maintaining a list of dates.

WHY IT IS NOT STARTED BY GITHUB'S TIMER
---------------------------------------
Measured over September 2026, the daily workflow scheduled for 06.30 UTC began
between 11.29 and 13.07 UTC — five to six and a half hours late, every day.
GitHub does not guarantee when a scheduled run starts. A morning briefing on
that timer would arrive after lunch.

So the clock is a Power Automate flow with a Recurrence trigger at 07.30
London time, which asks GitHub to start `morning.yml` straight away. A manually
started run does not wait in the scheduled queue. See MORNING-BRIEFING-SETUP.md.

WHAT "SINCE THE LAST BRIEFING" MEANS
------------------------------------
The Welsh Government section covers everything published since 07.30 on the
last day the Senedd sat — the last day a briefing would have gone out. That
date comes from senedd.tv's own "Latest meetings" strip, so no state is kept
between runs. It is capped at four days, so the first briefing after a recess
does not replay the whole summer.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .collectors.seneddtv import Meeting
from .forward import (DARK_BLUE, EDGE, FONT, LINE, MUTED, OFF_BLACK, OFF_WHITE,
                      ORANGE, WIDTH)
from .models import Item
from .relevance import Scorer, Taxonomy, find_terms


LONDON = ZoneInfo("Europe/London")

# The hour the Power Automate flow starts the run. Also the boundary between
# one briefing's Welsh Government section and the next.
BRIEFING_TIME = time(7, 30)

# However long since the Senedd last sat, never look further back than this.
MAX_WINDOW_DAYS = 4

# A committee agenda item with more sub-items than this is printed as its
# heading only, unless a sub-item is marked. The Petitions Committee on
# 24 September had twelve new petitions — horse tethering, red squirrels, a
# statue of Gwenllian ferch Gruffydd — and printing every one would bury the
# rest of the day under a list nobody asked for.
MAX_SUB_ITEMS = 3


def london_now(now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(LONDON)


def london_today(now_utc: datetime | None = None) -> date:
    """Today as a reader in Cardiff means it, not as the runner's UTC clock does."""
    return london_now(now_utc).date()


def previous_weekday(day: date) -> date:
    step = 3 if day.weekday() == 0 else 2 if day.weekday() == 6 else 1
    return day - timedelta(days=step)


def window_start(today: date, recent_sittings: list[date]) -> datetime:
    """07.30 London on the last sitting day before today, as naive UTC.

    Naive UTC because that is what the newsroom prints and what its cards are
    parsed into. Falls back to the previous weekday if senedd.tv's "Latest
    meetings" strip is empty or unreadable.
    """
    earlier = [d for d in recent_sittings if d < today]
    last = earlier[0] if earlier else previous_weekday(today)
    last = max(last, today - timedelta(days=MAX_WINDOW_DAYS))
    local = datetime.combine(last, BRIEFING_TIME, tzinfo=LONDON)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# What goes in
# ---------------------------------------------------------------------------

@dataclass
class Line:
    """One printed line of business, and whether it is marked."""

    text: str
    marked: bool = False
    detail: str = ""          # a muted second line, e.g. who asked
    url: str = ""


@dataclass
class MeetingBlock:
    meeting: Meeting
    marked: bool                          # the whole meeting is NRLA business
    lines: list[Line] = field(default_factory=list)


@dataclass
class Announcement:
    item: Item
    published_utc: datetime | None
    marked: bool
    lead: str = ""

    @property
    def summary(self) -> str:
        """The notice's own first line, verbatim — never a paraphrase.

        The card's first summary point when there is one; otherwise the first
        sentence of the body.
        """
        if self.lead:
            return self.lead if len(self.lead) <= 240 else (
                self.lead[:240].rsplit(" ", 1)[0] + "…")
        body = self.item.body or ""
        title = self.item.title or ""
        if body.startswith(title):
            body = body[len(title):]
        # Paragraph first, then sentence. The newsroom's card summary often has
        # no closing full stop, so splitting on sentences alone ran it into the
        # next paragraph: "…difficult to enforce against under current law
        # Views and evidence sought…".
        paragraph = next((p.strip() for p in re.split(r"\n+", body) if p.strip()), "")
        first = re.split(r"(?<=[.!?])\s+", paragraph, maxsplit=1)[0] if paragraph else ""
        if len(first) > 240:
            first = first[:240].rsplit(" ", 1)[0] + "…"
        return first


@dataclass
class Briefing:
    today: date
    today_blocks: list[MeetingBlock]
    next_day: date | None
    next_blocks: list[MeetingBlock]
    announcements: list[Announcement]
    since_utc: datetime
    notes: list[str] = field(default_factory=list)

    @property
    def marked_count(self) -> int:
        n = sum(1 for a in self.announcements if a.marked)
        for block in self.today_blocks:
            n += (1 if block.marked else 0) + sum(1 for l in block.lines if l.marked)
        return n


class Marker:
    """Decides what carries the NRLA tag, using the page's own rule.

    One definition of "relevant" for the whole system. If the morning email
    tagged something the page would not show, or the reverse, the reader would
    reasonably stop trusting either.
    """

    def __init__(self, tax: Taxonomy, scorer: Scorer | None = None):
        self.tax = tax
        self.scorer = scorer or Scorer(tax)
        self._always = tax.site_config.get("always_relevant_in_title", []) or []

    def meeting(self, meeting: Meeting) -> bool:
        """The housing committee's own meetings are always NRLA business."""
        return bool(find_terms(meeting.name, self._always))

    def text(self, text: str, forum: str = "") -> bool:
        probe = Item(source_kind="calendar", source_name="Senedd diary",
                     title=text, body=text, forum=forum)
        self.scorer.score_item(probe)
        return self.tax.qualifies_for_site(probe)

    def item(self, item: Item) -> bool:
        if not item.band:
            self.scorer.score_item(item)
        return self.tax.qualifies_for_site(item)


def meeting_block(meeting: Meeting, marker: Marker,
                  questions: list[Item] | None = None,
                  compact: bool = False) -> MeetingBlock:
    """The printable agenda of one meeting.

    Committee: substantive items, plus any procedural item or sub-item that is
    marked (a minister's letter under "Papers to note" can be the one line
    worth reading). Plenary: every item except Voting Time, as the supplier
    prints it — the question sessions tell the reader which ministers are up.
    """
    block = MeetingBlock(meeting=meeting, marked=marker.meeting(meeting))
    entries = meeting.agenda
    lines: list[Line] = []

    i = 0
    while i < len(entries):
        entry = entries[i]
        if entry.is_sub_item:
            i += 1
            continue
        subs = []
        j = i + 1
        while j < len(entries) and entries[j].is_sub_item:
            subs.append(entries[j])
            j += 1

        marked = marker.text(entry.text, meeting.name)
        sub_lines = [Line(text=s.text, marked=marker.text(s.text, meeting.name))
                     for s in subs]
        any_sub_marked = any(s.marked for s in sub_lines)

        if meeting.is_plenary:
            keep = not re.match(r"^\s*voting time", entry.text, re.I)
        else:
            keep = (not entry.is_procedural) or marked or any_sub_marked
        if keep:
            lines.append(Line(text=entry.text, marked=marked))
            if not compact:
                if any_sub_marked or len(sub_lines) > MAX_SUB_ITEMS:
                    shown = [s for s in sub_lines if s.marked]
                else:
                    shown = [] if entry.is_procedural else sub_lines
                for s in shown:
                    lines.append(Line(text=s.text, marked=s.marked,
                                      detail="__sub__"))
                hidden = len(sub_lines) - len(shown)
                if hidden and len(sub_lines) > MAX_SUB_ITEMS:
                    lines.append(Line(
                        text=f"{hidden} more item{'s' if hidden != 1 else ''} "
                             f"under this heading, none marked",
                        detail="__more__"))
        i = j

    if questions and meeting.is_plenary:
        for q in questions:
            who = q.speaker + (f" ({q.constituency})" if q.constituency else "")
            lines.append(Line(
                text=q.body or q.title, marked=True,
                detail=" · ".join(filter(None, [
                    f"Tabled for today: {q.title}" if q.title else "Tabled for today",
                    who, q.agenda_item])),
                url=q.url))

    block.lines = lines
    return block


def build(meetings: list[Meeting], announcements: list[tuple],
          questions: list[Item], tax: Taxonomy, today: date,
          recent_sittings: list[date] | None = None,
          scorer: Scorer | None = None) -> Briefing:
    marker = Marker(tax, scorer)

    def by_start(m: Meeting) -> tuple:
        return (m.start or "99.99", m.name)

    todays = sorted((m for m in meetings if m.when == today), key=by_start)
    later = sorted({m.when for m in meetings if m.when and m.when > today})
    next_day = later[0] if later else None
    nexts = sorted((m for m in meetings if m.when == next_day), key=by_start)

    oq_today = [q for q in questions
                if q.source_kind == "oral_question" and q.deadline == today
                and marker.item(q)]

    blocks_today = [meeting_block(m, marker, oq_today if m.is_plenary else None)
                    for m in todays]
    blocks_next = [meeting_block(m, marker, compact=True) for m in nexts]

    news = []
    for entry in announcements:
        at, it = entry[0], entry[1]
        lead = entry[2] if len(entry) > 2 else ""
        news.append(Announcement(item=it, published_utc=at,
                                 marked=marker.item(it), lead=lead))
    news.sort(key=lambda a: a.published_utc or datetime.combine(
        a.item.item_date or today, time(0)), reverse=True)

    return Briefing(
        today=today, today_blocks=blocks_today,
        next_day=next_day, next_blocks=blocks_next,
        announcements=news,
        since_utc=window_start(today, recent_sittings or []))


# ---------------------------------------------------------------------------
# How it looks — built for Outlook on Windows, like the Friday email
# ---------------------------------------------------------------------------

def _e(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _tag() -> str:
    return ('<span style="background:#FDEDE0;color:#A8501A;font-size:10px;'
            'font-weight:700;letter-spacing:.6px;padding:2px 6px;'
            'margin-right:7px">NRLA</span>')


def _link(text: str, url: str, colour: str = DARK_BLUE, weight: int = 600) -> str:
    if not url:
        return _e(text)
    return (f'<a href="{_e(url)}" style="color:{colour};font-weight:{weight};'
            f'text-decoration:none">{_e(text)}</a>')


def _card(rows: list[str]) -> str:
    if not rows:
        return ""
    body = "".join(rows[:-1]) + rows[-1].replace(f"border-bottom:1px solid {LINE};", "")
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="border-collapse:collapse;'
            f'width:100%;background:#ffffff;border:1px solid {EDGE}">'
            f'{body}</table>')


def _heading(title: str, lede: str = "") -> str:
    return (f'<tr><td style="padding:28px 28px 0;font-family:{FONT}">'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:1.2px;'
            f'text-transform:uppercase;color:{ORANGE}">{_e(title)}</div>'
            + (f'<div style="font-size:12.5px;color:{MUTED};line-height:1.5;'
               f'padding:6px 0 11px">{_e(lede)}</div>' if lede
               else '<div style="height:11px;line-height:11px;font-size:0">&nbsp;</div>'))


def _meeting_row(block: MeetingBlock, compact: bool) -> str:
    m = block.meeting
    cell = (f'padding:14px 18px;border-bottom:1px solid {LINE};'
            f'font-family:{FONT};vertical-align:top;')
    name = (_tag() if block.marked else "") + _link(m.name, m.papers_url, OFF_BLACK, 700)
    where = " · ".join(filter(None, [m.room, _link("watch on Senedd.tv", m.tv_url, DARK_BLUE, 600)
                                     if not compact else ""]))

    if compact:
        items = " · ".join(
            (_tag() if l.marked else "") + _e(l.text)
            for l in block.lines if l.detail not in ("__more__",))
        agenda = (f'<div style="font-size:12.5px;line-height:1.55;color:{MUTED};'
                  f'padding-top:4px">{items}</div>') if items else ""
    else:
        rows = []
        for l in block.lines:
            if l.detail == "__more__":
                rows.append(f'<div style="font-size:12px;color:{MUTED};'
                            f'padding:2px 0 0 28px;font-style:italic">{_e(l.text)}</div>')
                continue
            indent = 28 if l.detail == "__sub__" else 12
            weight = 600 if l.marked else 400
            text = _link(l.text, l.url, OFF_BLACK, weight) if l.url else _e(l.text)
            detail = (f'<div style="font-size:11.5px;color:{MUTED};padding-top:2px">'
                      f'{_e(l.detail)}</div>') if l.detail and not l.detail.startswith("__") else ""
            rows.append(
                f'<div style="font-size:13.5px;line-height:1.5;color:{OFF_BLACK};'
                f'font-weight:{weight};padding:4px 0 0 {indent}px">'
                f'{_tag() if l.marked else "&#8226;&nbsp;"}{text}{detail}</div>')
        agenda = "".join(rows) or (
            f'<div style="font-size:12.5px;color:{MUTED};padding-top:4px;'
            f'font-style:italic">Agenda not yet published.</div>')

    return (
        f'<tr><td width="64" style="{cell}width:64px;font-size:14px;'
        f'font-weight:700;color:{OFF_BLACK}">{_e(m.start or "—")}</td>'
        f'<td style="{cell}">'
        f'<div style="font-size:15px;line-height:1.45">{name}</div>'
        + (f'<div style="font-size:12px;color:{MUTED};padding-top:2px">{where}</div>'
           if where else "")
        + f'{agenda}</td></tr>')


def _announcement_row(a: Announcement) -> str:
    cell = (f'padding:14px 18px;border-bottom:1px solid {LINE};'
            f'font-family:{FONT};vertical-align:top;')
    when = ""
    if a.published_utc is not None:
        local = a.published_utc.replace(tzinfo=timezone.utc).astimezone(LONDON)
        when = local.strftime("%a %-d %b, %H.%M")
    elif a.item.item_date:
        when = a.item.item_date.strftime("%a %-d %b")
    return (
        f'<tr><td style="{cell}">'
        f'<div style="font-size:15px;line-height:1.45">'
        f'{_tag() if a.marked else ""}{_link(a.item.title, a.item.url, DARK_BLUE, 600)}</div>'
        + (f'<div style="font-size:13px;line-height:1.5;color:{OFF_BLACK};'
           f'padding-top:4px">{_e(a.summary)}</div>' if a.summary else "")
        + f'<div style="font-size:11.5px;color:{MUTED};padding-top:4px">{_e(when)}</div>'
        f'</td></tr>')


def _day_label(day: date, today: date) -> str:
    if day == today + timedelta(days=1):
        return "tomorrow"
    return day.strftime("%A %-d %B")


def render_morning(b: Briefing, page_url: str = "") -> tuple[str, str, int]:
    """Return ``(subject, html_body, count)``; count is today's meetings.

    A count of zero means the Senedd is not sitting and nothing is sent.
    """
    count = len(b.today_blocks)
    marked = b.marked_count
    day_text = b.today.strftime("%A %-d %B %Y")

    subject = (f"Bore da: Senedd morning briefing — {b.today.strftime('%a %-d %B')}"
               + (f" ({marked} marked NRLA)" if marked else ""))

    n_news = len(b.announcements)
    tally = " · ".join(filter(None, [
        f"{count} meeting{'s' if count != 1 else ''} today",
        f"{n_news} Welsh Government announcement{'s' if n_news != 1 else ''}"
        if n_news else "",
    ]))
    marked_line = (
        f'<b style="color:{ORANGE}">{marked} item{"s" if marked != 1 else ""} '
        f'marked NRLA.</b> Marked by the same rules as the live page; '
        f'everything else is listed so the whole day is visible.'
        if marked else
        "Nothing on today's agenda or in the announcements matches the NRLA's "
        "relevance rules. The whole day is listed below regardless.")

    today_rows = [_meeting_row(bl, compact=False) for bl in b.today_blocks]
    news_rows = [_announcement_row(a) for a in b.announcements]
    next_rows = [_meeting_row(bl, compact=True) for bl in b.next_blocks]

    since_local = b.since_utc.replace(tzinfo=timezone.utc).astimezone(LONDON)
    news_lede = (f"Published since {since_local.strftime('%H.%M on %A %-d %B')}. "
                 f"The line under each is the notice's own words.")

    sections = [
        _heading("On today's agenda") + _card(today_rows) + "</td></tr>",
    ]
    if news_rows:
        sections.append(_heading("Welsh Government", news_lede) + _card(news_rows) + "</td></tr>")
    else:
        sections.append(_heading("Welsh Government", news_lede)
                        + _card([f'<tr><td style="padding:14px 18px;font-family:{FONT};'
                                 f'font-size:13px;color:{MUTED}">No announcements in this '
                                 f'period.</td></tr>']) + "</td></tr>")
    if next_rows and b.next_day:
        sections.append(_heading(f"Coming up {_day_label(b.next_day, b.today)}")
                        + _card(next_rows) + "</td></tr>")

    notes = "".join(
        f'<p style="font-size:12px;color:#8A3B06;margin:0;padding-top:10px;'
        f'font-family:{FONT}"><b>Note:</b> {_e(n)}</p>' for n in b.notes)

    footer_link = (
        f'<p style="font-size:12.5px;color:{MUTED};margin:0;padding-top:22px;'
        f'font-family:{FONT}">The full archive, open consultations and what '
        f'was said in the Chamber are on the '
        f'<a href="{_e(page_url)}" style="color:{DARK_BLUE};font-weight:600">'
        f'live page</a>.</p>') if page_url else ""

    html_body = f"""<table role="presentation" width="100%" cellpadding="0" \
cellspacing="0" border="0" style="border-collapse:collapse;background:{OFF_WHITE}">
<tr><td align="center" style="padding:0">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" \
border="0" style="border-collapse:collapse;width:{WIDTH}px;max-width:{WIDTH}px">

  <tr><td bgcolor="{DARK_BLUE}" style="padding:24px 28px 20px;font-family:{FONT};color:#ffffff">
    <div style="font-size:20px;font-weight:700;letter-spacing:-.2px;color:#ffffff">
      Bore da — the Senedd today</div>
    <div style="font-size:13px;color:#C3D2DC;padding-top:5px">
      National Residential Landlords Association &nbsp;·&nbsp; {_e(day_text)}</div>
  </td></tr>
  <tr><td bgcolor="{ORANGE}" height="4" style="height:4px;line-height:4px;
    font-size:0">&nbsp;</td></tr>

  <tr><td style="padding:22px 28px 0;font-family:{FONT}">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      border="0" style="border-collapse:collapse;background:#ffffff;
      border:1px solid {EDGE}">
      <tr><td style="padding:15px 18px;font-family:{FONT}">
        <div style="font-size:14px;font-weight:700;color:{OFF_BLACK};
          line-height:1.5">{_e(tally)}</div>
        <div style="font-size:12.5px;color:{MUTED};line-height:1.55;
          padding-top:7px">{marked_line}</div>
      </td></tr>
    </table>
  </td></tr>

  {"".join(sections)}

  <tr><td style="padding:0 28px 34px;font-family:{FONT}">
    {notes}
    {footer_link}
    <p style="font-size:11.5px;color:{MUTED};line-height:1.55;margin:0;
      padding-top:16px;font-family:{FONT}">
      Meetings and agendas are from senedd.tv, which lists broadcast meetings
      only; a meeting held wholly in private will not appear. Senedd Cymru and
      Welsh Government material is reproduced under the Open Government Licence
      v3.0. Nothing in this email is summarised by a language model.
      Written questions are deliberately excluded; the team's dedicated tool
      tracks those.
    </p>
  </td></tr>

</table>
</td></tr></table>"""

    return subject, html_body, count

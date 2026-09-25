"""The week in review — the first half of the Friday email.

WHAT THIS REPLACES
------------------
Camlas's "W38 NRLA Weekly Briefing": a Word document of twelve pages, headed
by bullet points under Big Picture, Housing, Welfare, Planning, Economy &
Regeneration, Local Government and Legislation Watch, then long extracts.
The directorate wanted something "far more targeted to actual NRLA areas of
interest", in the body of the Friday email, a line and a link per item.

HOW IT IS BUILT
---------------
Only from what happened this week (Monday to Friday), and only what matches
the NRLA's relevance rules — the same rules as the live page:

  * The Senedd's draft Record of every meeting this week, read with the same
    selection as the debate summaries (monitor/debates.py): a relevant
    statement or debate as a whole, or the relevant questions and requests
    out of a question session or the Business Statement.
  * Welsh Government press notices and written statements this week, with
    the same selection as the press release alerts.
  * The political changes this week's news alerts reported (leadership,
    defections, reshuffles), from the news alerts' memory.

Each is filed under ONE NRLA theme (THEMES below), chosen from the taxonomy
themes it matches. Empty themes are left out. The five highest-scoring
entries of the week are also listed at the top as Headlines.

Nothing is summarised. Each line is a title, who and where, and the most
relevant sentence the speaker actually said, with a link to the Record.

WHEN THE SENEDD DID NOT SIT
---------------------------
No Plenary and no committee this week means no review: the Friday email is
then the future-business email alone, as before.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .forward import (DARK_BLUE, EDGE, FONT, LINE, MUTED, OFF_BLACK, ORANGE)
from .models import Item
from .relevance import Scorer, Taxonomy


# The NRLA's own areas, in the order the directorate reads them. Each maps to
# taxonomy themes; an entry goes under the first area holding its
# highest-weighted theme.
THEMES: list[tuple[str, str, tuple[str, ...]]] = [
    ("prs", "Private renting & renting reform",
     ("private_rented_sector", "tenancy_law", "evictions_and_possession",
      "rent_controls_and_affordability", "hmos_and_standards")),
    ("enforcement", "Rent Smart Wales, licensing & enforcement",
     ("rent_smart_wales", "local_government_enforcement")),
    ("safety", "Building safety & leasehold", ("building_safety_and_leasehold",)),
    ("energy", "Energy efficiency & retrofit", ("energy_efficiency",)),
    ("tax", "Property tax & second homes",
     ("taxation_of_property", "second_homes_and_short_term_lets")),
    ("supply", "Housing supply, planning & social housing",
     ("housing_supply", "planning_system", "social_housing")),
    ("homeless", "Homelessness & tenants' finances",
     ("homelessness", "welfare_and_support")),
]
OTHER = ("other", "Other relevant business")

HEADLINES = 5
PER_THEME = 6
QUOTE_WORDS = 40

# An entry must score at least this to be in the weekly. The relevance rule
# says whether something touches an NRLA theme at all; this says whether it is
# worth a line in a five-minute read. Set against the week of 21 September
# 2026: every exchange the debate summaries rightly picked scored 94 or more,
# while a question about the Senedd's own landlord and office move scored 44.
MIN_SCORE = 60


@dataclass
class Entry:
    title: str
    url: str
    meta: str                    # "Plenary · Tue 22 Sep · David Hughes MS"
    quote: str = ""              # verbatim
    quote_by: str = ""
    score: float = 0.0
    theme: str = OTHER[0]
    when: date | None = None


@dataclass
class Review:
    week_start: date
    week_end: date
    entries: list[Entry] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)   # political changes
    pending: list[str] = field(default_factory=list)    # Records not yet out
    sat: bool = False

    @property
    def count(self) -> int:
        return len(self.entries) + len(self.changes)

    def by_theme(self) -> list[tuple[str, list[Entry]]]:
        out = []
        for key, label, _ in THEMES + [(OTHER[0], OTHER[1], ())]:
            rows = sorted((e for e in self.entries if e.theme == key),
                          key=lambda e: (-e.score, e.when or date.min))
            if rows:
                out.append((label, rows[:PER_THEME]))
        return out

    def headlines(self) -> list[Entry]:
        return sorted(self.entries, key=lambda e: -e.score)[:HEADLINES]


def week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=4)


class Themer:
    """Files a piece of text under one NRLA theme, and scores it."""

    def __init__(self, tax: Taxonomy, scorer: Scorer | None = None):
        self.tax = tax
        self.scorer = scorer or Scorer(tax)
        self.weights = {k: float(v.get("weight", 0)) for k, v in tax.themes.items()}

    def _themes(self, title: str, body: str) -> tuple[list[str], float]:
        probe = Item(source_kind="plenary_transcript", source_name="review",
                     title=title, body=body)
        self.scorer.score_item(probe)
        return (sorted(probe.themes or [], key=lambda t: -self.weights.get(t, 0)),
                probe.score or 0.0)

    def __call__(self, title: str, body: str) -> tuple[str, float]:
        """The title decides when it names a theme — a "Building Safety
        Programme Update" is about building safety even though half the
        debate mentions renters — and the words spoken decide otherwise."""
        _, score = self._themes(title, body)
        for candidates in (self._themes(title, title)[0], self._themes(title, body)[0]):
            for theme in candidates:
                for key, _, members in THEMES:
                    if theme in members:
                        return key, score
        return OTHER[0], score


def _one_sentence(text: str) -> str:
    words = (text or "").split()
    if len(words) <= QUOTE_WORDS:
        return " ".join(words)
    return " ".join(words[:QUOTE_WORDS]) + " …"


def _label(c) -> str:
    return f"{c.speaker} MS" if getattr(c, "member", False) else c.speaker


def debate_entries(debates: list, tax: Taxonomy, themer: Themer) -> list[Entry]:
    """Entries from the debate summaries' selection: one per whole debate,
    one per question (a tabled question and its supplementaries together)."""
    from .debates import key_sentences

    out: list[Entry] = []
    for d in debates:
        when = d.meeting_date
        day = when.strftime("%a %-d %b") if when else ""
        forum = d.record.forum
        if d.whole:
            speakers = d.exchanges[0].contributions
            lead = speakers[0]
            text = "\n".join(c.text for c in speakers)
            theme, score = themer(d.title, text)
            others = [c for c in speakers if c.speaker != lead.speaker]
            n_others = len({c.speaker for c in others})
            meta = " · ".join(filter(None, [
                forum, day, _label(lead) + (f" and {n_others} others" if n_others > 1 else
                                            f" and {_label(others[0])}" if n_others == 1 else "")]))
            out.append(Entry(title=d.title, url=d.url, meta=meta,
                             quote=_one_sentence(key_sentences(lead, tax)),
                             quote_by=lead.role or _label(lead),
                             score=score, theme=theme, when=when))
            continue

        # Exchange mode. A question's supplementaries go with it, so
        # "Protecting Renters" with three follow-ups is one line, not four.
        # Without headings (the Business Statement, a statement's questions)
        # exchanges on the same theme are one line and different themes are
        # separate lines: John Clark on HMOs and Carmelo Colasanto on
        # housing delivery belong under different headings.
        groups: dict[str, list] = {}
        for ex in d.exchanges:
            if ex.heading:
                key = "h:" + ex.heading
            else:
                ex_theme, _ = themer("",
                                     "\n".join(c.text for c in ex.contributions))
                key = "t:" + ex_theme
            groups.setdefault(key, []).append(ex)
        for key, exs in groups.items():
            heading = exs[0].heading
            contribs = [c for ex in exs for c in ex.contributions]
            askers = []
            for ex in exs:
                if ex.contributions and _label(ex.contributions[0]) not in askers:
                    askers.append(_label(ex.contributions[0]))
            first = exs[0].contributions[0]
            text = "\n".join(c.text for c in contribs)
            theme, score = themer(heading or "", text)
            title = f"{heading} — {d.title}" if heading else d.title
            anchor = first.anchor
            url = f"{d.record.url}#{anchor}" if anchor else d.url
            meta = " · ".join(filter(None, [forum, day, ", ".join(askers[:3]) +
                                            (f" and {len(askers) - 3} more" if len(askers) > 3 else "")]))
            out.append(Entry(title=title, url=url, meta=meta,
                             quote=_one_sentence(key_sentences(first, tax)),
                             quote_by=_label(first), score=score, theme=theme,
                             when=when))
    return out


def notice_entries(releases: list, themer: Themer) -> list[Entry]:
    out = []
    for r in releases:
        it = r.item
        theme, score = themer(it.title, it.body or "")
        when = r.published_utc.date() if r.published_utc else it.item_date
        meta = " · ".join(filter(None, [
            "Welsh Government", r.label.lower(),
            when.strftime("%a %-d %b") if when else "", it.speaker]))
        out.append(Entry(title=it.title, url=it.url, meta=meta,
                         quote=_one_sentence(r.points[0]) if r.points else "",
                         score=score, theme=theme, when=when))
    return out


def political_changes(state_path: str, start: date, end: date) -> list[dict]:
    """This week's news alerts, from their memory (restored from the Actions
    cache by the Friday workflow). Missing memory means none are listed."""
    from .collectors.news import Headline
    from .news import classify
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for a in state.get("alerts", []):
        try:
            at = datetime.fromisoformat(a["at"]).date()
        except (KeyError, ValueError):
            continue
        # Re-judged by today's rules, so an alert later found to be wrong —
        # "Reform UK politician accuses Senedd member who defected of fraud",
        # sent on 25 September 2026 before the rule was fixed — is not
        # repeated in the weekly.
        if not classify(Headline(outlet="", title=a.get("title", ""),
                                 url=a.get("url", ""), at=None)):
            continue
        if start <= at <= end:
            out.append({"title": a.get("title", ""), "kind": a.get("kind", ""),
                        "url": a.get("url", ""), "at": at})
    return out


def build(today: date, tax: Taxonomy, fetcher, news_state: str = "",
          lookback_fix: int = 7) -> Review:
    """Read this week's Records and notices and build the review."""
    from .collectors.govwales import GovWalesNewsroomCollector, GovWalesRSSCollector
    from .collectors.record_html import RecordPageCollector
    from .collectors.record_transcripts import RecordTranscriptCollector
    from .collectors.seneddtv import SeneddTVScheduleCollector
    from .debates import Relevance, candidates, select
    from .morning import Marker, merge_announcements
    from .press import select as select_press

    start, end = week_bounds(today)
    review = Review(week_start=start, week_end=end)
    themer = Themer(tax)

    tv = SeneddTVScheduleCollector(fetcher)
    tv.schedule()
    recent = [tv.fill(m) for m in SeneddTVScheduleCollector.parse_recent_meetings(tv.home_html)
              if m.when and start <= m.when <= today]
    listing = RecordTranscriptCollector(fetcher, taxonomy=tax).list_meetings()
    meetings = candidates(recent, listing, today + timedelta(days=1), done=set(),
                          baseline=start - timedelta(days=1), lookback=lookback_fix)
    review.sat = bool(meetings)

    pages = RecordPageCollector(fetcher)
    rel = Relevance(tax)
    debates = []
    for cand in meetings:
        record = pages.record(cand.meeting_id, cand.forum)
        if record is None or not record.published:
            review.pending.append(cand.label)
            continue
        debates.extend(select(record, rel, cand.when))
    review.entries.extend(debate_entries(debates, tax, themer))

    since = datetime.combine(start, datetime.min.time())
    room, feed = GovWalesNewsroomCollector(fetcher), GovWalesRSSCollector(fetcher)
    notices = merge_announcements(room.recent(since, max_articles=40),
                                  feed.recent(since, max_articles=40))
    releases = select_press(notices, Marker(tax), {}, test=False)
    review.entries.extend(notice_entries(releases, themer))
    review.entries = [e for e in review.entries if e.score >= MIN_SCORE]

    if news_state:
        review.changes = political_changes(news_state, start, end)
    return review


# ---------------------------------------------------------------------------
# The email block — slotted into the top of the Friday email
# ---------------------------------------------------------------------------

def _e(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _entry_row(e: Entry, compact: bool = False) -> str:
    cell = (f'padding:12px 18px;border-bottom:1px solid {LINE};'
            f'font-family:{FONT};vertical-align:top;')
    quote = ""
    if e.quote and not compact:
        by = f'<span style="font-style:normal;color:{MUTED}"> — {_e(e.quote_by)}</span>' \
            if e.quote_by else ""
        quote = (f'<div style="font-size:13px;line-height:1.5;color:{OFF_BLACK};'
                 f'font-style:italic;padding-top:4px">&ldquo;{_e(e.quote)}&rdquo;{by}</div>')
    return (f'<tr><td style="{cell}">'
            f'<div style="font-size:14.5px;line-height:1.45;font-weight:600">'
            f'<a href="{_e(e.url)}" style="color:{OFF_BLACK};text-decoration:none">'
            f'{_e(e.title)}</a></div>'
            f'<div style="font-size:12px;color:{MUTED};padding-top:3px">{_e(e.meta)}</div>'
            f'{quote}</td></tr>')


def _card(rows: list[str]) -> str:
    if not rows:
        return ""
    body = "".join(rows[:-1]) + rows[-1].replace(f"border-bottom:1px solid {LINE};", "")
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="border-collapse:collapse;width:100%;background:#ffffff;'
            f'border:1px solid {EDGE}">{body}</table>')


def _heading(title: str, lede: str = "", big: bool = False) -> str:
    size = "13px" if big else "11px"
    return (f'<div style="font-size:{size};font-weight:700;letter-spacing:1.2px;'
            f'text-transform:uppercase;color:{ORANGE};padding-top:{22 if big else 20}px">'
            f'{_e(title)}</div>'
            + (f'<div style="font-size:12.5px;color:{MUTED};line-height:1.5;'
               f'padding:5px 0 10px">{_e(lede)}</div>' if lede else
               '<div style="height:10px;line-height:10px;font-size:0">&nbsp;</div>'))


def render_review(review: Review) -> tuple[str, int]:
    """``(html rows for the Friday email, count)``. Nothing if no sitting."""
    if not review.sat and not review.entries:
        return "", 0
    week = (f"{review.week_start:%-d} to {review.week_end:%-d %B}"
            if review.week_start.month == review.week_end.month else
            f"{review.week_start:%-d %B} to {review.week_end:%-d %B}")
    parts = [_heading("This week in the Senedd",
                      f"{week}. What was said and published on NRLA issues, "
                      f"by theme. Each line links to the Record or the notice; "
                      f"quotations are the speaker's own words.", big=True)]

    if not review.entries and not review.changes:
        parts.append(_card([f'<tr><td style="padding:14px 18px;font-family:{FONT};'
                            f'font-size:13px;color:{MUTED}">The Senedd sat, but nothing '
                            f'said or published this week matched the NRLA\'s relevance '
                            f'rules.</td></tr>']))
    else:
        heads = review.headlines()
        if heads:
            items = "".join(
                f'<tr><td width="16" style="width:16px;vertical-align:top;padding:5px 0 0;'
                f'font-family:{FONT};font-size:14px;color:{ORANGE}">&#8226;</td>'
                f'<td style="padding:5px 0 0;font-family:{FONT};font-size:14px;'
                f'line-height:1.5"><a href="{_e(e.url)}" style="color:{OFF_BLACK};'
                f'font-weight:600;text-decoration:none">{_e(e.title)}</a>'
                f'<span style="color:{MUTED};font-size:12px"> — {_e(e.meta)}</span></td></tr>'
                for e in heads)
            parts.append(_heading("Headlines"))
            parts.append(f'<table role="presentation" width="100%" cellpadding="0" '
                         f'cellspacing="0" border="0" style="border-collapse:collapse;'
                         f'background:#FFF8F2;border:1px solid #F6D3B8"><tr><td '
                         f'style="padding:10px 16px 12px"><table role="presentation" '
                         f'cellpadding="0" cellspacing="0" border="0" '
                         f'style="border-collapse:collapse">{items}</table></td></tr></table>')
        for label, rows in review.by_theme():
            parts.append(_heading(label))
            parts.append(_card([_entry_row(e) for e in rows]))
        if review.changes:
            rows = [f'<tr><td style="padding:10px 18px;border-bottom:1px solid {LINE};'
                    f'font-family:{FONT};font-size:14px;line-height:1.45">'
                    + (f'<a href="{_e(c["url"])}" style="color:{OFF_BLACK};font-weight:600;'
                       f'text-decoration:none">{_e(c["title"])}</a>' if c.get("url")
                       else f'<span style="font-weight:600;color:{OFF_BLACK}">{_e(c["title"])}</span>')
                    + f'<div style="font-size:12px;color:{MUTED};padding-top:2px">'
                    f'{_e(c["at"].strftime("%a %-d %b"))}</div></td></tr>'
                    for c in review.changes]
            parts.append(_heading("Political changes", "As reported in this week's news alerts."))
            parts.append(_card(rows))

    if review.pending:
        parts.append(f'<div style="font-size:12px;color:{MUTED};line-height:1.5;'
                     f'padding-top:10px">Not yet in the Record, so not covered above: '
                     f'{_e("; ".join(review.pending))}. The debate summaries will cover '
                     f'them when the Senedd publishes.</div>')

    block = (f'<tr><td style="padding:6px 28px 0;font-family:{FONT}">'
             + "".join(parts) + '</td></tr>')
    return block, review.count

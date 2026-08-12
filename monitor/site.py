"""The hosted database page — a real URL, in the NRLA house style.

WHY THIS EXISTS
---------------
The operator's verdict on four previous attempts was: *"that is not a database
at all. It's not even hosted on an actual page?"* — followed by two examples of
what was actually wanted:

    https://jhc220199.github.io/PA-Monitoring-Tools/
    https://jhc220199.github.io/Research-Live-Database/

Both are GitHub Pages sites in the NRLA house style: dark-blue header,
"last updated" stamp, stat cards, a search box, a download button.

WHY IT LOOKS THE WAY IT DOES
----------------------------
The first hosted version organised everything into week-by-week tabs and
showed every collected item. The operator's verdict on that was equally
direct: *"far too focussed on the week tracking"*, *"doesn't give clear lists
on for example relevant consultations, debates, etc."*, and *"it seems to have
every single consultation … you're just overloaded with information"*.

So this version makes two deliberate choices:

1. LISTS BY TYPE, NOT BY WEEK. A policy officer's questions are "which
   consultations are open?", "where has that bill got to?", "what was said
   about us in the Chamber?" — none of which is a question about a week.
   The page is therefore six lists: open consultations, bills & legislation,
   debates, committee work, questions & statements, and what's coming up.

2. STRICT RELEVANCE. Only items that match a substantive NRLA theme appear
   (plus the housing committee's own business). Generic Senedd machinery —
   budget debates, other committees' priorities consultations, unrelated
   LCMs — is collected and archived but never shown. The rule lives in the
   `site:` section of taxonomy.yaml and in `Taxonomy.qualifies_for_site`,
   where policy staff can tune it without touching this file.

The archive still keeps everything, so nothing is lost by being strict here.

WHAT THIS PRODUCES
------------------
`docs/index.html` — one self-contained file, no build step, no JS
dependencies, no browser storage. GitHub Pages serves `docs/` for free:

    https://jhc220199.github.io/Senedd-monitoring-tool/

The workflow rewrites and commits it on every run, so the page is current
without anyone touching it.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import date, datetime

from .models import Item
from .relevance import Taxonomy

# NRLA brand palette.
INK = "#0F2636"
BLUE = "#113B54"
ORANGE = "#E96C19"
PAPER = "#FCFCFC"
WASH = "#F6F7F8"
LINE = "#DFE4E8"
MUTED = "#5A7286"
RED = "#A32115"

KIND_LABELS = {
    "plenary_transcript": "Plenary",
    "committee_transcript": "Committee",
    "oral_question": "Oral question",
    "written_question": "Written question",
    "consultation": "Consultation",
    "legislation": "Legislation",
    "written_statement": "Written statement",
    "press_release": "Press release",
    "research": "Research",
    "calendar": "Scheduled",
    "other": "Other",
}

# Which section each source kind belongs to. Consultations, legislation and
# calendar items are handled specially before this map is consulted.
#
# written_question is DELIBERATELY in no section: the policy team already has
# a dedicated tool monitoring written questions, so showing them here would
# duplicate that tool's job (operator request, 12 Aug 2026). The kind stays
# collectable and searchable in the archive; it just never reaches this page —
# even if someone re-enables the written_questions source in taxonomy.yaml.
QUESTION_KINDS = {"oral_question"}
STATEMENT_KINDS = {"written_statement", "press_release", "research", "other"}


def _e(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def _display_date(d: date | None) -> str:
    return d.strftime("%d %b %Y") if d else ""


def _strip_agenda_number(title: str) -> str:
    """'2. Questions to the Cabinet Minister…' -> 'Questions to the…'."""
    return re.sub(r"^\s*\d+[.)]\s*", "", title or "").strip()


def _excerpt(item: Item, limit: int = 300) -> str:
    text = (item.excerpt or item.body or "").strip().replace("\n", " ")
    text = re.sub(r"<[^>]+>", "", text)          # written answers carry <p> tags
    text = re.sub(r"\s+", " ", text)

    # Collector output often prepends the title to the body — sometimes more
    # than once — so excerpts used to read "Forward work programme – LGHP
    # Committee Forward work programme – LGHP Committee Forward…". Strip
    # leading repeats always; strip interior repeats only for long titles,
    # where a false positive (the phrase used mid-sentence) is implausible.
    title = re.sub(r"\s+", " ", (item.title or "").strip())
    if title:
        pattern = re.compile(re.escape(title), re.IGNORECASE)
        while text.lower().startswith(title.lower()):
            text = text[len(title):].lstrip(" -–—:·.")
        if len(title) >= 20:
            text = re.sub(r"\s+", " ", pattern.sub(" ", text)).strip()

    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _norm_act(title: str) -> str:
    """Grouping key for legislation: the same Act arrives from both the Senedd
    bill-history page and legislation.gov.uk with cosmetically different
    titles ('The X Regulations 2026' vs 'X Regulations 2026')."""
    t = (title or "").strip().lower()
    t = re.sub(r"^the\s+", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _deadline_class(days_left: int | None) -> str:
    if days_left is None:
        return "later"
    if days_left < 0:
        return "later"
    if days_left <= 7:
        return "now"
    if days_left <= 21:
        return "soon"
    return "later"


def _deadline_text(days_left: int | None) -> str:
    if days_left is None:
        return ""
    if days_left < 0:
        return "Closed"
    if days_left == 0:
        return "Closes today"
    if days_left == 1:
        return "Closes tomorrow"
    return f"{days_left} days left"


def _band_badge(band: str) -> str:
    if band == "Critical":
        return f'<span class="badge crit">Critical</span>'
    if band == "High":
        return f'<span class="badge high">High</span>'
    return ""


def _haystack(*parts) -> str:
    return _e(" ".join(str(p or "") for p in parts).lower())


def _labels(item: Item, tax: Taxonomy) -> str:
    """Theme labels for the search haystack. The pills are VISIBLE on the row,
    so a search for what a pill says must match that row — 'rent smart' has to
    find every row wearing the 'Rent Smart Wales & licensing' pill even when
    the truncated excerpt happens not to contain the phrase."""
    return " ".join(tax.theme_label(t) for t in (item.themes or []))


def _theme_pills(item: Item, tax: Taxonomy) -> str:
    generic = set(tax.site_config.get("non_qualifying_themes", []) or [])
    labels = [tax.theme_label(t) for t in (item.themes or [])
              if t not in generic][:4]
    if not labels:
        return ""
    return ('<div class="pills">'
            + "".join(f'<span class="pill">{_e(l)}</span>' for l in labels)
            + "</div>")


# ---------------------------------------------------------------------------
# Section renderers. Each returns (count, html) and appends to the CSV rows.
# ---------------------------------------------------------------------------

def _link(title: str, url: str) -> str:
    if url:
        return (f'<a class="ttl" href="{_e(url)}" target="_blank" '
                f'rel="noopener">{_e(title)}</a>')
    return f'<span class="ttl">{_e(title)}</span>'


def _consultations(items: list[Item], tax: Taxonomy, today: date,
                   csv_rows: list[list[str]]) -> tuple[int, int, str]:
    """Open consultations & inquiries, soonest deadline first."""
    cons = [i for i in items if i.source_kind == "consultation"]

    def is_open(i: Item) -> bool:
        return i.deadline is None or i.deadline >= today

    open_, closed = [], []
    for i in cons:
        (open_ if is_open(i) else closed).append(i)

    # Deadlines first, soonest first; undated ones after, newest first. An
    # undated consultation is usually one whose closing date the collector
    # could not parse — hiding it would be worse than showing it undated.
    open_.sort(key=lambda i: (i.deadline is None,
                              i.deadline or date.max,
                              -(i.item_date or date.min).toordinal()))
    closed.sort(key=lambda i: i.deadline or date.min, reverse=True)
    closing_soon = sum(1 for i in open_
                       if i.deadline and (i.deadline - today).days <= 21)

    def row(i: Item, closed_row: bool = False) -> str:
        days = (i.deadline - today).days if i.deadline else None
        if closed_row:
            when = (f'<span class="dl later">Closed</span>'
                    f'<div class="meta">{_e(_display_date(i.deadline))}</div>')
        elif i.deadline:
            when = (f'<span class="dl {_deadline_class(days)}">'
                    f'{_e(_deadline_text(days))}</span>'
                    f'<div class="meta">{_e(_display_date(i.deadline))}</div>')
        else:
            when = '<span class="dl later">No closing date published</span>'
        csv_rows.append(["Consultations", _display_date(i.item_date),
                         "Consultation", i.title or "", i.forum or "",
                         "", "", _display_date(i.deadline),
                         "; ".join(i.themes or []), i.url or ""])
        return (f'<li data-s="{_haystack(i.title, i.forum, _excerpt(i), _labels(i, tax))}">'
                f'<div class="when">{when}</div>'
                f'<div class="what">{_link(i.title or "(untitled)", i.url)}'
                f'{_band_badge(i.band or "")}'
                f'<div class="ex">{_e(_excerpt(i))}</div>'
                f'<div class="meta">{_e(i.forum or i.source_name or "")}</div>'
                f'{_theme_pills(i, tax)}</div></li>')

    body = ""
    if open_:
        body += '<ul class="rows">' + "".join(row(i) for i in open_) + "</ul>"
    else:
        body += ('<div class="empty">No NRLA-relevant consultations are '
                 'currently open.</div>')
    if closed:
        body += ('<details class="more"><summary>Recently closed '
                 f'({len(closed)})</summary><ul class="rows">'
                 + "".join(row(i, closed_row=True) for i in closed)
                 + "</ul></details>")
    return len(open_), closing_soon, body


def _legislation(items: list[Item], tax: Taxonomy,
                 csv_rows: list[list[str]]) -> tuple[int, str]:
    """One row per Act/instrument, however many sources reported it."""
    groups: dict[str, list[Item]] = defaultdict(list)
    for i in items:
        if i.source_kind == "legislation":
            groups[_norm_act(i.title)].append(i)

    def latest(g: list[Item]) -> date:
        return max((i.item_date for i in g if i.item_date), default=date.min)

    ordered = sorted(groups.values(), key=latest, reverse=True)

    rows = []
    for g in ordered:
        g.sort(key=lambda i: i.item_date or date.min, reverse=True)
        lead = g[0]
        title = max((i.title or "" for i in g), key=len)
        links = []
        seen = set()
        for i in g:
            if not i.url or i.url in seen:
                continue
            seen.add(i.url)
            label = ("Senedd bill history" if "senedd" in i.url
                     else "legislation.gov.uk" if "legislation.gov.uk" in i.url
                     else "Source")
            links.append(f'<a href="{_e(i.url)}" target="_blank" '
                         f'rel="noopener">{_e(label)}</a>')
        csv_rows.append(["Bills & legislation", _display_date(lead.item_date),
                         "Legislation", title, lead.forum or "", "", "", "",
                         "; ".join(lead.themes or []), lead.url or ""])
        rows.append(
            f'<li data-s="{_haystack(title, _excerpt(lead), _labels(lead, tax))}">'
            f'<div class="when"><span class="d">'
            f'{_e(_display_date(lead.item_date))}</span>'
            f'<div class="meta">last activity</div></div>'
            f'<div class="what">{_link(title, lead.url)}'
            f'{_band_badge(lead.band or "")}'
            f'<div class="ex">{_e(_excerpt(lead, 220))}</div>'
            f'<div class="meta">{" · ".join(links)}</div>'
            f'{_theme_pills(lead, tax)}</div></li>')
    if not rows:
        return 0, ('<div class="empty">No relevant bills or instruments '
                   'on record.</div>')
    return len(rows), '<ul class="rows">' + "".join(rows) + "</ul>"


def _grouped_transcripts(items: list[Item], tax: Taxonomy, kind: str,
                         section: str,
                         csv_rows: list[list[str]]) -> tuple[int, str]:
    """Plenary or committee transcripts, grouped into their debates.

    One collected item is one CONTRIBUTION; a debate that touched the PRS
    thirty-eight times must be one entry saying so, not thirty-eight rows.
    """
    groups: dict[tuple, list[Item]] = defaultdict(list)
    for i in items:
        if i.source_kind == kind:
            groups[(i.item_date, i.title or "")].append(i)

    ordered = sorted(groups.items(),
                     key=lambda kv: (kv[0][0] or date.min,
                                     max(i.score or 0 for i in kv[1])),
                     reverse=True)

    rows = []
    for (d, raw_title), g in ordered:
        g.sort(key=lambda i: i.score or 0, reverse=True)
        title = _strip_agenda_number(raw_title) or "(untitled)"
        lead = g[0]
        speakers = []
        for i in g:
            if i.speaker and i.speaker not in speakers:
                speakers.append(i.speaker)
        who = ", ".join(speakers[:4]) + (
            f" and {len(speakers) - 4} others" if len(speakers) > 4 else "")
        n = len(g)
        watch = (f' · <a href="{_e(lead.video_url)}" target="_blank" '
                 f'rel="noopener">Watch</a>') if lead.video_url else ""
        csv_rows.append([section, _display_date(d),
                         KIND_LABELS.get(kind, kind), title,
                         lead.forum or "", who, "", "",
                         "; ".join(lead.themes or []), lead.url or ""])

        detail = ""
        if n > 1:
            inner = "".join(
                f'<div class="contrib"><b>{_e(i.speaker or "—")}</b>'
                + (f' <span class="meta">({_e(i.party)})</span>' if i.party else "")
                + f'<div class="ex">{_e(_excerpt(i, 260))}</div></div>'
                for i in g)
            detail = (f'<details class="inline"><summary>'
                      f'{n} relevant contributions</summary>{inner}</details>')
        else:
            detail = f'<div class="ex">{_e(_excerpt(lead))}</div>'

        rows.append(
            f'<li data-s="{_haystack(title, who, lead.forum, " ".join(_labels(i, tax) for i in g), *[_excerpt(i, 400) for i in g[:12]])}">'
            f'<div class="when"><span class="d">{_e(_display_date(d))}</span>'
            f'<div class="meta">{_e(lead.forum or "")}</div></div>'
            f'<div class="what"><span class="ttl">{_e(title)}</span>'
            f'{_band_badge(max((i.band or "" for i in g), key=_band_rank))}'
            f'{detail}'
            f'<div class="meta">{_e(who)}'
            + (f' · <a href="{_e(lead.url)}" target="_blank" rel="noopener">'
               f'Record</a>' if lead.url else "") + f'{watch}</div>'
            f'{_theme_pills(lead, tax)}</div></li>')
    if not rows:
        return 0, '<div class="empty">Nothing relevant on record.</div>'
    return len(rows), '<ul class="rows">' + "".join(rows) + "</ul>"


def _band_rank(band: str) -> int:
    return {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}.get(band, 0)


def _flat_items(items: list[Item], tax: Taxonomy, kinds: set[str],
                section: str,
                csv_rows: list[list[str]]) -> tuple[int, str]:
    """Questions, statements, research: one item per row, newest first."""
    picked = [i for i in items if i.source_kind in kinds]
    picked.sort(key=lambda i: (i.item_date or date.min, i.score or 0),
                reverse=True)
    rows = []
    for i in picked:
        who = " · ".join(p for p in (i.speaker, i.party) if p)
        kind = KIND_LABELS.get(i.source_kind, i.source_kind)
        csv_rows.append([section, _display_date(i.item_date), kind,
                         i.title or "", i.forum or "", i.speaker or "",
                         i.party or "", "",
                         "; ".join(i.themes or []), i.url or ""])
        rows.append(
            f'<li data-s="{_haystack(i.title, who, i.forum, _excerpt(i), _labels(i, tax))}">'
            f'<div class="when"><span class="d">'
            f'{_e(_display_date(i.item_date))}</span>'
            f'<div class="meta">{_e(kind)}</div></div>'
            f'<div class="what">{_link(i.title or "(untitled)", i.url)}'
            f'{_band_badge(i.band or "")}'
            f'<div class="ex">{_e(_excerpt(i))}</div>'
            f'<div class="meta">{_e(i.forum or i.source_name or "")}'
            + (f" · {_e(who)}" if who else "") + "</div>"
            f'{_theme_pills(i, tax)}</div></li>')
    if not rows:
        return 0, '<div class="empty">Nothing relevant on record.</div>'
    return len(rows), '<ul class="rows">' + "".join(rows) + "</ul>"


def _upcoming(items: list[Item], tax: Taxonomy, today: date,
              csv_rows: list[list[str]]) -> tuple[int, str]:
    """Scheduled sittings and meetings, soonest first."""
    future = [i for i in items
              if i.source_kind == "calendar"
              and i.item_date and i.item_date >= today]
    future.sort(key=lambda i: i.item_date)
    rows = []
    for i in future:
        days = (i.item_date - today).days
        rel = ("Today" if days == 0 else "Tomorrow" if days == 1
               else f"In {days} days")
        csv_rows.append(["Coming up", _display_date(i.item_date), "Scheduled",
                         i.title or "", i.forum or "", "", "", "",
                         "; ".join(i.themes or []), i.url or ""])
        rows.append(
            f'<li data-s="{_haystack(i.title, i.forum)}">'
            f'<div class="when"><span class="d">'
            f'{_e(_display_date(i.item_date))}</span>'
            f'<div class="meta">{_e(rel)}</div></div>'
            f'<div class="what">{_link(i.title or "(untitled)", i.url)}'
            f'<div class="meta">{_e(i.forum or i.source_name or "")}</div>'
            f'</div></li>')
    if not rows:
        return 0, ('<div class="empty">No relevant sittings scheduled — '
                   'the Senedd is in recess until 14 September.</div>')
    return len(rows), '<ul class="rows">' + "".join(rows) + "</ul>"


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def render_site(items: list[Item], tax: Taxonomy,
                generated: datetime | None = None,
                repo: str = "") -> str:
    """The whole database as one self-contained HTML page."""
    generated = generated or datetime.now()
    today = date.today()

    shown = [i for i in items if tax.qualifies_for_site(i)]
    csv_rows: list[list[str]] = []

    n_open, n_closing, cons_html = _consultations(shown, tax, today, csv_rows)
    n_leg, leg_html = _legislation(shown, tax, csv_rows)
    n_deb, deb_html = _grouped_transcripts(
        shown, tax, "plenary_transcript", "Debates", csv_rows)
    n_com, com_html = _grouped_transcripts(
        shown, tax, "committee_transcript", "Committee work", csv_rows)
    n_q, q_html = _flat_items(shown, tax, QUESTION_KINDS, "Questions", csv_rows)
    n_st, st_html = _flat_items(shown, tax, STATEMENT_KINDS,
                                "Statements & research", csv_rows)
    n_up, up_html = _upcoming(shown, tax, today, csv_rows)

    sections = [
        ("consultations", "Open consultations", n_open, cons_html,
         "Consultations and inquiries the NRLA can respond to, soonest "
         "deadline first."),
        ("legislation", "Bills & legislation", n_leg, leg_html,
         "Acts, Bills and statutory instruments affecting the private rented "
         "sector, most recent activity first."),
        ("debates", "Debates & Plenary", n_deb, deb_html,
         "Chamber business where NRLA issues were raised — grouped by debate, "
         "with every relevant contribution underneath."),
        ("committees", "Committee work", n_com, com_html,
         "Committee sessions touching the private rented sector."),
        ("questions", "Questions", n_q, q_html,
         "Oral questions to Ministers on NRLA issues. Written questions are "
         "deliberately not shown — the team's dedicated tool tracks those."),
        ("statements", "Statements & research", n_st, st_html,
         "Government statements, announcements and Senedd research on "
         "NRLA issues."),
        ("upcoming", "Coming up", n_up, up_html,
         "Relevant sittings and meetings in the diary."),
    ]

    nav = "".join(
        f'<a class="chip" href="#{k}">{_e(label)} <span class="n">{n}</span></a>'
        for k, label, n, _, _ in sections)

    body = "".join(
        f'<section id="{k}"><h2>{_e(label)} <span class="count">{n}</span></h2>'
        f'<p class="lede">{_e(lede)}</p>{content}</section>'
        for k, label, n, content, lede in sections)

    next_meeting = ""
    future = [i for i in shown if i.source_kind == "calendar"
              and i.item_date and i.item_date >= today]
    if future:
        next_meeting = _display_date(min(i.item_date for i in future))

    cards = "".join(
        f'<div class="card"><div class="k">{_e(k)}</div>'
        f'<div class="v">{_e(v)}</div></div>'
        for k, v in [
            ("OPEN CONSULTATIONS", str(n_open)),
            ("CLOSING IN 3 WEEKS", str(n_closing)),
            ("BILLS & LEGISLATION", str(n_leg)),
            ("DEBATES & QUESTIONS", str(n_deb + n_q)),
            ("NEXT COMMITTEE MEETING", next_meeting or "—"),
        ])

    payload = json.dumps(csv_rows, ensure_ascii=False)
    stamp = generated.strftime("%-d %b %Y at %H:%M") \
        if hasattr(generated, "strftime") else ""
    pages_url = f"https://github.com/{repo}" if repo else ""

    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Senedd Policy Monitor — NRLA</title>
<style>
  *{{box-sizing:border-box}}
  html{{scroll-behavior:smooth}}
  body{{margin:0;background:{WASH};color:{INK};
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}}
  header{{background:{BLUE};color:#fff;padding:18px 28px;display:flex;
    justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px}}
  .brand{{display:flex;gap:14px;align-items:center}}
  .mark{{width:44px;height:44px;border-radius:9px;background:{ORANGE};color:#fff;
    font-weight:700;font-size:22px;display:flex;align-items:center;justify-content:center}}
  h1{{margin:0;font-size:21px;letter-spacing:-.2px}}
  .sub{{opacity:.82;font-size:13.5px;margin-top:2px}}
  .when-stamp{{text-align:right;font-size:13px;opacity:.9;line-height:1.45}}
  main{{max-width:1150px;margin:0 auto;padding:22px 28px 70px}}
  .cards{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px}}
  .card{{background:#fff;border:1px solid {LINE};border-radius:10px;
    padding:13px 18px;min-width:140px;flex:1}}
  .card .k{{font-size:10.5px;letter-spacing:.6px;color:{MUTED};font-weight:600}}
  .card .v{{font-size:26px;font-weight:700;color:{BLUE};margin-top:4px;white-space:nowrap}}
  .bar{{display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap}}
  #q{{flex:1;min-width:260px;padding:12px 15px;border:1px solid {LINE};
    border-radius:9px;font-size:15px;background:#fff}}
  #q:focus{{outline:2px solid {ORANGE};outline-offset:-1px}}
  .btn{{background:{ORANGE};color:#fff;border:0;border-radius:9px;padding:12px 20px;
    font-size:14.5px;font-weight:600;cursor:pointer}}
  .chips{{display:flex;gap:8px;flex-wrap:wrap;position:sticky;top:0;z-index:5;
    background:{WASH};padding:10px 0;margin-bottom:8px;border-bottom:1px solid {LINE}}}
  .chip{{background:#fff;border:1px solid {LINE};border-radius:20px;color:{BLUE};
    padding:7px 14px;font-size:13.5px;font-weight:600;text-decoration:none;white-space:nowrap}}
  .chip:hover{{border-color:{ORANGE}}}
  .chip .n{{background:{WASH};border-radius:10px;padding:1px 7px;font-size:12px;
    margin-left:2px;color:{MUTED}}}
  section{{margin-top:30px}}
  h2{{font-size:19px;color:{BLUE};margin:0 0 2px;padding-top:6px}}
  h2 .count{{font-size:13px;color:{MUTED};font-weight:600;background:#fff;
    border:1px solid {LINE};border-radius:12px;padding:2px 9px;vertical-align:2px}}
  .lede{{color:{MUTED};font-size:13.5px;margin:2px 0 12px}}
  .rows{{list-style:none;margin:0;padding:0;background:#fff;
    border:1px solid {LINE};border-radius:10px;overflow:hidden}}
  .rows li{{display:flex;gap:18px;padding:15px 18px;border-bottom:1px solid {LINE}}}
  .rows li:last-child{{border-bottom:0}}
  .when{{flex:0 0 128px}}
  .when .d{{font-weight:600;font-size:13.5px;color:{INK};white-space:nowrap}}
  .what{{flex:1;min-width:0}}
  .ttl{{font-weight:600;color:{BLUE};text-decoration:none;font-size:15.5px}}
  a.ttl:hover{{text-decoration:underline}}
  .ex{{color:#33475B;font-size:13.5px;margin-top:5px}}
  .meta{{color:{MUTED};font-size:12.5px;margin-top:5px}}
  .meta a{{color:{BLUE}}}
  .pills{{margin-top:6px}}
  .pill{{display:inline-block;background:{WASH};border:1px solid {LINE};
    border-radius:20px;padding:2px 9px;font-size:11.5px;color:{MUTED};
    margin:3px 4px 0 0}}
  .badge{{display:inline-block;border-radius:5px;padding:1px 8px;font-size:11px;
    font-weight:700;margin-left:8px;vertical-align:2px;color:#fff}}
  .badge.crit{{background:{RED}}}
  .badge.high{{background:{ORANGE}}}
  .dl{{white-space:nowrap;font-size:13.5px;font-weight:700}}
  .now{{color:{RED}}} .soon{{color:{ORANGE}}} .later{{color:{MUTED}}}
  .empty{{padding:30px;text-align:center;color:{MUTED};background:#fff;
    border:1px solid {LINE};border-radius:10px;font-size:14px}}
  details.more{{margin-top:10px}}
  details.more summary{{cursor:pointer;color:{MUTED};font-size:13.5px;
    font-weight:600;padding:4px 2px}}
  details.inline{{margin-top:6px}}
  details.inline summary{{cursor:pointer;color:{BLUE};font-size:13px;font-weight:600}}
  .contrib{{border-left:3px solid {LINE};margin:10px 0 10px 2px;padding-left:12px;
    font-size:13.5px}}
  footer{{max-width:1150px;margin:0 auto;padding:0 28px 50px;color:{MUTED};font-size:12.5px}}
  a{{color:{BLUE}}}
  .hidden{{display:none!important}}
  #noresults{{display:none}}
  @media(max-width:700px){{
    .rows li{{flex-direction:column;gap:6px}}
    .when{{flex:none;display:flex;gap:10px;align-items:baseline}}
  }}
</style>

<header>
  <div class="brand">
    <div class="mark">N</div>
    <div>
      <h1>Senedd Policy Monitor</h1>
      <div class="sub">National Residential Landlords Association — what matters to the PRS in Senedd Cymru &amp; Welsh Government</div>
    </div>
  </div>
  <div class="when-stamp">
    <b>Last updated {_e(stamp)}</b><br>
    Refreshes automatically every weekday morning
  </div>
</header>

<main>
  <div class="cards">{cards}</div>

  <div class="bar">
    <input id="q" type="search" placeholder="Search everything shown — titles, members, quotes, keywords…"
           autocomplete="off">
    <button class="btn" id="csv">&#8595; Download CSV</button>
  </div>

  <nav class="chips">{nav}</nav>
  <div id="noresults" class="empty">Nothing matches that search.</div>

  {body}
</main>

<footer>
  Only items relevant to the private rented sector appear on this page; the
  <em>relevance rules are set by the NRLA policy team</em> and everything
  collected remains searchable in the archive. Every quotation is the verbatim
  published record — nothing on this page is summarised by a language model.
  Senedd Cymru and Welsh Government material is reproduced under the
  <a href="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/">Open Government Licence v3.0</a>.
  {'<a href="' + _e(pages_url) + '">Source and archive</a>.' if pages_url else ''}
</footer>

<script>
const CSV_ROWS = {payload};

// Search: every word must appear somewhere in a row for it to stay visible.
// Sections whose rows are all hidden collapse away, so a search for
// "rent smart wales" reads as a result set, not as a page of empty boxes.
const q = document.getElementById('q');
q.addEventListener('input', () => {{
  const words = q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
  let any = false;
  document.querySelectorAll('.rows li').forEach(li => {{
    const hay = li.getAttribute('data-s') || '';
    const hit = words.every(w => hay.includes(w));
    li.classList.toggle('hidden', !hit);
    if (hit) any = true;
  }});
  document.querySelectorAll('section').forEach(sec => {{
    const vis = sec.querySelectorAll('.rows li:not(.hidden)').length;
    sec.classList.toggle('hidden', words.length > 0 && vis === 0);
    if (words.length) {{
      sec.querySelectorAll('details').forEach(d => d.open = true);
    }}
  }});
  document.getElementById('noresults').style.display =
    (words.length && !any) ? 'block' : 'none';
}});

document.getElementById('csv').addEventListener('click', () => {{
  const head = ['Section','Date','Type','Title','Forum','Member','Party',
                'Closes','Themes','URL'];
  const esc = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const lines = [head.map(esc).join(',')]
    .concat(CSV_ROWS.map(r => r.map(esc).join(',')));
  // A BOM so Excel opens Welsh names and typographic quotes correctly.
  const blob = new Blob(['\\ufeff' + lines.join('\\n')],
                        {{type: 'text/csv;charset=utf-8'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'senedd-monitor.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}});
</script>
</html>
"""

"""The hosted database page — a real URL, in the NRLA house style.

WHY THIS EXISTS
---------------
The operator's verdict on four previous attempts was: *"that is not a database
at all. It's not even hosted on an actual page?"* — followed by two examples of
what was actually wanted:

    https://jhc220199.github.io/PA-Monitoring-Tools/
    https://jhc220199.github.io/Research-Live-Database/

Both are GitHub Pages sites: a dark-blue NRLA header with a "last updated"
stamp, a row of stat cards, a search box, a download button, tabbed periods
across the top, and rows of records underneath. That is the house pattern, and
it is what a policy team recognises as a database.

Everything I had produced instead — a gitignored HTML file, a build artifact, a
CI log page, a markdown file in a repository — was a *file*. None of them was a
page you could send someone. `BRIEFING.md` is a genuinely useful record and it
stays, but it is a document, not a database, and answering "where is the
dashboard" with "it's a markdown file in the repo" was answering a different
question from the one asked.

WHAT THIS PRODUCES
------------------
`docs/index.html` — one self-contained file, no build step, no JS dependencies,
no browser storage. GitHub Pages serves `docs/` on a public repository for free,
which is how the other two tools are hosted, giving:

    https://jhc220199.github.io/Senedd-monitoring-tool/

The workflow rewrites and commits it on every run, so the page is current
without anyone touching it.

Layout deliberately mirrors PA-Monitoring-Tools so the directorate learns one
interface, not three: stat cards, search, week tabs, records. The Senedd
addition is the deadline column, because unlike a written question a
consultation has a clock on it, and missing one is the failure that matters.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import date, datetime

from .models import Item
from .relevance import Taxonomy
from .weekly import iso_week, week_bounds

# NRLA brand palette.
INK = "#0F2636"
BLUE = "#113B54"
ORANGE = "#E96C19"
PAPER = "#FCFCFC"
WASH = "#F6F7F8"
LINE = "#DFE4E8"
MUTED = "#5A7286"

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


def _e(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def tab_label(week: str) -> str:
    """'WC 20th July' — the same short form as the other NRLA monitors.

    `week_title()` produces 'Week 29 · 13 to 19 July 2026', which is right for a
    page heading and far too long for a tab strip.
    """
    monday, _ = week_bounds(week)
    return f"WC {_ordinal(monday.day)} {monday.strftime('%B')}"


def _record(item: Item, tax: Taxonomy) -> dict:
    """One row of the database, as plain data the page can filter."""
    days_left = None
    if item.deadline:
        days_left = (item.deadline - date.today()).days

    excerpt = (item.excerpt or item.body or "").strip().replace("\n", " ")
    if len(excerpt) > 320:
        excerpt = excerpt[:320].rsplit(" ", 1)[0] + "…"

    return {
        "week": iso_week(item.item_date) if item.item_date else "",
        "date": item.item_date.isoformat() if item.item_date else "",
        "date_display": (item.item_date.strftime("%d %b %Y")
                         if item.item_date else "Undated"),
        "kind": KIND_LABELS.get(item.source_kind, item.source_kind),
        "kind_key": item.source_kind,
        "forum": item.forum or item.source_name or "",
        "title": item.title or "(untitled)",
        "excerpt": excerpt,
        "speaker": item.speaker or "",
        "party": item.party or "",
        "url": item.url or "",
        "video": item.video_url or "",
        "themes": [tax.theme_label(t) for t in item.themes][:4],
        "band": item.band or "",
        "deadline": item.deadline.isoformat() if item.deadline else "",
        "deadline_display": (item.deadline.strftime("%d %b %Y")
                             if item.deadline else ""),
        "days_left": days_left,
    }


def _stat_cards(records: list[dict], weeks: list[str]) -> list[tuple[str, str]]:
    open_consultations = [r for r in records
                          if r["kind_key"] == "consultation"
                          and r["days_left"] is not None and r["days_left"] >= 0]
    closing = [r for r in open_consultations if r["days_left"] <= 21]
    speakers = {r["speaker"] for r in records if r["speaker"]}
    return [
        ("TOTAL ITEMS", f"{len(records):,}"),
        ("WEEKS COVERED", str(len(weeks))),
        ("OPEN CONSULTATIONS", str(len(open_consultations))),
        ("CLOSING IN 3 WEEKS", str(len(closing))),
        ("MEMBERS", str(len(speakers))),
    ]


def render_site(items: list[Item], tax: Taxonomy,
                generated: datetime | None = None,
                repo: str = "") -> str:
    """The whole database as one self-contained HTML page."""
    generated = generated or datetime.now()
    records = [_record(i, tax) for i in items if i.item_date or i.deadline]

    # Newest first, and within a day the highest-scoring first — the same order
    # the briefing uses, so the two never contradict each other.
    records.sort(key=lambda r: (r["date"], r["band"] == "Critical"), reverse=True)

    # Scheduled sittings are dated in the future, so they would otherwise create
    # "WC 28th September" tabs sitting to the left of everything that has
    # actually happened — and open on an empty-looking future week by default.
    # They belong in their own forward-looking group.
    today = date.today()
    upcoming = [r for r in records if r["date"] and r["date"] > today.isoformat()]
    upcoming.sort(key=lambda r: r["date"])

    by_week: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if r["week"] and r["date"] <= today.isoformat():
            by_week[r["week"]].append(r)
    weeks = sorted(by_week, reverse=True)

    cards = "".join(
        f'<div class="card"><div class="k">{_e(k)}</div>'
        f'<div class="v">{_e(v)}</div></div>'
        for k, v in _stat_cards(records, weeks))

    tabs = "".join(
        f'<button class="tab{" on" if n == 0 else ""}" data-week="{_e(w)}">'
        f'{_e(tab_label(w))} <span class="n">{len(by_week[w])}</span></button>'
        for n, w in enumerate(weeks))
    if upcoming:
        tabs += (f'<button class="tab" data-week="__upcoming">Coming up '
                 f'<span class="n">{len(upcoming)}</span></button>')
        by_week["__upcoming"] = upcoming

    # `weeks` drives "show all" and the default tab; the upcoming group is
    # reachable by its own tab but must not be folded into the week history.
    payload = json.dumps({"weeks": weeks, "byWeek": by_week}, ensure_ascii=False)

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
  body{{margin:0;background:{WASH};color:{INK};
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}}
  header{{background:{BLUE};color:#fff;padding:18px 28px;display:flex;
    justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px}}
  .brand{{display:flex;gap:14px;align-items:center}}
  .mark{{width:44px;height:44px;border-radius:9px;background:{ORANGE};color:#fff;
    font-weight:700;font-size:22px;display:flex;align-items:center;justify-content:center}}
  h1{{margin:0;font-size:21px;letter-spacing:-.2px}}
  .sub{{opacity:.82;font-size:13.5px;margin-top:2px}}
  .when{{text-align:right;font-size:13px;opacity:.9;line-height:1.45}}
  .when b{{opacity:1}}
  main{{max-width:1400px;margin:0 auto;padding:22px 28px 70px}}
  .cards{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:20px}}
  .card{{background:#fff;border:1px solid {LINE};border-radius:10px;
    padding:14px 20px;min-width:150px;flex:1}}
  .card .k{{font-size:11px;letter-spacing:.6px;color:{MUTED};font-weight:600}}
  .card .v{{font-size:30px;font-weight:700;color:{BLUE};margin-top:4px}}
  .bar{{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
  #q{{flex:1;min-width:260px;padding:12px 15px;border:1px solid {LINE};
    border-radius:9px;font-size:15px;background:#fff}}
  #q:focus{{outline:2px solid {ORANGE};outline-offset:-1px}}
  .btn{{background:{ORANGE};color:#fff;border:0;border-radius:9px;padding:12px 20px;
    font-size:14.5px;font-weight:600;cursor:pointer}}
  .btn.ghost{{background:#fff;color:{BLUE};border:1px solid {LINE}}}
  .tabs{{display:flex;gap:2px;overflow-x:auto;border-bottom:2px solid {LINE};
    margin-bottom:0;padding-bottom:0}}
  .tab{{background:none;border:0;border-bottom:3px solid transparent;padding:11px 15px;
    font-size:14px;color:{MUTED};cursor:pointer;white-space:nowrap;font-weight:500}}
  .tab.on{{color:{BLUE};border-bottom-color:{ORANGE};font-weight:700}}
  .tab .n{{background:{WASH};border-radius:10px;padding:1px 7px;font-size:12px;
    margin-left:4px;color:{MUTED}}}
  table{{width:100%;border-collapse:collapse;background:#fff;
    border:1px solid {LINE};border-top:0}}
  th{{text-align:left;font-size:11.5px;letter-spacing:.5px;color:{MUTED};
    padding:11px 14px;border-bottom:1px solid {LINE};background:{PAPER};
    position:sticky;top:0}}
  td{{padding:14px;border-bottom:1px solid {LINE};vertical-align:top}}
  tr:last-child td{{border-bottom:0}}
  .ttl{{font-weight:600;color:{BLUE};text-decoration:none}}
  .ttl:hover{{text-decoration:underline}}
  .ex{{color:#33475B;font-size:13.5px;margin-top:5px}}
  .meta{{color:{MUTED};font-size:12.5px;margin-top:6px}}
  .pill{{display:inline-block;background:{WASH};border:1px solid {LINE};
    border-radius:20px;padding:2px 9px;font-size:11.5px;color:{MUTED};
    margin:3px 4px 0 0}}
  .kind{{white-space:nowrap;font-size:12.5px;color:{MUTED}}}
  .dl{{white-space:nowrap;font-size:13px;font-weight:600}}
  .now{{color:#B3261E}} .soon{{color:{ORANGE}}} .later{{color:{MUTED}}}
  .empty{{padding:44px;text-align:center;color:{MUTED};background:#fff;
    border:1px solid {LINE};border-top:0}}
  footer{{max-width:1400px;margin:0 auto;padding:0 28px 50px;color:{MUTED};font-size:12.5px}}
  a{{color:{BLUE}}}
  @media(max-width:820px){{ .hide-s{{display:none}} td,th{{padding:11px 10px}} }}
</style>

<header>
  <div class="brand">
    <div class="mark">N</div>
    <div>
      <h1>Senedd Policy Monitor</h1>
      <div class="sub">National Residential Landlords Association — Senedd Cymru &amp; Welsh Government</div>
    </div>
  </div>
  <div class="when">
    <b>Last updated {_e(stamp)}</b><br>
    Refreshes automatically every weekday morning
  </div>
</header>

<main>
  <div class="cards">{cards}</div>

  <div class="bar">
    <input id="q" type="search" placeholder="Search titles, members, quotes or keywords…"
           autocomplete="off">
    <button class="btn" id="csv">↓ Download CSV</button>
    <button class="btn ghost" id="all">Show all weeks</button>
  </div>

  <div class="tabs" id="tabs">{tabs}</div>
  <div id="out"></div>
</main>

<footer>
  Every quotation is the verbatim published record — nothing on this page is
  summarised by a language model. Senedd Cymru and Welsh Government material is
  reproduced under the
  <a href="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/">Open Government Licence v3.0</a>.
  {'<a href="' + _e(pages_url) + '">Source and archive</a>.' if pages_url else ''}
</footer>

<script>
const DATA = {payload};
let week = DATA.weeks[0] || "", showAll = false;

function rows(list) {{
  if (!list.length) return '<div class="empty">Nothing matches that search.</div>';
  return '<table><thead><tr>' +
    '<th style="width:112px">Date</th>' +
    '<th style="width:118px" class="hide-s">Type</th>' +
    '<th>Item</th>' +
    '<th style="width:130px">Closes</th>' +
    '</tr></thead><tbody>' +
    list.map(r => {{
      let dl = '';
      if (r.days_left !== null && r.days_left !== undefined) {{
        const c = r.days_left < 0 ? 'later'
                : r.days_left <= 7 ? 'now'
                : r.days_left <= 21 ? 'soon' : 'later';
        const t = r.days_left < 0 ? 'Closed'
                : r.days_left === 0 ? 'Today'
                : r.days_left === 1 ? 'Tomorrow'
                : r.days_left + ' days';
        dl = '<span class="dl ' + c + '">' + t + '</span><div class="meta">'
             + r.deadline_display + '</div>';
      }}
      const link = r.url
        ? '<a class="ttl" href="' + r.url + '" target="_blank" rel="noopener">'
          + esc(r.title) + '</a>'
        : '<span class="ttl">' + esc(r.title) + '</span>';
      const who = [r.speaker, r.party].filter(Boolean).join(' · ');
      const themes = (r.themes || []).map(t =>
        '<span class="pill">' + esc(t) + '</span>').join('');
      const vid = r.video
        ? ' · <a href="' + r.video + '" target="_blank" rel="noopener">Watch</a>' : '';
      return '<tr>'
        + '<td>' + esc(r.date_display) + '</td>'
        + '<td class="kind hide-s">' + esc(r.kind) + '</td>'
        + '<td>' + link
          + (r.excerpt ? '<div class="ex">' + esc(r.excerpt) + '</div>' : '')
          + '<div class="meta">' + esc(r.forum) + (who ? ' · ' + esc(who) : '') + vid + '</div>'
          + (themes ? '<div>' + themes + '</div>' : '')
        + '</td>'
        + '<td>' + dl + '</td>'
        + '</tr>';
    }}).join('') + '</tbody></table>';
}}

function esc(s) {{
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]);
}}

function current() {{
  const base = showAll
    ? DATA.weeks.flatMap(w => DATA.byWeek[w])
    : (DATA.byWeek[week] || []);
  const q = document.getElementById('q').value.trim().toLowerCase();
  if (!q) return base;
  // Every word must appear somewhere in the record — the same rule as the
  // archive search, so the two behave identically.
  const words = q.split(/\\s+/);
  return base.filter(r => {{
    const hay = (r.title + ' ' + r.excerpt + ' ' + r.speaker + ' ' + r.forum
                 + ' ' + (r.themes || []).join(' ')).toLowerCase();
    return words.every(w => hay.includes(w));
  }});
}}

function draw() {{ document.getElementById('out').innerHTML = rows(current()); }}

document.getElementById('tabs').addEventListener('click', e => {{
  const b = e.target.closest('.tab'); if (!b) return;
  showAll = false;
  week = b.dataset.week;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
  b.classList.add('on');
  draw();
}});
document.getElementById('all').addEventListener('click', () => {{
  showAll = true;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
  draw();
}});
document.getElementById('q').addEventListener('input', draw);

document.getElementById('csv').addEventListener('click', () => {{
  const list = current();
  const head = ['Date','Type','Forum','Title','Member','Party','Closes','Themes','URL'];
  const esc2 = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const body = list.map(r => [r.date, r.kind, r.forum, r.title, r.speaker,
      r.party, r.deadline, (r.themes || []).join('; '), r.url].map(esc2).join(','));
  // A BOM so Excel opens Welsh names and typographic quotes correctly.
  const blob = new Blob(['\\ufeff' + [head.map(esc2).join(','), ...body].join('\\n')],
                        {{type: 'text/csv;charset=utf-8'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'senedd-monitor.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}});

draw();
</script>
</html>
"""

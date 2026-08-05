"""Week-by-week views of the archive.

WHY THIS EXISTS
---------------
The policy directorate's mental model is a week, because that is what the
supplier's service trained them on: "W29 NRLA Weekly Briefing 17 July 2026".
Asked how they wanted the archive organised, the answer was to separate it by
week. That is not a concession to an old format — it is genuinely how
parliamentary business is shaped. The Senedd sits in weeks, Plenary is Tuesday
and Wednesday, committees Wednesday and Thursday, and business statements set
out the *following* week.

So the archive is stored by item and read by week.

WHAT A WEEK MEANS HERE
----------------------
ISO week, Monday to Sunday, identified as `2026-W29`. ISO is used rather than
"week commencing" because it is unambiguous at year boundaries and sorts
correctly as a string — 2026-W29 comes before 2026-W30 without any date parsing.

Note the deliberate coincidence: the supplier's W29 briefing of 17 July 2026
covers ISO week 2026-W29 (13–19 July). The numbering lines up, so a historical
comparison between the two services is a direct file-to-file comparison rather
than a judgement call.

WHAT THIS GIVES THE TEAM
------------------------
* `week_index()`   — every week in the archive with counts, for a picker.
* `week_summary()` — one week's business, grouped, with what closed and what
                     opened in that week.
* A permanent per-week HTML snapshot on disk, so "what did we know in week 29"
  has an answer that does not change when the archive is later re-scored. This
  matters for the audit trail: if someone asks in 2028 why NRLA did not respond
  to a 2026 consultation, the answer is a file with a date on it.

Retention is deliberately indefinite. The archive contains only published
parliamentary material, and its entire value is that it is historical. Weekly
snapshots are cheap — a few hundred kilobytes each, so a full Senedd term of
them is smaller than one of the Word briefings they replace.
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Item
from .relevance import Taxonomy
from .store import Store


# ---------------------------------------------------------------------------
# Week identity
# ---------------------------------------------------------------------------

def iso_week(day: date) -> str:
    """Return the ISO week label for a date, e.g. '2026-W29'."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(label: str) -> tuple[date, date]:
    """Return (Monday, Sunday) for an ISO week label like '2026-W29'."""
    year_part, week_part = label.split("-W")
    monday = date.fromisocalendar(int(year_part), int(week_part), 1)
    return monday, monday + timedelta(days=6)


def week_title(label: str) -> str:
    """Human title, e.g. 'Week 29 · 13 to 19 July 2026'."""
    monday, sunday = week_bounds(label)
    week_no = int(label.split("-W")[1])
    if monday.month == sunday.month:
        span = f"{monday.day} to {sunday.day} {sunday.strftime('%B %Y')}"
    else:
        span = (f"{monday.day} {monday.strftime('%B')} to "
                f"{sunday.day} {sunday.strftime('%B %Y')}")
    return f"Week {week_no} · {span}"


def current_week() -> str:
    return iso_week(date.today())


def previous_week(label: str) -> str:
    monday, _ = week_bounds(label)
    return iso_week(monday - timedelta(days=7))


# ---------------------------------------------------------------------------
# Reading the archive by week
# ---------------------------------------------------------------------------

def week_index(store: Store, min_score: float = 25) -> list[dict]:
    """Every week present in the archive, newest first, with counts.

    Drives the week picker. Weeks with nothing in them are omitted rather than
    shown as empty, because a recess week with no business is not a gap in the
    data — it is an accurate record of a quiet week, and listing dozens of them
    makes the picker useless.
    """
    rows = store.conn.execute("""
        SELECT item_date, score, band, source_kind, deadline
          FROM items
         WHERE item_date IS NOT NULL AND score >= ?
    """, (min_score,)).fetchall()

    buckets: dict[str, dict] = defaultdict(
        lambda: {"items": 0, "critical": 0, "high": 0, "consultations": 0,
                 "closing": 0})

    for row in rows:
        try:
            when = date.fromisoformat(row["item_date"])
        except (TypeError, ValueError):
            continue
        bucket = buckets[iso_week(when)]
        bucket["items"] += 1
        if row["band"] == "Critical":
            bucket["critical"] += 1
        elif row["band"] == "High":
            bucket["high"] += 1
        if row["source_kind"] == "consultation":
            bucket["consultations"] += 1

    # Count deadlines by the week they FALL IN, not the week the item was
    # published. "What closes in week 38" is a different and more useful
    # question than "what did we learn about in week 29".
    for row in rows:
        if not row["deadline"]:
            continue
        try:
            closes = date.fromisoformat(row["deadline"])
        except (TypeError, ValueError):
            continue
        buckets[iso_week(closes)]["closing"] += 1

    out = []
    for label in sorted(buckets, reverse=True):
        monday, sunday = week_bounds(label)
        out.append({
            "label": label,
            "title": week_title(label),
            "starts": monday,
            "ends": sunday,
            **buckets[label],
        })
    return out


def week_items(store: Store, label: str, min_score: float = 25) -> list[Item]:
    """Everything published in one ISO week."""
    monday, sunday = week_bounds(label)
    rows = store.conn.execute("""
        SELECT * FROM items
         WHERE item_date >= ? AND item_date <= ? AND score >= ?
         ORDER BY score DESC, item_date DESC
    """, (monday.isoformat(), sunday.isoformat(), min_score)).fetchall()
    return [store._to_item(r) for r in rows]


def week_deadlines(store: Store, label: str) -> list[Item]:
    """Everything whose closing date falls in one ISO week."""
    monday, sunday = week_bounds(label)
    rows = store.conn.execute("""
        SELECT * FROM items
         WHERE deadline >= ? AND deadline <= ?
         ORDER BY deadline ASC, score DESC
    """, (monday.isoformat(), sunday.isoformat())).fetchall()
    return [store._to_item(r) for r in rows]


def week_summary(store: Store, label: str, tax: Taxonomy,
                 min_score: float = 25) -> dict:
    """One week's business, in the shape a briefing wants."""
    items = week_items(store, label, min_score)
    monday, sunday = week_bounds(label)

    by_tier: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        for tier in (item.tiers or ["Other"]):
            by_tier[tier].append(item)

    consultations = [i for i in items if i.source_kind == "consultation"]

    return {
        "label": label,
        "title": week_title(label),
        "starts": monday,
        "ends": sunday,
        "items": items,
        "by_tier": dict(by_tier),
        "consultations": consultations,
        # Windows that closed during this week — the retrospective check on
        # whether anything was missed.
        "closed_this_week": week_deadlines(store, label),
        "critical": [i for i in items if i.band == "Critical"],
        "high": [i for i in items if i.band == "High"],
        "sitting_days": sorted({i.item_date for i in items
                                if i.item_date and i.source_kind.endswith("transcript")}),
    }


# ---------------------------------------------------------------------------
# Permanent weekly snapshot
# ---------------------------------------------------------------------------

TIER_ORDER = ["Private rented sector", "Property & energy", "Tax & finance",
              "Planning & place", "Housing system", "Context", "Other"]


def render_week(summary: dict, tax: Taxonomy) -> str:
    """A self-contained HTML snapshot of one week.

    Deliberately plain and printable. This is the archival record, not the
    working dashboard: it must still make sense opened cold in three years, so
    it carries no filters, no JavaScript and no dependency on the database.
    """
    label = summary["label"]
    title = summary["title"]

    def card(item: Item) -> str:
        meta = " · ".join(filter(None, [
            item.item_date.strftime("%d %B %Y") if item.item_date else "",
            item.speaker, item.speaker_role, item.forum, item.source_name,
        ]))
        deadline = ""
        if item.deadline:
            deadline = (f'<p class="due">Closed {item.deadline.strftime("%d %B %Y")}</p>'
                        if item.deadline < date.today() else
                        f'<p class="due">Closes {item.deadline.strftime("%d %B %Y")}</p>')
        link = (f'<p class="lnk"><a href="{html.escape(item.url)}">'
                f'{html.escape(item.url)}</a></p>' if item.url else "")
        return f"""<article class="it">
  <h3>{html.escape(item.title)}</h3>
  <p class="meta">{html.escape(meta)}</p>
  {deadline}
  <p class="body">{html.escape(item.excerpt)}</p>
  {link}
</article>"""

    sections = []
    for tier in sorted(summary["by_tier"],
                       key=lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else 99):
        entries = sorted(summary["by_tier"][tier], key=lambda i: -i.score)[:20]
        sections.append(
            f'<h2>{html.escape(tier)} <span class="n">({len(summary["by_tier"][tier])})</span></h2>'
            + "".join(card(i) for i in entries))

    closed = summary["closed_this_week"]
    closed_html = ""
    if closed:
        rows = "".join(
            f'<li><strong>{i.deadline.strftime("%d %B")}</strong> — '
            f'{html.escape(i.title)}'
            + (f' · <a href="{html.escape(i.url)}">source</a>' if i.url else '')
            + "</li>" for i in closed)
        closed_html = (f'<h2>Windows closing this week</h2><ul class="dl">{rows}</ul>')

    sitting = summary["sitting_days"]
    sitting_html = (", ".join(d.strftime("%A %d %B") for d in sitting)
                    if sitting else "No sitting days recorded in this week.")

    body = "".join(sections) if sections else (
        '<div class="empty"><p>No relevant Senedd or Welsh Government business '
        'in this week.</p><p>During recess this is the correct record of a quiet '
        'week, not a gap in the data.</p></div>')

    return f"""<!DOCTYPE html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(label)} — NRLA Senedd monitor</title>
<style>
  body {{ margin:0; background:#F6F7F8; color:#0F2636; line-height:1.55;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; }}
  .wrap {{ max-width:860px; margin:0 auto; padding:0 24px 60px; }}
  header {{ background:#113B54; color:#FCFCFC; padding:24px 0; }}
  header h1 {{ margin:0 0 4px; font-size:1.4rem; font-weight:650; }}
  header p {{ margin:0; font-size:.9rem; opacity:.85; }}
  .summary {{ background:#FCFCFC; border:1px solid #DCE2E7; border-radius:10px;
    padding:14px 18px; margin:20px 0; font-size:.93rem; }}
  .summary dl {{ display:grid; grid-template-columns:auto 1fr; gap:4px 14px; margin:0; }}
  .summary dt {{ color:#5A7286; }}
  .summary dd {{ margin:0; font-weight:600; }}
  h2 {{ font-size:1.15rem; margin:30px 0 10px; font-weight:650; }}
  h2 .n {{ color:#5A7286; font-weight:400; font-size:.9rem; }}
  .it {{ background:#FCFCFC; border:1px solid #DCE2E7; border-left:4px solid #113B54;
    border-radius:8px; padding:12px 16px; margin-bottom:10px; }}
  .it h3 {{ margin:0 0 3px; font-size:1rem; font-weight:640; }}
  .it .meta {{ margin:0 0 6px; font-size:.83rem; color:#5A7286; }}
  .it .due {{ margin:0 0 6px; font-size:.86rem; color:#E96C19; font-weight:650; }}
  .it .body {{ margin:0 0 6px; font-size:.92rem; }}
  .it .lnk {{ margin:0; font-size:.78rem; word-break:break-all; }}
  .it .lnk a {{ color:#113B54; }}
  ul.dl {{ background:#FCFCFC; border:1px solid #DCE2E7; border-left:4px solid #E96C19;
    border-radius:8px; padding:12px 18px 12px 36px; font-size:.92rem; }}
  .empty {{ background:#FCFCFC; border:1px dashed #DCE2E7; border-radius:10px;
    padding:26px; text-align:center; color:#5A7286; }}
  footer {{ margin-top:28px; font-size:.78rem; color:#5A7286;
    border-top:1px solid #DCE2E7; padding-top:12px; }}
  @media print {{ .it {{ break-inside:avoid; }} }}
</style></head><body>
<header><div class="wrap">
  <h1>Senedd monitor — {html.escape(title)}</h1>
  <p>{html.escape(label)} · NRLA policy directorate</p>
</div></header>
<div class="wrap">
  <div class="summary"><dl>
    <dt>Items</dt><dd>{len(summary['items'])}</dd>
    <dt>Critical</dt><dd>{len(summary['critical'])}</dd>
    <dt>High priority</dt><dd>{len(summary['high'])}</dd>
    <dt>Consultations</dt><dd>{len(summary['consultations'])}</dd>
    <dt>Sitting days</dt><dd>{html.escape(sitting_html)}</dd>
  </dl></div>
  {closed_html}
  {body}
  <footer>
    <p>Permanent weekly record, generated {datetime.now().strftime('%d %B %Y at %H.%M')}.
       Quotations are the verbatim published text of Senedd Cymru and the Welsh
       Government, reproduced under the Open Government Licence v3.0.</p>
    <p>This snapshot is fixed. Re-scoring the archive later does not change it,
       so it remains an accurate record of what was known in this week.</p>
  </footer>
</div></body></html>"""


def write_week_snapshot(store: Store, label: str, tax: Taxonomy,
                        out_dir: str | Path = "out/weeks",
                        min_score: float = 25) -> Path:
    """Write the permanent snapshot for one week and return its path.

    Filename mirrors the supplier's own convention closely enough to compare
    like with like: `2026-W29.html` against `W29 NRLA Weekly Briefing`.
    """
    summary = week_summary(store, label, tax, min_score)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}.html"
    path.write_text(render_week(summary, tax), encoding="utf-8")
    return path


def backfill_snapshots(store: Store, tax: Taxonomy,
                       out_dir: str | Path = "out/weeks",
                       min_score: float = 25,
                       skip_current: bool = True) -> list[Path]:
    """Write a snapshot for every complete week in the archive.

    The current week is skipped by default: it is not finished, so freezing it
    would produce a permanent record that is wrong by Friday.
    """
    written = []
    now = current_week()
    for week in week_index(store, min_score):
        if skip_current and week["label"] == now:
            continue
        written.append(write_week_snapshot(store, week["label"], tax,
                                          out_dir, min_score))
    return written

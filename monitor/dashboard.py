"""Dashboard renderer: one self-contained HTML file.

REDESIGN NOTE — why this looks nothing like the first version
------------------------------------------------------------
The first version was a ranked list of 440 items with a numeric score on each.
The verdict from the policy directorate was blunt and correct: "incredibly
difficult to look at and prioritise what needs doing."

It was a data view pretending to be a work view. Three specific faults:

1. **It answered the wrong question.** It showed "what is most relevant" when a
   policy officer needs "what do I have to do, and by when". Relevance ranking is
   a means, not the output.
2. **Debates were fragmented.** Every contribution was its own card, so a single
   agenda item appeared up to 48 times. Five cards all headed "Statement by the
   First Minister: Legislation" is noise, not coverage.
3. **Scores were on display.** "148.5" tells a policy officer nothing and quietly
   invites them to compare numbers that are not comparable across sources.

This version is organised as work, in three zones:

    RESPOND      open consultations and inquiries, by closing date. The only
                 section with a hard clock. Usually a handful of items.
    REVIEW       developments worth a policy officer's attention, grouped into
                 one card per debate or per topic, newest first.
    ARCHIVE      everything else, collapsed by default, fully searchable.

Scores are hidden unless you ask for them. Everything is still one
self-contained HTML file: no server, no build step, no login, works offline,
opens on a phone, drops into a SharePoint library.

Design follows the NRLA brand: dark blue #113B54 primary, orange #E96C19 for
emphasis, off-white #FCFCFC in place of white, off-black #0F2636 in place of
black, sentence-case headings.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import date, datetime

from .models import Item
from .pipeline import RunReport
from .relevance import Taxonomy


NRLA = {
    "dark_blue": "#113B54",
    "orange": "#E96C19",
    "off_white": "#FCFCFC",
    "bg": "#F6F7F8",
    "off_black": "#0F2636",
}

TRANSCRIPT_KINDS = {"plenary_transcript", "committee_transcript"}
RESPOND_KINDS = {"consultation"}


# ---------------------------------------------------------------------------
# Grouping: one card per debate, not one per sentence
# ---------------------------------------------------------------------------

def group_transcripts(items: list[Item]) -> list[dict]:
    """Collapse transcript contributions into one entry per agenda item.

    Fixes the single worst readability fault of the first version. Measured on
    real data: 31 of 55 agenda items produced more than one card, and one
    produced 48. A policy officer wants "Questions to the Cabinet Minister for
    Local Government, Housing and Planning, 15 July — Gwenllian, O'Brien, Fox
    and 6 others" as one line they can open, not 38 separate cards.

    The group keeps the highest-scoring contribution as its headline excerpt,
    because that is the one most worth reading first, and lists every speaker.
    """
    grouped: dict[tuple, list[Item]] = defaultdict(list)
    singles: list[Item] = []

    for item in items:
        if item.source_kind in TRANSCRIPT_KINDS and item.agenda_item:
            key = (item.forum, item.item_date, item.agenda_item)
            grouped[key].append(item)
        else:
            singles.append(item)

    out: list[dict] = []

    for (forum, when, agenda), members in grouped.items():
        # Speakers in the order they spoke, captured BEFORE sorting. The
        # collector yields contributions in document order, so this is the real
        # running order — which is what a reader expects ("Rhun ap Iorwerth,
        # Dan Thomas, Ken Skates and 4 others" reads as a debate). Sorting by
        # score first put the loudest match at the front and made the list look
        # arbitrary.
        speakers = list(dict.fromkeys(m.speaker for m in members if m.speaker))
        # The lead contribution — used for the headline excerpt — is still the
        # highest-scoring one, because that is the bit worth reading first.
        members = sorted(members, key=lambda i: -i.score)
        lead = members[0]
        out.append({
            "kind": "debate",
            "lead": lead,
            "members": members,
            "count": len(members),
            "speakers": speakers,
            "forum": forum,
            "date": when,
            "title": agenda,
            # A debate's weight is its strongest moment, not the sum: summing
            # would make any long debate outrank a single decisive statement.
            "score": lead.score,
            "band": lead.band,
            "themes": sorted({t for m in members for t in m.themes}),
            "tiers": sorted({t for m in members for t in m.tiers}),
        })

    for item in singles:
        out.append({
            "kind": "item",
            "lead": item,
            "members": [item],
            "count": 1,
            "speakers": [item.speaker] if item.speaker else [],
            "forum": item.forum,
            "date": item.item_date,
            "title": item.title,
            "score": item.score,
            "band": item.band,
            "themes": item.themes,
            "tiers": item.tiers,
        })

    return out


# ---------------------------------------------------------------------------
# Plain-language framing
# ---------------------------------------------------------------------------

def urgency_label(deadline: date | None, today: date | None = None) -> tuple[str, str]:
    """Return (label, severity) in words a person can act on."""
    if deadline is None:
        return ("No closing date published", "none")
    today = today or date.today()
    days = (deadline - today).days
    if days < 0:
        return ("Closed", "closed")
    if days == 0:
        return ("Closes today", "now")
    if days == 1:
        return ("Closes tomorrow", "now")
    if days <= 7:
        return (f"{days} days left", "now")
    if days <= 21:
        return (f"{days} days left", "soon")
    return (f"Closes {deadline.strftime('%d %B')}", "later")


def why_it_matters(entry: dict, tax: Taxonomy) -> str:
    """One sentence, in plain English, on why this reached the team."""
    labels = [tax.theme_label(t) for t in entry["themes"][:3]]
    if not labels:
        return ""
    if len(labels) == 1:
        return f"Touches {labels[0].lower()}."
    return f"Touches {', '.join(l.lower() for l in labels[:-1])} and {labels[-1].lower()}."


def suggested_action(entry: dict) -> str:
    """What a policy officer would actually do next. Deliberately generic —
    the system suggests a next step, it does not decide NRLA's position."""
    lead = entry["lead"]
    kind = lead.source_kind
    if kind == "consultation":
        if "inquiry" in (lead.source_name or "").lower():
            return "Decide whether to submit written evidence."
        return "Decide whether to respond, and who drafts it."
    if kind == "legislation":
        return "Check whether the stage affects members, and brief if so."
    if kind == "calendar":
        return "Note the date; committee papers usually appear two weeks before."
    if kind in ("written_question", "oral_question"):
        return "Watch for the answer — it will set out the Government's position."
    if kind == "research":
        return "Read for context before briefing or responding."
    return "Read and decide whether a line to take is needed."


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def _entry_payload(entry: dict, tax: Taxonomy) -> dict:
    lead = entry["lead"]
    band = next((b for b in tax.bands if b["name"] == entry["band"]), {})
    label, severity = urgency_label(lead.deadline)

    speakers = entry["speakers"]
    if len(speakers) > 3:
        who = f"{', '.join(speakers[:3])} and {len(speakers) - 3} others"
    else:
        who = ", ".join(speakers)

    return {
        "uid": lead.uid,
        "title": entry["title"] or lead.title,
        "who": who,
        "role": lead.speaker_role,
        "forum": entry["forum"],
        "source_name": lead.source_name,
        "source_kind": lead.source_kind,
        "date": entry["date"].isoformat() if entry["date"] else "",
        "date_display": (entry["date"].strftime("%d %B %Y")
                         if entry["date"] else "Undated"),
        "url": lead.url,
        "video": lead.video_url,
        "excerpt": lead.excerpt,
        "full": "\n\n".join(
            (f"{m.speaker}: " if m.speaker else "") + m.body
            for m in entry["members"][:25]),
        "count": entry["count"],
        "score": entry["score"],
        "band": entry["band"],
        "band_colour": band.get("colour", "#5A7286"),
        "themes": [tax.theme_label(t) for t in entry["themes"]],
        "tiers": entry["tiers"],
        "signals": lead.signals,
        "why": why_it_matters(entry, tax),
        "action": suggested_action(entry),
        "deadline": lead.deadline.isoformat() if lead.deadline else "",
        "deadline_display": (lead.deadline.strftime("%d %B %Y")
                             if lead.deadline else ""),
        "urgency": label,
        "severity": severity,
    }


def render(items: list[Item], tax: Taxonomy, report: RunReport | None = None,
           stats: dict | None = None, deadlines: list[Item] | None = None,
           upcoming: list[Item] | None = None,
           title: str = "Senedd policy monitor") -> str:
    grouped = group_transcripts(items)
    payload = [_entry_payload(g, tax) for g in grouped]

    # RESPOND: consultations and inquiries. These are the only items with a
    # genuine clock on them, and they lead the page for that reason.
    # Split dated from undated. Seventeen cards all headed "No closing date
    # published" is not a to-do list. The dated ones are the ones with a clock,
    # so they get the section; the rest become a short watch-list underneath.
    all_respond = [p for p in payload if p["source_kind"] in RESPOND_KINDS]
    respond = sorted([p for p in all_respond if p["deadline"]],
                     key=lambda p: (p["deadline"], -p["score"]))
    undated = sorted([p for p in all_respond if not p["deadline"]],
                     key=lambda p: -p["score"])

    # REVIEW: developments worth attention, excluding the respond set and the
    # sitting calendar. Newest first, because recency is what a policy officer
    # actually scans by.
    review = [p for p in payload
              if p["source_kind"] not in RESPOND_KINDS
              and p["source_kind"] != "calendar"
              and p["band"] in ("Critical", "High")]
    review.sort(key=lambda p: (p["date"] or "", p["score"]), reverse=True)

    # COMING UP: scheduled sittings, compact.
    coming = [_entry_payload({"kind": "item", "lead": i, "members": [i],
                              "count": 1, "speakers": [], "forum": i.forum,
                              "date": i.item_date, "title": i.title,
                              "score": i.score, "band": i.band,
                              "themes": i.themes, "tiers": i.tiers}, tax)
              for i in (upcoming or [])]
    coming.sort(key=lambda p: p["deadline"] or "9999")

    # ARCHIVE: everything, for search.
    archive = sorted(payload, key=lambda p: (p["date"] or ""), reverse=True)

    closing_soon = sum(1 for p in respond if p["severity"] in ("now", "soon"))
    tiers = sorted({t for p in payload for t in p["tiers"]})

    errors = report.errors if report else []
    failed = report.sources_failed if report else []
    substituted = getattr(report, "sources_substituted", []) if report else []
    generated = datetime.now().strftime("%d %B %Y at %H.%M")

    data_json = json.dumps({
        "respond": respond, "undated": undated, "review": review,
        "coming": coming, "archive": archive, "tiers": tiers,
    }, ensure_ascii=False)

    # --- headline sentence, in words -------------------------------------
    bits = []
    if respond:
        bits.append(f"<strong>{len(respond)}</strong> "
                    f"{'consultation closing' if len(respond) == 1 else 'consultations closing'}")
    if closing_soon:
        bits.append(f"<strong>{closing_soon}</strong> closing within three weeks")
    if review:
        bits.append(f"<strong>{len(review)}</strong> "
                    f"{'development' if len(review) == 1 else 'developments'} to review")
    headline = " · ".join(bits) if bits else "Nothing needs attention right now."

    banner = ""
    if failed:
        rows = "".join(f"<li>{html.escape(e)}</li>" for e in errors[:8])
        banner = f"""<div class="banner banner--warn" role="alert">
          <strong>This view is incomplete.</strong>
          <p>A source we depend on returned nothing, so treat gaps with caution
             rather than as a quiet week.</p><ul>{rows}</ul></div>"""
    elif substituted:
        names = ", ".join(html.escape(s) for s in sorted(set(substituted)))
        banner = f"""<div class="banner banner--ok">
          <strong>Everything this view depends on is up to date.</strong>
          <p>{names} returned nothing, but its content reaches us another way.</p></div>"""

    run_rows = ""
    if report:
        run_rows = "".join(
            f"<tr><td>{html.escape(k)}</td><td class='num'>{v}</td></tr>"
            for k, v in sorted(report.per_source.items(), key=lambda x: -x[1]))
    error_details = "".join(f"<li>{html.escape(e)}</li>" for e in errors) or "<li>None.</li>"

    return f"""<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — NRLA</title>
<style>
  :root {{
    --blue: {NRLA['dark_blue']}; --orange: {NRLA['orange']};
    --white: {NRLA['off_white']}; --bg: {NRLA['bg']}; --ink: {NRLA['off_black']};
    --line: #DCE2E7; --muted: #5A7286; --red: #A32115; --r: 10px;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Arial,sans-serif;
    font-size:16px; line-height:1.55; }}
  a {{ color:var(--blue); }} a:hover {{ color:var(--orange); }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:0 24px 72px; }}
  header {{ background:var(--blue); color:var(--white); padding:26px 0 22px; }}
  header .wrap {{ padding-bottom:0; }}
  header h1 {{ margin:0 0 6px; font-size:1.5rem; font-weight:650; }}
  header .lede {{ margin:0; font-size:1.02rem; opacity:.95; }}
  header .lede strong {{ color:#FFD9BC; }}
  header .stamp {{ margin:8px 0 0; font-size:.82rem; opacity:.72; }}

  h2 {{ font-size:1.22rem; margin:34px 0 2px; font-weight:660; }}
  h2 .n {{ color:var(--muted); font-weight:400; font-size:.95rem; }}
  .sub {{ margin:0 0 14px; color:var(--muted); font-size:.9rem; }}

  .banner {{ border-radius:var(--r); padding:13px 16px; margin:18px 0;
    background:var(--white); border:1px solid var(--line); border-left:4px solid var(--muted); }}
  .banner--warn {{ border-left-color:var(--red); background:#FBF1F0; }}
  .banner--ok {{ border-left-color:#2E7D5B; }}
  .banner p {{ margin:5px 0 0; font-size:.88rem; }}
  .banner ul {{ margin:7px 0 0; padding-left:20px; font-size:.84rem; }}

  /* ---- action cards (RESPOND) ---- */
  .act {{ background:var(--white); border:1px solid var(--line);
    border-left:5px solid var(--orange); border-radius:var(--r);
    padding:16px 18px; margin-bottom:12px; }}
  .act.now {{ border-left-color:var(--red); }}
  .act.none {{ border-left-color:var(--muted); }}
  .act__top {{ display:flex; gap:12px; align-items:baseline;
    justify-content:space-between; flex-wrap:wrap; margin-bottom:4px; }}
  .due {{ font-weight:700; font-size:.86rem; letter-spacing:.02em;
    text-transform:uppercase; color:var(--orange); white-space:nowrap; }}
  .act.now .due {{ color:var(--red); }}
  .act.none .due {{ color:var(--muted); text-transform:none; font-weight:600; }}
  .act h3 {{ margin:0; font-size:1.06rem; font-weight:650; flex:1 1 300px; }}
  .act .meta {{ font-size:.85rem; color:var(--muted); margin:2px 0 9px; }}
  .act .why {{ font-size:.92rem; margin:0 0 8px; }}
  .act .todo {{ font-size:.92rem; margin:0 0 10px; padding:8px 12px;
    background:#F1F5F8; border-radius:7px; }}
  .act .todo b {{ font-weight:650; }}

  /* ---- review cards ---- */
  .rev {{ background:var(--white); border:1px solid var(--line);
    border-left:4px solid var(--blue); border-radius:var(--r);
    padding:14px 17px; margin-bottom:10px; }}
  .rev.critical {{ border-left-color:var(--red); }}
  .rev h3 {{ margin:0 0 3px; font-size:1rem; font-weight:640; }}
  .rev .meta {{ font-size:.84rem; color:var(--muted); margin:0 0 8px; }}
  .rev .why {{ font-size:.9rem; margin:0 0 8px; color:var(--ink); }}
  .rev .excerpt {{ font-size:.91rem; margin:0 0 9px; }}
  .rev .full {{ display:none; font-size:.91rem; white-space:pre-wrap;
    margin:0 0 9px; border-left:3px solid var(--line); padding-left:12px; }}
  .rev.open .full {{ display:block; }} .rev.open .excerpt {{ display:none; }}
  .pill {{ display:inline-block; font-size:.72rem; font-weight:700;
    text-transform:uppercase; letter-spacing:.04em; padding:2px 8px;
    border-radius:4px; color:var(--white); margin-right:7px; }}
  .contribs {{ font-size:.82rem; color:var(--muted); }}

  .links {{ font-size:.87rem; display:flex; gap:15px; flex-wrap:wrap; }}
  .tog {{ background:none; border:none; padding:0; font:inherit;
    color:var(--blue); cursor:pointer; text-decoration:underline; font-size:.87rem; }}
  .tog:hover {{ color:var(--orange); }}

  /* ---- coming up ---- */
  .up {{ display:grid; gap:6px; }}
  .up__row {{ display:flex; gap:14px; align-items:baseline; padding:8px 14px;
    background:var(--white); border:1px solid var(--line); border-radius:8px;
    font-size:.9rem; }}
  .up__row .d {{ font-weight:640; min-width:118px; white-space:nowrap;
    font-variant-numeric:tabular-nums; }}

  /* ---- archive ---- */
  details.arch {{ margin-top:34px; background:var(--white);
    border:1px solid var(--line); border-radius:var(--r); padding:14px 18px; }}
  details.arch > summary {{ cursor:pointer; font-weight:650; font-size:1.05rem; }}
  input[type=search], select {{ width:100%; padding:9px 11px;
    border:1px solid var(--line); border-radius:7px; font:inherit;
    background:var(--white); color:var(--ink); }}
  input[type=search]:focus, select:focus {{
    outline:3px solid rgba(233,108,25,.35); border-color:var(--orange); }}
  .ctrl {{ display:flex; gap:12px; flex-wrap:wrap; margin:14px 0 10px; }}
  .ctrl > div {{ flex:1 1 200px; }}
  label.f {{ display:block; font-size:.75rem; font-weight:650;
    text-transform:uppercase; letter-spacing:.04em; color:var(--muted);
    margin-bottom:4px; }}
  .chips {{ display:flex; gap:6px; flex-wrap:wrap; margin:4px 0 12px; }}
  .chip {{ border:1px solid var(--line); background:var(--white);
    border-radius:999px; padding:4px 12px; font-size:.83rem; cursor:pointer;
    font:inherit; color:var(--ink); }}
  .chip[aria-pressed=true] {{ background:var(--blue); color:var(--white);
    border-color:var(--blue); font-weight:600; }}
  .arow {{ padding:9px 0; border-bottom:1px solid var(--line); font-size:.91rem; }}
  .arow:last-child {{ border-bottom:none; }}
  .arow .m {{ color:var(--muted); font-size:.82rem; }}
  .count {{ font-size:.85rem; color:var(--muted); margin:6px 0 10px; }}
  .empty {{ padding:26px; text-align:center; color:var(--muted);
    background:var(--white); border:1px dashed var(--line); border-radius:var(--r); }}
  mark {{ background:#FCE3CB; color:inherit; }}
  table {{ border-collapse:collapse; width:100%; margin-top:8px; font-size:.87rem; }}
  th,td {{ text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  footer {{ margin-top:30px; font-size:.8rem; color:var(--muted); }}
  .scoretoggle {{ font-size:.8rem; color:var(--muted); }}
  body:not(.show-scores) .sc {{ display:none; }}
  .sc {{ font-size:.78rem; color:var(--muted); font-variant-numeric:tabular-nums; }}
  @media print {{ details.arch {{ display:none; }} .rev, .act {{ break-inside:avoid; }} }}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>{html.escape(title)}</h1>
  <p class="lede">{headline}</p>
  <p class="stamp">Senedd Cymru and Welsh Government · updated {generated}</p>
</div></header>

<div class="wrap">
  {banner}

  <h2>Respond <span class="n" id="nRespond"></span></h2>
  <p class="sub">Open consultations and inquiries. These have deadlines, so they
     come first. Everything else can wait.</p>
  <div id="respond"></div>
  <div id="undatedWrap" hidden>
    <p class="sub" style="margin-top:16px;"><b>Open, but no closing date published
       yet</b> — worth checking the source, and worth watching.</p>
    <div class="up" id="undated"></div>
  </div>

  <h2>Review <span class="n" id="nReview"></span></h2>
  <p class="sub">What has happened that a policy officer should know about. One
     card per debate, not one per speaker.</p>
  <div id="review"></div>

  <h2>Coming up <span class="n" id="nComing"></span></h2>
  <p class="sub">Scheduled Senedd business. Committee papers usually appear about
     two weeks before a sitting.</p>
  <div class="up" id="coming"></div>

  <details class="arch">
    <summary>Search everything <span class="n">— the full archive</span></summary>
    <div class="ctrl">
      <div style="flex:2 1 300px;">
        <label class="f" for="q">Search</label>
        <input type="search" id="q" placeholder="rent control · Rent Smart Wales · Gwenllian · empty properties">
      </div>
      <div>
        <label class="f" for="sort">Order by</label>
        <select id="sort">
          <option value="date">Most recent</option>
          <option value="score">Relevance</option>
        </select>
      </div>
    </div>
    <div class="chips" id="chips" role="group" aria-label="Filter by policy area"></div>
    <div class="count" id="count"></div>
    <div id="archive"></div>
  </details>

  <details class="arch">
    <summary>How this was produced</summary>
    <p style="font-size:.9rem;">Nothing on this page is written or summarised by a
       language model. Every quotation is the verbatim published text of Senedd
       Cymru or the Welsh Government, with a link to the source. Relevance is
       scored by a keyword and entity model defined in
       <code>config/taxonomy.yaml</code>, which the policy team edits directly.
       Scores are hidden by default because they are a means of sorting, not a
       measure anyone should act on.
       <label class="scoretoggle">
         <input type="checkbox" id="showScores"> show relevance scores
       </label></p>
    <table><thead><tr><th>Source</th><th class="num">Items</th></tr></thead>
      <tbody>{run_rows or '<tr><td colspan="2">No run data.</td></tr>'}</tbody></table>
    <p style="font-size:.9rem;margin-top:12px;"><b>Run notes</b></p>
    <ul style="font-size:.84rem;">{error_details}</ul>
  </details>

  <footer>
    <p>Senedd Cymru, Welsh Government and legislation.gov.uk content reproduced
       under the Open Government Licence v3.0.</p>
  </footer>
</div>

<script>
const D = {data_json};
const el = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);

let active = new Set();

function hl(t, q) {{
  const s = esc(t);
  if (!q) return s;
  try {{
    return s.replace(new RegExp('(' + q.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\\\$&') + ')','gi'), '<mark>$1</mark>');
  }} catch (e) {{ return s; }}
}}

function match(p, q) {{
  if (!q) return true;
  const hay = [p.title,p.excerpt,p.full,p.who,p.forum,p.source_name,
               (p.themes||[]).join(' ')].join(' ').toLowerCase();
  return q.toLowerCase().split(/\\s+/).filter(Boolean).every(w => hay.includes(w));
}}

/* ---------- RESPOND ---------- */
function drawRespond() {{
  el('nRespond').textContent = D.respond.length ? `— ${{D.respond.length}}` : '';
  el('respond').innerHTML = D.respond.length ? D.respond.map(p => `
    <article class="act ${{esc(p.severity)}}">
      <div class="act__top">
        <h3>${{p.url ? `<a href="${{esc(p.url)}}" target="_blank" rel="noopener">${{esc(p.title)}}</a>` : esc(p.title)}}</h3>
        <span class="due">${{esc(p.urgency)}}</span>
      </div>
      <p class="meta">${{esc(p.source_name)}}${{p.forum ? ' · ' + esc(p.forum) : ''}}${{
        p.deadline_display ? ' · closes ' + esc(p.deadline_display) : ''}}
        <span class="sc">· relevance ${{p.score}}</span></p>
      ${{p.why ? `<p class="why">${{esc(p.why)}}</p>` : ''}}
      <p class="todo"><b>Next step:</b> ${{esc(p.action)}}</p>
      <p class="excerpt" style="font-size:.9rem;color:#5A7286;">${{esc(p.excerpt)}}</p>
      <div class="links">
        ${{p.url ? `<a href="${{esc(p.url)}}" target="_blank" rel="noopener">Open the consultation</a>` : ''}}
      </div>
    </article>`).join('')
    : `<div class="empty">No open consultations or inquiries. Nothing to respond to
        right now.</div>`;
}}

/* ---------- open, but undated ---------- */
function drawUndated() {{
  if (!D.undated.length) return;
  el('undatedWrap').hidden = false;
  el('undated').innerHTML = D.undated.slice(0,12).map(p => `
    <div class="up__row">
      <span class="d" style="min-width:150px;color:#5A7286;">No date published</span>
      <span style="flex:1 1 auto;">${{p.url
        ? `<a href="${{esc(p.url)}}" target="_blank" rel="noopener">${{esc(p.title)}}</a>`
        : esc(p.title)}}
        <span class="sc">· ${{p.score}}</span></span>
    </div>`).join('');
}}

/* ---------- REVIEW ---------- */
function drawReview() {{
  el('nReview').textContent = D.review.length ? `— ${{D.review.length}}` : '';
  el('review').innerHTML = D.review.length ? D.review.map((p,i) => `
    <article class="rev ${{p.band === 'Critical' ? 'critical' : ''}}" data-i="${{i}}">
      <h3>${{p.url ? `<a href="${{esc(p.url)}}" target="_blank" rel="noopener">${{esc(p.title)}}</a>` : esc(p.title)}}</h3>
      <p class="meta">${{esc(p.date_display)}}${{p.forum ? ' · ' + esc(p.forum) : ''}}${{
        p.who ? ' · ' + esc(p.who) : ''}}${{
        p.count > 1 ? ` · <span class="contribs">${{p.count}} contributions</span>` : ''}}
        <span class="sc">· relevance ${{p.score}}</span></p>
      ${{p.why ? `<p class="why">${{esc(p.why)}}</p>` : ''}}
      <p class="excerpt">${{esc(p.excerpt)}}</p>
      <p class="full">${{esc(p.full)}}</p>
      <div class="links">
        ${{p.url ? `<a href="${{esc(p.url)}}" target="_blank" rel="noopener">Open the source</a>` : ''}}
        ${{p.video ? `<a href="${{esc(p.video)}}" target="_blank" rel="noopener">Watch this moment</a>` : ''}}
        <button class="tog" type="button">${{p.count > 1 ? 'Read the whole item' : 'Read the full text'}}</button>
      </div>
    </article>`).join('')
    : `<div class="empty">Nothing new to review. During recess this is expected.</div>`;
}}

/* ---------- COMING UP ---------- */
function drawComing() {{
  el('nComing').textContent = D.coming.length ? `— ${{D.coming.length}}` : '';
  el('coming').innerHTML = D.coming.length ? D.coming.slice(0,20).map(p => `
    <div class="up__row">
      <span class="d">${{esc(p.deadline_display || p.date_display)}}</span>
      <span style="flex:1 1 auto;">${{p.url
        ? `<a href="${{esc(p.url)}}" target="_blank" rel="noopener">${{esc(p.title)}}</a>`
        : esc(p.title)}}</span>
    </div>`).join('')
    : `<div class="empty">No scheduled business in the next 60 days.</div>`;
}}

/* ---------- ARCHIVE ---------- */
function drawArchive() {{
  const q = el('q').value.trim();
  const sort = el('sort').value;
  let rows = D.archive.filter(p =>
    (active.size === 0 || (p.tiers||[]).some(t => active.has(t))) && match(p,q));
  rows = rows.slice();
  if (sort === 'score') rows.sort((a,b) => b.score - a.score);
  else rows.sort((a,b) => (b.date||'').localeCompare(a.date||''));

  el('count').textContent =
    `${{rows.length}} of ${{D.archive.length}} items` +
    (q ? ` matching “${{q}}”` : '') +
    (active.size ? ` in ${{[...active].join(', ')}}` : '');

  el('archive').innerHTML = rows.length ? rows.slice(0,400).map(p => `
    <div class="arow">
      ${{p.url ? `<a href="${{esc(p.url)}}" target="_blank" rel="noopener">${{hl(p.title,q)}}</a>` : hl(p.title,q)}}
      <div class="m">${{esc(p.date_display)}} · ${{esc(p.source_name)}}${{
        p.who ? ' · ' + esc(p.who) : ''}}${{p.count>1 ? ` · ${{p.count}} contributions` : ''}}
        <span class="sc">· ${{p.score}}</span></div>
      <div style="font-size:.87rem;color:#5A7286;">${{hl(p.excerpt.slice(0,220),q)}}</div>
    </div>`).join('')
    : `<div class="empty">Nothing matches. Try clearing the filters.</div>`;
}}

/* ---------- init ---------- */
(function () {{
  el('chips').innerHTML = D.tiers.map(t =>
    `<button class="chip" aria-pressed="false" data-t="${{esc(t)}}">${{esc(t)}}</button>`).join('');
  el('chips').addEventListener('click', e => {{
    const b = e.target.closest('.chip'); if (!b) return;
    const t = b.dataset.t;
    if (active.has(t)) {{ active.delete(t); b.setAttribute('aria-pressed','false'); }}
    else {{ active.add(t); b.setAttribute('aria-pressed','true'); }}
    drawArchive();
  }});
  ['q','sort'].forEach(id => {{
    el(id).addEventListener('input', drawArchive);
    el(id).addEventListener('change', drawArchive);
  }});
  el('showScores').addEventListener('change', e =>
    document.body.classList.toggle('show-scores', e.target.checked));
  document.addEventListener('click', e => {{
    const t = e.target.closest('.tog'); if (!t) return;
    const card = t.closest('.rev'); card.classList.toggle('open');
    t.textContent = card.classList.contains('open') ? 'Show less' : 'Read the whole item';
  }});
  drawRespond(); drawUndated(); drawReview(); drawComing(); drawArchive();
}})();
</script>
</body>
</html>"""

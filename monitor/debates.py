"""Debate summaries — what was said in the Senedd yesterday, in the email.

WHAT THIS REPLACES
------------------
Camlas send a short note after any debate that touches the NRLA's interests:
a Word attachment with one paragraph per speaker, in reported speech. On
22 September 2026 there were two — the Building Safety Programme statement
(the minister, then each MS and the minister's reply) and two requests made
during the Business Statement (nutrient neutrality and housing delivery; HMOs
used for Home Office schemes), each with the Trefnydd's answer.

This module produces the same thing, in the body of one email, the next
morning. The directorate asked for three changes and they are the spec:

  * in the email, not an attachment;
  * short — a sentence or two per contribution, not a transcript;
  * by the next day at the latest (the morning after is fine).

WHERE THE WORDS COME FROM
-------------------------
The Senedd's own draft Record, read from its web page — see
collectors/record_html.py for why not the XML (it lags by days). Plenary is
complete the same evening, so a sitting is always ready by 07.30 the next
day. A committee's Record usually follows the next day and sometimes takes
two or three; a committee meeting is summarised on the first morning its
Record is up, and the email says which are still awaited.

WHAT COUNTS AS RELEVANT
-----------------------
The live page's own rule, Taxonomy.qualifies_for_site, applied to each
contribution. One definition of "relevant" for the whole system.

  * A whole debate is summarised when its title qualifies, or when at least
    40% of what was said qualifies (and at least two contributions). A
    statement on building safety: yes. A Programme for Government statement
    in which one MS mentioned empty homes: no — that becomes an exchange.
  * Otherwise the item is cut into EXCHANGES: an MS's question or request
    and the reply to it. An exchange is summarised when any part of it
    qualifies. This is how Camlas treat the Business Statement and question
    sessions: two relevant requests out of fifteen, with the answers.
  * Question sessions are always cut into exchanges, even "Questions to the
    Cabinet Minister for Housing": half of that session is about planning or
    local government, and a summary of all of it is a transcript.

HOW IT IS SUMMARISED
--------------------
Two modes, chosen by whether the ANTHROPIC_API_KEY secret exists.

  * AI summaries (with the key). Claude writes one or two sentences of
    reported speech per contribution, as Camlas do. It is given only that
    debate's words and is told to add nothing. Every figure in its summary is
    then checked against the speaker's actual words; a summary containing a
    number the speaker did not say is thrown away and replaced by the
    speaker's own sentences. The email says plainly that the summaries are
    AI-written and links every line to the Record.
  * Key sentences (without the key). The most relevant one or two sentences
    of each contribution, verbatim. No third party sees anything, nothing can
    be misstated, and it costs nothing — but it reads as quotations, not as a
    summary.

Both modes give the same structure, so the email looks the same either way.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from .collectors.record_html import AgendaItem, Contribution, Record
from .forward import (DARK_BLUE, EDGE, FONT, LINE, MUTED, OFF_BLACK, OFF_WHITE,
                      ORANGE, WIDTH)
from .models import Item
from .relevance import Scorer, Taxonomy, find_terms


# A whole debate is summarised if this share of it is relevant.
WHOLE_DEBATE_SHARE = 0.4

# Plenary items that are never debate content.
SKIP_ITEMS = re.compile(r"^\s*(\d+\.?\s*)?(voting time|cyfnod pleidleisio)\b", re.I)

# How much of a contribution the key-sentence mode keeps.
KEY_SENTENCES = 2
KEY_SENTENCES_LEAD = 3
KEY_WORDS_MAX = 75

# Opening courtesies that are never the point.
_COURTESY = re.compile(
    r"^(diolch|thank you|thanks|i thank|can I thank|may I thank|"
    r"good afternoon|prynhawn da|llywydd|dirprwy lywydd|presiding officer|"
    r"trefnydd[,.]|minister[,.]|first minister[,.]|cabinet minister[,.])\b",
    re.I)

_MINISTERIAL = re.compile(
    r"\b(Minister|Trefnydd|Counsel General|Cwnsler Cyffredinol|Gweinidog|"
    r"Prif Weinidog|Deputy Minister)\b", re.I)


# ---------------------------------------------------------------------------
# What is summarised
# ---------------------------------------------------------------------------

@dataclass
class Point:
    """One contribution in the email: who, and what they said."""
    contribution: Contribution
    summary: str = ""
    verbatim: bool = False      # key sentences, not an AI summary

    @property
    def speaker_label(self) -> str:
        c = self.contribution
        return f"{c.speaker} MS" if c.member else c.speaker


@dataclass
class Exchange:
    heading: str                # the question's title, or ""
    contributions: list[Contribution]


@dataclass
class Debate:
    """One agenda item as it will appear in the email."""
    record: Record
    item: AgendaItem
    meeting_date: date | None
    whole: bool                 # the whole debate, or selected exchanges
    exchanges: list[Exchange] = field(default_factory=list)
    overview: str = ""
    points: list[list[Point]] = field(default_factory=list)   # per exchange
    mode: str = "verbatim"      # "ai" or "verbatim"
    papers_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.item.heading_text or self.item.title

    @property
    def url(self) -> str:
        return (f"{self.record.url}#{self.item.anchor}" if self.item.anchor
                else self.record.url)

    @property
    def video_url(self) -> str:
        for c in self.item.contributions:
            if c.video_url:
                return c.video_url
        return ""

    @property
    def contribution_count(self) -> int:
        return sum(len(e.contributions) for e in self.exchanges)


class Relevance:
    """The live page's rule, applied to a piece of the Record."""

    def __init__(self, tax: Taxonomy, scorer: Scorer | None = None):
        self.tax = tax
        self.scorer = scorer or Scorer(tax)
        self._cache: dict[tuple[str, str], bool] = {}

    def _check(self, title: str, body: str) -> bool:
        key = (title, body)
        if key not in self._cache:
            probe = Item(source_kind="plenary_transcript",
                         source_name="Senedd Record", title=title, body=body)
            self.scorer.score_item(probe)
            self._cache[key] = self.tax.qualifies_for_site(probe)
        return self._cache[key]

    def text(self, text: str) -> bool:
        """A contribution, judged on its own words only.

        Not on the agenda item's title: under "Questions to the Cabinet
        Minister for Local Government, Housing and Planning" a question about
        bus services would otherwise qualify because of the heading."""
        return self._check("", text)

    def title(self, title: str) -> bool:
        return self._check(title, title)


def _chairs(record: Record) -> set[str]:
    """Who is chairing: anyone with a chair's role, and, in a committee, the
    first speaker of the meeting (committee chairs carry no role on the
    page)."""
    names = {c.speaker for i in record.items for c in i.contributions if c.is_chair}
    if not record.is_plenary:
        for i in record.items:
            if i.contributions:
                names.add(i.contributions[0].speaker)
                break
    return names


def _responder(contribs: list[Contribution]) -> str:
    """The person answering — the minister, the Trefnydd, the First
    Minister: whoever speaks most often in the item."""
    counts = Counter(c.speaker for c in contribs)
    ministerial = [c.speaker for c in contribs if _MINISTERIAL.search(c.role or "")]
    if ministerial:
        return Counter(ministerial).most_common(1)[0][0]
    return counts.most_common(1)[0][0] if counts else ""


def exchanges(item: AgendaItem, chairs: set[str]) -> list[Exchange]:
    """Cut an item into exchanges: one question or request, and the reply.

    Pairs, not a questioner's whole run: a party leader asks three questions
    on three subjects, and only the one about council housing belongs in the
    email. A tabled question keeps its heading on every pair, so when the
    heading itself is relevant ("Protecting Renters") the whole run comes
    through anyway.
    """
    speaking = [c for c in item.contributions if c.speaker not in chairs]
    responder = _responder(speaking)
    out: list[Exchange] = []
    for block in item.blocks:
        current: Exchange | None = None
        for c in block.contributions:
            if c.speaker in chairs:
                continue
            asking = c.speaker != responder
            if current is None or (asking and any(
                    x.speaker == responder for x in current.contributions)) or (
                    asking and current.contributions[-1].speaker not in (c.speaker, responder)):
                current = Exchange(heading=block.heading, contributions=[])
                out.append(current)
            current.contributions.append(c)
    return [e for e in out if e.contributions]


def select(record: Record, rel: Relevance,
           meeting_date: date | None = None) -> list[Debate]:
    """The relevant debates and exchanges in one meeting's Record."""
    chairs = _chairs(record)
    out: list[Debate] = []
    for item in record.items:
        if SKIP_ITEMS.search(item.title) or rel.tax.is_procedural_agenda_item(item.title):
            continue
        speaking = [c for c in item.contributions if c.speaker not in chairs]
        if not speaking:
            continue
        title = item.heading_text
        is_questions = any(b.heading for b in item.blocks)
        hits = [c for c in speaking if rel.text(c.text)]

        whole = (not is_questions) and (
            rel.title(title)
            or (len(hits) >= 2 and len(hits) >= WHOLE_DEBATE_SHARE * len(speaking)))
        if whole:
            out.append(Debate(record=record, item=item, meeting_date=meeting_date,
                              whole=True,
                              exchanges=[Exchange(heading="", contributions=speaking)]))
            continue

        if not record.is_plenary:
            continue            # committees: whole sessions only (see above)

        chosen = []
        for ex in exchanges(item, chairs):
            # Judged as one text, heading included, so a veto sees the whole
            # exchange: "enforcement action" in a reply is not housing when
            # the question was headed "Illegally Dumped Waste".
            whole_text = "\n".join([ex.heading] + [c.text for c in ex.contributions])
            if (ex.heading and rel.title(ex.heading)) or rel.text(whole_text):
                chosen.append(ex)
        if chosen:
            out.append(Debate(record=record, item=item, meeting_date=meeting_date,
                              whole=False, exchanges=chosen))
    return out


# ---------------------------------------------------------------------------
# Key sentences — no third party, nothing paraphrased
# ---------------------------------------------------------------------------

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z‘“'\"(])")


def sentences(text: str) -> list[str]:
    out = []
    for para in text.split("\n"):
        out.extend(s.strip() for s in _SENTENCE.split(para) if s.strip())
    return out


def key_sentences(c: Contribution, tax: Taxonomy, lead: bool = False) -> str:
    """The contribution's most relevant sentences, verbatim, in order."""
    sents = sentences(c.text)
    if not sents:
        return ""
    terms = [t for spec in tax.themes.values() for t in spec.get("terms", [])]
    scored = []
    for i, s in enumerate(sents):
        n_words = len(s.split())
        score = 4.0 * len(find_terms(s, terms))
        if _COURTESY.search(s):
            score -= 3 if n_words >= 25 else 6
        if "?" in s:
            score += 1.0        # the question is usually the point
        if n_words < 6:
            score -= 2
        score -= i * 0.05       # earlier is better, gently
        scored.append((score, i, s))

    keep = KEY_SENTENCES_LEAD if lead else KEY_SENTENCES
    ranked = sorted(scored, key=lambda t: (-t[0], t[1]))
    if any(t[0] >= 0 for t in ranked):
        ranked = [t for t in ranked if t[0] >= 0]
    picked: list[tuple[int, str]] = []
    words = 0
    for _, i, s in ranked:
        w = s.split()
        if len(w) > KEY_WORDS_MAX:
            s, w = " ".join(w[:KEY_WORDS_MAX]) + " …", w[:KEY_WORDS_MAX]
        if picked and words + len(w) > KEY_WORDS_MAX:
            continue
        picked.append((i, s))
        words += len(w)
        if len(picked) >= keep:
            break
    return " ".join(s for _, s in sorted(picked))


def summarise_verbatim(debate: Debate, tax: Taxonomy) -> Debate:
    debate.mode = "verbatim"
    debate.points = []
    for ex in debate.exchanges:
        row = []
        for n, c in enumerate(ex.contributions):
            lead = debate.whole and n == 0 and _MINISTERIAL.search(c.role or "")
            text = key_sentences(c, tax, lead=bool(lead))
            if text:
                row.append(Point(contribution=c, summary=text, verbatim=True))
        debate.points.append(row)
    return debate


# ---------------------------------------------------------------------------
# AI summaries — Claude, when the key is there
# ---------------------------------------------------------------------------

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_INPUT_CHARS = 150_000       # about 35,000 words; a long debate is 10,000

SYSTEM_PROMPT = """You summarise debates in the Senedd (the Welsh Parliament) \
for the policy team of the National Residential Landlords Association.

You will be given the draft Record of one agenda item: numbered contributions, \
each with the speaker and their role. Write a short summary of each \
contribution in reported speech, in the style of a public affairs consultancy \
note: neutral, British English, past tense.

Rules:
- Use ONLY what is in the text you are given. Add no facts, context, figures, \
dates, party labels or opinions of your own. If you are unsure, leave it out.
- One or two sentences per contribution. The opening statement of a minister \
may have up to four. Keep figures, commitments, dates and named policies \
exactly as the speaker gave them.
- Give most space to anything touching housing, the private rented sector, \
landlords, renters, building safety, homelessness, property taxation and \
local authority enforcement.
- Do not name private individuals — constituents, residents, campaigners, \
company employees. Describe them generically ("a resident in Cardiff"). \
Members, ministers, public bodies and companies may be named.
- Refer to a minister by title ("The Cabinet Minister said..."), and to other \
speakers by name without "MS" (the email adds it).
- Skip contributions that are only thanks or procedure by returning an empty \
summary for them.
- "overview" is one sentence saying what the item was about. Leave it empty \
if the item is a set of unrelated questions or requests.

Reply with JSON only, no prose around it, in exactly this shape:
{"overview": "...", "points": [{"n": 1, "summary": "..."}, ...]}"""


_NUMBER = re.compile(r"\d[\d,.]*")


def _numbers(text: str) -> set[str]:
    """Figures in a text, normalised: '£1,200' and '1200' are the same."""
    out = set()
    for raw in _NUMBER.findall(text or ""):
        n = raw.replace(",", "").rstrip(".")
        if n:
            out.add(n)
            if "." in n:
                out.add(n.rstrip("0").rstrip("."))
    return out


def figures_check(summary: str, source: str) -> bool:
    """True when every figure in the summary appears in the source.

    Numbers are where a paraphrase does the most damage — a wrong figure in a
    briefing gets repeated — and they are the one thing that can be checked
    mechanically. Numbers written as words ("three") are allowed through in
    both directions; this catches a figure appearing from nowhere.
    """
    return _numbers(summary) <= _numbers(source)


def _prompt(debate: Debate) -> tuple[str, list[Contribution]]:
    flat = [c for ex in debate.exchanges for c in ex.contributions]
    budget = MAX_INPUT_CHARS // max(1, len(flat))
    lines = [f"Meeting: {debate.record.forum}",
             f"Agenda item: {debate.title}",
             ("This is the whole debate." if debate.whole else
              "These are selected exchanges from the item; each is a question "
              "or request and the reply to it."), ""]
    last_heading = None
    for n, c in enumerate(flat, 1):
        heading = next((ex.heading for ex in debate.exchanges if c in ex.contributions), "")
        if heading and heading != last_heading:
            lines.append(f"[Question: {heading}]")
            last_heading = heading
        who = c.speaker + (f" ({c.role})" if c.role else "")
        text = c.text if len(c.text) <= budget else c.text[:budget] + " …"
        lines.append(f"{n}. {who}:\n{text}\n")
    return "\n".join(lines), flat


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def summarise_ai(debate: Debate, tax: Taxonomy, api_key: str,
                 model: str = "", post=None) -> Debate:
    """Claude's summary, checked; any contribution that fails the check, or a
    debate the call fails on entirely, falls back to key sentences."""
    import requests

    post = post or requests.post
    prompt, flat = _prompt(debate)
    try:
        resp = post(API_URL, timeout=180, headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={
            "model": model or DEFAULT_MODEL,
            "max_tokens": 4000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        })
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        text = "".join(part.get("text", "") for part in resp.json().get("content", [])
                       if part.get("type") == "text")
        data = _parse_json(text)
        if data is None:
            raise RuntimeError("the reply was not the JSON asked for")
    except Exception as exc:                    # noqa: BLE001 — any failure falls back
        summarise_verbatim(debate, tax)
        debate.overview = ""
        debate.notes = [f"AI summary unavailable ({exc}); key sentences shown instead."]
        return debate

    by_n: dict[int, str] = {}
    for p in data.get("points", []) or []:
        try:
            n = int(p.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(flat):
            by_n[n] = re.sub(r"\s+", " ", str(p.get("summary") or "")).strip()

    source_all = "\n".join(c.text for c in flat)
    overview = re.sub(r"\s+", " ", str(data.get("overview") or "")).strip()
    debate.overview = overview if figures_check(overview, source_all) else ""

    debate.mode = "ai"
    debate.points = []
    rejected = 0
    n = 0
    for ex in debate.exchanges:
        row = []
        for c in ex.contributions:
            n += 1
            summary = by_n.get(n, "")
            if not summary:
                continue            # thanks or procedure, as instructed
            if figures_check(summary, c.text):
                row.append(Point(contribution=c, summary=summary))
            else:
                rejected += 1
                fallback = key_sentences(c, tax)
                if fallback:
                    row.append(Point(contribution=c, summary=fallback, verbatim=True))
        debate.points.append(row)
    if rejected:
        debate.notes = [f"{rejected} AI summar{'y' if rejected == 1 else 'ies'} "
                        f"quoted a figure not found in the speaker's words and "
                        f"{'was' if rejected == 1 else 'were'} replaced by the "
                        f"speaker's own sentences."]
    return debate


def summarise(debates: list[Debate], tax: Taxonomy,
              api_key: str | None = None, model: str = "") -> list[Debate]:
    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
    model = model or os.environ.get("DEBATE_SUMMARY_MODEL", "")
    for d in debates:
        if key:
            summarise_ai(d, tax, key, model)
        else:
            summarise_verbatim(d, tax)
    return [d for d in debates if any(d.points)]


# ---------------------------------------------------------------------------
# The email — Outlook-safe, like the Friday and morning emails
# ---------------------------------------------------------------------------

def _e(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _link(text: str, url: str, colour: str = DARK_BLUE, weight: int = 600) -> str:
    if not url:
        return _e(text)
    return (f'<a href="{_e(url)}" style="color:{colour};font-weight:{weight};'
            f'text-decoration:none">{_e(text)}</a>')


def _point_row(p: Point, record_url: str) -> str:
    c = p.contribution
    role = (f'<span style="color:{MUTED};font-weight:400"> · {_e(c.role)}</span>'
            if c.role else "")
    text = (f'&ldquo;{_e(p.summary)}&rdquo;' if p.verbatim else _e(p.summary))
    style = "font-style:italic;" if p.verbatim else ""
    anchor = f"{record_url}#{c.anchor}" if c.anchor else record_url
    return (
        f'<tr><td style="padding:10px 18px 10px;border-bottom:1px solid {LINE};'
        f'font-family:{FONT};vertical-align:top">'
        f'<div style="font-size:13px;font-weight:700;color:{OFF_BLACK};'
        f'line-height:1.45">{_e(p.speaker_label)}{role}</div>'
        f'<div style="font-size:13.5px;line-height:1.55;color:{OFF_BLACK};'
        f'padding-top:3px;{style}">{text} '
        f'<a href="{_e(anchor)}" style="color:{MUTED};font-size:11.5px;'
        f'font-style:normal;text-decoration:none;white-space:nowrap">'
        f'Record&nbsp;&rsaquo;</a></div>'
        f'</td></tr>')


def _debate_card(d: Debate) -> str:
    when = d.meeting_date.strftime("%a %-d %B") if d.meeting_date else ""
    links = " · ".join(filter(None, [
        _link("Record", d.url),
        _link("watch", d.video_url) if d.video_url else "",
        _link("agenda papers", d.papers_url) if d.papers_url else "",
    ]))
    meta = " · ".join(filter(None, [_e(d.record.forum), _e(when)]))
    scope = ("" if d.whole else
             f'<div style="font-size:12px;color:{MUTED};padding-top:4px">'
             f'{len(d.exchanges)} relevant exchange'
             f'{"s" if len(d.exchanges) != 1 else ""} from this item</div>')
    overview = (f'<div style="font-size:13.5px;line-height:1.55;color:{OFF_BLACK};'
                f'padding-top:8px">{_e(d.overview)}</div>') if d.overview else ""
    head = (f'<tr><td style="padding:15px 18px 12px;border-bottom:1px solid {LINE};'
            f'font-family:{FONT};background:#F7FAFC">'
            f'<div style="font-size:15px;line-height:1.4;font-weight:700">'
            f'{_link(d.title, d.url, OFF_BLACK, 700)}</div>'
            f'<div style="font-size:12px;color:{MUTED};padding-top:4px">'
            f'{meta} &nbsp;·&nbsp; {links}</div>{scope}{overview}</td></tr>')

    rows = []
    last_heading = ""
    for ex, pts in zip(d.exchanges, d.points):
        if not pts:
            continue
        if ex.heading and not d.whole and ex.heading != last_heading:
            last_heading = ex.heading
            rows.append(f'<tr><td style="padding:10px 18px 0;font-family:{FONT};'
                        f'font-size:11px;font-weight:700;letter-spacing:.6px;'
                        f'text-transform:uppercase;color:{ORANGE}">'
                        f'{_e(ex.heading)}</td></tr>')
        rows.extend(_point_row(p, d.record.url) for p in pts)
    notes = "".join(
        f'<tr><td style="padding:8px 18px;font-family:{FONT};font-size:11.5px;'
        f'color:#8A3B06">{_e(n)}</td></tr>' for n in d.notes)
    body = head + "".join(rows) + notes
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="border-collapse:collapse;'
            f'width:100%;background:#ffffff;border:1px solid {EDGE};'
            f'">{body}</table>'
            f'<div style="height:18px;line-height:18px;font-size:0">&nbsp;</div>')


def render_debates(debates: list[Debate], pending: list[str] | None = None,
                   page_url: str = "") -> tuple[str, str, int]:
    """Return ``(subject, html_body, count)``. Count 0 means send nothing."""
    count = len(debates)
    if not count:
        return "", "", 0
    dates = sorted({d.meeting_date for d in debates if d.meeting_date})
    forums = []
    for d in debates:
        if d.record.forum not in forums:
            forums.append(d.record.forum)
    day_text = (" and ".join(x.strftime("%a %-d %B") for x in dates[-2:])
                if len(dates) <= 2 else f"{dates[0]:%-d} to {dates[-1]:%-d %B}")
    where = forums[0] if len(forums) == 1 else "Plenary and committees" \
        if "Plenary" in forums else "committees"
    subject = (f"Senedd debate summaries — {where}, {day_text} "
               f"({count} item{'s' if count != 1 else ''})")

    ai = any(d.mode == "ai" for d in debates)
    how = (
        "Summaries are written by AI (Claude) from the Senedd's draft Record, "
        "which is not yet final. Figures are checked automatically against "
        "what was said. Check the Record, linked on every line, before "
        "quoting anyone." if ai else
        "Each line is the speaker's own words — the most relevant sentences of "
        "what they said, taken verbatim from the Senedd's draft Record, which "
        "is not yet final. Every line links to the Record.")

    cards = "".join(_debate_card(d) for d in debates)
    pending_html = ""
    if pending:
        pending_html = (
            f'<p style="font-size:12.5px;color:{MUTED};margin:0;padding-top:4px;'
            f'font-family:{FONT};line-height:1.55"><b>Still awaited:</b> '
            + _e("; ".join(pending)) +
            ". The Senedd has not yet published the Record of these meetings; "
            "any relevant debate will be summarised on the morning it appears.</p>")
    footer_link = (
        f'<p style="font-size:12.5px;color:{MUTED};margin:0;padding-top:18px;'
        f'font-family:{FONT}">Everything else said in the Chamber and in '
        f'committee is searchable on the <a href="{_e(page_url)}" '
        f'style="color:{DARK_BLUE};font-weight:600">live page</a>.</p>'
    ) if page_url else ""

    html_body = f"""<table role="presentation" width="100%" cellpadding="0" \
cellspacing="0" border="0" style="border-collapse:collapse;background:{OFF_WHITE}">
<tr><td align="center" style="padding:0">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" \
border="0" style="border-collapse:collapse;width:{WIDTH}px;max-width:{WIDTH}px">

  <tr><td bgcolor="{DARK_BLUE}" style="padding:24px 28px 20px;font-family:{FONT};color:#ffffff">
    <div style="font-size:20px;font-weight:700;letter-spacing:-.2px;color:#ffffff">
      What was said — Senedd debate summaries</div>
    <div style="font-size:13px;color:#C3D2DC;padding-top:5px">
      National Residential Landlords Association &nbsp;·&nbsp; {_e(day_text)}</div>
  </td></tr>
  <tr><td bgcolor="{ORANGE}" height="4" style="height:4px;line-height:4px;
    font-size:0">&nbsp;</td></tr>

  <tr><td style="padding:20px 28px 0;font-family:{FONT}">
    <p style="font-size:12.5px;color:{MUTED};line-height:1.55;margin:0 0 16px">
      Debates and exchanges that match the NRLA's relevance rules — the same
      rules as the live page. {_e(how)}</p>
    {cards}
    {pending_html}
  </td></tr>

  <tr><td style="padding:0 28px 34px;font-family:{FONT}">
    {footer_link}
    <p style="font-size:11.5px;color:{MUTED};line-height:1.55;margin:0;
      padding-top:16px;font-family:{FONT}">
      Contains Senedd Cymru information licensed under the Open Government
      Licence v3.0.
    </p>
  </td></tr>

</table>
</td></tr></table>"""
    return subject, html_body, count


# ---------------------------------------------------------------------------
# Which meetings to look at, and remembering which have been done
# ---------------------------------------------------------------------------

# How far back a meeting is still looked for. A committee's Record has taken
# up to three days in September 2026; ten covers a recess week's delays.
LOOKBACK_DAYS = 10

# How long a finished meeting is remembered. Past the lookback it can never
# be picked up again, so this only needs to be comfortably longer.
REMEMBER_DAYS = 45

STATE_PATH = "data/debates-sent.json"


@dataclass
class Candidate:
    meeting_id: str
    forum: str
    when: date | None
    papers_url: str = ""

    @property
    def label(self) -> str:
        return f"{self.forum}, {self.when:%a %-d %B}" if self.when else self.forum


def candidates(tv_meetings: list, listing: list[dict], today: date,
               done: set[str], baseline: date | None = None,
               lookback: int = LOOKBACK_DAYS) -> list[Candidate]:
    """Meetings that have happened, are recent, and have not been done.

    Two sources, merged by meeting id, because neither is complete alone:

      * senedd.tv's "Latest meetings" strip — every broadcast meeting,
        straight after it happens, but only the last nine or so;
      * the Record's own export listing — the sixteen most recent meetings
        with a published Record, including any the strip has dropped.
    """
    out: dict[str, Candidate] = {}

    def keep(mid: str, when: date | None) -> bool:
        if not mid or mid in done or when is None:
            return False
        if when >= today:
            return False            # still sitting, or not yet sat
        if baseline and when <= baseline:
            return False
        return (today - when).days <= lookback

    for m in tv_meetings:
        if keep(m.meeting_id, m.when):
            out[m.meeting_id] = Candidate(
                meeting_id=m.meeting_id,
                forum="Plenary" if m.is_plenary else m.name,
                when=m.when,
                papers_url=m.papers_url if m.meeting_id else "")
    for row in listing:
        mid = str(row.get("meeting_id") or "")
        when = row.get("date")
        if keep(mid, when) and mid not in out:
            forum = row.get("forum") or ""
            plenary = "plenary" in forum.lower()
            out[mid] = Candidate(
                meeting_id=mid, forum="Plenary" if plenary else forum, when=when,
                papers_url=(f"https://business.senedd.wales/ieListDocuments.aspx"
                            f"?CId=908&MId={mid}") if plenary else "")
    return sorted(out.values(),
                  key=lambda c: (c.when or date.min, c.forum != "Plenary", c.forum))


def load_state(path: str = STATE_PATH) -> dict:
    """``{"baseline": "YYYY-MM-DD", "meetings": {id: {...}}}``.

    The baseline is the day before this feature was switched on, so the first
    run does not send a fortnight of back numbers.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("meetings", {})
    return data


def save_state(state: dict, today: date, path: str = STATE_PATH) -> None:
    keep = {}
    for mid, row in (state.get("meetings") or {}).items():
        try:
            when = date.fromisoformat(row.get("date", ""))
        except ValueError:
            when = today
        if (today - when).days <= REMEMBER_DAYS:
            keep[mid] = row
    state["meetings"] = dict(sorted(keep.items()))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def state_baseline(state: dict) -> date | None:
    try:
        return date.fromisoformat(state.get("baseline") or "")
    except ValueError:
        return None

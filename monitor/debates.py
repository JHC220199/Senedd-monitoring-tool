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
    rank: int = 0               # verbatim: 0 is the most relevant in the item
    priority: bool = False      # about private renting: put first
    at: int = 0                 # where in the item it happened
    cited: list = field(default_factory=list)   # AI: the contributions drawn on

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
    # With the key (7 October 2026): the few points that matter most, for
    # the email, and a short note of proceedings, for the Word document.
    # Each Point's contribution is the first one it draws on.
    key_points: list[Point] = field(default_factory=list)
    note: list[Point] = field(default_factory=list)
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

    @property
    def has_content(self) -> bool:
        return bool(self.key_points or self.note or any(self.points))


# ---------------------------------------------------------------------------
# Private renting — the NRLA's own ground, which always goes first
# ---------------------------------------------------------------------------
#
# 8 October 2026: the email on 7 October's Plenary led with building safety
# and energy efficiency. Anthony Slaughter MS asking for an end to no-fault
# evictions and a rent freeze came third, without the Cabinet Minister's
# answer; the closing dates of Leasing Scheme Wales were left out; and the
# debate on HMOs came after one on family drug and alcohol courts. "Those
# are big parts and ... 100% should have been front and centre of both the
# email and the document." So anything about private renting is marked,
# put first in the email and the document, and never left out of the
# document.

PRS_THEMES = ("private_rented_sector", "rent_controls_and_affordability",
              "evictions_and_possession", "rent_smart_wales", "tenancy_law",
              "hmos_and_standards")
# A strong sign is enough on its own ("renters", "no-fault eviction",
# "HMOs", "Leasing Scheme Wales"); the everyday words need to come twice —
# "across different tenancies", said once in an answer on energy
# efficiency, is not a question about private renting.
_PRS_STRONG = re.compile(
    r"\b(renters?|evict\w*|letting agen\w+|leasing scheme\w*|HMOs?|houses? in "
    r"multiple occupation|private(?:ly)? rent\w*|private landlords?|private tenants?)\b",
    re.I)
_PRS_WEAK = re.compile(
    r"\b(landlords?|tenants?|tenanc(?:y|ies)|renting|rents?|rental|damp|mould|"
    r"overcrowding)\b", re.I)
PRS_SCORE = 2
# Social housing has landlords, tenants and rents too, and is not the NRLA's.
_NOT_PRS = re.compile(
    r"\b(?:registered social|social(?: housing)?|council(?: housing)?|housing "
    r"associations?|community)\s+(?:landlords?|tenants?|tenanc(?:y|ies)|rents?)\b",
    re.I)
# What counts as housing in a debate that is not about housing (below).
_NOT_HOUSING_THEMES = {"planning_system", "fiscal_and_legislative_context",
                       "committee_scrutiny", "data_and_evidence"}


def prs_score(text: str, tax: Taxonomy) -> int:
    t = _NOT_PRS.sub(" ", text or "")
    terms = [x for k in PRS_THEMES for x in tax.themes.get(k, {}).get("terms", [])
             if not _PRS_WEAK.fullmatch(x)]
    strong = {m.group(0).lower() for m in _PRS_STRONG.finditer(t)}
    strong |= {x.lower() for x in find_terms(t, terms)}
    return 2 * len(strong) + len(_PRS_WEAK.findall(t))


def is_prs(text: str, tax: Taxonomy) -> bool:
    """Is this about private renting?"""
    return prs_score(text, tax) >= PRS_SCORE


def housing_terms(text: str, tax: Taxonomy) -> int:
    """How many different housing terms the text uses."""
    return sum(len(find_terms(text, spec.get("terms", [])))
               for k, spec in tax.themes.items() if k not in _NOT_HOUSING_THEMES)


# A debate (a Member, opposition or short debate) is one subject argued
# through. When the subject is not housing, a passing mention is not worth
# an email: 7 October's debate on family drug and alcohol courts came
# through because one Member said care leavers risk homelessness. Such a
# debate now needs a part about private renting, or a part that is really
# about housing (two housing terms or more).
_DEBATE_ITEM = re.compile(r"\bdebate\b", re.I)
DEBATE_HOUSING_TERMS = 2


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

        strict = (not is_questions) and bool(_DEBATE_ITEM.search(title))
        chosen = []
        for ex in exchanges(item, chairs):
            # Judged as one text, heading included, so a veto sees the whole
            # exchange: "enforcement action" in a reply is not housing when
            # the question was headed "Illegally Dumped Waste".
            whole_text = "\n".join([ex.heading] + [c.text for c in ex.contributions])
            if (ex.heading and rel.title(ex.heading)) or rel.text(whole_text):
                if strict and not (is_prs(whole_text, rel.tax) or
                                   housing_terms(whole_text, rel.tax) >= DEBATE_HOUSING_TERMS):
                    continue
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
    # Only the most relevant contributions, so the fallback is no longer than
    # the AI version: VERBATIM_DOCUMENT for the document, the first
    # VERBATIM_EMAIL of those (by rank) for the email.
    debate.points = most_relevant(debate, tax, VERBATIM_DOCUMENT)
    return debate


# ---------------------------------------------------------------------------
# AI summaries — Claude, when the key is there
# ---------------------------------------------------------------------------

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_INPUT_CHARS = 150_000       # about 35,000 words; a long debate is 10,000

SYSTEM_PROMPT = """You write short notes on debates in the Senedd (the \
Welsh Parliament) for the policy team of the National Residential Landlords \
Association (NRLA), which represents private landlords.

You will be given the draft Record of one agenda item: numbered contributions, \
each with the speaker and their role, and a list of the parts of the item to \
cover. Parts and contributions marked ★ are about private renting — \
landlords, tenants, rents, evictions, HMOs, licensing, leasing schemes — the \
NRLA's own ground. Write in the style of a public affairs consultancy note: \
reported speech, neutral, British English, past tense.

Reply with three things:
- "overview": one or two sentences, no more than 40 words, saying what the \
item was about and what in it matters most to the NRLA, starting with \
anything marked ★. Leave it empty if the item is a set of unrelated questions.
- "key_points": for the email, the {k} points from the item that matter most \
to the NRLA — a question and its answer, a commitment, a figure. Points about \
★ parts come first, the most important first. Most important of all is \
anything that would change what private landlords must do or may charge — \
new duties, licence conditions, rent or eviction rules — and the \
Government's answer to it. Each is one sentence, two at \
most, no more than 35 words (50 for a ★ point), naming who said it.
- "note": for a document read later, ONE ENTRY FOR EACH PART in the list, \
and no more: {m} entries. ★ parts first, then the rest in the order they \
happened. Combine a question and its answer in one entry ('Marc Jones MS \
asked where the figures for empty properties stood. Dr Henry Dawson \
said...'). No more than 70 words for a ★ part, 45 for any other. Never \
leave out the answer to keep within the limit: shorten the question instead. Leave out \
introductions, thanks, procedure and repetition: this is a summary, not a \
transcript.

For each key point and note entry, "n" lists the numbered contributions it \
draws on.

For anything marked ★, say exactly what was asked or proposed and what the \
answer was — including whether the Government agreed, refused or would not \
commit — and keep every date, deadline, timescale and next step given ('no \
new properties after December', 'a second and third Bill'). Write a date as \
the speaker gave it ('March next year'); never work out a year.

Inside the text of an entry, use single quotation marks, never double ones.

Rules for all three:
- Use ONLY what is in the text you are given. Add no facts, context, figures, \
dates, party labels or opinions of your own. If you are unsure, leave it out.
- Keep commitments, dates and named policies as the speaker gave them. Write \
a figure either as the speaker said it or as the same number in digits \
("seventy per cent" or "70 per cent"). Never work out a new figure: no \
totals, differences or percentages the speaker did not give.
- After private renting, give most weight to housing, building safety, empty \
homes, homelessness, property taxation and local authority enforcement.
- Do not name private individuals — constituents, residents, campaigners, \
company employees. Describe them generically ("a resident in Cardiff"). \
Members, ministers, public bodies, companies and witnesses giving evidence \
to a committee may be named.
- Refer to a minister by title ("The Cabinet Minister said..."), to other \
Members as "Name MS", and to witnesses by name.

Reply with JSON only, no prose around it, in exactly this shape:
{{"overview": "...", "key_points": [{{"n": [3, 4], "text": "..."}}], \
"note": [{{"n": [1, 2], "text": "..."}}]}}"""


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


_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
             "eleventh": 11, "twelfth": 12, "twentieth": 20, "thirtieth": 30,
             "hundredth": 100}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000,
           "billion": 1_000_000_000}
_NUMBER_WORD = re.compile(
    r"\b(?:" + "|".join(sorted(list(_UNITS) + list(_TENS) + list(_SCALES) + ["a", "and"],
                                key=len, reverse=True))
    + r")(?:[\s-]+(?:" + "|".join(sorted(list(_UNITS) + list(_TENS) + list(_SCALES)
                                         + ["and"], key=len, reverse=True))
    + r"))*\b", re.I)


def _words_value(words: list[str]) -> int | None:
    total = current = 0
    seen = False
    for w in words:
        if w in ("and", "a"):
            if w == "a":
                current = max(current, 1)
            continue
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = max(current, 1) * 100
        elif w in _SCALES:
            total += max(current, 1) * _SCALES[w]
            current = 0
        else:
            return None
        seen = True
    return total + current if seen else None


def _word_numbers(text: str) -> set[str]:
    """Numbers the speaker said in words, as digits: "Seventy per cent" gives
    70, "four of 161" gives 4, "two and a half thousand" gives 2 and 1000s
    of its parts. The Record writes out numbers under a hundred and many
    round ones, so a faithful summary that writes "70%" must pass."""
    out: set[str] = set()
    for m in _NUMBER_WORD.finditer(text or ""):
        words = [w.lower() for w in re.split(r"[\s-]+", m.group(0)) if w]
        while words and words[0] in ("a", "and"):
            words = words[1:]
        while words and words[-1] in ("a", "and"):
            words = words[:-1]
        if not words:
            continue
        v = _words_value(words)
        if v is not None:
            out.add(str(v))
        for w in words:                    # each part on its own as well
            v = _UNITS.get(w, _TENS.get(w))
            if v is not None:
                out.add(str(v))
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w in _ORDINALS:
            out.add(str(_ORDINALS[w]))
    # "a half" / "half" appear as 0.5 or 50%
    if re.search(r"\bhalf\b", text or "", re.I):
        out.update({"50", "0.5"})
    if re.search(r"\bquarter\b", text or "", re.I):
        out.update({"25", "0.25"})
    return out


def figures_check(summary: str, source: str) -> bool:
    """True when every figure in the summary appears in the source.

    Numbers are where a paraphrase does the most damage — a wrong figure in a
    briefing gets repeated — and they are the one thing that can be checked
    mechanically. A figure the speaker said in words counts ("Seventy per
    cent" allows 70%); this catches a figure appearing from nowhere.
    """
    have = _numbers(source) | _word_numbers(source)
    # "£2 million" may be summarised as "£2m" or as "£2,000,000".
    for m in re.finditer(r"(\d[\d,.]*)\s*(thousand|million|billion|bn|m)\b", source or "", re.I):
        base = m.group(1).replace(",", "").rstrip(".")
        mult = {"thousand": 1000, "million": 1_000_000, "m": 1_000_000,
                "billion": 1_000_000_000, "bn": 1_000_000_000}[m.group(2).lower()]
        try:
            have.add(str(int(round(float(base) * mult))))
        except ValueError:
            pass
    return _numbers(summary) <= have


# Length limits, in words (7 October 2026: the housing committee's 1 October
# email ran to 9,000 words and its document to 20 pages, against the
# consultancy's 1,700 and seven. "In no world should it ever be 20 pages").
WORDS_OVERVIEW = 40
WORDS_KEY_POINT = 35
WORDS_NOTE = 45
# Private renting gets room for the answer, the dates and the next steps.
WORDS_KEY_POINT_PRS = 50
WORDS_NOTE_PRS = 70
KEY_POINTS_MAX = 5
NOTE_MAX = 20


def limits(n_contributions: int, n_parts: int = 0, n_prs: int = 0) -> tuple[int, int]:
    """(key points for the email, note entries for the document) for an
    item of this many contributions, parts to cover and parts about private
    renting. A three-hour evidence session gets four key points, not 80 —
    but every part of it that matters gets its line in the document."""
    k = 3 if n_contributions <= 6 else 4
    k = max(k, min(KEY_POINTS_MAX, n_prs))
    m = min(10, max(2, -(-n_contributions // 3)))
    m = min(NOTE_MAX, max(m, n_parts))
    return k, m


@dataclass
class Part:
    """A part of an item the document must cover: one Member's question and
    the answers to it, or, in a whole debate, one speaker."""
    label: str
    contributions: list[Contribution]
    prs: bool = False


def parts(debate: Debate, tax: Taxonomy) -> list[Part]:
    """What the document must cover, so that nothing relevant is left out
    (8 October 2026: "all relevant contributions are captured in at least
    the word document").

    Selected exchanges: each Member's question under each heading, with the
    answers. A whole debate: each speaker who said something relevant."""
    out: list[Part] = []
    if debate.whole:
        rel = Relevance(tax)
        by: dict[str, list[Contribution]] = {}
        for ex in debate.exchanges:
            for c in ex.contributions:
                by.setdefault(c.speaker, []).append(c)
        for who, cs in by.items():
            prs = any(is_prs(c.text, tax) for c in cs)
            if prs or any(rel.text(c.text) for c in cs):
                label = f"{who} MS" if cs[0].member else who
                out.append(Part(label=label, contributions=cs, prs=prs))
        return out
    groups: dict[tuple[str, str], Part] = {}
    for ex in debate.exchanges:
        asker = ex.contributions[0]
        key = (ex.heading, asker.speaker)
        if key not in groups:
            label = (f"{asker.speaker} MS" if asker.member else asker.speaker) + \
                (f" — {ex.heading}" if ex.heading else "")
            groups[key] = Part(label=label, contributions=[])
            out.append(groups[key])
        groups[key].contributions.extend(ex.contributions)
    for part in out:
        heading = part.label.split(" — ", 1)[1] if " — " in part.label else ""
        part.prs = is_prs("\n".join([heading] + [c.text for c in part.contributions]), tax)
    return out


def _numbers_text(ns: list[int]) -> str:
    """[1, 2, 3, 7] → "1–3, 7"."""
    out, run = [], []
    for n in sorted(ns):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}–{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append(f"{run[0]}–{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


# Verbatim (no key, or the call failed): the most relevant contributions
# only, so the fallback is no longer than the AI version.
VERBATIM_EMAIL = 4
VERBATIM_DOCUMENT = 10


def _prompt(debate: Debate, tax: Taxonomy | None = None,
            the_parts: list[Part] | None = None) -> tuple[str, list[Contribution]]:
    """The prompt, and the contributions it numbers."""
    flat = [c for ex in debate.exchanges for c in ex.contributions]
    budget = MAX_INPUT_CHARS // max(1, len(flat))
    the_parts = the_parts if the_parts is not None else (parts(debate, tax) if tax else [])
    n_prs = sum(1 for p in the_parts if p.prs)
    k, m = limits(len(flat), len(the_parts), n_prs)
    starred = {id(c) for p in the_parts if p.prs for c in p.contributions}
    index = {id(c): n for n, c in enumerate(flat, 1)}
    lines = [f"Meeting: {debate.record.forum}",
             f"Agenda item: {debate.title}",
             ("This is the whole item." if debate.whole else
              "These are selected exchanges from the item; each is a question "
              "or request and the reply to it."),
             f"Give at most {k} key points and exactly one note entry for each "
             f"part below ({len(the_parts) or m} entries).", ""]
    if the_parts:
        lines.append("Parts to cover in the note (★ = private renting):")
        for p in sorted(the_parts, key=lambda p: not p.prs):
            ns = [index[id(c)] for c in p.contributions if id(c) in index]
            lines.append(f"{'★ ' if p.prs else '- '}{p.label}: contributions "
                         f"{_numbers_text(ns)}")
        lines.append("")
    last_heading = None
    for n, c in enumerate(flat, 1):
        heading = next((ex.heading for ex in debate.exchanges if c in ex.contributions), "")
        if heading and heading != last_heading:
            lines.append(f"[Question: {heading}]")
            last_heading = heading
        who = (f"{c.speaker} MS" if c.member else c.speaker) + (f" ({c.role})" if c.role else "")
        star = " ★" if id(c) in starred else ""
        text = c.text if len(c.text) <= budget else c.text[:budget] + " …"
        lines.append(f"{n}.{star} {who}:\n{text}\n")
    return "\n".join(lines), flat


def _repair_quotes(text: str) -> str:
    """Escape double quotation marks inside JSON strings.

    8 October 2026: a preview of 7 October's housing questions failed twice
    with "the reply was not the JSON asked for" — a note quoting a speaker
    ("a second and third Bill") in plain double quotes ends the string
    early. A quote mark that is not followed by , : } or ] is inside the
    text, so it is escaped."""
    out, in_string, i = [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and in_string:
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            if not in_string:
                in_string = True
            else:
                rest = text[i + 1:].lstrip()
                if not rest or rest[0] in ",:}]":
                    in_string = False
                else:
                    out.append('\\"')
                    i += 1
                    continue
        elif ch == "\n" and in_string:
            out.append("\\n")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    for candidate in (m.group(0), _repair_quotes(m.group(0))):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else None
    return None


def _ask(prompt: str, api_key: str, model: str, post, usage, k: int = 4,
         m: int = 10) -> dict:
    resp = post(API_URL, timeout=180, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, json={
        "model": model or DEFAULT_MODEL,
        "max_tokens": 12000,
        "system": SYSTEM_PROMPT.format(k=k, m=m),
        "messages": [{"role": "user", "content": prompt}],
    })
    if usage is not None:
        usage.calls += 1
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if usage is not None:
        u = body.get("usage") or {}
        usage.input_tokens += int(u.get("input_tokens") or 0) + \
            int(u.get("cache_creation_input_tokens") or 0) + \
            int(u.get("cache_read_input_tokens") or 0)
        usage.output_tokens += int(u.get("output_tokens") or 0)
    text = "".join(part.get("text", "") for part in body.get("content", [])
                   if part.get("type") == "text")
    data = _parse_json(text)
    if data is None:
        why = ("it was cut off at the length limit" if body.get("stop_reason") == "max_tokens"
               else f"it began {text[:60]!r}")
        raise RuntimeError(f"the reply was not the JSON asked for ({why})")
    return data


def _entries(data: dict, key: str, flat: list[Contribution], cap: int,
             limit: int, usage=None, starred: set[int] | None = None,
             cap_prs: int | None = None) -> list[Point]:
    """Checked, length-limited entries from a reply: each must keep to the
    figures in the contributions it cites, and fit in ``cap`` words
    (``cap_prs`` for one about private renting)."""
    from .weekly_ai import fit

    starred = starred or set()
    out: list[Point] = []
    for e in data.get(key, []) or []:
        if not isinstance(e, dict):
            continue
        text = re.sub(r"\s+", " ", str(e.get("text") or "")).strip()
        if not text:
            continue
        ns = e.get("n")
        ns = ns if isinstance(ns, list) else [ns]
        cited, at = [], []
        for n in ns:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(flat) and flat[n - 1] not in cited:
                cited.append(flat[n - 1])
                at.append(n)
        source = "\n".join(c.text for c in (cited or flat))
        if not figures_check(text, source):
            if usage is not None:
                usage.rejected += 1
            continue
        priority = any(id(c) in starred for c in cited)
        short = _fit_entry(text, (cap_prs or cap) if priority else cap)
        if not short:
            if usage is not None:
                usage.too_long += 1
            continue
        out.append(Point(contribution=cited[0] if cited else flat[0], summary=short,
                         priority=priority, at=min(at) if at else 0, cited=cited))
        if len(out) >= limit:
            break
    return out


def _fit_entry(text: str, cap: int) -> str:
    """An entry kept whole if it is within half as much again as ``cap``;
    otherwise cut back to whole sentences.

    8 October 2026: cutting at ``cap`` itself dropped the second sentence of
    an entry — the answer: "Anthony Slaughter MS asked ... whether the
    Minister would commit to ending no-fault evictions", without her
    reply."""
    from .weekly_ai import fit
    soft = int(cap * 1.5)
    if len(text.split()) <= soft:
        return text
    return fit(text, soft)


def _fill(the_parts: list[Part], note: list[Point], flat: list[Contribution],
          tax: Taxonomy, usage=None) -> list[Point]:
    """Points, in the speakers' own words, for any part the AI note left
    out — so nothing relevant is missing from the document."""
    covered = set()
    for pt in note:
        covered.update(id(c) for c in (pt.cited or [pt.contribution]))
    index = {id(c): n for n, c in enumerate(flat, 1)}
    cited_ok = lambda p: any(id(c) in covered for c in p.contributions)  # noqa: E731
    terms = [t for spec in tax.themes.values() for t in spec.get("terms", [])]

    def best(cs):
        return max(cs, key=lambda c: (is_prs(c.text, tax),
                                      len(find_terms(c.text, terms)), -index.get(id(c), 0)))
    extra: list[Point] = []
    for p in the_parts:
        if cited_ok(p):
            continue
        asker = p.contributions[0].speaker
        asked = [c for c in p.contributions if c.speaker == asker]
        answers = [c for c in p.contributions if c.speaker != asker]
        for c in [best(asked)] + ([best(answers)] if answers else []):
            text = key_sentences(c, tax)
            if text:
                extra.append(Point(contribution=c, summary=text, verbatim=True,
                                   priority=p.prs, at=index.get(id(c), 0)))
        if usage is not None:
            usage.filled += 1
    return extra


def summarise_ai(debate: Debate, tax: Taxonomy, api_key: str,
                 model: str = "", post=None, usage=None) -> Debate:
    """An overview, the few key points for the email, and a note of
    proceedings for the document — every figure checked against what was
    said, every entry cut to length, private renting first, and every
    relevant part covered (in the speakers' own words if the AI left it
    out). If the call fails, or nothing usable comes back after a second
    try, the speakers' key sentences are used."""
    import requests
    from .weekly_ai import fit

    post = post or requests.post
    the_parts = parts(debate, tax)
    prompt, flat = _prompt(debate, tax, the_parts)
    k, m = limits(len(flat), len(the_parts), sum(1 for p in the_parts if p.prs))
    starred = {id(c) for p in the_parts if p.prs for c in p.contributions}
    source_all = "\n".join(c.text for c in flat)
    for attempt in (1, 2):
        try:
            data = _ask(prompt, api_key, model, post, usage, k=k, m=m)
        except Exception as exc:                # noqa: BLE001 — any failure falls back
            if attempt == 2 or "JSON" not in str(exc):
                summarise_verbatim(debate, tax)
                debate.overview = ""
                debate.notes = [f"AI summary unavailable ({exc}); the speakers' own "
                                f"sentences are shown instead."]
                if usage is not None:
                    usage.failures += 1
                return debate
            continue
        key_points = _entries(data, "key_points", flat, WORDS_KEY_POINT, k, usage,
                              starred, WORDS_KEY_POINT_PRS)
        note = _entries(data, "note", flat, WORDS_NOTE, m, usage,
                        starred, WORDS_NOTE_PRS)
        if key_points:
            break
        if usage is not None:
            usage.retried += 1
    if not key_points:
        summarise_verbatim(debate, tax)
        debate.overview = ""
        if usage is not None:
            usage.extracts += 1
        return debate

    overview = re.sub(r"\s+", " ", str(data.get("overview") or "")).strip()
    debate.overview = (fit(overview, WORDS_OVERVIEW) or "") \
        if overview and figures_check(overview, source_all) else ""
    debate.mode = "ai"
    # Private renting first; in the document, each group in the order it
    # happened.
    debate.key_points = sorted(key_points, key=lambda p: not p.priority)
    note = (note or list(key_points)) + _fill(the_parts, note, flat, tax, usage)
    debate.note = sorted(note, key=lambda p: (not p.priority, p.at))
    debate.points = []
    debate.notes = []
    return debate


def prs_weight(debate: Debate, tax: Taxonomy) -> int:
    """How much of the item is about private renting: its title counts for
    a great deal, then each contribution."""
    n = sum(1 for ex in debate.exchanges for c in ex.contributions if is_prs(c.text, tax))
    return n + (100 if is_prs(debate.title, tax) else 0)


def summarise(debates: list[Debate], tax: Taxonomy,
              api_key: str | None = None, model: str = "", usage=None) -> list[Debate]:
    """Summarised, with the items most about private renting first (a
    debate on HMOs before questions on planning), otherwise in the order
    they happened."""
    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
    model = model or os.environ.get("DEBATE_SUMMARY_MODEL", "")
    for d in debates:
        if key:
            summarise_ai(d, tax, key, model, usage=usage)
        else:
            summarise_verbatim(d, tax)
    kept = [d for d in debates if d.has_content]
    weight = {id(d): prs_weight(d, tax) for d in kept}
    return sorted(kept, key=lambda d: -weight[id(d)])


def most_relevant(debate: Debate, tax: Taxonomy, limit: int) -> list[list[Point]]:
    """The verbatim points cut to the ``limit`` most relevant, kept in order
    and in their exchanges."""
    terms = [t for spec in tax.themes.values() for t in spec.get("terms", [])]
    flat = [(x, y, p) for x, row in enumerate(debate.points) for y, p in enumerate(row)]
    ranked = sorted(flat, key=lambda t: (not is_prs(t[2].summary, tax),
                                         -len(find_terms(t[2].summary, terms)),
                                         t[0], t[1]))[:limit]
    for r, (_x, _y, p) in enumerate(ranked):
        p.rank = r
    keep = {(x, y) for x, y, _p in ranked}
    return [[p for y, p in enumerate(row) if (x, y) in keep]
            for x, row in enumerate(debate.points)]


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


def _group_row(label: str) -> str:
    return (f'<tr><td style="padding:10px 18px 0;font-family:{FONT};'
            f'font-size:11px;font-weight:700;letter-spacing:.6px;'
            f'text-transform:uppercase;color:{ORANGE}">{_e(label)}</td></tr>')


def _key_point_row(p: Point, record_url: str) -> str:
    c = p.contribution
    anchor = f"{record_url}#{c.anchor}" if c.anchor else record_url
    return (
        f'<tr><td style="padding:9px 18px 9px 30px;border-bottom:1px solid {LINE};'
        f'font-family:{FONT};vertical-align:top;font-size:13.5px;line-height:1.55;'
        f'color:{OFF_BLACK}">&bull;&nbsp; {_e(p.summary)} '
        f'<a href="{_e(anchor)}" style="color:{MUTED};font-size:11.5px;'
        f'text-decoration:none;white-space:nowrap">Record&nbsp;&rsaquo;</a>'
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
    if d.key_points:
        first = [p for p in d.key_points if p.priority]
        rest = [p for p in d.key_points if not p.priority]
        if first:
            rows.append(_group_row("Private renting"))
            rows.extend(_key_point_row(p, d.record.url) for p in first)
            if rest:
                rows.append(_group_row("Also raised"))
        rows.extend(_key_point_row(p, d.record.url) for p in rest)
    for ex, pts in zip(d.exchanges, d.points if not d.key_points else []):
        pts = [p for p in pts if p.rank < VERBATIM_EMAIL]
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
                   page_url: str = "", late: bool = False,
                   document: bool = False) -> tuple[str, str, int]:
    """Return ``(subject, html_body, count)``. Count 0 means send nothing.

    ``late``: the Record of one past meeting, published after the
    morning-after summaries had gone — sent as an email of its own."""
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
    title = "What was said — Senedd debate summaries"
    late_note = ""
    if late:
        subject = (f"Record now published — {where}, {day_text} "
                   f"({count} item{'s' if count != 1 else ''})")
        title = "Record now published — what was said"
        late_note = (f"The Senedd has now published the Record of this meeting, "
                     f"held on {day_text}. ")

    ai = any(d.mode == "ai" for d in debates)
    how = (
        "Key points summarised by AI (Claude) from the Senedd's draft Record, "
        "with figures checked against what was said. Check the Record before "
        "quoting anyone." if ai else
        "The most relevant sentences, in the speakers' own words, from the "
        "Senedd's draft Record.")

    cards = "".join(_debate_card(d) for d in debates)
    pending_html = ""
    if pending:
        pending_html = (
            f'<p style="font-size:12.5px;color:{MUTED};margin:0;padding-top:4px;'
            f'font-family:{FONT};line-height:1.55"><b>Still awaited:</b> '
            + _e("; ".join(pending)) +
            ". The Senedd has not yet published the Record of these meetings; "
            "any relevant debate will be summarised on the morning it appears.</p>")
    doc_note = "" if not document else (
        f'<p style="font-size:12.5px;color:{MUTED};margin:0;padding-top:14px;'
        f'font-family:{FONT};line-height:1.55"><b>In more detail:</b> the attached '
        f'Word document gives the witnesses and a short note of each item.</p>')
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
      {_e(title)}</div>
    <div style="font-size:13px;color:#C3D2DC;padding-top:5px">
      National Residential Landlords Association &nbsp;·&nbsp; {_e(day_text)}</div>
  </td></tr>
  <tr><td bgcolor="{ORANGE}" height="4" style="height:4px;line-height:4px;
    font-size:0">&nbsp;</td></tr>

  <tr><td style="padding:20px 28px 0;font-family:{FONT}">
    <p style="font-size:12.5px;color:{MUTED};line-height:1.55;margin:0 0 16px">
      {_e(late_note)}{_e(how)}</p>
    {cards}
    {pending_html}
    {doc_note}
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

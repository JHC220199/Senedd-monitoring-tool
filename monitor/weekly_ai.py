"""AI summaries for the Friday briefing — Claude, when the key is there.

WHAT THIS DOES
--------------
With the ANTHROPIC_API_KEY secret set, each item in the week in review
(monitor/weekly_review.py) is sent to Claude with its own words from the
Record, and comes back with:

  * ``line``  — one sentence for the Friday email, in place of the verbatim
    quotation;
  * a summary of every contribution, for the Word document, in the style of
    Camlas's weekly briefing ("Francesca O'Brien said progress on remediation
    had been too slow..."): one sentence, 30 words at most (50 for a
    minister's opening statement);
  * for a press release or written statement, a summary of 50 words at most.

The directorate's choices, 29 September 2026: everything summarised, in the
email and the document; Claude Sonnet 5.

SAFEGUARDS
----------
  * Only the text sent is used; the instructions forbid adding anything.
  * Every summary is checked mechanically: if it contains a figure the
    speaker did not say (in digits or in words), it is thrown away (the same
    check as the debate summaries). A contribution of substance with no
    summary, for that reason or because it came back empty, is asked for
    again on its own; if there is still none, the document shows its key
    sentence or two, marked as an extract — never the whole speech.
  * Private individuals are not named.
  * Any failure — no key, an error from the API, a reply that is not the JSON
    asked for — falls back to the verbatim version, so the email always goes.
  * The email and the document say plainly that the summaries are AI-written
    and link to the Record for the exact words.

COST
----
Every call's token use is added up and printed with its cost at the end of
the run, and written to the run's summary page, so the estimate can be
checked against real use (AI summaries cost estimate, 25 September 2026:
about $0.20 a sitting week on Sonnet 5 for the debate emails and this
together).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .debates import API_URL, figures_check

DEFAULT_MODEL = "claude-sonnet-5"
MAX_INPUT_CHARS = 120_000
MAX_TOKENS = 6000

# US dollars per million tokens (input, output), from Anthropic's pricing page
# on 25 September 2026. Used only to report what a run cost.
PRICES = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
}

SYSTEM_PROMPT = """You write the weekly briefing on the Senedd (the Welsh \
Parliament) for the policy team of the National Residential Landlords \
Association (NRLA), which represents private landlords.

You will be given one item from this week's business: numbered contributions \
from the Record of Proceedings, each with the speaker and their role, or the \
text of a Welsh Government notice.

Write in the style of a public affairs consultancy briefing: reported speech, \
neutral, British English, past tense.

Rules:
- Use ONLY what is in the text you are given. Add no facts, context, figures, \
dates, party labels or opinions of your own. If you are unsure, leave it out.
- Keep figures, commitments, dates and named policies exactly as given.
- Give most weight to anything touching housing, the private rented sector, \
landlords, tenants, building safety, homelessness, property taxation and \
local authority enforcement.
- Do not name private individuals — constituents, residents, campaigners, \
company employees. Describe them generically. Members of the Senedd, \
ministers, public bodies and organisations may be named.
- Refer to a minister by their title ("The Cabinet Minister said..."), and to \
other speakers by full name followed by MS ("Francesca O'Brien MS asked...").
- "line" is ONE sentence of no more than 35 words saying what was raised or \
announced that matters to the NRLA.
- Summarise EVERY numbered contribution, including political exchanges and \
contributions that are not about housing. Be brief: each summary is ONE \
sentence of no more than 30 words, giving only the speaker's main point or \
question and any commitment or figure that matters to the NRLA. A \
contribution marked (opening statement) may have up to two sentences and 50 \
words. Leave out background, examples, anecdotes, thanks and rhetoric. Never \
copy a speech out. The only contribution that may have an empty summary is \
one that is nothing but thanks or procedure ("Thank you, Llywydd", calling \
the next speaker).
- Write a figure either as the speaker said it or as the same number in \
digits ("seventy per cent" or "70 per cent"). Never work out a new figure: no \
totals, differences or percentages the speaker did not give.

Reply with JSON only, no prose around it."""

DEBATE_SHAPE = ('{"line": "...", "points": [{"n": 1, "summary": "..."}, '
                '{"n": 2, "summary": "..."}]}')
NOTICE_SHAPE = '{"line": "...", "summary": "..."}'


@dataclass
class Usage:
    model: str = DEFAULT_MODEL
    calls: int = 0
    failures: int = 0
    rejected: int = 0            # summaries dropped by the figures check
    retried: int = 0             # contributions asked for a second time
    too_long: int = 0            # summaries far over length, asked for again
    extracts: int = 0            # still unsummarised: a short extract is shown
    filled: int = 0              # debates: a part the note missed, shown verbatim
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost(self) -> float:
        base = self.model
        price = PRICES.get(base) or next(
            (v for k, v in PRICES.items() if base.startswith(k)), PRICES[DEFAULT_MODEL])
        return (self.input_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000

    def report(self) -> str:
        return (f"AI summaries ({self.model}): {self.calls} call(s), "
                f"{self.input_tokens:,} input + {self.output_tokens:,} output tokens, "
                f"about ${self.cost:.3f}"
                + (f"; {self.failures} item(s) fell back to verbatim" if self.failures else "")
                + (f"; {self.rejected} summar{'y' if self.rejected == 1 else 'ies'} "
                   f"dropped by the figures check" if self.rejected else "")
                + (f"; {self.retried} contribution(s) asked for again" if self.retried else "")
                + (f" ({self.too_long} for being too long)" if self.too_long else "")
                + (f"; {self.extracts} left as a short extract" if self.extracts else "")
                + (f"; {self.filled} part(s) of a debate the AI note missed, shown "
                   f"in the speakers' own words" if self.filled else "")
                + ".")


def enabled() -> tuple[str, str]:
    """(api key, model), or ("", "") when AI summaries are off. The key alone
    switches them on; the repository variable WEEKLY_SUMMARIES_AI=off
    switches them off again without removing the key."""
    if os.environ.get("WEEKLY_SUMMARIES_AI", "").strip().lower() == "off":
        return "", ""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("WEEKLY_SUMMARY_MODEL", "").strip() or DEFAULT_MODEL
    return (key, model) if key else ("", "")


def _words(text: str) -> int:
    return len((text or "").split())


def flat_contributions(entry) -> list[tuple[tuple[int, int], object]]:
    """(key, contribution) for every contribution worth summarising, in
    order. The key (exchange index, contribution index) is how the document
    finds the summary again."""
    out = []
    for i, (_heading, contribs) in enumerate(entry.exchanges):
        for j, c in enumerate(contribs):
            if _words(c.text) >= 3:
                out.append(((i, j), c))
    return out


# A contribution this long must come back summarised; a shorter one with no
# summary ("Thank you, Minister.") is simply printed as it is.
MUST_SUMMARISE_WORDS = 40

# The longest a summary may be, in words (1 October 2026: the directorate
# found three-sentence summaries far too long — the document grew from six
# pages to seven). A longer reply is cut back to whole sentences; one whose
# first sentence alone is far over is asked for again, shorter.
WORDS_CONTRIBUTION = 30
WORDS_OPENING = 50
WORDS_NOTICE = 50

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'\u201d\u2019]?\s+(?=[A-Z\"'\u2018\u201c(])")


def fit(text: str, cap: int) -> str:
    """``text`` cut back to whole sentences within ``cap`` words; the first
    sentence is always kept. "" if even that is far too long (more than half
    as long again), so that it is asked for again."""
    sents = [x.strip() for x in _SENTENCE_END.split(text or "") if x.strip()]
    if not sents:
        return ""
    if _words(sents[0]) > cap * 1.5:
        return ""
    out = [sents[0]]
    for x in sents[1:]:
        if _words(" ".join(out + [x])) > cap:
            break
        out.append(x)
    return " ".join(out)


def _cap(entry, key) -> int:
    return WORDS_OPENING if _is_opening(entry, key) else WORDS_CONTRIBUTION


def _is_opening(entry, key) -> bool:
    """The first contribution of a whole debate or statement, by a minister."""
    if not entry.whole or key != (0, 0) or not entry.exchanges:
        return False
    c = entry.exchanges[0][1][0]
    return bool(re.search(r"minister|trefnydd|counsel general", (c.role or ""), re.I))


def _debate_prompt(entry, flat=None, again: bool = False) -> tuple[str, list]:
    flat = flat_contributions(entry) if flat is None else flat
    budget = MAX_INPUT_CHARS // max(1, len(flat))
    lines = [f"Item: {entry.title}", f"Where and when: {entry.meta}",
             ("These contributions still need a summary. Every one of them must "
              "have one, no longer than the rules allow (one sentence, 30 words at "
              "most): none of them is only thanks or procedure. Leave \"line\" "
              "empty." if again else
              "This is the whole debate." if entry.whole else
              "These are the relevant exchanges from the item: each is a question "
              "or request and the reply to it."), ""]
    last = None
    for n, (key, c) in enumerate(flat, 1):
        heading = entry.exchanges[key[0]][0]
        if heading and heading != last:
            lines.append(f"[Question: {heading}]")
            last = heading
        who = (f"{c.speaker} MS" if getattr(c, "member", False) else c.speaker) + \
            (f" ({c.role})" if c.role else "")
        text = c.text if len(c.text) <= budget else c.text[:budget] + " …"
        mark = " (opening statement)" if _is_opening(entry, key) else ""
        lines.append(f"{n}. {who}{mark}:\n{text}\n")
    lines.append(f"Reply in exactly this shape: {DEBATE_SHAPE}")
    return "\n".join(lines), flat


def _notice_prompt(entry) -> tuple[str, str]:
    text = "\n".join(list(entry.points) + list(entry.paragraphs))[:MAX_INPUT_CHARS]
    prompt = (f"Welsh Government {entry.label or 'notice'}: {entry.title}\n"
              f"Published: {entry.meta}\n\n{text}\n\n"
              f"Write \"summary\" in one or two sentences, no more than "
              f"{WORDS_NOTICE} words, on what the notice announces. Reply in exactly "
              f"this shape: {NOTICE_SHAPE}")
    return prompt, text


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _call(prompt: str, api_key: str, model: str, usage: Usage, post) -> dict:
    resp = post(API_URL, timeout=240, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, json={
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    })
    usage.calls += 1
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:300]}")
    body = resp.json()
    u = body.get("usage") or {}
    usage.input_tokens += int(u.get("input_tokens") or 0) + \
        int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
    usage.output_tokens += int(u.get("output_tokens") or 0)
    text = "".join(p.get("text", "") for p in body.get("content", []) if p.get("type") == "text")
    data = _parse_json(text)
    if data is None:
        raise RuntimeError("the reply was not the JSON asked for")
    return data


def summarise_entry(entry, api_key: str, model: str, usage: Usage, post) -> None:
    """Fill ``entry.ai`` with checked summaries; leave it empty on failure."""
    if entry.kind == "notice":
        prompt, source = _notice_prompt(entry)
        data = _call(prompt, api_key, model, usage, post)
        line, summary = _clean(data.get("line")), _clean(data.get("summary"))
        ai = {}
        summary = fit(summary, WORDS_NOTICE) or summary
        for key, value in (("line", line), ("notice", summary)):
            if value and figures_check(value, f"{entry.title}\n{source}"):
                ai[key] = value
            elif value:
                usage.rejected += 1
        entry.ai = ai
        return

    prompt, flat = _debate_prompt(entry)
    if not flat:
        return
    data = _call(prompt, api_key, model, usage, post)
    ai = {"by": {}}
    _take(entry, data, flat, ai["by"], usage)
    # Anything of substance that came back empty, or whose summary failed the
    # figures check, is asked for once more on its own (29 September 2026:
    # Francesca O'Brien MS's building safety speech came back with "70%"
    # where she said "Seventy per cent", and the FMQs exchanges came back
    # empty, so the document printed them in full).
    missing = [(key, c) for key, c in flat
               if key not in ai["by"] and _words(c.text) >= MUST_SUMMARISE_WORDS]
    if missing:
        usage.retried += len(missing)
        try:
            again, _ = _debate_prompt(entry, missing, again=True)
            _take(entry, _call(again, api_key, model, usage, post), missing, ai["by"],
                  usage, last_try=True)
        except Exception as exc:            # noqa: BLE001 — the first pass stands
            print(f"  second AI pass failed for {entry.title[:60]!r}: {exc}")
    usage.extracts += sum(1 for key, c in flat
                          if key not in ai["by"] and _words(c.text) >= MUST_SUMMARISE_WORDS)
    line = _clean(data.get("line"))
    everything = "\n".join([entry.title] + [c.text for _k, c in flat])
    if line and figures_check(line, everything):
        ai["line"] = line
    elif line:
        usage.rejected += 1
    entry.ai = ai


def _take(entry, data: dict, flat: list, by: dict, usage: Usage,
          last_try: bool = False) -> None:
    """Put the checked summaries from a reply into ``by``, cut to length."""
    by_n = {}
    for p in data.get("points", []) or []:
        try:
            n = int(p.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= n <= len(flat):
            by_n[n] = _clean(p.get("summary"))
    for n, (key, c) in enumerate(flat, 1):
        s = by_n.get(n, "")
        if not s:
            continue
        if not figures_check(s, c.text):
            usage.rejected += 1
            continue
        short = fit(s, _cap(entry, key))
        if not short and last_try:
            # Still one over-long sentence after asking again: keep it,
            # rather than fall back to the speaker's own words.
            short = s
        if short:
            by[key] = short
        else:
            usage.too_long += 1


def summarise_review(review, api_key: str, model: str = "", post=None) -> Usage:
    """Summarise every entry of the week in review, in place."""
    import requests

    post = post or requests.post
    usage = Usage(model=model or DEFAULT_MODEL)
    for entry in review.entries:
        for attempt in (1, 2):              # a passing API error is tried again
            try:
                summarise_entry(entry, api_key, usage.model, usage, post)
                break
            except Exception as exc:        # noqa: BLE001 — any failure falls back
                entry.ai = {}
                if attempt == 2:
                    usage.failures += 1
                    print(f"  AI summary unavailable for {entry.title[:60]!r}: {exc}")
    review.ai_model = usage.model if any(e.ai for e in review.entries) else ""
    return usage

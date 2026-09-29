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
    had been too slow...");
  * for a press release or written statement, a short summary of the notice.

The directorate's choices, 29 September 2026: everything summarised, in the
email and the document; Claude Sonnet 5.

SAFEGUARDS
----------
  * Only the text sent is used; the instructions forbid adding anything.
  * Every summary is checked mechanically: if it contains a figure the
    speaker did not say, it is thrown away and the verbatim extract is used
    for that contribution instead (the same check as the debate summaries).
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
- For contributions: one to three sentences each; up to five for a \
minister's opening statement. Give an empty summary for a contribution that \
is only thanks or procedure.

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


def _debate_prompt(entry) -> tuple[str, list]:
    flat = flat_contributions(entry)
    budget = MAX_INPUT_CHARS // max(1, len(flat))
    lines = [f"Item: {entry.title}", f"Where and when: {entry.meta}",
             ("This is the whole debate." if entry.whole else
              "These are the relevant exchanges from the item: each is a question "
              "or request and the reply to it."), ""]
    last = None
    for n, ((i, _j), c) in enumerate(flat, 1):
        heading = entry.exchanges[i][0]
        if heading and heading != last:
            lines.append(f"[Question: {heading}]")
            last = heading
        who = (f"{c.speaker} MS" if getattr(c, "member", False) else c.speaker) + \
            (f" ({c.role})" if c.role else "")
        text = c.text if len(c.text) <= budget else c.text[:budget] + " …"
        lines.append(f"{n}. {who}:\n{text}\n")
    lines.append(f"Reply in exactly this shape: {DEBATE_SHAPE}")
    return "\n".join(lines), flat


def _notice_prompt(entry) -> tuple[str, str]:
    text = "\n".join(list(entry.points) + list(entry.paragraphs))[:MAX_INPUT_CHARS]
    prompt = (f"Welsh Government {entry.label or 'notice'}: {entry.title}\n"
              f"Published: {entry.meta}\n\n{text}\n\n"
              f"Write \"summary\" as a short paragraph (up to five sentences) on what "
              f"the notice announces. Reply in exactly this shape: {NOTICE_SHAPE}")
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
    by_n = {}
    for p in data.get("points", []) or []:
        try:
            n = int(p.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(flat):
            by_n[n] = _clean(p.get("summary"))
    ai = {"by": {}}
    for n, (key, c) in enumerate(flat, 1):
        s = by_n.get(n, "")
        if not s:
            continue
        if figures_check(s, c.text):
            ai["by"][key] = s
        else:
            usage.rejected += 1
    line = _clean(data.get("line"))
    everything = "\n".join([entry.title] + [c.text for _k, c in flat])
    if line and figures_check(line, everything):
        ai["line"] = line
    elif line:
        usage.rejected += 1
    entry.ai = ai


def summarise_review(review, api_key: str, model: str = "", post=None) -> Usage:
    """Summarise every entry of the week in review, in place."""
    import requests

    post = post or requests.post
    usage = Usage(model=model or DEFAULT_MODEL)
    for entry in review.entries:
        try:
            summarise_entry(entry, api_key, usage.model, usage, post)
        except Exception as exc:            # noqa: BLE001 — any failure falls back
            usage.failures += 1
            entry.ai = {}
            print(f"  AI summary unavailable for {entry.title[:60]!r}: {exc}")
    review.ai_model = usage.model if any(e.ai for e in review.entries) else ""
    return usage

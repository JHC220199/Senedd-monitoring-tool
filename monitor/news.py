"""Political news alerts — an email when something big happens.

WHAT THIS REPLACES
------------------
Camlas's "breaking news" emails: a defection, a leader standing down, a new
shadow cabinet. They are rare, and they matter because they change who holds
which brief and how the numbers fall in the Senedd.

WHAT COUNTS
-----------
Political CHANGES only — the directorate's choice (24 September 2026):
leadership changes, defections, resignations, appointments, reshuffles,
shadow cabinets, suspensions and the whip, by-elections, votes of no
confidence. A story about an MS's private life is not an alert unless it
leads to one of those. A headline must:

  * contain a change word (TRIGGERS), and
  * be about the Senedd or the Welsh Government (CONTEXT), and
  * not be a council story, a Westminster story, or a feature (EXCLUDE).

Several outlets report the same event, and each outlet runs follow-ups. So
headlines are grouped by the people they name, and an event already alerted
on is not alerted again for three days unless the KIND of change is new
("Helen Jenner elected leader" then "Helen Jenner names shadow cabinet" are
two alerts; three outlets' versions of the first are one).

WHAT AN ALERT CONTAINS
----------------------
The outlets' own headlines, each linked, with the time. Nothing else: no
article text, no summary. Where a story is about an allegation, the alert
names someone only if the headline does — the directorate's choice.

Separately, the Welsh Government's own ministers page is compared with the
copy from the last run; any change is an alert, with the before and after.

TIMING
------
Started every hour, 08.00–18.00 on weekdays, by a Power Automate recurrence
(NEWS-ALERTS-SETUP.md) — GitHub's own timer runs hours late. News that
breaks overnight or at the weekend is caught by the first run after it.
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .collectors.news import Headline
from .forward import (DARK_BLUE, EDGE, FONT, LINE, MUTED, OFF_BLACK, OFF_WHITE,
                      ORANGE, WIDTH)


STATE_PATH = "data/news-state.json"

# A headline older than this is not news any more, however it was missed.
MAX_AGE = timedelta(days=4)

# An event already alerted on is not alerted again within this time.
REPEAT_WINDOW = timedelta(days=3)

SEEN_DAYS = 30


# (kind, label, pattern) — first match wins, so the more specific come first.
TRIGGERS: list[tuple[str, str, re.Pattern]] = [(k, l, re.compile(p, re.I)) for k, l, p in [
    ("defection", "Defection",
     r"\bdefect(s|ed|ing)?\b|\bcross(es|ed)? the floor\b|"
     r"\b(joins|joined|quits|quit|leaves|left)\b.{0,40}\b(Plaid|Labour|Reform|"
     r"Conservatives?|Tories|Lib ?Dems?|Liberal Democrats|Greens?|the party)\b"),
    ("shadow_cabinet", "Shadow cabinet",
     r"\bshadow cabinet\b|\bfront ?bench\b|\bshadow (housing|finance|health) "
     r"(minister|secretary|spokesperson)\b"),
    ("reshuffle", "Reshuffle", r"\breshuffle\b"),
    ("resignation", "Resignation",
     r"\bresign(s|ed|ing|ation)?\b|\bstands? down\b|\bstood down\b|"
     r"\bsteps? down\b|\bstepped down\b|\bquits\b|\bsacked\b|\bdismissed\b"),
    ("leadership", "Leadership",
     r"\bnew (deputy )?leader\b|\bleadership (contest|election|race|challenge|bid)\b|"
     r"\b(elected|named|unveiled|confirmed|chosen|becomes)\b.{0,60}\b(deputy )?leader\b"),
    ("appointment", "Appointment",
     r"\b(appointed|appoints|named|names|unveils?|confirmed)\b.{0,60}"
     r"\b(minister|spokesperson|chair|whip|counsel general|trefnydd|llywydd|"
     r"presiding officer|cabinet)\b|\bnew (cabinet )?minister\b"),
    ("suspension", "Suspension",
     r"\bsuspend(s|ed)?\b|\bsuspension\b|\b(loses|lost|withdrawn|removed|"
     r"restored)\b.{0,20}\bwhip\b|\bwhip\b.{0,20}\b(withdrawn|removed|restored|suspended)\b"),
    ("by_election", "By-election", r"\bby-?election\b"),
    ("confidence", "Confidence vote", r"\bno[- ]confidence\b|\bconfidence vote\b"),
    ("agreement", "Agreement between parties",
     r"\bcoalition\b|\bco-?operation agreement\b|\bbudget deal\b"),
    ("death", "Death", r"\b(dies|died|death of)\b"),
]]

# About the Senedd or the Welsh Government.
_SENEDD = re.compile(
    r"\b(Senedd|MSs?|Welsh Government|First Minister|Deputy First Minister|"
    r"Trefnydd|Llywydd|Counsel General|Cabinet Minister|Plaid( Cymru)?|"
    r"Welsh (Labour|Conservatives?|Tories|Lib ?Dems?|Liberal Democrats|Greens?|"
    r"Green Party|leader)|Wales Green Party|Reform (UK )?Wales)\b", re.I)
_WALES = re.compile(r"\b(Wales|Welsh|Cymru)\b", re.I)
_PARTY = re.compile(r"\b(Reform|Plaid|Labour|Conservatives?|Tories|Lib ?Dems?|"
                    r"Liberal Democrats|Greens?)\b", re.I)

EXCLUDE = re.compile(
    r"\bcouncil(lor|lors|s)?\b|\bcouncil leader\b|"
    r"^(who is|what we learned|analysis|opinion|comment|explained|watch|live)\b|"
    r"\bthe making of\b|\bin full\b|\bQ&A\b|\bexplained\b|\bprofile\b", re.I)
_WESTMINSTER = re.compile(r"\b(MPs?|Westminster|House of Commons|Downing Street)\b")

# Words that make an alert NRLA business in its own right.
HOUSING = re.compile(r"\b(housing|homes?|renters?|renting|landlords?|tenants?|"
                     r"homelessness|building safety|leaseholders?)\b", re.I)

# Capitalised runs that look like names, for grouping headlines by person.
_WORD = r"[A-Z][a-zà-ÿ'’]+(?:-[A-Z][a-zà-ÿ'’]+)?"
_NAME = re.compile(rf"\b({_WORD}(?:\s+(?:ap\s+|ab\s+)?{_WORD}){{1,2}})")

_PARTIES = {
    "reform": r"\bReform\b", "plaid": r"\bPlaid\b",
    "labour": r"\bLabour\b", "conservative": r"\b(Conservatives?|Tories|Tory)\b",
    "libdem": r"\b(Lib ?Dems?|Liberal Democrats?)\b", "green": r"\bGreens?\b|\bGreen Party\b",
}

# Two headlines naming no one in common are still one event when they are
# the same kind of change, about the same party, this close together:
# "Helen Jenner unveiled as new leader" and "Reform UK announces new leader".
SAME_EVENT = timedelta(hours=12)
_NOT_NAMES = {
    "reform uk", "plaid cymru", "welsh government", "first minister", "welsh labour",
    "welsh conservatives", "lib dems", "liberal democrats", "green party",
    "nigel farage", "senedd cymru", "prime minister", "deputy first minister",
    "cabinet minister", "shadow cabinet", "news wales", "bbc news", "wales green party",
    "welsh green party", "new ambition", "stands down", "steps down",
}


@dataclass
class Match:
    headline: Headline
    kind: str
    label: str
    names: set[str]
    housing: bool
    parties: set[str] = field(default_factory=set)


@dataclass
class Story:
    kind: str
    label: str
    names: set[str]
    matches: list[Match] = field(default_factory=list)
    parties: set[str] = field(default_factory=set)

    @property
    def first(self) -> Headline:
        return sorted(self.matches, key=lambda m: m.headline.at or datetime.max)[0].headline

    @property
    def housing(self) -> bool:
        return any(m.housing for m in self.matches)


@dataclass
class MinisterChange:
    before: dict[str, str]
    after: dict[str, str]

    @property
    def lines(self) -> list[tuple[str, str, str]]:
        """``(name, was, now)`` — '' for joined or left."""
        out = []
        for name in sorted(set(self.before) | set(self.after)):
            was, now = self.before.get(name, ""), self.after.get(name, "")
            if was != now:
                out.append((name, was, now))
        return out

    @property
    def housing(self) -> bool:
        return any(HOUSING.search(f"{w} {n}") for _, w, n in self.lines)


def names_in(text: str) -> set[str]:
    out = set()
    for m in _NAME.finditer(text or ""):
        name = m.group(1).strip()
        if name.lower() in _NOT_NAMES or len(name) < 6:
            continue
        out.add(name.lower())
    return out


def parties_in(text: str) -> set[str]:
    return {k for k, p in _PARTIES.items() if re.search(p, text or "", re.I)}


def _same_event(names_a: set, parties_a: set, at_a, names_b: set, parties_b: set, at_b) -> bool:
    if names_a & names_b:
        return True
    if names_a and names_b:
        return False            # two different people named
    if not (parties_a & parties_b) and (parties_a or parties_b):
        return False
    if at_a is None or at_b is None:
        return True
    return abs(at_a - at_b) <= SAME_EVENT


def classify(h: Headline) -> Match | None:
    """Is this headline a political change in Wales? ``None`` if not."""
    title = h.title
    context = f"{title} {h.standfirst}"
    if EXCLUDE.search(title) and not re.search(r"\b(Senedd|MS)\b", title):
        return None
    if _WESTMINSTER.search(title) and not re.search(r"\b(Senedd|MSs?)\b", title):
        return None
    if not (_SENEDD.search(context) or (_WALES.search(context) and _PARTY.search(context))):
        return None
    for kind, label, pattern in TRIGGERS:
        if pattern.search(title):
            return Match(headline=h, kind=kind, label=label, names=names_in(title),
                         housing=bool(HOUSING.search(title)),
                         parties=parties_in(title))
    return None


def group(matches: list[Match]) -> list[Story]:
    """One story per event: the same kind of change, and the same person —
    or, where a headline names no one, the same party within 12 hours."""
    stories: list[Story] = []
    for m in sorted(matches, key=lambda m: m.headline.at or datetime.max):
        home = None
        for s in stories:
            if s.kind == m.kind and any(
                    _same_event(m.names, m.parties, m.headline.at,
                                x.names, x.parties, x.headline.at) for x in s.matches):
                home = s
                break
        if home is None:
            home = Story(kind=m.kind, label=m.label, names=set())
            stories.append(home)
        home.matches.append(m)
        home.names |= m.names
        home.parties |= m.parties
    return stories


def new_stories(headlines: list[Headline], state: dict, now: datetime) -> list[Story]:
    """Stories not alerted on before, from headlines not seen before."""
    seen = state.get("seen", {})
    fresh = [h for h in headlines
             if h.url not in seen and (h.at is None or now - h.at <= MAX_AGE)]
    matches = [m for m in (classify(h) for h in fresh) if m]
    out = []
    for story in group(matches):
        repeat = False
        for past in state.get("alerts", []):
            try:
                when = datetime.fromisoformat(past["at"])
            except (KeyError, ValueError):
                continue
            if now - when > REPEAT_WINDOW or past.get("kind") != story.kind:
                continue
            if _same_event(story.names, story.parties, None,
                           set(past.get("names", [])), set(past.get("parties", [])), None):
                repeat = True
                break
        if not repeat:
            out.append(story)
    return out


def remember(state: dict, headlines: list[Headline], stories: list[Story],
             ministers: dict[str, str] | None, now: datetime) -> dict:
    seen = state.setdefault("seen", {})
    for h in headlines:
        seen.setdefault(h.url, now.isoformat(timespec="seconds"))
    cutoff = now - timedelta(days=SEEN_DAYS)
    state["seen"] = {u: t for u, t in seen.items()
                     if _parse(t) is None or _parse(t) >= cutoff}
    alerts = [a for a in state.get("alerts", [])
              if (_parse(a.get("at", "")) or now) >= cutoff]
    for s in stories:
        alerts.append({"at": now.isoformat(timespec="seconds"), "kind": s.kind,
                       "names": sorted(s.names), "parties": sorted(s.parties),
                       "title": s.first.title})
    state["alerts"] = alerts
    if ministers is not None:
        state["ministers"] = ministers
    state.setdefault("initialised", now.isoformat(timespec="seconds"))
    return state


def _parse(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def minister_change(state: dict, ministers: dict[str, str] | None) -> MinisterChange | None:
    before = state.get("ministers")
    if not before or ministers is None or before == ministers:
        return None
    change = MinisterChange(before=before, after=ministers)
    return change if change.lines else None


def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict, path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------------

def _e(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _tag(text: str, bg: str = "#FDEDE0", fg: str = "#A8501A") -> str:
    return (f'<span style="background:{bg};color:{fg};font-size:10px;font-weight:700;'
            f'letter-spacing:.6px;padding:2px 6px;margin-right:7px;'
            f'text-transform:uppercase">{_e(text)}</span>')


def _when(h: Headline) -> str:
    if h.at is None:
        return ""
    from .morning import LONDON
    from datetime import timezone
    return h.at.replace(tzinfo=timezone.utc).astimezone(LONDON).strftime("%a %-d %b, %H.%M")


def _story_card(s: Story) -> str:
    rows = []
    for i, m in enumerate(sorted(s.matches, key=lambda m: m.headline.at or datetime.max)):
        h = m.headline
        size = "15px" if i == 0 else "13.5px"
        weight = 700 if i == 0 else 600
        rows.append(
            f'<tr><td style="padding:{12 if i == 0 else 8}px 18px;font-family:{FONT};'
            f'border-bottom:1px solid {LINE}">'
            f'<div style="font-size:{size};line-height:1.45">'
            f'<a href="{_e(h.url)}" style="color:{OFF_BLACK if i == 0 else DARK_BLUE};'
            f'font-weight:{weight};text-decoration:none">{_e(h.title)}</a></div>'
            f'<div style="font-size:11.5px;color:{MUTED};padding-top:3px">'
            f'{_e(h.outlet)}{" · " + _e(_when(h)) if h.at else ""}</div></td></tr>')
    head = (f'<tr><td style="padding:12px 18px 0;font-family:{FONT}">'
            + (_tag("NRLA") if s.housing else "")
            + _tag(s.label, "#E8EEF2", DARK_BLUE) + '</td></tr>')
    return _card(head + "".join(rows))


def _ministers_card(c: MinisterChange) -> str:
    rows = []
    for name, was, now in c.lines:
        change = (f"was {was}; now {now}" if was and now else
                  f"now {now}" if now else f"no longer listed (was {was})")
        rows.append(f'<tr><td style="padding:8px 18px;font-family:{FONT};'
                    f'border-bottom:1px solid {LINE};font-size:13.5px;color:{OFF_BLACK}">'
                    f'<b>{_e(name)}</b> — {_e(change)}</td></tr>')
    head = (f'<tr><td style="padding:12px 18px 6px;font-family:{FONT}">'
            + (_tag("NRLA") if c.housing else "")
            + _tag("Welsh Government ministers", "#E8EEF2", DARK_BLUE)
            + f'<div style="font-size:15px;font-weight:700;color:{OFF_BLACK};padding-top:8px">'
            f'The list of ministers on gov.wales has changed</div></td></tr>')
    foot = (f'<tr><td style="padding:10px 18px;font-family:{FONT};font-size:12px;'
            f'color:{MUTED}"><a href="https://www.gov.wales/cabinet-ministers-and-deputy-ministers" '
            f'style="color:{DARK_BLUE};font-weight:600">The ministers page</a>'
            + (". A housing portfolio has changed: the tool's list of key people "
               "(config/taxonomy.yaml, entities) needs the new name." if c.housing else "")
            + '</td></tr>')
    return _card(head + "".join(rows) + foot)


def _card(body: str) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="border-collapse:collapse;width:100%;background:#ffffff;'
            f'border:1px solid {EDGE}">{body}</table>'
            f'<div style="height:16px;line-height:16px;font-size:0">&nbsp;</div>')


def test_stories(headlines: list[Headline], now: datetime, limit: int = 3) -> list[Story]:
    """For a test email: the latest political changes in the feeds, whether
    or not they have been alerted on already. Nothing is remembered."""
    recent = [h for h in headlines if h.at is None or now - h.at <= timedelta(days=14)]
    stories = group([m for m in (classify(h) for h in recent) if m])
    stories.sort(key=lambda s: s.first.at or datetime.min, reverse=True)
    return stories[:limit]


def render_news(stories: list[Story], change: MinisterChange | None,
                test: bool = False) -> tuple[str, str, int]:
    count = len(stories) + (1 if change else 0)
    if not count:
        return "", "", 0
    if stories:
        lead = stories[0].first.title
    else:
        lead = "Welsh Government ministers have changed"
    more = count - 1
    subject = (("TEST — " if test else "") + f"Senedd news: {lead}"
               + (f" (+{more} more)" if more else ""))
    banner = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
              f'border="0" style="border-collapse:collapse;background:#FFF4E5;'
              f'border:1px solid #F3C98B"><tr><td style="padding:12px 16px;font-family:{FONT};'
              f'font-size:13px;line-height:1.5;color:#8A3B06"><b>This is a test.</b> '
              f'It shows the most recent political changes in the news feeds, to '
              f'prove the alert reaches you; they may already be old news. Real '
              f'alerts carry no banner and only ever contain something new.</td></tr>'
              f'</table><div style="height:16px;line-height:16px;font-size:0">&nbsp;</div>'
              ) if test else ""
    cards = banner + "".join(_story_card(s) for s in stories) + (
        _ministers_card(change) if change else "")
    body = f"""<table role="presentation" width="100%" cellpadding="0" \
cellspacing="0" border="0" style="border-collapse:collapse;background:{OFF_WHITE}">
<tr><td align="center" style="padding:0">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" \
border="0" style="border-collapse:collapse;width:{WIDTH}px;max-width:{WIDTH}px">
  <tr><td bgcolor="{DARK_BLUE}" style="padding:20px 28px 16px;font-family:{FONT};color:#ffffff">
    <div style="font-size:19px;font-weight:700;color:#ffffff">Senedd news alert</div>
    <div style="font-size:13px;color:#C3D2DC;padding-top:4px">
      National Residential Landlords Association</div>
  </td></tr>
  <tr><td bgcolor="{ORANGE}" height="4" style="height:4px;line-height:4px;font-size:0">&nbsp;</td></tr>
  <tr><td style="padding:20px 28px 0;font-family:{FONT}">{cards}</td></tr>
  <tr><td style="padding:0 28px 30px;font-family:{FONT}">
    <p style="font-size:11.5px;color:{MUTED};line-height:1.55;margin:0;padding-top:4px">
      Headlines are each outlet's own, linked to the story; nothing is copied
      or summarised. Sources: BBC News Wales, Nation.Cymru, WalesOnline, and
      the Welsh Government's list of ministers (Open Government Licence v3.0).
      Only political changes are alerted: leadership, defections,
      resignations, appointments, reshuffles, suspensions, by-elections.
    </p>
  </td></tr>
</table>
</td></tr></table>"""
    return subject, body, count

"""Statements of Opinion, read from the Senedd's Record website.

WHAT THEY ARE
-------------
The Senedd's nearest equivalent of Westminster's early day motions: a short
statement tabled by a Member ("This Senedd: 1. Recognises… 2. Calls on…")
that other Members can sign ("subscribe to"). Each has a reference such as
OPIN-2026-0544 and a page of its own:

    https://record.senedd.wales/StatementOfOpinion/544

There is no list or feed of them. The pages are numbered in sequence, so new
ones are found by reading the next numbers after the last one seen. A number
not yet used returns a page with no statement on it (not an error), which is
how the end is recognised. Verified 29 September 2026: 540 ("Remembering
Aberfan") and 544 ("Local Housing Allowance", tabled that day) existed; 545
onwards were empty.

WHAT IS READ
------------
The reference and title, the date tabled, the text, who tabled it (name and
constituency) and who has signed it, each with the date they signed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from bs4 import BeautifulSoup

from .base import Collector

RECORD_BASE = "https://record.senedd.wales"

_REF = re.compile(r"\bOPIN-(\d{4})-(\d+)\b")
_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


@dataclass
class Member:
    name: str
    constituency: str = ""
    signed: date | None = None


@dataclass
class Opinion:
    number: int
    reference: str               # "OPIN-2026-0544"
    title: str                   # "Local Housing Allowance"
    tabled: date | None
    text: str                    # the statement, one numbered point per line
    tabled_by: Member | None
    supporters: list[Member] = field(default_factory=list)

    @property
    def url(self) -> str:
        return opinion_url(self.number)

    @property
    def points(self) -> list[str]:
        return [p for p in self.text.split("\n") if p.strip()]


def opinion_url(number: int) -> str:
    return f"{RECORD_BASE}/StatementOfOpinion/{number}"


def _date(text: str) -> date | None:
    m = _DATE.search(text or "")
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def parse_opinion(html: str, number: int) -> Opinion | None:
    """The statement on one page, or None if the number is not used yet."""
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one("main") or soup.body
    if main is None:
        return None
    lines = [_clean(x) for x in main.get_text("\n").split("\n")]
    lines = [x for x in lines if x]
    head = next((i for i, x in enumerate(lines) if _REF.search(x)), None)
    if head is None:
        return None
    m = _REF.search(lines[head])
    reference = m.group(0)
    title = _clean(lines[head][m.end():]).lstrip("-–: ")

    tabled = None
    body: list[str] = []
    section = "body"
    i = head + 1
    while i < len(lines):
        x = lines[i]
        low = x.lower()
        if low in ("(e)", "(w)", "(c)"):
            pass
        elif low.startswith("tabled on"):
            tabled = _date(x)
        elif low == "tabled by":
            section = "tabled"
            break
        elif low == "this senedd:":
            body.append(x)
        elif section == "body":
            body.append(x)
        i += 1

    # The people, from the markup: each .memberBar has .name, .area (the
    # constituency) and, for those who signed, .dateSupported.
    tabled_by = None
    supporters: list[Member] = []
    for sec in main.select(".itemContent__supporter-section"):
        heading = _clean((sec.select_one("h3") or sec).get_text(" ")).lower()
        people = []
        for bar in sec.select(".memberBar"):
            name = bar.select_one(".name")
            if name is None or not _clean(name.get_text(" ")):
                continue
            area = bar.select_one(".area")
            when = bar.select_one(".dateSupported")
            people.append(Member(name=_clean(name.get_text(" ")),
                                 constituency=_clean(area.get_text(" ")) if area else "",
                                 signed=_date(when.get_text(" ")) if when else None))
        if heading.startswith("tabled"):
            tabled_by = people[0] if people else None
        else:
            supporters.extend(people)

    return Opinion(number=number, reference=reference, title=title, tabled=tabled,
                   text="\n".join(body), tabled_by=tabled_by, supporters=supporters)


class OpinionCollector(Collector):
    name = "statements_of_opinion"
    source_kind = "statement_of_opinion"

    # Numbers are not always used in order (one can be withdrawn before it is
    # published), so a few empty pages in a row, not one, mark the end.
    MISSES_BEFORE_STOP = 4
    MAX_PER_RUN = 40

    def fetch(self, number: int) -> tuple[bool, Opinion | None]:
        """(page was read, the statement or None if the number is unused)."""
        html = self.fetcher.get_text(opinion_url(number))
        if html is None:
            self.note_error(f"Statement of Opinion {number} could not be read "
                            f"({opinion_url(number)}).")
            return False, None
        return True, parse_opinion(html, number)

    def after(self, last: int) -> tuple[list[Opinion], int]:
        """Statements numbered after ``last``, and the new last number.

        Stops at the first page that cannot be read, so a network failure
        never moves the marker past a statement nobody has seen.
        """
        found: list[Opinion] = []
        newest = last
        misses = 0
        n = last
        for _ in range(self.MAX_PER_RUN):
            n += 1
            ok, op = self.fetch(n)
            if not ok:
                break
            if op is None:
                misses += 1
                if misses >= self.MISSES_BEFORE_STOP:
                    break
                continue
            misses = 0
            found.append(op)
            newest = n
        return found, newest

    def before(self, last: int, count: int = 15) -> list[Opinion]:
        """The statements numbered up to ``last``, newest first (for a test)."""
        out = []
        for n in range(last, max(0, last - count), -1):
            ok, op = self.fetch(n)
            if ok and op is not None:
                out.append(op)
        return out


def now_date() -> date:
    return datetime.utcnow().date()

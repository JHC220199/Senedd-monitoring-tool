"""The Senedd's draft Record of Proceedings, read from its web page.

WHY THE WEB PAGE AND NOT THE XML
--------------------------------
The XML export (see record_transcripts.py) is a snapshot, and it is not kept
up to date during the first days. Checked on 23 September 2026 at 18.22:

    Plenary 22 Sep   XML generated 14.49 on the day — First Minister's
                     Questions only, 55 contributions. Still the same file
                     27 hours later.
    Plenary 15 Sep   XML not regenerated until 18 September.

The web page for the same sitting, https://record.senedd.wales/Plenary/16262,
held all seven agenda items and 248 contributions by the same evening, and
fills in close to live during the sitting. A next-morning summary has to read
the web page. The daily collection can carry on using the XML, which catches
up within a few days.

WHAT THE PAGE LOOKS LIKE (verified on Plenary 16262 and Committee 16256)
------------------------------------------------------------------------
Every block is a ``div.itemContent`` with an ``id`` that is also a permalink
anchor (``#C771122``), in the order spoken:

    agendaItem       "7. Statement by the Cabinet Minister for ... "
    subHeading       a question's title, inside a question session
    oralQuestion     the tabled question, as asked
    contribution     anyone speaking
    proceduralText   "The Senedd met ...", "Motion agreed", timings
    motion           a committee's motion to meet in private

Each speaking block has ``.memberDetail .name``, ``.memberDetail .time`` and
``.memberTitle`` (the role, where there is one), then
one or more ``.contributionText`` blocks — one per run of language — each
holding either ``.verbatim.fullWidth`` (spoken in English) or ``.verbatim``
(spoken in Welsh) followed by ``.translation`` (the simultaneous
interpretation into English). Once the bilingual Record is out, English
speeches gain a ``.translation`` into Welsh, so the English side is chosen by
its words — see ``_english``.

A committee's page exists, empty, before its Record is published — the page
returns 200 with no contributions — so "published" means "has contributions".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .base import Collector


RECORD_BASE = "https://record.senedd.wales"


@dataclass
class Contribution:
    anchor: str                 # "C771122"
    speaker: str
    role: str = ""
    time: str = ""              # "18:03:47"
    text: str = ""              # English, paragraphs separated by "\n"
    kind: str = "contribution"  # or "oralQuestion"
    video_url: str = ""
    member: bool = False        # a Member of the Senedd (not a witness)

    @property
    def is_chair(self) -> bool:
        """The Llywydd or a deputy calling the next speaker."""
        return bool(_CHAIR.search(self.role or ""))


@dataclass
class Block:
    """Contributions under one sub-heading (a question), or under none."""
    heading: str = ""
    anchor: str = ""
    contributions: list[Contribution] = field(default_factory=list)


@dataclass
class AgendaItem:
    title: str
    anchor: str = ""
    blocks: list[Block] = field(default_factory=list)

    @property
    def contributions(self) -> list[Contribution]:
        return [c for b in self.blocks for c in b.contributions]

    @property
    def number(self) -> str:
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*[.\s]", self.title)
        return m.group(1) if m else ""

    @property
    def heading_text(self) -> str:
        """The title without its number: "Statement by ..." """
        return re.sub(r"^\s*\d+(?:\.\d+)?\s*\.?\s*", "", self.title).strip()


@dataclass
class Record:
    meeting_id: str
    forum: str                  # "Plenary" or the committee's name
    url: str
    items: list[AgendaItem] = field(default_factory=list)

    @property
    def is_plenary(self) -> bool:
        return self.forum.strip().lower() == "plenary"

    @property
    def published(self) -> bool:
        return any(i.contributions for i in self.items)


_CHAIR = re.compile(r"\b(Llywydd|Presiding Officer|Chair|Cadeirydd)\b", re.I)


def record_url(meeting_id: str, plenary: bool) -> str:
    return f"{RECORD_BASE}/{'Plenary' if plenary else 'Committee'}/{meeting_id}"


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


# Words that mark a passage as English or as Welsh. Only words that are
# common in one language and absent from the other ("a", "i" and "am" are
# both, so they are not here).
_EN_WORDS = frozenset("""the and of to that is for we it this are have with be
was will not which has would they there their what can our been thank you
minister member""".split())
_CY_WORDS = frozenset("""yn y yr mae ac ar gan ei bod hyn wedi ein eich gyda fod
sydd hefyd gyfer ydy oes hynny iawn rydym byddai gael rwy'n rydw diolch
weinidog aelod ond hwn hon""".split())
_WORD = re.compile(r"[a-zA-Z\u00C0-\u017F'’]+")


def _englishness(text: str) -> int:
    """English marker words minus Welsh ones: above 0 reads as English."""
    words = [w.lower().replace("’", "'") for w in _WORD.findall(text or "")]
    return (sum(w in _EN_WORDS for w in words)
            - sum(w in _CY_WORDS for w in words))


def _paras(target) -> str:
    paras = target.find_all("p")
    if paras:
        return _clean("\n".join(p.get_text(" ", strip=True) for p in paras))
    return _clean(target.get_text(" ", strip=True))


def _english(node) -> str:
    """English text of a ``.contributionText``.

    The page takes two forms. While the Record is a draft, words spoken in
    English are a lone ``.verbatim.fullWidth``, and words spoken in Welsh are
    a ``.verbatim`` (Welsh) followed by a ``.translation`` (English). Once the
    bilingual Record is published — within a few days — every block has both,
    the language spoken on the left and a translation into the OTHER language
    on the right, so for an English speech the ``.translation`` is Welsh.
    Nothing in the markup says which language is which (checked on Plenary
    16262, 25 September 2026: 336 blocks, every one ``verbatim`` +
    ``translation``), so the words decide.

    Taking the translation regardless read the Business Statement of
    22 September in Welsh, and John Clark's question on HMOs and Carmelo
    Colasanto's on housing delivery were missed by the weekly review.
    """
    if node is None:
        return ""
    verbatim = node.select_one(".verbatim")
    translation = node.select_one(".translation")
    if verbatim is None and translation is None:
        return ""
    if verbatim is None or translation is None:
        return _paras(verbatim if translation is None else translation)
    spoken, other = _paras(verbatim), _paras(translation)
    if _englishness(spoken) > _englishness(other):
        return spoken
    return other


def parse_record(html: str, meeting_id: str, forum: str, url: str = "") -> Record:
    soup = BeautifulSoup(html or "", "html.parser")
    record = Record(meeting_id=meeting_id, forum=forum,
                    url=url or record_url(meeting_id, forum.lower() == "plenary"))
    item: AgendaItem | None = None

    for node in soup.select("div.itemContent"):
        classes = set(node.get("class") or [])
        anchor = node.get("id", "")

        if "agendaItem" in classes:
            title = _english(node.select_one(".contributionText"))
            item = AgendaItem(title=title.split("\n")[0], anchor=anchor,
                              blocks=[Block()])
            record.items.append(item)
            continue

        if item is None:
            continue            # the preamble before the first agenda item

        if "subHeading" in classes:
            item.blocks.append(Block(
                heading=_english(node.select_one(".contributionText")).split("\n")[0],
                anchor=anchor))
            continue

        if not classes & {"contribution", "oralQuestion"}:
            continue            # procedural text, motions

        name = node.select_one(".memberDetail .name")
        if name is None:
            continue
        role = node.select_one(".memberTitle")
        when = node.select_one(".memberDetail .time")
        video = node.select_one("a.seneddTV[href]")
        link = node.select_one(".memberBar a[href]")
        # A contribution is split into one direct .contributionText child per
        # run of language: a minister who answers in Welsh, switches to
        # English for the figures and back to Welsh has three. Reading only
        # the first lost most of the words (22 September 2026, the Building
        # Safety statement: 2,265 characters read of 10,769).
        text = "\n".join(filter(None, (
            _english(chunk) for chunk in
            node.find_all("div", class_="contributionText", recursive=False))))
        if not text:
            continue
        item.blocks[-1].contributions.append(Contribution(
            anchor=anchor,
            speaker=_clean(name.get_text(" ", strip=True)),
            role=_clean(role.get_text(" ", strip=True)) if role else "",
            time=when.get_text(strip=True) if when else "",
            text=text,
            kind="oralQuestion" if "oralQuestion" in classes else "contribution",
            video_url=(video["href"].replace("http://", "https://", 1)
                       if video else ""),
            member="mgUserInfo" in (link.get("href", "") if link else ""),
        ))

    # The page prints a speaker's role on their first contribution of an item
    # and leaves it off after that. Carry it forward, so a minister replying
    # for the third time is still labelled as the minister.
    roles: dict[str, str] = {}
    for it in record.items:
        it.blocks = [b for b in it.blocks if b.contributions or b.heading]
        for c in it.contributions:
            if c.role:
                roles[c.speaker] = c.role
            elif c.speaker in roles:
                c.role = roles[c.speaker]
    return record


class RecordPageCollector(Collector):
    """Fetches one meeting's draft Record page."""

    name = "senedd_record_pages"

    def record(self, meeting_id: str, forum: str) -> Record | None:
        url = record_url(meeting_id, forum.strip().lower() == "plenary")
        html = self.fetcher.get_text(url)
        if html is None:
            self.note_error(f"the Record page for {forum} ({url}) could not be fetched")
            return None
        return parse_record(html, meeting_id, forum, url)

    def collect(self):          # not used by the pipeline; see monitor/debates.py
        return iter(())

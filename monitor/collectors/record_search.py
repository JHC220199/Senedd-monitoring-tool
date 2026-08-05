"""Tabled business from the Record search: written questions and more.

Written questions are disproportionately valuable for an organisation like the
NRLA. They are cheap for an MS to table, so they are where an interest first
surfaces — often months before a debate. The Camlas briefing carries them under
"Written Questions for Future Answer" and "Answers to Written Questions", and
they are frequently the earliest warning of a policy direction.

Verified behaviour (4 August 2026)
----------------------------------
The search form POSTs to ``/Search`` and immediately 302-redirects to a
canonical GET URL with **lowercase** parameters and **ISO** dates:

    https://record.senedd.wales/Search/
        ?query=rent&type=7&start=2026-05-01&end=2026-08-04

Getting this right matters: the title-case/UK-date form that the HTML form
advertises returns an empty, JavaScript-populated shell, whereas the canonical
lowercase form renders complete results server-side. This was only discoverable
by watching where the POST redirected to.

``type`` values (read from the form's own checkboxes):

    -1  All                 6  Speeches
     7  Written Question    1  Transcripts
     4  Oral Question       2  Statement of Opinion
     3  Motions/Amendments  8  QNR
                           13  Emergency Question / questions not reached

Result markup
-------------
Each result is a self-contained block, and the class names are camelCase — a
detail that cost a debugging cycle, because a lowercase-only class regex
concluded there was no structure at all and fell back to scraping flattened
page text. That fallback silently ingested the page footer and the member
filter dropdown into every item, which made an unrelated written question about
an agricultural loan scheme score as Critical housing business. Parsing the
real containers fixes that at source:

    <div class="searchResult daiCorner writtenQuestion">
      <a href="../WrittenQuestion/99808" class="detail">      <-- real permalink
        <span class="title">Written Question - WQ99808</span>
        <span class="subTitle">Tabled on 16/07/2026 for answer on 23/07/2026</span>
        <div class="context">Does the Welsh Government have plans to bring in
             <span class='highlightedText'>rent</span> controls?</div>
      </a>
      <div class="memberBar">
        <a href="https://business.senedd.wales/mgUserInfo.aspx?UID=12147">
          <span class="name">Dan Thomas</span>
          <span class="area">Casnewydd Islwyn</span>
    </div></div>

Two useful consequences of parsing the container rather than the text:

* We get a genuine permalink per item (``/WrittenQuestion/99808``), so the
  dashboard links to the question itself rather than to a search results page.
* ``get_text()`` transparently rejoins the search-term highlight spans, so
  "cur<span>rent</span>ly" reads as "currently" instead of being mangled.

Note on query strategy
----------------------
The Record's own search is a substring match: searching "rent" also returns
"cur*rent*ly". We use the site search only as a coarse net to pull candidates
cheaply, and let our own word-boundary scorer make the relevance decision.
Precision comes from `relevance.py`, not from the upstream search box.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from ..models import Item
from ..relevance import Taxonomy
from .base import Collector


SEARCH_URL = "https://record.senedd.wales/Search/"
RECORD_ROOT = "https://record.senedd.wales"

TYPE_ALL = "-1"
TYPE_WRITTEN_QUESTION = "7"
TYPE_ORAL_QUESTION = "4"
TYPE_MOTION = "3"
TYPE_TOPICAL_QUESTION = "6"
TYPE_SPEECH = "1"
TYPE_TRANSCRIPT = "2"
TYPE_STATEMENT_OF_OPINION = "8"
TYPE_QNR = "13"

TYPE_TO_KIND = {
    TYPE_WRITTEN_QUESTION: ("written_question", "Written Question"),
    TYPE_ORAL_QUESTION: ("oral_question", "Oral Question"),
    TYPE_TOPICAL_QUESTION: ("oral_question", "Topical Question"),
    TYPE_QNR: ("oral_question", "Question not reached"),
    TYPE_MOTION: ("other", "Motion or Amendment"),
    TYPE_STATEMENT_OF_OPINION: ("other", "Statement of Opinion"),
    TYPE_SPEECH: ("plenary_transcript", "Speech"),
}

# Result-container CSS classes map to the item type, which is more reliable
# than parsing the visible heading text.
CLASS_TO_KIND = {
    "writtenQuestion": ("written_question", "Written Question"),
    "oralQuestion": ("oral_question", "Oral Question"),
    "topicalQuestion": ("oral_question", "Topical Question"),
    "emergencyQuestion": ("oral_question", "Emergency Question"),
    "statementOfOpinion": ("other", "Statement of Opinion"),
    "motion": ("other", "Motion or Amendment"),
    "amendment": ("other", "Motion or Amendment"),
    "transcript": ("plenary_transcript", "Transcript"),
    "speech": ("plenary_transcript", "Speech"),
    "qnr": ("oral_question", "Question not reached"),
}

# Broad seed queries. Intentionally wide — the scorer decides what matters, so
# a false positive here costs one HTTP request while a false negative costs a
# missed policy development.
DEFAULT_QUERIES = [
    "rent", "landlord", "tenant", "tenancy", "housing", "eviction",
    "Rent Smart Wales", "private rented", "letting", "leasehold",
    "second home", "holiday let", "council tax", "homelessness",
    "planning", "energy efficiency", "EPC", "building safety",
    "HMO", "licensing", "occupation contract", "Renting Homes",
]

_TABLED_RE = re.compile(
    r"Tabled on (\d{2}/\d{2}/\d{4})(?:\s*for answer on (\d{2}/\d{2}/\d{4}))?", re.I)
_ANSWERED_RE = re.compile(r"Answered on (\d{2}/\d{2}/\d{4})", re.I)
_MEETING_RE = re.compile(r"Meeting on (\d{2}/\d{2}/\d{4})", re.I)
_UID_RE = re.compile(r"UID=(\d+)")

# Hard ceiling on the text we will accept as an item body. Real questions and
# motions are short; anything longer means the parse has escaped its container
# and started swallowing the page, which is exactly the failure this module
# was rewritten to prevent.
MAX_BODY_CHARS = 4000


def _dmy(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


class RecordSearchCollector(Collector):
    """Collects tabled business (written questions etc.) from the Record."""

    name = "senedd_record_search"

    def __init__(self, *args, taxonomy: Taxonomy | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.tax = taxonomy or Taxonomy.load()

    def enabled_types(self) -> list[str]:
        """Types to search, per the `sources` switches in taxonomy.yaml.

        Written questions are off by default because the policy directorate has
        a separate tool for them. Skipping them also removes the largest block
        of requests in a run.
        """
        wanted: list[str] = []
        if self.tax.source_enabled("written_questions"):
            wanted.append(TYPE_WRITTEN_QUESTION)
        if self.tax.source_enabled("oral_questions"):
            wanted.extend([TYPE_ORAL_QUESTION, TYPE_TOPICAL_QUESTION])
        if self.tax.source_enabled("statements_of_opinion"):
            wanted.append(TYPE_STATEMENT_OF_OPINION)
        return wanted

    def collect(self, start: date | None = None, end: date | None = None,
                queries: list[str] | None = None,
                types: list[str] | None = None):
        queries = queries or DEFAULT_QUERIES
        types = types if types is not None else self.enabled_types()
        if not types:
            self.note_error(
                "every tabled-business type is switched off in taxonomy.yaml "
                "(`sources`), so this source collected nothing. That is a "
                "configuration choice, not a failure."
            )
            return

        seen: set[str] = set()
        for type_code in types:
            for query in queries:
                for item in self._search(query, type_code, start, end):
                    # The same question is returned by several seed queries.
                    # Deduping on the permalink here keeps the run log honest
                    # about real volumes; the store dedupes again on content.
                    key = item.url or f"{item.title}|{item.body[:120]}"
                    if key in seen:
                        continue
                    seen.add(key)
                    yield item

    def _search(self, query: str, type_code: str,
                start: date | None, end: date | None):
        params = {"query": query, "type": type_code}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        html = self.fetcher.get_text(SEARCH_URL, params=params)
        if not html:
            self.note_error(f"search failed for query={query!r} type={type_code}")
            return

        fallback = TYPE_TO_KIND.get(type_code, ("other", "Tabled business"))
        yield from self._parse_results(html, fallback, query)

    # -- parsing -----------------------------------------------------------

    def _parse_results(self, html: str, fallback: tuple[str, str], query: str):
        soup = BeautifulSoup(html, "html.parser")

        # Confine parsing to the results container. Anything outside it is
        # navigation, filter controls or the site footer, and must never reach
        # an Item.
        container = soup.find(class_="searchResultContainer")

        # Distinguish "no results" from "the page changed shape". Verified
        # behaviour: a genuine zero-result page still renders
        # .searchResultContainer but omits the .searchResultCount element
        # entirely. Treating a missing counter as a layout change produced a
        # spurious warning on every seed query that legitimately found nothing,
        # which is precisely the kind of cry-wolf noise that trains people to
        # ignore the run report. Only a missing container is a real problem.
        if container is None:
            self.note_error(
                "search results container not found — the Record's result "
                "markup may have changed. Verify the .searchResult / "
                ".context / .memberBar classes before trusting this source."
            )
            return

        blocks = container.find_all(class_="searchResult")
        if not blocks:
            return          # legitimately nothing matched this seed query

        for block in blocks:
            item = self._block_to_item(block, fallback, query)
            if item:
                yield item

    def _block_to_item(self, block, fallback: tuple[str, str], query: str
                       ) -> Item | None:
        classes = block.get("class") or []
        kind, label = fallback
        for css_class in classes:
            if css_class in CLASS_TO_KIND:
                kind, label = CLASS_TO_KIND[css_class]
                break

        title_el = block.find(class_="title")
        title = title_el.get_text(" ", strip=True) if title_el else label

        # The question text.
        #
        # Extraction here is fussier than it looks. The Record wraps every
        # search match in <span class="highlightedText">, so the DOM for one
        # sentence is a run of text nodes:
        #
        #     "provided by " | "Rent" | " Smart Wales?"
        #     "are cur"      | "rent" | "ly in "        | "rent" | " arrears"
        #
        # get_text() with no separator rejoins these correctly, giving
        # "provided by Rent Smart Wales?" and "are currently in rent arrears".
        # But get_text("", strip=True) strips each node individually and
        # produces "provided byRentSmart Wales?" — which then matches no
        # taxonomy term at all and silently scores the item zero. So: no strip
        # here, and normalise whitespace afterwards instead.
        context_el = block.find(class_="context")
        body = context_el.get_text() if context_el else ""
        if not body:
            summary = block.find(class_="summary") or block.find(class_="detail")
            body = summary.get_text(" ", strip=True) if summary else ""
        body = re.sub(r"\s+", " ", body).strip()
        if not body or len(body) < 12:
            return None
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "…"

        subtitle_el = block.find(class_="subTitle")
        subtitle = subtitle_el.get_text(" ", strip=True) if subtitle_el else ""

        tabled = answer_due = answered = happened = None
        if m := _TABLED_RE.search(subtitle):
            tabled = _dmy(m.group(1))
            answer_due = _dmy(m.group(2))
        if m := _ANSWERED_RE.search(subtitle):
            answered = _dmy(m.group(1))
        if m := _MEETING_RE.search(subtitle):
            happened = _dmy(m.group(1))

        # Member attribution comes from the memberBar, which is a sibling of
        # the detail link, so it is never confused with the page's member filter.
        speaker = constituency = speaker_id = ""
        member_bar = block.find(class_="memberBar")
        if member_bar:
            name_el = member_bar.find(class_="name")
            area_el = member_bar.find(class_="area")
            speaker = name_el.get_text(" ", strip=True) if name_el else ""
            constituency = area_el.get_text(" ", strip=True) if area_el else ""
            if link := member_bar.find("a", href=True):
                if uid := _UID_RE.search(link["href"]):
                    speaker_id = uid.group(1)

        # Real permalink, resolved from the relative href the Record uses.
        url = SEARCH_URL
        detail = block.find("a", class_="detail", href=True)
        if detail:
            href = detail["href"]
            url = (href if href.startswith("http")
                   else f"{RECORD_ROOT}/{href.lstrip('./')}")

        # A written question's answer-due date is a real, actionable deadline:
        # it says when a government position will exist in writing.
        deadline = answer_due if not answered else None

        return Item(
            source_kind=kind,
            source_name=label,
            title=title,
            body=body,
            url=url,
            item_date=tabled or answered or happened,
            speaker=speaker,
            speaker_id=speaker_id,
            constituency=constituency,
            forum="Senedd (tabled business)",
            agenda_item=subtitle,
            deadline=deadline,
            raw_ref=f"record_search:type={kind}:query={query}",
        )

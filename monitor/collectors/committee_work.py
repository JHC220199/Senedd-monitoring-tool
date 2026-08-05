"""Senedd committee consultations and inquiries — the influence opportunities.

WHY THIS EXISTS, AND WHY ITS ABSENCE WAS THE WORST GAP IN THE SYSTEM
-------------------------------------------------------------------
The first three revisions monitored what the Senedd had *already said*. They did
not monitor what its committees were *asking to be told*. Those are not the same
thing, and the second is more valuable: a committee consultation is an open,
dated invitation to put NRLA's position on the record.

Two live examples that the system missed entirely, both from the Local
Government, Housing and Planning Committee — the committee that scrutinises the
housing minister:

  * **Priorities for the Local Government, Housing and Planning Committee**
    (consultation ID 626, closing **14 September 2026**). The Committee is
    asking organisations to name the top three issues it should prioritise for
    the whole Seventh Senedd. For a landlord body this is close to the highest-
    value single submission available in the entire term.

  * **Follow-up inquiry into Empty Properties** (issue IId 47957). Revisits the
    Fifth Senedd's 2019 ELGC report, covering residential empty properties —
    which runs straight into council tax premiums, bringing stock back into use
    and enforcement powers.

Missing a debate means reading it late. Missing this means the deadline passes
and the opportunity is gone for four years.

WHERE THIS DATA LIVES
---------------------
Committee consultations and inquiries are NOT in the Welsh Government's
consultation list, and they are NOT in the Record. They live in the Senedd's own
ModernGov instance, in two places, both verified 4 August 2026:

1. **Active consultations RSS** — clean, structured, no scraping:

       https://business.senedd.wales/mgRss.aspx?f=76
       -> "Senedd - Active consultations", 12 items

   Each links to `mgConsultationDisplay.aspx?ID=<n>`, whose page carries the
   purpose and the closing date in prose ("The closing date for sharing your
   views is 14 September 2026").

2. **Committee "Current Work" issues** — inquiries that have no consultation
   record yet, which is how the Empty Properties inquiry was invisible:

       https://senedd.wales/committee/<committeeId>
       -> "Current Work" links to business.senedd.wales/mgIssueHistoryHome.aspx?IId=<n>

   Committee IDs come from `GetCommittees` on the ModernGov SOAP service
   (see forward_look.py), which returns the full register — including committees
   the Record's own dropdown omits.

The RSS feed is preferred wherever it covers an item. The committee pages are
scraped only for inquiries the feed does not carry.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from ..models import Item, _clean
from .base import Collector


CONSULTATIONS_RSS = "https://business.senedd.wales/mgRss.aspx?f=76"
CONSULTATION_PAGE = "https://business.senedd.wales/mgConsultationDisplay.aspx?ID={id}"
CONSULTATION_LIST = "https://business.senedd.wales/mgConsultationListDisplay.aspx?h=1"
COMMITTEE_PAGE = "https://senedd.wales/committee/{cid}"
ISSUE_PAGE = "https://business.senedd.wales/mgIssueHistoryHome.aspx?IId={iid}"

_ID_RE = re.compile(r"mgConsultationDisplay\.aspx\?ID=(\d+)", re.I)
_IID_RE = re.compile(r"mgIssueHistoryHome\.aspx\?IId=(\d+)", re.I)

# Issue pages that exist for every committee and are never an opportunity to
# influence anything. Skipped at source rather than scored down, because they
# would otherwise fill the dashboard: "Completed work and published reports"
# scored 140 (Critical) for six committees at once before this filter.
_ADMIN_TITLE_RE = re.compile(
    r"^(completed work and published reports|membership|remit|"
    r"committee membership|contact)", re.I)

# The consultations RSS carries structured dates in its <description>, which is
# far more reliable than scraping prose off the page:
#
#   "Priorities for the Local Government, Housing and Planning Committee,
#    start date: Fri, 17 Jul 2026 00:00:00 GMT, end date: Mon, 14 Sep 2026 23:59:00 GMT"
#
# This is the primary source for a closing date. Prose parsing of the
# consultation page is the fallback, for items the feed does not cover.
_FEED_END_RE = re.compile(
    r"end date:\s*(?:\w{3},\s*)?(\d{1,2}\s+\w{3,}\s+\d{4})", re.I)


def parse_feed_end_date(description: str) -> date | None:
    """Read the closing date out of the RSS description."""
    if not description:
        return None
    match = _FEED_END_RE.search(description)
    if not match:
        return None
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(match.group(1).strip(), fmt).date()
        except ValueError:
            continue
    return None


# Closing-date phrasings used on Senedd consultation pages. The first pattern is
# the exact form on consultation 626, which is the one that matters most.
_DEADLINE_PATTERNS = [
    re.compile(r"closing date for (?:sharing your views|responses|submissions)"
               r"[^.]{0,40}?is\s+(\d{1,2}\s+\w+\s+\d{4})", re.I),
    re.compile(r"clos(?:es|ing)(?:\s+date)?[:\s]{1,12}(\d{1,2}\s+\w+\s+\d{4})", re.I),
    re.compile(r"deadline[^.]{0,30}?(\d{1,2}\s+\w+\s+\d{4})", re.I),
    re.compile(r"respond by\s+(\d{1,2}\s+\w+\s+\d{4})", re.I),
    re.compile(r"by\s+(\d{1,2}\s+\w+\s+\d{4})\s+at", re.I),
]

# Committees whose consultations and inquiries matter to the NRLA. Everything is
# still collected and scored on its own merits; this only affects the label.
PRIORITY_COMMITTEES = (
    "local government, housing and planning",
    "equality, human rights and social justice",
    "climate change, environment, sustainability and rural affairs",
    "finance",
    "legislation",
    "petitions",
)


def parse_closing_date(text: str) -> date | None:
    """Extract a closing date, or return None rather than guessing.

    A wrong deadline is worse than no deadline: it invites the team to plan
    around a date that does not exist. Where this returns None the dashboard
    says the closing date could not be read and links to the source.
    """
    if not text:
        return None
    for pattern in _DEADLINE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(match.group(1).strip(), fmt).date()
            except ValueError:
                continue
    return None


def _page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    container = soup.find("main") or soup
    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
    # ModernGov pages open with site chrome before the real content.
    marker = text.find("Skip to main content")
    if marker >= 0:
        text = text[marker + 20:]
    return text.strip()


def _page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title:
        return re.sub(r"\s+", " ", soup.title.get_text(strip=True)).strip()
    heading = soup.find(["h1", "h2"])
    return re.sub(r"\s+", " ", heading.get_text(" ", strip=True)) if heading else ""


class SeneddCommitteeWorkCollector(Collector):
    """Committee consultations, calls for evidence and inquiries."""

    name = "senedd_committee_work"
    source_kind = "consultation"

    def __init__(self, *args, committees: dict[str, str] | None = None,
                 **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.committees = committees or {}

    # -- active consultations (structured feed) ----------------------------

    def active_consultations(self) -> list[dict]:
        xml = self.fetcher.get_bytes(CONSULTATIONS_RSS)
        if not xml:
            self.note_error(
                f"active consultations feed unavailable: {CONSULTATIONS_RSS}. "
                f"This feed is how committee consultations are discovered — "
                f"without it, calls for evidence will be missed."
            )
            return []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            self.note_error(f"consultations feed was not valid XML: {exc}")
            return []

        channel = root.find("channel")
        out: list[dict] = []
        for entry in (channel.findall("item") if channel is not None else []):
            title = _clean(entry.findtext("title") or "")
            link = (entry.findtext("link") or "").strip()
            if not title:
                continue
            published = None
            if raw := entry.findtext("pubDate"):
                try:
                    published = parsedate_to_datetime(raw).date()
                except (TypeError, ValueError):
                    published = None
            match = _ID_RE.search(link)
            description = _clean(entry.findtext("description") or "")
            out.append({
                "title": title,
                "url": link,
                "id": match.group(1) if match else "",
                "published": published,
                "summary": description,
                # Structured, and therefore trusted over page prose.
                "closes": parse_feed_end_date(description),
            })
        return out

    def consultation_detail(self, url: str) -> tuple[str, date | None]:
        html = self.fetcher.get_text(url)
        if not html:
            return "", None
        text = _page_text(html)
        return text, parse_closing_date(text)

    # -- committee inquiries (not always in the feed) -----------------------

    def committee_current_work(self, committee_id: str) -> list[str]:
        """Return issue IIds listed under a committee's Current Work.

        This is how the Empty Properties follow-up inquiry is found: it has a
        ModernGov issue page but no consultation record, so it never appears in
        the consultations feed.
        """
        html = self.fetcher.get_text(COMMITTEE_PAGE.format(cid=committee_id))
        if not html:
            return []
        return list(dict.fromkeys(_IID_RE.findall(html)))

    def issue_detail(self, iid: str) -> Item | None:
        url = ISSUE_PAGE.format(iid=iid)
        html = self.fetcher.get_text(url)
        if not html:
            return None

        title = _page_title(html)
        text = _page_text(html)
        if not title or len(text) < 60:
            return None
        if _ADMIN_TITLE_RE.match(title):
            return None

        closing = parse_closing_date(text)

        # Classify: an inquiry with terms of reference is an influence
        # opportunity even before a formal call for evidence opens.
        is_inquiry = bool(re.search(r"\binquiry\b|terms of reference", text, re.I))
        label = "Committee inquiry" if is_inquiry else "Committee work"

        body_parts = [title]
        if is_inquiry:
            body_parts.append(
                "This is a committee inquiry. Inquiries take written evidence, "
                "and a call for evidence is an opportunity to influence.")
        body_parts.append(text[:2500])

        return Item(
            source_kind="consultation",
            source_name=f"Senedd — {label}",
            title=title,
            body="\n\n".join(body_parts),
            url=url,
            item_date=date.today(),
            forum="Senedd committee",
            deadline=closing,
            raw_ref=url,
        )

    # -- orchestration -----------------------------------------------------

    def collect(self, include_committee_pages: bool = True,
                max_committees: int = 12):
        seen_urls: set[str] = set()

        # 1. Active consultations, from the structured feed.
        seen_titles: set[str] = set()
        for entry in self.active_consultations():
            url = entry["url"]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            # The same consultation can be published by two committees (the
            # Draft Budget appears under both Finance and the subject
            # committee), which produced visible duplicates on the dashboard.
            key = entry["title"].strip().lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)

            text, prose_closing = self.consultation_detail(url)
            # Feed date first: it is structured. Prose is the fallback.
            closing = entry["closes"] or prose_closing
            body = "\n\n".join(filter(None, [
                entry["title"],
                "This is an open Senedd consultation. Responding puts NRLA's "
                "position formally on the record.",
                entry["summary"],
                text[:2500],
            ]))
            if closing is None:
                body += ("\n\nNOTE: the closing date could not be read "
                         "automatically — check the source page.")
            else:
                body += f"\n\nClosing date: {closing.strftime('%d %B %Y')}."

            yield Item(
                source_kind="consultation",
                source_name="Senedd — committee consultation",
                title=entry["title"],
                body=body,
                url=url,
                item_date=entry["published"],
                forum="Senedd committee",
                deadline=closing,
                raw_ref=CONSULTATIONS_RSS,
            )

        # 2. Committee Current Work, for inquiries the feed does not carry.
        if not include_committee_pages:
            return
        if not self.committees:
            self.note_error(
                "no committee register supplied, so committee inquiry pages were "
                "not checked. Pass committees={id: title} from GetCommittees — "
                "without this, inquiries with no consultation record (such as the "
                "Empty Properties follow-up) are invisible."
            )
            return

        ordered = sorted(
            self.committees.items(),
            key=lambda kv: (0 if any(p in kv[1].lower()
                                     for p in PRIORITY_COMMITTEES) else 1, kv[1]))

        for cid, title in ordered[:max_committees]:
            for iid in self.committee_current_work(cid):
                url = ISSUE_PAGE.format(iid=iid)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                item = self.issue_detail(iid)
                if item is None:
                    continue
                item.forum = title
                yield item

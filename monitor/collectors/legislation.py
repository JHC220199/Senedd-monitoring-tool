"""Legislation tracking via legislation.gov.uk.

legislation.gov.uk is the best-behaved public data source in this system: a
proper Atom API, stable URLs, content negotiation, no WAF, and a published
licence. It gives us Legislation Watch — the Camlas briefing's tracking table —
with far better reliability than scraping Senedd Bill pages.

Verified endpoints (4 August 2026)
----------------------------------
    https://www.legislation.gov.uk/asc/data.feed          Acts of Senedd Cymru
    https://www.legislation.gov.uk/wsi/2026/data.feed     Welsh SIs for a year
    https://www.legislation.gov.uk/asc/2026/data.feed     Acts for a year

Both returned HTTP 200 with valid Atom. Feeds support ``page`` and
``results-count`` parameters and expose CSV alternates.

Why this matters for the NRLA specifically
------------------------------------------
The commencement of Welsh legislation is where landlords actually get hit, and
it happens through statutory instruments that attract almost no press coverage.
The Renters' Rights Act 2025 (Commencement) (Wales) Order 2026 — SI 2026/6 — is
what brought the new anti-discrimination contract terms into force on 1 June
2026, giving landlords a 14-day window to serve updated written statements. An
SI feed catches that class of change on the day it is made; a weekly narrative
briefing frequently does not.

Bills before the Senedd
-----------------------
Bills in progress are NOT on legislation.gov.uk — they only appear at Royal
Assent. In-progress Bill stages live on senedd.wales/legislation, which the
Senedd's open-data page documents as publishing Bills and Acts in Crown XML.
`SeneddBillCollector` below covers that, and is written defensively because the
page structure is not part of any documented API contract.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from bs4 import BeautifulSoup

from ..models import Item
from .base import Collector


ATOM = "{http://www.w3.org/2005/Atom}"
LEG = "{http://www.legislation.gov.uk/namespaces/legislation}"
UKM = "{http://www.legislation.gov.uk/namespaces/metadata}"

FEEDS = {
    "asc": ("https://www.legislation.gov.uk/asc/data.feed",
            "Acts of Senedd Cymru"),
    "wsi": ("https://www.legislation.gov.uk/wsi/{year}/data.feed",
            "Welsh Statutory Instruments"),
    "anaw": ("https://www.legislation.gov.uk/anaw/data.feed",
             "Acts of the National Assembly for Wales"),
}


def _parse_long_date(value: str | None) -> date | None:
    """Parse "7 July 2025" / "27 Apr 2026" as used on ModernGov bill pages."""
    if not value:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class LegislationCollector(Collector):
    """Acts of the Senedd and Welsh statutory instruments."""

    name = "legislation_gov_uk"
    source_kind = "legislation"

    def collect(self, years: list[int] | None = None):
        years = years or [date.today().year, date.today().year - 1]

        # Acts of the Senedd: one small feed, always worth reading in full.
        yield from self._read_feed(FEEDS["asc"][0], FEEDS["asc"][1])

        # Welsh SIs: one feed per year. This is the high-volume, high-value one.
        for year in years:
            url = FEEDS["wsi"][0].format(year=year)
            yield from self._read_feed(url, f"{FEEDS['wsi'][1]} {year}")

    @staticmethod
    def _entry_title(entry: ET.Element) -> str:
        """Extract the English title from a legislation.gov.uk Atom entry.

        Titles are ``type="xhtml"`` and bilingual, with the two languages in
        sibling spans separated by a slash:

            <title type="xhtml"><div>
              <span xml:lang="en">Prohibition of Greyhound Racing (Wales) Act 2026</span>
               / <span xml:lang="cy">Deddf Gwahardd Rasio Milgwn (Cymru) 2026</span>
            </div></title>

        ``findtext`` therefore returns an empty string, which silently produced
        zero legislation items until this was caught against live data. We take
        the ``xml:lang="en"`` span where present, and fall back to the full
        concatenated text so a monolingual entry still works.
        """
        node = entry.find(f"{ATOM}title")
        if node is None:
            return ""
        for span in node.iter():
            lang = span.get("{http://www.w3.org/XML/1998/namespace}lang")
            if lang == "en" and (span.text or "").strip():
                return " ".join((span.text or "").split())
        return " ".join("".join(node.itertext()).split()).strip(" /")

    @staticmethod
    def _date_from_links(entry: ET.Element) -> date | None:
        """Recover a date from the versioned URLs when metadata is absent.

        legislation.gov.uk link hrefs embed the in-force/version date, e.g.
        ``/asc/2026/11/2026-04-28/data.xml``. For Acts this is normally the
        commencement or Royal Assent date and is good enough to sort by.
        """
        for link in entry.findall(f"{ATOM}link"):
            href = link.get("href") or ""
            match = re.search(r"/(\d{4}-\d{2}-\d{2})(?:/|$)", href)
            if match:
                return _iso(match.group(1))
        return None

    def _read_feed(self, url: str, label: str):
        xml = self.fetcher.get_bytes(url)
        if not xml:
            self.note_error(f"could not read legislation feed {url}")
            return
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            self.note_error(f"malformed Atom from {url}: {exc}")
            return

        for entry in root.findall(f"{ATOM}entry"):
            title = self._entry_title(entry)
            if not title:
                continue

            link = ""
            for ln in entry.findall(f"{ATOM}link"):
                rel = ln.get("rel")
                if rel in (None, "self", "alternate") and ln.get("href"):
                    link = ln.get("href", "")
                    if rel == "alternate":
                        break
            link = link.replace("http://", "https://")

            summary = (entry.findtext(f"{ATOM}summary")
                       or entry.findtext(f"{ATOM}content") or "")

            # legislation.gov.uk exposes rich structured metadata. Number and
            # year let us cite an SI precisely, which the policy team needs
            # when briefing members.
            number = (entry.findtext(f"{LEG}number")
                      or entry.findtext(f"{UKM}Number") or "")
            made = _iso(entry.findtext(f"{LEG}madeDate")
                        or entry.findtext(f"{UKM}Made"))
            in_force = _iso(entry.findtext(f"{LEG}comingIntoForce"))
            updated = _iso(entry.findtext(f"{ATOM}updated"))
            if not (made or updated):
                made = self._date_from_links(entry)

            body_parts = [summary.strip()]
            if number:
                body_parts.append(f"Number: {number}")
            if made:
                body_parts.append(f"Made: {made.isoformat()}")
            if in_force:
                body_parts.append(f"Coming into force: {in_force.isoformat()}")
            # Include the title in the body too: for legislation, the title IS
            # the substance, and the scorer only reads title+body.
            body_parts.insert(0, title)

            yield Item(
                source_kind="legislation",
                source_name=label,
                title=title,
                body="\n".join(p for p in body_parts if p),
                url=link,
                item_date=made or updated,
                forum="legislation.gov.uk",
                deadline=in_force,
                raw_ref=url,
            )


class SeneddBillCollector(Collector):
    """Bills before the Senedd, with their current stage — Legislation Watch.

    HOW THIS WAS FIXED — read before changing anything
    --------------------------------------------------
    The first version reported "Senedd bills index unreachable" on every run, so
    Legislation Watch was permanently empty. It looked like the CloudFront WAF
    that blocks gov.wales. It was not. There were two independent bugs, and
    both were ours:

      1. **A self-inflicted 403.** Our User-Agent ended with the token
         "python-requests", which triggers CloudFront's managed bot rules.
         Removing it — while keeping an honest, identifying UA — returns 200.
         See the comment on `USER_AGENT` in `base.py`.

      2. **A wrong URL.** `senedd.wales/senedd-business/bills-and-laws/` does
         not exist and returns 404. The correct path is
         `senedd.wales/senedd-business/legislation/`.

    The 403 masked the 404, which is why this took two rounds to find: fixing
    the User-Agent turned the error from 403 into 404 and revealed the real
    problem underneath.

    With both fixed, `senedd.wales/senedd-business/legislation/` returns 200 and
    carries 11 bill and Act IDs as links to ModernGov issue-history pages,
    `business.senedd.wales/mgIssueHistoryHome.aspx?IId=<id>`. Each of those
    returns the English title plus the full dated stage history — verified with
    IId 46141: "Building Safety (Wales) Act 2026", Stage 1 through Stage 4, Royal
    Assent 27 April 2026.

    Lesson worth keeping: an HTTP 403 from a CDN is not evidence that anyone has
    decided to block you. Check your own request first.
    """

    name = "senedd_bills"
    source_kind = "legislation"

    # English index pages, verified 4 August 2026 with a clean User-Agent.
    # senedd.wales/senedd-business/legislation/ returns 200 and carries 11 bill
    # and Act IDs. The .cymru equivalents are kept as fallbacks because they are
    # served from separate infrastructure and have been observed to stay up when
    # the .wales host is being redeployed.
    INDEX_PAGES = (
        "https://senedd.wales/senedd-business/legislation/",
        "https://senedd.wales/senedd-business/legislation/senedd-acts/",
        "https://senedd.cymru/deddfwriaeth/",
    )

    # English ModernGov issue-history page for a given bill/Act.
    ISSUE_HISTORY = "https://business.senedd.wales/mgIssueHistoryHome.aspx?IId={iid}"

    _IID_RE = re.compile(r"mgIssueHistoryHome\.aspx\?IId=(\d+)", re.I)

    # Stage headings on the issue-history page carry their date in brackets,
    # e.g. "Stage 4, Vote by the Senedd to pass the final text of the Bill
    # (10 March 2026)" and "Bill introduced (7 July 2025)".
    _STAGE_RE = re.compile(
        r"(Bill introduced|Stage\s*[1-4]|General [Pp]rinciples|Post Stage 4|"
        r"Royal Assent|Bill passed|Bill withdrawn|Bill rejected)"
        r"[^()\n]{0,120}?\((\d{1,2}\s+\w+\s+\d{4})"
        r"(?:\s*[–-]\s*(\d{1,2}\s+\w+\s+\d{4}))?\)", re.I)

    # Stage ordering, so we can report the furthest stage reached rather than
    # whichever one happens to appear last in the page text.
    _STAGE_ORDER = {
        "bill introduced": 1, "general principles": 2, "stage 1": 2,
        "stage 2": 3, "stage 3": 4, "stage 4": 5, "bill passed": 5,
        "post stage 4": 6, "royal assent": 7,
        "bill withdrawn": 0, "bill rejected": 0,
    }

    def discover_ids(self) -> dict[str, str]:
        """Collect bill/Act issue IDs from the reachable index pages."""
        found: dict[str, str] = {}
        for url in self.INDEX_PAGES:
            html = self.fetcher.get_text(url)
            if not html:
                continue
            for iid in self._IID_RE.findall(html):
                found.setdefault(iid, url)
        if not found:
            self.note_error(
                "no bill IDs found on any Senedd legislation index. Checked: "
                + ", ".join(self.INDEX_PAGES)
                + ". Note that senedd.wales (without .cymru) returns 403 from "
                  "cloud hosts; use the .cymru host, which is not blocked."
            )
        return found

    def fetch_bill(self, iid: str) -> Item | None:
        """Fetch one bill's English title and stage history."""
        url = self.ISSUE_HISTORY.format(iid=iid)
        html = self.fetcher.get_text(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")

        # The page <title> is the bill's English name and is the most reliable
        # single field on the page.
        title = ""
        if soup.title:
            title = re.sub(r"\s+", " ", soup.title.get_text(strip=True)).strip()
        if not title or len(title) < 6:
            heading = soup.find(["h1", "h2"])
            title = (re.sub(r"\s+", " ", heading.get_text(" ", strip=True))
                     if heading else "")
        if not title:
            return None

        # Strip chrome before reading stage text, so navigation links that
        # happen to mention "Stage 1" are not mistaken for stage history.
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

        stages: list[tuple[int, str, date | None]] = []
        for match in self._STAGE_RE.finditer(text):
            name = re.sub(r"\s+", " ", match.group(1)).strip()
            when = _parse_long_date(match.group(3) or match.group(2))
            rank = self._STAGE_ORDER.get(name.lower(), 0)
            stages.append((rank, name, when))

        current_stage, stage_date = "", None
        if stages:
            rank, current_stage, stage_date = max(
                stages, key=lambda s: (s[0], s[2] or date.min))

        is_act = bool(re.search(r"\bAct\s+\d{4}\b", title))
        summary_lines = [title]
        if current_stage:
            summary_lines.append(
                f"Current stage: {current_stage}"
                + (f" ({stage_date.strftime('%d %B %Y')})" if stage_date else ""))
        if stages:
            history = "; ".join(
                f"{name}" + (f" {when.strftime('%d %b %Y')}" if when else "")
                for _, name, when in sorted(stages, key=lambda s: s[0]))
            summary_lines.append(f"Stage history: {history}")
        if not is_act:
            summary_lines.append(
                "This Bill is still before the Senedd, so amendments are still "
                "possible and there may be an opportunity to influence it.")

        return Item(
            source_kind="legislation",
            source_name="Senedd Act" if is_act else "Senedd Bill",
            title=title,
            body="\n".join(summary_lines),
            url=url,
            item_date=stage_date,
            forum="Senedd",
            # For a live Bill the next stage is the window to act, so the stage
            # date is carried as a deadline only where it is still in the future.
            deadline=(stage_date if stage_date and stage_date >= date.today()
                      else None),
            raw_ref=url,
        )

    def collect(self, max_bills: int = 40):
        ids = self.discover_ids()
        if not ids:
            return
        for iid in list(ids)[:max_bills]:
            item = self.fetch_bill(iid)
            if item is not None:
                yield item

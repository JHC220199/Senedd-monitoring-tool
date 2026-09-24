"""Sources for political news alerts: news headlines, and the Government.

WHAT THIS IS FOR
----------------
Camlas send a short email when something big happens in Welsh politics: a
defection (Sarah Cooper-Lesadd MS, Reform UK to Plaid Cymru, 15 September
2026), a leader standing down (Dan Thomas MS, the same afternoon), a new
shadow cabinet (Reform UK, 24 September). The news alerts replace those.

TWO KINDS OF SOURCE
-------------------
1. Headlines from three Welsh news outlets' own RSS feeds. All three answer a
   cloud server (checked 24 September 2026):

       BBC News Wales politics  feeds.bbci.co.uk/news/wales/wales_politics/rss.xml
       Nation.Cymru             nation.cymru/feed/          (all news; filtered)
       WalesOnline politics     walesonline.co.uk/news/politics/?service=rss

   Only the headline, the link and the time are used. No article text is
   copied, stored or summarised — the alert points the reader to the outlet.

2. The Welsh Government's own list of ministers, gov.wales/cabinet-ministers-
   and-deputy-ministers. It is authoritative for the government side: when a
   name or a portfolio changes there, that is a reshuffle, whatever the
   headlines say. The page is compared with the copy kept from the last run.

WHAT IS NOT A SOURCE, AND WHY
-----------------------------
Party websites (partyof.wales, reformparty.uk) refuse cloud servers (403), and
business.senedd.wales — which has the Senedd's own members list — does too.
senedd.wales draws its member list in the browser, so there is nothing to
read without running the page. The news feeds pick up what those would have
said, a little later.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from .base import Collector


FEEDS = {
    "BBC News Wales": "https://feeds.bbci.co.uk/news/wales/wales_politics/rss.xml",
    "Nation.Cymru": "https://nation.cymru/feed/",
    "WalesOnline": "https://www.walesonline.co.uk/news/politics/?service=rss",
}

MINISTERS_URL = "https://www.gov.wales/cabinet-ministers-and-deputy-ministers"


@dataclass
class Headline:
    outlet: str
    title: str
    url: str
    at: datetime | None          # naive UTC
    standfirst: str = ""         # the feed's one-line description, for matching only


def _clean(text: str) -> str:
    text = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _strip_tracking(url: str) -> str:
    return re.sub(r"[?#](at_|utm_|ns_)[^#]*$", "", (url or "").strip())


def parse_feed(outlet: str, xml_text: str) -> list[Headline]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out = []
    for entry in root.iter("item"):
        title = _clean(entry.findtext("title") or "")
        link = _strip_tracking(entry.findtext("link") or "")
        if not title or not link:
            continue
        at = None
        try:
            at = parsedate_to_datetime(entry.findtext("pubDate") or "")
            at = at.astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            pass
        standfirst = _clean(entry.findtext("description") or "")[:300]
        out.append(Headline(outlet=outlet, title=title, url=link, at=at,
                            standfirst=standfirst))
    return out


def parse_ministers(html: str) -> dict[str, str]:
    """``{name: role}`` from the ministers page's ``.key-person`` cards."""
    soup = BeautifulSoup(html or "", "html.parser")
    out: dict[str, str] = {}
    for card in soup.select(".key-person"):
        details = card.select_one(".key-person__details")
        if details is None:
            continue
        role_el = details.select_one(".subtitle")
        role = _clean(role_el.get_text(" ", strip=True)) if role_el else ""
        if role_el:
            role_el.extract()
        name = _clean(details.get_text(" ", strip=True))
        if name:
            out[name] = role
    return out


class NewsCollector(Collector):
    name = "political_news"

    def headlines(self, feeds: dict[str, str] | None = None) -> list[Headline]:
        found: list[Headline] = []
        for outlet, url in (feeds or FEEDS).items():
            text = self.fetcher.get_text(url)
            if not text:
                self.note_error(f"{outlet}'s feed could not be read ({url}).")
                continue
            try:
                found.extend(parse_feed(outlet, text))
            except ET.ParseError as exc:
                self.note_error(f"{outlet}'s feed was not valid XML: {exc}")
        return found

    def ministers(self) -> dict[str, str] | None:
        html = self.fetcher.get_text(MINISTERS_URL)
        if not html:
            self.note_error(f"The Welsh Government ministers page could not be read ({MINISTERS_URL}).")
            return None
        found = parse_ministers(html)
        if len(found) < 5:
            # A page that parses to almost nobody has changed shape; comparing
            # it with last time would announce that the whole Cabinet resigned.
            self.note_error("The Welsh Government ministers page was read but "
                            "fewer than five ministers were found on it — its "
                            "layout has probably changed. No comparison made.")
            return None
        return found

    def collect(self):          # not part of the daily pipeline
        return iter(())

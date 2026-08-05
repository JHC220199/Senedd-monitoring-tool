"""Senedd Research articles — the best available substitute for gov.wales.

Why this collector exists
-------------------------
gov.wales is unreachable from any cloud host. Verified 4 August 2026, with a
clean identifying User-Agent, on every path tried:

    /announcements/rss  -> 403        /rss.xml       -> 403
    /consultations      -> 403        /sitemap.xml   -> 403
    llyw.cymru (Welsh)  -> 403

Unlike the senedd.wales 403s — which turned out to be our own User-Agent — this
one is a genuine IP-based restriction and cannot be fixed in code. The mailbox
route in `govwales.py` is the production answer.

But there is a partial substitute that *is* reachable, and it is good:
`research.senedd.wales`. Senedd Research is the Senedd's own impartial research
service, and it publishes analysis of Welsh Government policy, bills and
consultations — often within days, and usually with more useful framing than the
Welsh Government's own announcement. Its article on the First Minister's
14 July 2026 legislation statement is precisely the material a policy team wants
on the Rental Bill.

So this collector does two useful things at once:

* It partially closes the gov.wales gap when running anywhere gov.wales blocks.
* It adds a source the current supplier briefing does not carry at all.

There is no RSS feed (`/feed/` and `/rss/` both 404), so the article index is
parsed. The parse is deliberately shallow and forgiving: it takes article links
and titles from the index, and optionally fetches each article for its body. It
keys off the URL pattern `/research-articles/<slug>/`, which is derived from the
content itself and is therefore more stable than any CSS class name.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from ..models import Item
from .base import Collector


ROOT = "https://research.senedd.wales"
INDEX = f"{ROOT}/research-articles/"

# Index pages worth reading. Senedd Research organises by topic; these are the
# ones that carry housing, planning, tax and local government material.
TOPIC_PAGES = (
    INDEX,
    f"{ROOT}/research-articles/?search=housing",
    f"{ROOT}/research-articles/?search=renting",
    f"{ROOT}/research-articles/?search=planning",
)

_ARTICLE_RE = re.compile(r"^/research-articles/(?!$)([a-z0-9\-]{12,})/?$", re.I)

# Index headings and section links that are not articles.
_NOT_ARTICLES = {
    "research articles", "guides for constituents", "in brief",
    "research service", "senedd research",
}

_DATE_RE = re.compile(r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|"
                      r"July|August|September|October|November|December)\s+\d{4})\b")


class SeneddResearchCollector(Collector):
    """Impartial Senedd Research analysis of Welsh Government policy."""

    name = "senedd_research"
    source_kind = "research"    # see source_multipliers in taxonomy.yaml

    def collect(self, max_articles: int = 25, fetch_bodies: bool = True):
        found: dict[str, str] = {}

        for page in TOPIC_PAGES:
            html = self.fetcher.get_text(page)
            if not html:
                continue
            for href, title in self._index_links(html):
                found.setdefault(href, title)

        if not found:
            self.note_error(
                f"no articles found on {INDEX}. Senedd Research has no RSS feed "
                f"(/feed/ and /rss/ both 404), so this collector parses the "
                f"article index — check the /research-articles/<slug>/ URL "
                f"pattern still holds."
            )
            return

        for href, title in list(found.items())[:max_articles]:
            url = href if href.startswith("http") else ROOT + href
            body, published = title, None

            if fetch_bodies:
                article_html = self.fetcher.get_text(url)
                if article_html:
                    body, published = self._article_content(article_html, title)

            yield Item(
                source_kind="research",
                source_name="Senedd Research",
                title=title,
                body=body,
                url=url,
                item_date=published,
                forum="Senedd Research",
                raw_ref=INDEX,
            )

    # -- parsing -----------------------------------------------------------

    def _index_links(self, html: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].split("?")[0].split("#")[0]
            path = href.replace(ROOT, "")
            if not _ARTICLE_RE.match(path):
                continue
            title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
            if len(title) < 15 or title.lower() in _NOT_ARTICLES:
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append((path, title))
        return out

    def _article_content(self, html: str,
                         fallback_title: str) -> tuple[str, date | None]:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        container = (soup.find("main")
                     or soup.find("article")
                     or soup.find(class_=re.compile("content|body|article", re.I))
                     or soup)

        paragraphs = [re.sub(r"\s+", " ", p.get_text(" ", strip=True))
                      for p in container.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > 40]

        # Six paragraphs was too few. Senedd Research articles are structured
        # by topic, so a piece on the legislative programme reaches housing
        # several sections in — with a six-paragraph cut it matched only
        # "social housing" and scored Low. Fourteen captures the substance
        # while still keeping items to a sensible size.
        body = "\n\n".join([fallback_title] + paragraphs[:14])

        published = None
        text = " ".join(paragraphs[:3]) + " " + (
            soup.find("time").get_text(" ", strip=True) if soup.find("time") else "")
        if match := _DATE_RE.search(text):
            for fmt in ("%d %B %Y",):
                try:
                    published = datetime.strptime(match.group(1), fmt).date()
                except ValueError:
                    pass
        if published is None and (tag := soup.find("time")):
            raw = tag.get("datetime") or ""
            try:
                published = datetime.fromisoformat(
                    raw.replace("Z", "+00:00")).date()
            except ValueError:
                published = None

        return body, published

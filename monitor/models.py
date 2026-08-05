"""Canonical data model for the NRLA Senedd Monitor.

Everything collected from every source is normalised into a single `Item`.
This is the most important design decision in the system: because a written
question, a paragraph of a Plenary speech, a consultation and a bill stage all
become the same shape, the relevance engine, the dashboard, the alerting and
the archive each need to understand only one thing.

Adding a new source therefore means writing one collector that emits `Item`s.
No other part of the system changes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any


# Source kinds. These strings are the keys used in taxonomy.yaml's
# `source_multipliers`, so keep the two in step.
SOURCE_KINDS = (
    "plenary_transcript",
    "committee_transcript",
    "written_question",
    "oral_question",
    "consultation",
    "legislation",
    "written_statement",
    "press_release",
    "research",
    "calendar",
    "other",
)


def _clean(text: str | None) -> str:
    """Strip HTML tags and normalise whitespace.

    The Senedd Record embeds each contribution as HTML fragments
    (``<p>...</p>``), and gov.wales RSS descriptions contain entities and
    markup. Everything downstream — scoring, search, digests — wants plain
    text, so we normalise once here rather than in five different places.
    """
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    # Named and numeric entities that appear in Senedd/gov.wales payloads.
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&#58;": ":", "&rsquo;": "’",
        "&lsquo;": "‘", "&ldquo;": "“", "&rdquo;": "”",
        "&pound;": "£", "&ndash;": "–", "&mdash;": "—",
        "&hellip;": "…", "&apos;": "'",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class Item:
    """A single unit of monitored political activity."""

    # --- identity ---------------------------------------------------------
    source_kind: str                 # one of SOURCE_KINDS
    source_name: str                 # human label, e.g. "Plenary" or "Written Questions"
    title: str                       # what this is, e.g. "WQ99808" or agenda item
    body: str                        # the substantive text that gets scored
    url: str = ""                    # canonical public link
    item_date: date | None = None    # when it happened / was published

    # --- attribution ------------------------------------------------------
    speaker: str = ""                # MS or minister name
    speaker_role: str = ""           # job title at time of speaking
    speaker_id: str = ""             # Senedd Member_Id, for stable linking
    party: str = ""                  # filled from the member register where known
    constituency: str = ""

    # --- context ----------------------------------------------------------
    forum: str = ""                  # "Plenary", committee name, "Welsh Government"
    agenda_item: str = ""            # which item of business
    meeting_id: str = ""             # Senedd meeting ID, lets us group a sitting
    video_url: str = ""              # Senedd.tv deep link, timestamped where available

    # --- deadlines --------------------------------------------------------
    # Only consultations, calls for evidence and answer-due dates populate this.
    # It drives the "closing soon" logic, which is the single most valuable
    # feature for a policy team.
    deadline: date | None = None

    # --- scoring (populated by relevance.py) ------------------------------
    score: float = 0.0
    band: str = ""
    channel: str = ""
    themes: list[str] = field(default_factory=list)
    tiers: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    force_alert: bool = False

    # --- provenance -------------------------------------------------------
    collected_at: datetime | None = None
    raw_ref: str = ""                # the exact upstream URL/ID this came from

    def __post_init__(self) -> None:
        self.title = _clean(self.title)
        self.body = _clean(self.body)
        self.speaker = _clean(self.speaker)
        self.speaker_role = _clean(self.speaker_role)
        self.agenda_item = _clean(self.agenda_item)
        if self.collected_at is None:
            self.collected_at = datetime.now()

    @property
    def uid(self) -> str:
        """Stable content-addressed ID, used for deduplication.

        Deliberately derived from source + url + title + a hash of the body
        rather than from an upstream ID. The Senedd re-publishes corrected
        transcripts and gov.wales edits pages in place; hashing the content
        means a genuine correction produces a new item (so the team sees the
        change) while a re-run of the collector does not produce duplicates.
        """
        basis = "|".join([
            self.source_kind,
            self.url or self.raw_ref,
            self.title,
            self.body[:2000],
        ])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]

    @property
    def searchable(self) -> str:
        """Everything that should be considered when matching terms."""
        return "\n".join(filter(None, [
            self.title, self.agenda_item, self.body,
            self.speaker, self.speaker_role, self.forum,
        ]))

    @property
    def excerpt(self) -> str:
        """A short human-readable preview for digests and dashboard cards."""
        text = self.body or self.title
        if len(text) <= 320:
            return text
        cut = text[:320]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        return cut + "…"

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["uid"] = self.uid
        for key in ("themes", "tiers", "entities", "signals", "matched_terms"):
            d[key] = "; ".join(d[key])
        d["item_date"] = self.item_date.isoformat() if self.item_date else None
        d["deadline"] = self.deadline.isoformat() if self.deadline else None
        d["collected_at"] = self.collected_at.isoformat() if self.collected_at else None
        d["force_alert"] = int(d["force_alert"])
        return d

"""Relevance scoring.

The value of a political monitoring system is almost entirely in this file.
Collecting Senedd data is easy; deciding which 12 of the week's 3,000
contributions a policy officer should actually read is the hard part.

The model has four components:

1. THEMES     — what the item is about. Weighted by how much it matters to NRLA.
2. ENTITIES   — who is involved. Amplifies, never creates, relevance.
3. SIGNALS    — the shape of the item. Is something actually about to happen?
4. MULTIPLIER — where it came from. A minister in Plenary outranks a press release.

Two decisions are worth flagging explicitly, because they are the difference
between a tool people use and a tool people mute:

* Word-boundary matching. "rent" must not match "current", "different" or
  "parent". A naive substring search on the Senedd Record returns hundreds of
  false positives a week. We verified this against real data: a substring
  search for "rent" matched "Will the Cabinet Minister confirm how many
  households in Wales are cur*rent*ly in rent arrears" twice — once
  legitimately, once not.

* Entity boosts cannot create relevance on their own. If the housing minister
  answers a question about ambulance waiting times, that is not a housing item.
  An item that matches no theme is discarded no matter who spoke.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .models import Item


DEFAULT_TAXONOMY = Path(__file__).resolve().parent.parent / "config" / "taxonomy.yaml"


# ---------------------------------------------------------------------------
# Term matching
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8192)
def _compile_term(term: str) -> re.Pattern[str]:
    """Compile one taxonomy term into a word-boundary-aware pattern.

    - Internal whitespace becomes ``\\s+`` so "rent  control" and
      "rent\\ncontrol" (common where the Record wraps lines) both match.
    - ``\\b`` guards are only added where the term actually starts/ends with a
      word character, so terms like "s106" and "£" behave sensibly.
    - Apostrophes are normalised: the Senedd Record uses the typographic ’
      while taxonomy authors type '.
    """
    escaped = re.escape(term.strip())
    escaped = escaped.replace(r"\ ", r"\s+")
    # Accept either apostrophe form wherever one appears. Taxonomy authors type
    # a straight quote; the Senedd Record publishes the typographic one, so
    # "Renters' Rights Act" must match "Renters’ Rights Act".
    #
    # Note: since Python 3.7 re.escape() leaves the apostrophe unescaped, so
    # the pattern contains a bare ' and not \'. An earlier version of this
    # function looked for \' and therefore never fired — the tests caught it.
    escaped = re.sub(r"['’]", "['’]", escaped)
    prefix = r"\b" if re.match(r"\w", term.strip()) else ""
    suffix = r"\b" if re.search(r"\w$", term.strip()) else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


def find_terms(text: str, terms: list[str]) -> list[str]:
    """Return the subset of `terms` present in `text`, word-boundary aware."""
    if not text:
        return []
    return [t for t in terms if _compile_term(t).search(text)]


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

@dataclass
class Taxonomy:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Taxonomy":
        p = Path(path) if path else DEFAULT_TAXONOMY
        with open(p, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    # -- convenience accessors --------------------------------------------
    @property
    def themes(self) -> dict[str, dict]:
        return self.raw.get("themes", {})

    @property
    def entities(self) -> dict[str, dict]:
        return self.raw.get("entities", {})

    @property
    def signals(self) -> dict[str, dict]:
        return self.raw.get("signals", {})

    @property
    def multipliers(self) -> dict[str, float]:
        return self.raw.get("source_multipliers", {})

    @property
    def bands(self) -> list[dict]:
        # Highest band first, so the first match wins.
        return sorted(self.raw.get("bands", []),
                      key=lambda b: b.get("min_score", 0), reverse=True)

    @property
    def thresholds(self) -> dict[str, Any]:
        return self.raw.get("thresholds", {})

    @property
    def contribution_types(self) -> dict[str, dict]:
        return self.raw.get("contribution_types", {})

    @property
    def sources(self) -> dict[str, bool]:
        """Which sources to collect at all. See `sources` in taxonomy.yaml."""
        return self.raw.get("sources", {}) or {}

    def source_enabled(self, name: str) -> bool:
        """Default to enabled, so adding a collector does not need a config edit."""
        return bool(self.sources.get(name, True))

    @property
    def procedural_agenda_items(self) -> list[str]:
        return self.raw.get("procedural_agenda_items", []) or []

    def is_procedural_agenda_item(self, agenda_item: str) -> bool:
        """True for committee housekeeping that is never policy content.

        Checked on the agenda item title, after stripping any leading item
        number. Without this, "1. Introductions, apologies, substitutions and
        declarations of interest" reached the Review section of the dashboard as
        a High-priority development, because the surrounding transcript text
        mentions the committee's own name and remit.
        """
        if not agenda_item:
            return False
        title = re.sub(r"^\s*\d+[.)]?\s*", "", agenda_item).strip().lower()
        return any(title.startswith(p.strip().lower())
                   for p in self.procedural_agenda_items)

    def includes_contribution_type(self, code: str) -> bool:
        """Should this Record contribution type reach the pipeline at all?

        Type ``I`` is the bilingual-column explainer and the "[R] indicates a
        declared interest" boilerplate that appears in every single transcript.
        Including it would put identical noise in every day's results.
        """
        if not code:
            return True
        spec = self.contribution_types.get(code)
        return True if spec is None else bool(spec.get("include", True))

    def band_for(self, score: float) -> dict:
        for band in self.bands:
            if score >= band.get("min_score", 0):
                return band
        return {"name": "Noise", "channel": "archive", "colour": "#9AA5AF"}

    def theme_tier(self, theme_key: str) -> str:
        return self.themes.get(theme_key, {}).get("tier", "Other")

    def theme_label(self, theme_key: str) -> str:
        return self.themes.get(theme_key, {}).get("label", theme_key)

    # -- the public page's strict filter -----------------------------------
    @property
    def site_config(self) -> dict[str, Any]:
        return self.raw.get("site", {}) or {}

    def qualifies_for_site(self, item: Item) -> bool:
        """Does this item belong on the public page?

        The archive keeps everything; the page is strict. See the `site`
        section of taxonomy.yaml for the rationale and the two rules this
        implements. The Noise band is excluded here too, so every caller
        gets the same definition of "shown" and the page, its stat cards
        and its CSV can never disagree with each other.
        """
        # Demonstration data NEVER reaches the page, whatever it scores.
        #
        # tools/load_govwales_fixture.py writes sample Welsh Government
        # notifications into the archive so the mailbox parser can be exercised
        # without a Microsoft Graph tenant. Those rows were committed into
        # data/archive.sql and then shown on the live page for weeks as open
        # consultations with a deadline countdown, indistinguishable from real
        # ones. Pruning them fixes today; this line means a future demo run
        # cannot put them back in front of a policy officer.
        if "FIXTURE" in (item.raw_ref or ""):
            return False

        # Suppression wins over everything, including the title exception
        # below — an administrative index page is not made actionable by
        # naming the housing committee.
        if find_terms(item.title or "",
                      self.site_config.get("suppress_titles", []) or []):
            return False

        # The title exception is checked FIRST, before the Noise cut: a
        # scheduled housing-committee sitting has no agenda text yet, so it
        # scores weakly BY CONSTRUCTION — that is the very case the
        # exception exists to rescue.
        if find_terms(item.title or "",
                      self.site_config.get("always_relevant_in_title", []) or []):
            return True
        if (item.band or "") == "Noise":
            return False
        generic = set(self.site_config.get("non_qualifying_themes", []) or [])
        return any(t not in generic for t in (item.themes or []))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class Scorer:
    """Scores `Item`s against a `Taxonomy`, in place."""

    def __init__(self, taxonomy: Taxonomy | None = None) -> None:
        self.tax = taxonomy or Taxonomy.load()

    def score_item(self, item: Item) -> Item:
        text = item.searchable

        theme_score, themes, tiers, matched = self._score_themes(text)

        # An item with no thematic match is not relevant to NRLA, full stop.
        # We still record the zero so tuning decisions can be audited.
        if not themes:
            item.score = 0.0
            item.themes, item.tiers, item.entities, item.signals = [], [], [], []
            item.matched_terms = []
            band = self.tax.band_for(0.0)
            item.band, item.channel = band["name"], band.get("channel", "archive")
            return item

        entity_score, entities, force = self._score_entities(text)
        signal_score, signals, signal_terms = self._score_signals(text)
        matched.extend(signal_terms)

        subtotal = theme_score + entity_score + signal_score
        multiplier = float(self.tax.multipliers.get(item.source_kind, 1.0))
        total = subtotal * multiplier

        item.score = round(total, 1)
        item.themes = themes
        item.tiers = sorted(set(tiers))
        item.entities = entities
        item.signals = signals
        item.matched_terms = sorted(set(matched))[:40]
        item.force_alert = force

        band = self.tax.band_for(item.score)
        item.band = band["name"]
        item.channel = band.get("channel", "archive")

        # A direct NRLA mention always escalates to immediate, regardless of
        # what else did or did not match. If we are being named in the Chamber,
        # the policy team needs to know before the digest goes out.
        if force:
            item.channel = "immediate"
            if item.band not in ("Critical",):
                item.band = "Critical"

        # Consultations are protected: the cost of missing a closing date is
        # asymmetric and unrecoverable, so we never bury one.
        if (item.source_kind == "consultation"
                and self.tax.thresholds.get("never_drop_consultations", True)
                and item.channel in ("archive", "dashboard")):
            item.channel = "digest"
            if item.band == "Noise":
                item.band = "Low"

        return item

    # -- components -------------------------------------------------------

    def _score_themes(self, text: str) -> tuple[float, list[str], list[str], list[str]]:
        total = 0.0
        keys: list[str] = []
        tiers: list[str] = []
        matched: list[str] = []

        for key, spec in self.tax.themes.items():
            weight = float(spec.get("weight", 0))
            if weight <= 0:
                continue                      # muted theme, kept for history
            hits = find_terms(text, spec.get("terms", []))
            if not hits:
                continue
            vetoes = find_terms(text, spec.get("exclude_if", []) or [])
            if vetoes and not self._veto_overridden(hits, vetoes):
                continue
            total += weight                   # fires once at full weight
            keys.append(key)
            tiers.append(spec.get("tier", "Other"))
            matched.extend(hits)

        return total, keys, tiers, matched

    @staticmethod
    def _veto_overridden(hits: list[str], vetoes: list[str]) -> bool:
        """Allow a veto to be overridden by an unambiguous hit.

        Example: a debate about the social housing sector that also contains
        the exact phrase "private rented sector" is genuinely a PRS item even
        though "social landlord" appears. Without this, a comparative debate —
        precisely the kind NRLA most wants to see — would be silently dropped.
        """
        unambiguous = {"private rented sector", "private rental sector",
                       "private landlord", "private landlords", "prs",
                       "buy to let", "buy-to-let", "rent smart wales"}
        return any(h.lower() in unambiguous for h in hits)

    def _score_entities(self, text: str) -> tuple[float, list[str], bool]:
        total = 0.0
        labels: list[str] = []
        force = False
        for key, spec in self.tax.entities.items():
            hits = find_terms(text, spec.get("terms", []))
            if not hits:
                continue
            total += float(spec.get("boost", 0))
            labels.append(spec.get("label", key))
            if spec.get("always_alert"):
                force = True
        return total, labels, force

    def _score_signals(self, text: str) -> tuple[float, list[str], list[str]]:
        total = 0.0
        labels: list[str] = []
        matched: list[str] = []
        for key, spec in self.tax.signals.items():
            hits = find_terms(text, spec.get("terms", []))
            if not hits:
                continue
            total += float(spec.get("boost", 0))
            labels.append(spec.get("label", key))
            matched.extend(hits)
        return total, labels, matched

    # -- filtering --------------------------------------------------------

    def keep(self, item: Item) -> bool:
        """Should this item be stored at all?"""
        floor = float(self.tax.thresholds.get("store_minimum", 0))
        if item.source_kind == "consultation" and item.themes:
            return True
        return item.score >= floor

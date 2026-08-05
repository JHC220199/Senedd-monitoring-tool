"""Run orchestration.

One entry point that collects from every configured source, scores everything,
stores it, and reports honestly on what worked and what did not.

The design principle throughout: **a broken source must be loud**. The failure
mode that destroys trust in a monitoring system is not a crash — it is a quiet
week that was actually a broken feed. Every collector error is captured, stored
against the run, and rendered at the top of the dashboard.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .collectors.base import Fetcher
from .collectors.committee_work import SeneddCommitteeWorkCollector
from .collectors.forward_look import SeneddCalendarCollector
from .collectors.govwales import (GovWalesMailboxCollector,
                                  GovWalesRSSCollector)
from .collectors.legislation import LegislationCollector, SeneddBillCollector
from .collectors.record_search import RecordSearchCollector
from .collectors.record_transcripts import RecordTranscriptCollector
from .collectors.senedd_research import SeneddResearchCollector
from .models import Item
from .relevance import Scorer, Taxonomy
from .store import Store


log = logging.getLogger(__name__)


@dataclass
class RunReport:
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    collected: int = 0
    stored: int = 0
    new_items: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    sources_attempted: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    # Sources that returned nothing but were expected to, and whose data is
    # covered by another source. Reported separately so the health banner does
    # not cry wolf: gov.wales RSS is unreachable by design from a cloud host,
    # and when the mailbox route is configured its data arrives anyway.
    sources_substituted: list[str] = field(default_factory=list)

    @property
    def duration(self) -> str:
        if not self.finished_at:
            return "—"
        seconds = int((self.finished_at - self.started_at).total_seconds())
        return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

    @property
    def healthy(self) -> bool:
        """True when every source we actually depend on returned data.

        A substituted source (gov.wales RSS when the mailbox route is live) does
        not make a run unhealthy. Getting this wrong is not cosmetic: a banner
        that says "incomplete" on every single run trains people to ignore it,
        which defeats the entire point of having it.
        """
        return not self.sources_failed


class Pipeline:
    def __init__(self, store: Store, taxonomy: Taxonomy | None = None,
                 fetcher: Fetcher | None = None,
                 mailbox: str = "", graph_token: str = "",
                 govwales_route: str = "auto") -> None:
        self.store = store
        self.tax = taxonomy or Taxonomy.load()
        self.scorer = Scorer(self.tax)
        self.fetcher = fetcher or Fetcher()
        self.mailbox = mailbox
        self.graph_token = graph_token
        # How gov.wales content is expected to arrive in THIS deployment:
        #   "auto"    - infer from whether a mailbox is configured (default)
        #   "mailbox" - the shared mailbox is the intended route, so a failed
        #               RSS fetch is expected and must not flag the run as broken
        #   "rss"     - direct RSS is expected to work (i.e. running on the NRLA
        #               network), so a failure IS a real problem worth flagging
        #
        # This exists because gov.wales returns 403 from any cloud host
        # regardless of User-Agent. Without a way to declare intent, the health
        # banner went red on every single run — and a warning that is always on
        # is a warning nobody reads.
        self.govwales_route = govwales_route

    # -- source registry ---------------------------------------------------

    def _sources(self, lookback_days: int):
        """Yield (label, collector, callable, optional) tuples.

        Order is deliberate: cheapest and most reliable first, so a partial run
        still returns the most valuable data.

        `optional=True` marks a source whose absence must not be reported as a
        failure — either because another source covers it, or because it is
        known to be unavailable in this deployment topology.
        """
        start = date.today() - timedelta(days=lookback_days)
        end = date.today()

        transcripts = RecordTranscriptCollector(self.fetcher, taxonomy=self.tax)
        search = RecordSearchCollector(self.fetcher, taxonomy=self.tax)
        legislation = LegislationCollector(self.fetcher)
        bills = SeneddBillCollector(self.fetcher)
        calendar = SeneddCalendarCollector(self.fetcher)
        gov_rss = GovWalesRSSCollector(self.fetcher)
        gov_mail = GovWalesMailboxCollector(
            self.fetcher, mailbox=self.mailbox, access_token=self.graph_token)
        research = SeneddResearchCollector(self.fetcher)
        # The committee register comes from the SOAP service, and the committee
        # work collector needs it to find inquiries that have no consultation
        # record — such as the Empty Properties follow-up.
        committee_work = SeneddCommitteeWorkCollector(
            self.fetcher, committees=calendar.current_committees())

        have_mailbox = bool(self.mailbox and self.graph_token)
        # RSS is optional when the mailbox is the declared route, or when a
        # mailbox is configured and we are inferring.
        rss_optional = (self.govwales_route == "mailbox"
                        or (self.govwales_route == "auto" and have_mailbox))

        # FIRST, because it is the highest-value source in the system: open
        # consultations and inquiries are dated invitations to influence, and a
        # missed deadline cannot be recovered.
        yield ("Senedd committee consultations and inquiries", committee_work,
               lambda: committee_work.collect(), False)
        yield ("Senedd Record — transcripts", transcripts,
               lambda: transcripts.collect(start=start, end=end), False)
        yield ("Senedd Record — tabled business", search,
               lambda: search.collect(start=start, end=end), False)
        yield ("Legislation (Acts and Welsh SIs)", legislation,
               lambda: legislation.collect(), False)
        yield ("Senedd Bills and Acts", bills, lambda: bills.collect(), False)
        yield ("Senedd forward look", calendar, lambda: calendar.collect(), False)

        if have_mailbox:
            yield ("Welsh Government — mailbox", gov_mail,
                   lambda: gov_mail.collect(), False)
        yield ("Welsh Government — RSS", gov_rss,
               lambda: gov_rss.collect(), rss_optional)
        # Reachable everywhere, and partially closes the gov.wales gap: Senedd
        # Research analyses Welsh Government policy, bills and consultations.
        yield ("Senedd Research", research,
               lambda: research.collect(max_articles=20), False)

    # -- the run -----------------------------------------------------------

    def run(self, lookback_days: int = 14) -> RunReport:
        report = RunReport(run_id=uuid.uuid4().hex[:12], started_at=datetime.now())

        all_items: list[Item] = []

        for label, collector, call, optional in self._sources(lookback_days):
            report.sources_attempted.append(label)
            count = 0
            try:
                for item in call():
                    self.scorer.score_item(item)
                    report.collected += 1
                    count += 1
                    if self.scorer.keep(item):
                        all_items.append(item)
            except Exception as exc:  # noqa: BLE001
                # A single misbehaving source must not end the run.
                message = f"{label}: unhandled error — {type(exc).__name__}: {exc}"
                log.exception(message)
                report.errors.append(message)
                report.sources_failed.append(label)

            report.per_source[label] = count
            if collector.errors:
                report.errors.extend(f"{label}: {e}" for e in collector.errors)
            if count == 0:
                if optional:
                    report.sources_substituted.append(label)
                elif collector.errors or label in report.sources_failed:
                    if label not in report.sources_failed:
                        report.sources_failed.append(label)

        new, total = self.store.upsert_many(all_items)
        report.new_items = new
        report.stored = total
        report.finished_at = datetime.now()

        self.store.record_run(
            report.run_id, report.started_at, report.finished_at,
            report.collected, report.stored, report.errors,
            report.sources_attempted, report.sources_failed,
            report.sources_substituted,
        )
        return report

    # -- outputs -----------------------------------------------------------

    def items_for_digest(self, since_days: int = 7) -> list[Item]:
        return self.store.query(
            since=date.today() - timedelta(days=since_days),
            channels=["immediate", "digest"],
            min_score=float(self.tax.thresholds.get("dashboard_minimum", 25)),
        )

    def items_for_alert(self) -> list[Item]:
        """Unnotified Critical items only — the things that cannot wait."""
        return self.store.query(channels=["immediate"], unnotified_only=True)

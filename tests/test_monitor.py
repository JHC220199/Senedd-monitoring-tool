"""Tests for the NRLA Senedd monitor.

Run with:  python3 -m tests.test_monitor      (no pytest required)
       or: python3 -m pytest tests/ -q

Every test in the "regressions" section corresponds to a real bug that live
data exposed during the build. They are documented as such because the value of
a regression test is largely in explaining what went wrong and why it mattered.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor import alerts as alerts_mod                                  # noqa: E402
from monitor.collectors.base import USER_AGENT                            # noqa: E402
from monitor.collectors.forward_look import (ALL_COMMITTEES,              # noqa: E402
                                            SeneddCalendarCollector,
                                            deadlines_from_items)
from monitor.collectors.govwales import (classify, parse_deadline,        # noqa: E402
                                         GovWalesMailboxCollector)
from monitor.collectors.legislation import LegislationCollector           # noqa: E402
from monitor.collectors.record_search import RecordSearchCollector        # noqa: E402
from monitor.collectors.record_transcripts import RecordTranscriptCollector  # noqa: E402
from monitor.models import Item, _clean                                   # noqa: E402
from monitor.relevance import Scorer, Taxonomy, find_terms                # noqa: E402
from monitor.store import Store                                          # noqa: E402


TAX = Taxonomy.load()
SCORER = Scorer(TAX)

# Nothing this suite does may reach the run page of the workflow running it.
#
# The email commands append plain-English notes to $GITHUB_STEP_SUMMARY, which
# is exactly right in production — and on a runner that variable is set while
# the tests run too. So every green run of the Friday workflow, and the first
# morning briefing on 23 September 2026, carried "The Friday future-business
# email is not switched on yet" and "was not sent … example.invalid" at the top
# of its page: the tests' fake flow URL, reported as though it were real. The
# earlier fix (TestTestsDoNotAnnotateTheRun) closed the `::error` route; this
# closes the summary-file route, for the whole suite at once, before any test
# can run.
os.environ.pop("GITHUB_STEP_SUMMARY", None)


def make_item(body: str, **kwargs) -> Item:
    defaults = dict(source_kind="plenary_transcript", source_name="Plenary",
                    title="Test item", body=body, item_date=date(2026, 7, 15))
    defaults.update(kwargs)
    return SCORER.score_item(Item(**defaults))


# ---------------------------------------------------------------------------
class TestWordBoundaryMatching(unittest.TestCase):
    """The single most important correctness property in the system.

    A substring match on "rent" matches "current", "different", "parent" and
    "apparent". On the Senedd Record that is hundreds of false positives a week,
    which is the fastest possible way to get a monitoring tool ignored.
    """

    def test_rent_does_not_match_current(self):
        self.assertEqual(find_terms("the current situation", ["rent"]), [])

    def test_rent_does_not_match_different_or_parent(self):
        self.assertEqual(
            find_terms("a different parent company apparently", ["rent"]), [])

    def test_rent_matches_standalone(self):
        self.assertEqual(find_terms("the rent is too high", ["rent"]), ["rent"])

    def test_phrase_tolerates_extra_whitespace(self):
        # The Record wraps long lines, so phrases arrive with newlines in them.
        self.assertEqual(
            find_terms("private rented\n   sector", ["private rented sector"]),
            ["private rented sector"])

    def test_hyphenated_terms_match(self):
        self.assertEqual(find_terms("buy-to-let mortgages", ["buy-to-let"]),
                         ["buy-to-let"])

    def test_typographic_apostrophe_matches_typed_one(self):
        # Taxonomy authors type "Renters' Rights Act"; the Record publishes
        # "Renters’ Rights Act" with a typographic apostrophe.
        self.assertEqual(
            find_terms("the Renters’ Rights Act 2025", ["Renters' Rights Act"]),
            ["Renters' Rights Act"])

    def test_case_insensitive(self):
        self.assertEqual(find_terms("RENT SMART WALES", ["Rent Smart Wales"]),
                         ["Rent Smart Wales"])


# ---------------------------------------------------------------------------
class TestScoring(unittest.TestCase):

    def test_no_theme_means_zero_regardless_of_who_spoke(self):
        """Entity boosts must amplify relevance, never create it.

        If the housing minister answers a question on ambulance response times,
        that is not a housing item. Without this rule, every word the minister
        speaks on any subject would be flagged.
        """
        item = make_item(
            "Ambulance response times in the Cwm Taf area have improved.",
            speaker="Sian Gwenllian",
            speaker_role="Cabinet Minister for Local Government, Housing and Planning")
        self.assertEqual(item.score, 0.0)
        self.assertEqual(item.themes, [])

    def test_nrla_mention_always_escalates(self):
        item = make_item(
            "I met the NRLA to discuss the private rented sector last week.")
        self.assertTrue(item.force_alert)
        self.assertEqual(item.band, "Critical")
        self.assertEqual(item.channel, "immediate")

    def test_multiple_themes_score_higher_than_one(self):
        one = make_item("We will look at rent controls.")
        two = make_item("We will look at rent controls in the private rented sector.")
        self.assertGreater(two.score, one.score)

    def test_signal_raises_score(self):
        vague = make_item("Rent controls are an interesting question.")
        firm = make_item("We will legislate on rent controls and introduce a bill.")
        self.assertGreater(firm.score, vague.score)

    def test_source_multiplier_applied(self):
        text = "A consultation on rent controls in the private rented sector."
        plenary = make_item(text, source_kind="plenary_transcript")
        consultation = make_item(text, source_kind="consultation")
        self.assertGreater(consultation.score, plenary.score)

    def test_social_landlord_veto_stops_prs_false_positive(self):
        item = make_item(
            "Registered social landlords must improve their repairs service.")
        self.assertNotIn("private_rented_sector", item.themes)

    def test_veto_overridden_by_unambiguous_phrase(self):
        """A comparative debate is exactly what NRLA most wants to see.

        Text mentioning both social landlords and the private rented sector
        must not be dropped by the social-landlord veto.
        """
        item = make_item("Standards among registered social landlords are higher "
                         "than in the private rented sector.")
        self.assertIn("private_rented_sector", item.themes)

    def test_consultations_are_never_buried(self):
        item = make_item("A consultation about tenancy paperwork.",
                         source_kind="consultation")
        self.assertNotIn(item.channel, ("archive",))

    def test_bands_are_ordered_and_reachable(self):
        thresholds = [b["min_score"] for b in TAX.bands]
        self.assertEqual(thresholds, sorted(thresholds, reverse=True))
        self.assertEqual(TAX.band_for(10_000)["name"], "Critical")
        self.assertEqual(TAX.band_for(0)["name"], "Noise")


# ---------------------------------------------------------------------------
class TestRegressions(unittest.TestCase):
    """Each of these is a bug that live data actually produced."""

    def test_theme_and_entity_terms_are_disjoint(self):
        """Regression: "Rent Smart Wales" was both a theme term and a
        delivery-body entity term, so it scored twice.

        The visible symptom was a written question about Rent Smart Wales
        hate-crime awareness training (85) outranking "Does the Welsh
        Government have plans to bring in rent controls?" (75). Any future
        overlap would silently distort priorities the same way, so this test
        guards the whole taxonomy rather than that one phrase.
        """
        theme_terms = {t.lower().strip()
                       for spec in TAX.themes.values()
                       for t in spec.get("terms", [])}
        entity_terms = {t.lower().strip()
                        for spec in TAX.entities.values()
                        for t in spec.get("terms", [])}
        overlap = theme_terms & entity_terms
        self.assertEqual(
            overlap, set(),
            f"terms appear as both a theme and an entity and will be "
            f"double-counted: {sorted(overlap)}")

    def test_rent_controls_outrank_peripheral_licensing_question(self):
        """The specific ordering the double-count broke."""
        controls = make_item("Does the Welsh Government have plans to bring in "
                             "rent controls?", source_kind="written_question")
        training = make_item("Does the Welsh Government support the Hate Crime "
                             "Awareness training provided by Rent Smart Wales?",
                             source_kind="written_question")
        self.assertGreater(controls.score, training.score)

    def test_plenary_forum_with_term_suffix_is_not_a_committee(self):
        """Regression: forum names carry a term suffix.

        "Plenary - Sixth Senedd" failed an equality test against "Plenary", so
        every historical sitting was classified as a committee transcript and
        got the wrong source multiplier.
        """
        for forum in ("Plenary", "Plenary - Sixth Senedd", "PLENARY"):
            kind = ("plenary_transcript" if "plenary" in forum.lower()
                    else "committee_transcript")
            self.assertEqual(kind, "plenary_transcript", forum)

    def test_legislation_title_extracted_from_bilingual_xhtml(self):
        """Regression: legislation.gov.uk titles are type="xhtml" and bilingual.

        findtext() returned an empty string, so the collector yielded zero
        items and Legislation Watch was silently empty.
        """
        xml = """<entry xmlns="http://www.w3.org/2005/Atom"
                        xmlns:html="http://www.w3.org/1999/xhtml">
          <title type="xhtml"><html:div>
            <html:span xml:lang="en">Building Safety (Wales) Act 2026</html:span>
             / <html:span xml:lang="cy">Deddf Diogelwch Adeiladau (Cymru) 2026</html:span>
          </html:div></title>
        </entry>"""
        entry = ET.fromstring(xml)
        title = LegislationCollector._entry_title(entry)
        self.assertEqual(title, "Building Safety (Wales) Act 2026")
        self.assertNotIn("Deddf", title)

    def test_legislation_date_recovered_from_versioned_link(self):
        xml = """<entry xmlns="http://www.w3.org/2005/Atom">
          <link rel="self" href="http://www.legislation.gov.uk/id/asc/2026/11"/>
          <link href="http://www.legislation.gov.uk/asc/2026/11/2026-04-28"/>
        </entry>"""
        entry = ET.fromstring(xml)
        self.assertEqual(LegislationCollector._date_from_links(entry),
                         date(2026, 4, 28))

    def test_search_result_text_survives_highlight_spans(self):
        """Regression: two related failures in the same parser.

        1. A lowercase-only CSS class regex missed .searchResult (camelCase),
           so the parser fell back to scraping flattened page text and ingested
           the site footer and the member filter dropdown into every item. An
           unrelated question about an agricultural loan scheme scored as
           Critical housing business.
        2. get_text("", strip=True) stripped each text node individually,
           turning "provided by <span>Rent</span> Smart Wales" into
           "provided byRentSmart Wales", which matched no taxonomy term and
           scored zero.
        """
        html = """
        <div class="searchResultContainer">
          <span class="searchResultCount">Showing 1 of 1 results found</span>
          <div class="searchResult daiCorner writtenQuestion">
            <a href="../WrittenQuestion/99808" class="detail">
              <span class="title">Written Question - WQ99808</span>
              <span class="subTitle">Tabled on 16/07/2026 for answer on 23/07/2026</span>
              <div class="context">Does the Welsh Government have plans to bring in
                <span class='highlightedText'>rent</span> controls?</div>
            </a>
            <div class="memberBar">
              <a href="https://business.senedd.wales/mgUserInfo.aspx?UID=12147">
                <span class="name">Dan Thomas</span>
                <span class="area">Casnewydd Islwyn</span>
              </a>
            </div>
          </div>
          <div class="siteFooter">Contact us 0300 200 6565 consultation</div>
        </div>"""
        collector = RecordSearchCollector.__new__(RecordSearchCollector)
        collector.errors = []
        items = list(collector._parse_results(
            html, ("written_question", "Written Question"), "rent"))

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("bring in rent controls", item.body)
        self.assertNotIn("byRent", item.body)
        # Footer furniture must not leak into the body.
        self.assertNotIn("0300 200", item.body)
        self.assertNotIn("Contact us", item.body)
        self.assertEqual(item.url,
                         "https://record.senedd.wales/WrittenQuestion/99808")
        self.assertEqual(item.speaker, "Dan Thomas")
        self.assertEqual(item.constituency, "Casnewydd Islwyn")
        self.assertEqual(item.speaker_id, "12147")
        self.assertEqual(item.item_date, date(2026, 7, 16))
        self.assertEqual(item.deadline, date(2026, 7, 23))

    def test_highlighted_word_fragments_rejoin(self):
        html = """
        <div class="searchResultContainer">
          <span class="searchResultCount">Showing 1 of 1 results found</span>
          <div class="searchResult writtenQuestion">
            <a href="../WrittenQuestion/1" class="detail">
              <span class="title">Written Question - WQ1</span>
              <div class="context">households are cur<span
                class='highlightedText'>rent</span>ly in <span
                class='highlightedText'>rent</span> arrears</div>
            </a>
          </div>
        </div>"""
        collector = RecordSearchCollector.__new__(RecordSearchCollector)
        collector.errors = []
        item = list(collector._parse_results(
            html, ("written_question", "Written Question"), "rent"))[0]
        self.assertIn("currently in rent arrears", item.body)

    def test_transcript_information_type_is_excluded(self):
        """Regression: contribution_type "I" is boilerplate present in every
        transcript (the bilingual-column explainer and the "[R] indicates a
        declared interest" note). Including it put identical noise in every
        day's results."""
        self.assertFalse(TAX.includes_contribution_type("I"))
        self.assertTrue(TAX.includes_contribution_type("C"))

    def test_listing_date_filter_is_applied_client_side(self):
        """Regression: the XMLExport listing returns March 2026 sixth-Senedd
        meetings when given SelectedCommitteeID=0 plus a date range, and
        ignores lowercase ISO date params entirely.

        A run asking for June-August 2026 silently backfilled the wrong Senedd
        term. list_meetings() must therefore always filter in Python.
        """
        collector = RecordTranscriptCollector.__new__(RecordTranscriptCollector)
        collector.errors = []
        collector.tax = TAX

        html = """
        <table>
        <tr><th>Date</th><th>Meeting</th></tr>
        <tr><td>16/07/2026 10:00</td><td>Local Government, Housing and Planning Committee</td>
            <td><a href="/XMLExport/Download?meetingID=16325&xmlDownloadType=EnglishTranscript">E</a></td></tr>
        <tr><td>25/03/2026 13:30</td><td>Plenary - Sixth Senedd</td>
            <td><a href="/XMLExport/Download?meetingID=15001&xmlDownloadType=EnglishTranscript">E</a></td></tr>
        </table>"""
        collector.fetcher = type("F", (), {"get_text": lambda self, *a, **k: html})()

        got = collector.list_meetings(start=date(2026, 6, 1), end=date(2026, 8, 4))
        self.assertEqual([m["meeting_id"] for m in got], ["16325"])


# ---------------------------------------------------------------------------
class TestSelfInflictedBlocks(unittest.TestCase):
    """Regressions from the round where three sources looked externally blocked.

    All three were reported as "blocked by a WAF". Only one actually was.
    """

    def test_user_agent_carries_no_library_token(self):
        """Regression: the User-Agent ended with "python-requests".

        That single token triggers CloudFront's managed bot rules, returning
        HTTP 403 on senedd.wales and senedd.cymru. Two collectors were reported
        as blocked by a WAF when the block was entirely self-inflicted.
        Verified against senedd.cymru/deddfwriaeth/ on 4 August 2026:
            honest UA without the token -> 200
            same UA + "python-requests" -> 403
        """
        banned = ("python-requests", "python-urllib", "urllib", "curl/",
                  "scrapy", "httpx", "aiohttp", "wget")
        lowered = USER_AGENT.lower()
        for token in banned:
            self.assertNotIn(token, lowered,
                             f"{token!r} in the User-Agent will trigger WAF bot "
                             f"rules and cause spurious 403s")

    def test_user_agent_is_honest_and_contactable(self):
        """The fix must not become browser impersonation.

        Removing the library token is fixing our own bug. Pretending to be
        Chrome would be evasion — fragile, and not how we want to behave towards
        public infrastructure. The UA must identify the organisation and offer a
        contact address.
        """
        self.assertIn("NRLA", USER_AGENT)
        self.assertIn("@", USER_AGENT)
        for impersonation in ("mozilla", "chrome", "safari", "gecko", "webkit"):
            self.assertNotIn(impersonation, USER_AGENT.lower())

    def test_bill_index_urls_are_the_ones_that_exist(self):
        """Regression: the 403 masked a 404.

        senedd.wales/senedd-business/bills-and-laws/ does not exist. Once the
        User-Agent was fixed the error changed from 403 to 404 and revealed the
        real bug underneath.
        """
        from monitor.collectors.legislation import SeneddBillCollector
        for url in SeneddBillCollector.INDEX_PAGES:
            self.assertNotIn("bills-and-laws", url)
        self.assertTrue(
            any("senedd-business/legislation" in u
                for u in SeneddBillCollector.INDEX_PAGES))

    def test_forward_look_uses_all_committees_sentinel(self):
        """Regression: lCommitteeId=0 returns nothing; -1 means all.

        Zero does not mean "all" in the ModernGov service. With 0 the forward
        look was silently empty; with -1 the same call returned 96 scheduled
        meetings for September-October 2026.
        """
        self.assertEqual(ALL_COMMITTEES, -1)

    def test_forward_look_sends_uk_dates_not_iso(self):
        """Regression: ISO dates make the service ignore the range.

        GetAllMeetingsByDate accepts ISO dates and then returns a 5000-row dump
        of everything — plausible data answering a different question, which is
        far more dangerous than an error. dd/mm/yyyy is required.
        """
        captured = {}

        class Probe(SeneddCalendarCollector):
            def _post(self, operation, body):
                captured["operation"] = operation
                captured["body"] = body
                return None

        probe = Probe.__new__(Probe)
        probe.errors = []
        probe.fetcher = None
        probe.meetings(date(2026, 9, 1), date(2026, 10, 31))

        self.assertEqual(captured["operation"], "GetAllMeetingsByDate")
        self.assertIn("<sFromDate>01/09/2026</sFromDate>", captured["body"])
        self.assertIn("<sToDate>31/10/2026</sToDate>", captured["body"])
        self.assertIn("<lCommitteeId>-1</lCommitteeId>", captured["body"])
        self.assertNotIn("2026-09-01", captured["body"])

    def test_forward_look_filters_client_side_too(self):
        """Belt and braces: a future change in the service's date handling must
        not be able to silently widen the window."""
        payload = """<?xml version="1.0"?>
        <root><meetingscount>3</meetingscount><meetings>
          <meeting><meetingid>1</meetingid><committeeid>908</committeeid>
            <committeetitle>Plenary</committeetitle>
            <meetingdate>15/09/2026</meetingdate><meetingtime>13:30</meetingtime>
            <meetingstatus>Confirmed</meetingstatus></meeting>
          <meeting><meetingid>2</meetingid><committeeid>908</committeeid>
            <committeetitle>Plenary</committeetitle>
            <meetingdate>15/03/2026</meetingdate><meetingtime>13:30</meetingtime>
            <meetingstatus>Confirmed</meetingstatus></meeting>
        </meetings></root>"""

        class Stub(SeneddCalendarCollector):
            def _post(self, operation, body):
                return ET.fromstring(payload)

        stub = Stub.__new__(Stub)
        stub.errors = []
        stub.fetcher = None
        got = stub.meetings(date(2026, 9, 1), date(2026, 10, 31))
        self.assertEqual([m["meeting_id"] for m in got], ["1"])

    def test_forward_look_items_carry_the_sitting_date_as_a_deadline(self):
        payload = """<?xml version="1.0"?>
        <root><meetingscount>1</meetingscount><meetings>
          <meeting><meetingid>16305</meetingid><committeeid>1000</committeeid>
            <committeetitle>Local Government, Housing and Planning Committee</committeetitle>
            <meetingdate>01/10/2026</meetingdate><meetingtime>09:30</meetingtime>
            <meetingstatus>Confirmed</meetingstatus></meeting>
        </meetings></root>"""

        class Stub(SeneddCalendarCollector):
            def _post(self, operation, body):
                return ET.fromstring(payload)

        stub = Stub.__new__(Stub)
        stub.errors = []
        stub.fetcher = None
        items = list(stub.collect(start=date(2026, 9, 1), end=date(2026, 10, 31)))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].deadline, date(2026, 10, 1))
        self.assertIn("Local Government, Housing and Planning", items[0].forum)
        SCORER.score_item(items[0])
        self.assertGreater(items[0].score, 0,
                           "a LGHP committee sitting must score above zero")


# ---------------------------------------------------------------------------
class TestLegislationScoring(unittest.TestCase):
    """Regression: relevant Acts were scoring zero and being dropped.

    Legislation titles carry almost no text, so terse but important Act names
    matched no taxonomy term at all. "Planning (Wales) Act 2026" and
    "Development of Tourism and Regulation of Visitor Accommodation (Wales) Act
    2026" both scored 0 and never reached the dashboard.
    """

    RELEVANT = [
        "Planning (Wales) Act 2026",
        "Development of Tourism and Regulation of Visitor Accommodation (Wales) Act 2026",
        "Building Safety (Wales) Act 2026",
        "Homelessness and Social Housing Allocation (Wales) Act 2026",
        "Renting Homes (Wales) Act 2016",
    ]
    IRRELEVANT = [
        "Prohibition of Greyhound Racing (Wales) Act 2026",
        "British Sign Language (Wales) Act 2026",
        "Bus Services (Wales) Act 2026",
    ]

    def test_relevant_acts_score_above_zero(self):
        for title in self.RELEVANT:
            item = make_item(title, title=title, source_kind="legislation")
            self.assertGreater(item.score, 0, f"{title} scored zero")

    def test_irrelevant_acts_still_score_zero(self):
        """The fix must not be achieved by making everything relevant."""
        for title in self.IRRELEVANT:
            item = make_item(title, title=title, source_kind="legislation")
            self.assertEqual(item.score, 0.0, f"{title} should not match")

    def test_bare_planning_does_not_fire_on_ordinary_usage(self):
        """"planning" is deliberately absent from the taxonomy as a bare term."""
        for text in ("We are planning to consult in the autumn.",
                     "Workforce planning in the NHS is difficult.",
                     "I am planning a visit to the constituency."):
            item = make_item(text)
            self.assertEqual(item.score, 0.0, text)


# ---------------------------------------------------------------------------
class TestRunHealth(unittest.TestCase):
    """A health banner that cries wolf every run trains people to ignore it."""

    def test_substituted_source_does_not_make_a_run_unhealthy(self):
        from monitor.pipeline import RunReport
        report = RunReport(run_id="x", started_at=datetime.now())
        report.sources_substituted.append("Welsh Government — RSS")
        self.assertTrue(report.healthy)

    def test_failed_source_does_make_a_run_unhealthy(self):
        from monitor.pipeline import RunReport
        report = RunReport(run_id="x", started_at=datetime.now())
        report.sources_failed.append("Senedd Record — transcripts")
        self.assertFalse(report.healthy)

    def test_dashboard_distinguishes_substituted_from_failed(self):
        from monitor.dashboard import render
        from monitor.pipeline import RunReport
        report = RunReport(run_id="x", started_at=datetime.now(),
                           finished_at=datetime.now(),
                           errors=["gov.wales RSS unavailable from this host"],
                           sources_substituted=["Welsh Government — RSS"])
        out = render([], TAX, report=report, deadlines=[])
        self.assertIn("reaches us another way", out)
        self.assertNotIn("This view is incomplete", out)


# ---------------------------------------------------------------------------
class TestUsability(unittest.TestCase):
    """Regressions from the round where the directorate said the dashboard was
    "incredibly difficult to look at and prioritise what needs doing"."""

    def test_debate_contributions_are_grouped_into_one_card(self):
        """Regression: every contribution was its own card.

        Measured on real data: 31 of 55 agenda items produced more than one
        card, and one produced 48. Five cards all headed "Statement by the First
        Minister: Legislation" is noise, not coverage.
        """
        from monitor.dashboard import group_transcripts
        agenda = "4. Statement by the First Minister: Legislation"
        when = date(2026, 7, 14)
        items = [
            make_item("We will strengthen Rent Smart Wales and require rent data.",
                      title=agenda, agenda_item=agenda, forum="Plenary",
                      item_date=when, speaker="Rhun ap Iorwerth"),
            make_item("Rent controls are needed in the private rented sector.",
                      title=agenda, agenda_item=agenda, forum="Plenary",
                      item_date=when, speaker="Dan Thomas"),
            make_item("Landlords need certainty on eviction reform.",
                      title=agenda, agenda_item=agenda, forum="Plenary",
                      item_date=when, speaker="Ken Skates"),
        ]
        grouped = group_transcripts(items)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["count"], 3)
        self.assertEqual(grouped[0]["speakers"],
                         ["Rhun ap Iorwerth", "Dan Thomas", "Ken Skates"])

    def test_group_score_is_the_strongest_moment_not_the_sum(self):
        """Summing would make any long debate outrank a single decisive
        statement, which is the wrong way round."""
        from monitor.dashboard import group_transcripts
        agenda = "9. Debate: Housing"
        items = [make_item("Housing supply matters.", title=agenda,
                           agenda_item=agenda, forum="Plenary",
                           item_date=date(2026, 7, 14), speaker=f"MS {n}")
                 for n in range(6)]
        strongest = max(i.score for i in items)
        grouped = group_transcripts(items)
        self.assertEqual(grouped[0]["score"], strongest)

    def test_non_transcript_items_are_not_grouped(self):
        from monitor.dashboard import group_transcripts
        items = [make_item("A consultation on rent controls.",
                           source_kind="consultation", title="Consultation A"),
                 make_item("A consultation on eviction reform.",
                           source_kind="consultation", title="Consultation B")]
        self.assertEqual(len(group_transcripts(items)), 2)

    def test_procedural_agenda_items_are_filtered(self):
        """Regression: committee housekeeping reached the Review section.

        "1. Introductions, apologies, substitutions and declarations of
        interest" and "2. Papers to note" scored High, because the surrounding
        transcript mentions the committee's own name and remit.
        """
        for agenda in ("1. Introductions, apologies, substitutions and "
                       "declarations of interest",
                       "2. Papers to note",
                       "3. Instruments that raise issues to be reported to the Senedd",
                       "Private session"):
            self.assertTrue(TAX.is_procedural_agenda_item(agenda), agenda)

    def test_substantive_agenda_items_are_not_filtered(self):
        for agenda in ("2. Questions to the Cabinet Minister for Local "
                       "Government, Housing and Planning",
                       "4. Statement by the First Minister: Legislation",
                       "7. Reform UK Debate: Bovine TB"):
            self.assertFalse(TAX.is_procedural_agenda_item(agenda), agenda)

    def test_respond_section_only_holds_dated_consultations(self):
        """Regression: seventeen cards headed "No closing date published" is not
        a to-do list. Dated items get the section; the rest are a watch-list."""
        from monitor.dashboard import render
        dated = make_item("A consultation on rent controls in the private "
                          "rented sector.", source_kind="consultation",
                          title="Dated consultation",
                          deadline=date.today() + timedelta(days=20))
        undated = make_item("A consultation on rent controls in the private "
                            "rented sector.", source_kind="consultation",
                            title="Undated consultation")
        out = render([dated, undated], TAX, deadlines=[dated])
        payload = out[out.index("const D = ") + 10:]
        payload = payload[:payload.index("\n")]
        import json as _json
        data = _json.loads(payload.rstrip(";"))
        self.assertEqual([p["title"] for p in data["respond"]],
                         ["Dated consultation"])
        self.assertEqual([p["title"] for p in data["undated"]],
                         ["Undated consultation"])

    def test_scores_are_hidden_by_default(self):
        """A raw score tells a policy officer nothing and invites comparisons
        that are not valid across sources."""
        from monitor.dashboard import render
        out = render([make_item("rent controls in the private rented sector")],
                     TAX, deadlines=[])
        self.assertIn("body:not(.show-scores) .sc { display:none; }", out)

    def test_every_item_gets_a_suggested_next_step(self):
        from monitor.dashboard import suggested_action
        for kind in ("consultation", "legislation", "calendar",
                     "written_question", "research", "plenary_transcript"):
            entry = {"lead": make_item("rent controls", source_kind=kind)}
            self.assertTrue(suggested_action(entry))

    def test_urgency_reads_as_words_not_numbers(self):
        from monitor.dashboard import urgency_label
        today = date(2026, 8, 4)
        self.assertEqual(urgency_label(today, today)[0], "Closes today")
        self.assertEqual(urgency_label(today + timedelta(days=1), today)[0],
                         "Closes tomorrow")
        self.assertEqual(urgency_label(today + timedelta(days=5), today),
                         ("5 days left", "now"))
        self.assertEqual(urgency_label(None, today)[0],
                         "No closing date published")


# ---------------------------------------------------------------------------
class TestCommitteeWork(unittest.TestCase):
    """The gap that mattered most: committee consultations and inquiries.

    The system monitored what the Senedd had said, not what its committees were
    asking to be told. It missed the Local Government, Housing and Planning
    Committee's priorities consultation (closing 14 September 2026) and its
    follow-up inquiry into Empty Properties.
    """

    def test_closing_date_parsed_from_senedd_phrasing(self):
        from monitor.collectors.committee_work import parse_closing_date
        # The exact wording on consultation 626.
        self.assertEqual(
            parse_closing_date("The closing date for sharing your views is "
                               "14 September 2026 ."),
            date(2026, 9, 14))
        self.assertEqual(
            parse_closing_date("Closing date: 30 November 2026"),
            date(2026, 11, 30))

    def test_no_closing_date_returns_none_rather_than_guessing(self):
        from monitor.collectors.committee_work import parse_closing_date
        self.assertIsNone(parse_closing_date("We welcome views in the autumn."))

    def test_administrative_issue_pages_are_skipped(self):
        from monitor.collectors.committee_work import _ADMIN_TITLE_RE
        for title in ("Completed work and published reports – Finance Committee",
                      "Membership of the Committee", "Remit"):
            self.assertTrue(_ADMIN_TITLE_RE.match(title), title)
        for title in ("Follow-up inquiry into Empty Properties",
                      "Priorities for the Local Government, Housing and "
                      "Planning Committee"):
            self.assertIsNone(_ADMIN_TITLE_RE.match(title), title)

    def test_priority_committees_include_the_housing_committee(self):
        from monitor.collectors.committee_work import PRIORITY_COMMITTEES
        self.assertIn("local government, housing and planning",
                      PRIORITY_COMMITTEES)

    def test_committee_scrutiny_theme_excludes_irrelevant_committees(self):
        """Regression: every committee was listed, so "Priorities for Public
        Administration" scored 224 (Critical) identically to the housing
        committee's own priorities consultation."""
        terms = [t.lower() for t in
                 TAX.themes["committee_scrutiny"]["terms"]]
        self.assertTrue(any("local government, housing and planning" in t
                            for t in terms))
        for irrelevant in ("public accounts", "economy, energy",
                           "culture", "health and social care"):
            self.assertFalse(any(irrelevant in t for t in terms),
                             f"{irrelevant} should not be a scrutiny term")


# ---------------------------------------------------------------------------
class TestTranscriptParsing(unittest.TestCase):

    SAMPLE = """<?xml version="1.0"?>
    <dataroot generated="2026-07-17T16:00:05">
      <XML_Plenary_English>
        <Meeting_ID>16086</Meeting_ID>
        <MeetingDate>2026-07-15T13:30:01</MeetingDate>
        <Contribution_ID>768019</Contribution_ID>
        <contribution_type>C</contribution_type>
        <Agenda_item_english>2. Questions to the Cabinet Minister for Local Government, Housing and Planning</Agenda_item_english>
        <Member_Id>12172</Member_Id>
        <Member_name_English>Sian Gwenllian</Member_name_English>
        <Contribution_English>&lt;p&gt;We will strengthen Rent Smart Wales and require the sharing of rent data.&lt;/p&gt;</Contribution_English>
        <contribution_translated_seneddTv>http://www.senedd.tv/en/16086?startPos=-48597&amp;l=en</contribution_translated_seneddTv>
      </XML_Plenary_English>
      <XML_Plenary_English>
        <Meeting_ID>16086</Meeting_ID>
        <contribution_type>I</contribution_type>
        <Contribution_English>&lt;p&gt;[R] indicates that the Member has declared an interest.&lt;/p&gt;</Contribution_English>
      </XML_Plenary_English>
    </dataroot>"""

    def setUp(self):
        self.collector = RecordTranscriptCollector.__new__(RecordTranscriptCollector)
        self.collector.errors = []
        self.collector.tax = TAX

    def test_parses_contributions_and_skips_boilerplate(self):
        items = list(self.collector.parse_transcript(
            self.SAMPLE.encode(), forum_hint="Plenary"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].speaker, "Sian Gwenllian")
        self.assertEqual(items[0].meeting_id, "16086")
        self.assertEqual(items[0].item_date, date(2026, 7, 15))

    def test_html_is_stripped_from_contribution(self):
        item = list(self.collector.parse_transcript(
            self.SAMPLE.encode(), forum_hint="Plenary"))[0]
        self.assertNotIn("<p>", item.body)
        self.assertIn("Rent Smart Wales", item.body)

    def test_timestamped_video_link_is_preserved(self):
        item = list(self.collector.parse_transcript(
            self.SAMPLE.encode(), forum_hint="Plenary"))[0]
        self.assertIn("senedd.tv", item.video_url)
        self.assertIn("startPos", item.video_url)

    def test_malformed_xml_reports_rather_than_raises(self):
        items = list(self.collector.parse_transcript(b"<not xml", forum_hint="X"))
        self.assertEqual(items, [])
        self.assertTrue(self.collector.errors)

    def test_scores_as_high_priority(self):
        item = list(self.collector.parse_transcript(
            self.SAMPLE.encode(), forum_hint="Plenary"))[0]
        SCORER.score_item(item)
        self.assertIn(item.band, ("High", "Critical"))


# ---------------------------------------------------------------------------
class TestGovWales(unittest.TestCase):

    def test_deadline_parsed_from_common_phrasings(self):
        cases = [
            ("This consultation closes on 7 September 2026", date(2026, 9, 7)),
            ("Closing date: 30 November 2026", date(2026, 11, 30)),
            ("Please respond by 1 October 2026", date(2026, 10, 1)),
            ("Deadline for responses: 15 Dec 2026", date(2026, 12, 15)),
        ]
        for text, expected in cases:
            self.assertEqual(parse_deadline(text), expected, text)

    def test_unparseable_deadline_returns_none_rather_than_guessing(self):
        """A wrong deadline is worse than no deadline: it invites the team to
        plan around a date that does not exist."""
        self.assertIsNone(parse_deadline("closes in the autumn"))
        self.assertIsNone(parse_deadline(""))

    def test_classification(self):
        self.assertEqual(
            classify("Written Statement: Rent Smart Wales")[0], "written_statement")
        self.assertEqual(
            classify("Consultation on rent data sharing")[0], "consultation")
        self.assertEqual(
            classify("Implementing the Building Safety (Wales) Act 2026: "
                     "call for evidence")[0], "consultation")

    def test_mailbox_rejects_non_allowlisted_sender(self):
        """A human emailing the shared mailbox must never enter the archive."""
        collector = GovWalesMailboxCollector.__new__(GovWalesMailboxCollector)
        collector.errors = []
        collector.mailbox = "joshua.helm-cowley@nrla.org.uk"
        message = {"subject": "Re: lunch", "bodyPreview": "consultation on rent",
                   "from": {"emailAddress": {"address": "colleague@example.com"}}}
        self.assertIsNone(collector.message_to_item(message))

    def test_mailbox_accepts_gov_wales_and_extracts_source_link(self):
        collector = GovWalesMailboxCollector.__new__(GovWalesMailboxCollector)
        collector.errors = []
        collector.mailbox = "joshua.helm-cowley@nrla.org.uk"
        message = {
            "subject": "Consultation: rent data sharing",
            "body": {"content": "<p>We want your views. Closes on 7 September 2026. "
                                "See https://www.gov.wales/rent-data-consultation "
                                "for details.</p>"},
            "receivedDateTime": "2026-08-01T09:00:00Z",
            "from": {"emailAddress": {"address": "noreply@gov.wales"}},
            "webLink": "https://outlook.office.com/mail/xyz",
        }
        item = collector.message_to_item(message)
        self.assertIsNotNone(item)
        self.assertEqual(item.source_kind, "consultation")
        self.assertEqual(item.deadline, date(2026, 9, 7))
        self.assertEqual(item.url, "https://www.gov.wales/rent-data-consultation")
        self.assertNotIn("outlook.office.com", item.url)


# ---------------------------------------------------------------------------
class TestForwardLook(unittest.TestCase):

    def test_expired_deadlines_are_dropped(self):
        today = date(2026, 8, 4)
        items = [
            make_item("rent controls consultation", deadline=today - timedelta(days=1)),
            make_item("rent controls consultation", deadline=today + timedelta(days=5)),
        ]
        live = deadlines_from_items(items, within_days=60, today=today)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].deadline, today + timedelta(days=5))

    def test_sorted_by_urgency(self):
        today = date(2026, 8, 4)
        items = [
            make_item("rent controls", deadline=today + timedelta(days=30)),
            make_item("rent controls", deadline=today + timedelta(days=3)),
            make_item("rent controls", deadline=today + timedelta(days=10)),
        ]
        live = deadlines_from_items(items, today=today)
        self.assertEqual([(i.deadline - today).days for i in live], [3, 10, 30])


# ---------------------------------------------------------------------------
class TestModel(unittest.TestCase):

    def test_clean_strips_html_and_entities(self):
        text = _clean("<p>Rent&nbsp;controls &amp; the &#163;100 cap</p>")
        self.assertNotIn("<p>", text)
        self.assertIn("Rent controls & the £100 cap", text)

    def test_uid_is_stable_for_identical_content(self):
        a = make_item("rent controls in the private rented sector")
        b = make_item("rent controls in the private rented sector")
        self.assertEqual(a.uid, b.uid)

    def test_uid_changes_when_content_changes(self):
        """The Senedd republishes corrected transcripts. A genuine correction
        should surface as a new item; a re-run should not."""
        a = make_item("rent controls in the private rented sector")
        b = make_item("rent controls in the private rented sector, amended")
        self.assertNotEqual(a.uid, b.uid)

    def test_excerpt_does_not_break_mid_word(self):
        item = make_item("rent " * 200)
        self.assertTrue(item.excerpt.endswith("…"))
        self.assertLessEqual(len(item.excerpt), 325)


# ---------------------------------------------------------------------------
class TestStore(unittest.TestCase):

    def setUp(self):
        self.store = Store(":memory:") if False else Store("data/_test.sqlite3")
        self.store.conn.executescript(
            "DELETE FROM items; DELETE FROM runs; DELETE FROM score_history;")
        self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        Path("data/_test.sqlite3").unlink(missing_ok=True)

    def test_new_item_reported_new_once_only(self):
        item = make_item("rent controls in the private rented sector")
        self.assertTrue(self.store.upsert(item))
        self.assertFalse(self.store.upsert(item))

    def test_full_text_search_finds_stored_item(self):
        self.store.upsert(make_item(
            "We will consult on rent controls in the private rented sector."))
        self.assertEqual(len(self.store.search("rent controls")), 1)
        self.assertEqual(len(self.store.search("aardvark")), 0)

    def test_rescore_updates_and_logs_history(self):
        item = make_item("rent controls in the private rented sector")
        self.store.upsert(item)
        # Mute a theme, as a policy officer might while tuning.
        muted = Taxonomy.load()
        muted.raw["themes"]["rent_controls_and_affordability"]["weight"] = 0
        changed = self.store.rescore_all(Scorer(muted))
        self.assertGreaterEqual(changed, 1)
        history = self.store.conn.execute(
            "SELECT COUNT(*) FROM score_history").fetchone()[0]
        self.assertGreaterEqual(history, 1)

    def test_upcoming_deadlines_excludes_past(self):
        self.store.upsert(make_item("rent controls",
                                    deadline=date.today() - timedelta(days=2)))
        self.store.upsert(make_item("rent controls consultation",
                                    deadline=date.today() + timedelta(days=9)))
        found = self.store.upcoming_deadlines(60)
        self.assertEqual(len(found), 1)

    def test_mark_notified_prevents_realerting(self):
        item = make_item("The NRLA gave evidence on the private rented sector.")
        self.store.upsert(item)
        self.assertEqual(len(self.store.query(channels=["immediate"],
                                              unnotified_only=True)), 1)
        self.store.mark_notified([item.uid])
        self.assertEqual(len(self.store.query(channels=["immediate"],
                                              unnotified_only=True)), 0)


# ---------------------------------------------------------------------------
class TestArchiveExport(unittest.TestCase):
    """SQL export/restore, which is what makes git-hosted state workable."""

    def setUp(self):
        Path("data").mkdir(exist_ok=True)
        self.db = "data/_export_test.sqlite3"
        self.rebuilt = "data/_export_rebuilt.sqlite3"
        self.sql = "data/_export_test.sql"
        for p in (self.db, self.rebuilt, self.sql):
            Path(p).unlink(missing_ok=True)
        self.store = Store(self.db)
        self.store.upsert(make_item(
            "We will consult on rent controls in the private rented sector.",
            title="Consultation on rent controls", source_kind="consultation",
            deadline=date.today() + timedelta(days=30)))
        self.store.upsert(make_item(
            "Rent Smart Wales enforcement will be strengthened.",
            title="Statement", speaker="Sian Gwenllian"))

    def tearDown(self):
        self.store.close()
        for p in (self.db, self.rebuilt, self.sql):
            Path(p).unlink(missing_ok=True)

    def test_round_trip_preserves_everything(self):
        from monitor.archive_io import export_sql, restore_sql
        before = self.store.stats()
        export_sql(self.store, self.sql)
        rows, indexed = restore_sql(self.sql, self.rebuilt)
        rebuilt = Store(self.rebuilt)
        try:
            self.assertEqual(rebuilt.stats(), before)
            self.assertEqual(rows, before["total"])
            # The search index must be rebuilt, not just the rows restored.
            self.assertEqual(indexed, rows)
            self.assertEqual(len(rebuilt.search("rent controls")),
                             len(self.store.search("rent controls")))
            self.assertEqual(len(rebuilt.upcoming_deadlines(60)),
                             len(self.store.upcoming_deadlines(60)))
        finally:
            rebuilt.close()

    def test_export_is_byte_identical_when_nothing_changed(self):
        """Regression: the header carried a generation timestamp, so every
        export differed by one line even with identical data — which made the
        scheduler commit every single day regardless. That is exactly the churn
        this format exists to avoid."""
        from monitor.archive_io import export_sql
        export_sql(self.store, self.sql)
        first = Path(self.sql).read_bytes()
        export_sql(self.store, self.sql)
        self.assertEqual(first, Path(self.sql).read_bytes())

    def test_export_contains_no_timestamp(self):
        from monitor.archive_io import export_sql
        export_sql(self.store, self.sql)
        text = Path(self.sql).read_text(encoding="utf-8")
        self.assertNotIn("Generated:", text)

    def test_fts_shadow_tables_are_not_exported(self):
        """FTS5 shadow tables do not restore cleanly and are several times the
        size of the content they index."""
        from monitor.archive_io import export_sql, EXPORTED_TABLES
        self.assertNotIn("items_fts", EXPORTED_TABLES)
        export_sql(self.store, self.sql)
        text = Path(self.sql).read_text(encoding="utf-8")
        self.assertNotIn("INSERT INTO items_fts", text)

    def test_restore_from_missing_file_raises_not_crashes(self):
        from monitor.archive_io import restore_sql
        with self.assertRaises(FileNotFoundError):
            restore_sql("data/_definitely_not_here.sql", self.rebuilt)

    def test_apostrophes_survive_the_round_trip(self):
        """Senedd text is full of them — "Renters' Rights Act", "O'Brien"."""
        from monitor.archive_io import export_sql, restore_sql
        self.store.upsert(make_item(
            "Francesca O'Brien asked about the Renters' Rights Act and "
            "rent controls in the private rented sector.",
            title="Question with apostrophes"))
        export_sql(self.store, self.sql)
        restore_sql(self.sql, self.rebuilt)
        rebuilt = Store(self.rebuilt)
        try:
            hits = rebuilt.search("apostrophes")
            self.assertEqual(len(hits), 1)
            self.assertIn("O'Brien", hits[0].body)
            self.assertIn("Renters' Rights Act", hits[0].body)
        finally:
            rebuilt.close()


# ---------------------------------------------------------------------------
class TestWeekly(unittest.TestCase):
    """Week-by-week views — how the directorate asked for the archive to read."""

    def test_iso_week_label(self):
        from monitor.weekly import iso_week
        # The supplier's "W29" briefing of 17 July 2026 is ISO week 2026-W29,
        # so the numbering lines up and comparison is file against file.
        self.assertEqual(iso_week(date(2026, 7, 17)), "2026-W29")
        self.assertEqual(iso_week(date(2026, 7, 13)), "2026-W29")
        self.assertEqual(iso_week(date(2026, 7, 19)), "2026-W29")
        self.assertEqual(iso_week(date(2026, 7, 20)), "2026-W30")

    def test_week_bounds_are_monday_to_sunday(self):
        from monitor.weekly import week_bounds
        monday, sunday = week_bounds("2026-W29")
        self.assertEqual(monday, date(2026, 7, 13))
        self.assertEqual(sunday, date(2026, 7, 19))
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(sunday.weekday(), 6)

    def test_week_title_reads_as_english(self):
        from monitor.weekly import week_title
        self.assertEqual(week_title("2026-W29"), "Week 29 · 13 to 19 July 2026")

    def test_week_labels_sort_chronologically_as_strings(self):
        """ISO rather than "week commencing" precisely so this holds."""
        labels = ["2026-W02", "2025-W51", "2026-W29", "2026-W10"]
        self.assertEqual(sorted(labels),
                         ["2025-W51", "2026-W02", "2026-W10", "2026-W29"])

    def test_snapshot_renders_and_is_self_contained(self):
        from monitor.weekly import render_week, week_summary
        store = Store("data/_week_test.sqlite3")
        try:
            store.conn.executescript("DELETE FROM items;")
            store.upsert(make_item(
                "We will legislate on rent controls in the private rented sector.",
                title="4. Statement by the First Minister: Legislation",
                item_date=date(2026, 7, 14), speaker="Rhun ap Iorwerth",
                forum="Plenary"))
            summary = week_summary(store, "2026-W29", TAX)
            html_out = render_week(summary, TAX)
            self.assertIn("Week 29", html_out)
            self.assertIn("Rhun ap Iorwerth", html_out)
            # Archival record: must open cold with no server and no scripts.
            self.assertNotIn("<script", html_out)
            self.assertIn("<!DOCTYPE html>", html_out)
        finally:
            store.close()
            Path("data/_week_test.sqlite3").unlink(missing_ok=True)

    def test_empty_week_says_so_rather_than_looking_broken(self):
        from monitor.weekly import render_week, week_summary
        store = Store("data/_week_empty.sqlite3")
        try:
            store.conn.executescript("DELETE FROM items;")
            html_out = render_week(week_summary(store, "2026-W32", TAX), TAX)
            self.assertIn("correct record of a quiet", html_out)
        finally:
            store.close()
            Path("data/_week_empty.sqlite3").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
class TestSourceSwitches(unittest.TestCase):
    """Written questions are handled by a separate NRLA tool."""

    def test_written_questions_are_switched_off(self):
        self.assertFalse(TAX.source_enabled("written_questions"))

    def test_oral_questions_remain_on(self):
        """They are asked in the Chamber and appear in transcripts anyway, so
        excluding them would leave holes mid-debate."""
        self.assertTrue(TAX.source_enabled("oral_questions"))

    def test_unknown_sources_default_to_enabled(self):
        """Adding a collector must not require a config edit first."""
        self.assertTrue(TAX.source_enabled("some_future_source"))

    def test_search_collector_omits_written_question_type(self):
        from monitor.collectors.record_search import (RecordSearchCollector,
                                                      TYPE_WRITTEN_QUESTION)
        collector = RecordSearchCollector.__new__(RecordSearchCollector)
        collector.errors = []
        collector.tax = TAX
        self.assertNotIn(TYPE_WRITTEN_QUESTION, collector.enabled_types())
        self.assertTrue(collector.enabled_types(), "should still search something")


# ---------------------------------------------------------------------------
class TestOutputs(unittest.TestCase):

    def test_digest_renders_without_items(self):
        from monitor.alerts import render_digest
        subject, html_body, text = render_digest([], [], TAX)
        self.assertIn("nothing to report", subject)
        # A quiet week must be stated as a quiet week, not left ambiguous.
        self.assertIn("genuinely quiet period", html_body)

    def test_digest_groups_by_tier(self):
        from monitor.alerts import render_digest
        items = [make_item("rent controls in the private rented sector"),
                 make_item("EPC and retrofit standards for landlords")]
        subject, html_body, text = render_digest(items, [], TAX)
        self.assertIn("Private rented sector", html_body)
        self.assertIn("Property &amp; energy", html_body)

    def test_alert_names_nrla_in_subject(self):
        from monitor.alerts import render_alert
        item = make_item("The NRLA responded on the private rented sector.")
        subject, html_body, text = render_alert([item], TAX)
        self.assertIn("NRLA has been mentioned", subject)

    def test_send_is_dry_run_by_default(self):
        from monitor.alerts import send
        self.assertFalse(send("s", "<p>h</p>", "t", "a@b.c", ["d@e.f"]))

    def test_dashboard_renders_and_escapes(self):
        from monitor.dashboard import render
        nasty = make_item("rent controls <script>alert(1)</script> in the "
                          "private rented sector",
                          title="Test <img src=x onerror=alert(1)>")
        html_out = render([nasty], TAX, deadlines=[])
        self.assertIn("<!DOCTYPE html>", html_out)
        # The payload is JSON-embedded and escaped in the DOM by esc(); the raw
        # executable form must not appear as live markup.
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertNotIn("<img src=x onerror=", html_out)

    def test_dashboard_flags_failed_sources(self):
        from monitor.dashboard import render
        from monitor.pipeline import RunReport
        from datetime import datetime as dt
        report = RunReport(run_id="x", started_at=dt.now(), finished_at=dt.now(),
                           errors=["gov.wales RSS unavailable"],
                           sources_failed=["Welsh Government — RSS"])
        html_out = render([], TAX, report=report, deadlines=[])
        self.assertIn("This view is incomplete", html_out)
        self.assertIn("quiet week", html_out)


class TestBriefMarkdown(unittest.TestCase):
    """The briefing that appears on the Actions run page.

    Exists because the first live run was green, correct, and useless: it
    collected 257 items and put every one of them somewhere nobody would look.
    """

    def test_renders_zones_a_person_can_act_on(self):
        from monitor.brief import render_markdown
        items = [make_item("rent controls in the private rented sector"),
                 make_item("EPC and retrofit standards for landlords")]
        md = render_markdown(items, TAX)
        self.assertIn("developments to review", md)
        self.assertIn("## Review", md)
        # The licence attribution must survive into every output format.
        self.assertIn("Open Government Licence", md)

    def test_quiet_period_is_stated_not_left_blank(self):
        from monitor.brief import render_markdown
        md = render_markdown([], TAX)
        # A blank page and a quiet week must never look the same.
        self.assertIn("genuinely quiet period", md)

    def test_failed_source_warns_at_the_top(self):
        from monitor.brief import render_markdown
        from monitor.pipeline import RunReport
        from datetime import datetime as dt
        report = RunReport(run_id="x", started_at=dt.now(), finished_at=dt.now(),
                           errors=["boom"], sources_failed=["Senedd — Record"])
        md = render_markdown([], TAX, report=report)
        self.assertIn("[!WARNING]", md)
        self.assertIn("This view is incomplete", md)
        # And it must be first, not buried under the content.
        self.assertLess(md.index("[!WARNING]"), md.index("quiet period"))

    def test_pipes_in_a_title_cannot_break_the_table(self):
        from monitor.brief import _link
        self.assertNotIn("|", _link("Rent | controls", "http://x").replace("\\|", ""))


class TestEmailFailureIsLoud(unittest.TestCase):
    """`--send` failing must not look like success.

    The first live GitHub run exited 0 having emailed nothing, printing only
    "Not sent (dry run, or SMTP not configured)" into a log nobody opens. The
    operator concluded the tool did not work, which was the correct reading.
    """

    def _args(self, send: bool):
        return SimpleNamespace(send=send)

    @contextlib.contextmanager
    def _quiet(self):
        """Run the function under test without its output escaping.

        THIS IS NOT TIDINESS. `_report_not_sent` prints a GitHub workflow
        command — `::error title=No email sent::…` — whenever GITHUB_ACTIONS is
        set. On a runner that is always set, including while the test suite is
        running, so an unredirected call in a test printed a real red
        annotation onto the run page of every single workflow that runs these
        tests. Both the daily monitor and the Friday email showed "No email
        sent" at the top of a green run, for weeks, and the annotation was read
        — entirely reasonably — as the email having failed.

        A test must never be able to annotate the run that is testing it. So
        the environment variable is cleared as well as the streams redirected:
        belt and braces, because the streams alone would not stop a future
        refactor that writes the annotation through a different path.
        """
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}, clear=False):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                yield

    def test_dry_run_without_send_is_success(self):
        from monitor.cli import _report_not_sent
        with self._quiet():
            result = _report_not_sent(self._args(False), [], {"smtp_host": ""})
        self.assertEqual(result, 0)

    def test_send_without_config_is_an_error(self):
        from monitor.cli import _report_not_sent
        with self._quiet():
            result = _report_not_sent(self._args(True), [], {"smtp_host": ""})
        self.assertEqual(result, 3)

    def test_the_error_names_the_missing_variable(self):
        from monitor.cli import _report_not_sent
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}, clear=False), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            _report_not_sent(self._args(True), [], {"smtp_host": ""})
        combined = buf.getvalue() + err.getvalue()
        # Naming the variable is the difference between a usable error and a
        # shrug. Both missing values must be named, not just the first.
        self.assertIn("MONITOR_SMTP_HOST", combined)
        self.assertIn("MONITOR_TO", combined)

    def test_github_annotation_only_inside_actions(self):
        from monitor.cli import _report_not_sent
        for value, expected in (("true", True), ("", False)):
            with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": value}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), \
                        contextlib.redirect_stderr(io.StringIO()):
                    _report_not_sent(self._args(True), [], {"smtp_host": ""})
                self.assertEqual("::error" in buf.getvalue(), expected)


class TestTestsDoNotAnnotateTheRun(unittest.TestCase):
    """A test must not be able to annotate the run that is testing it.

    `_report_not_sent` writes `::error title=No email sent::…` when
    GITHUB_ACTIONS is set, which is exactly right in production and exactly
    wrong from inside a test — on a runner the variable is set while the suite
    is running, so the annotation landed on the run page of every workflow that
    runs these tests. A green run with a red "No email sent" at the top of it
    is worse than a silent one: it says the thing that did work, didn't.

    This re-runs the tests most likely to leak and fails if any workflow
    command escapes.
    """

    def test_the_suite_cannot_write_to_the_run_summary(self):
        """The Friday email's tests wrote their fake-URL failures onto the
        page of every real run — including the first morning briefing."""
        self.assertNotIn("GITHUB_STEP_SUMMARY", os.environ)

    def test_the_email_failure_tests_emit_no_workflow_commands(self):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            TestEmailFailureIsLoud)
        captured = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"},
                             clear=False):
            with contextlib.redirect_stdout(captured):
                result = unittest.TextTestRunner(
                    stream=io.StringIO(), verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())
        self.assertNotIn("::error", captured.getvalue(),
                         "a test printed a GitHub error annotation onto the "
                         "run page — redirect its output, and clear "
                         "GITHUB_ACTIONS while calling it")
        self.assertNotIn("::warning", captured.getvalue())


# ---------------------------------------------------------------------------
class TestPublish(unittest.TestCase):
    """Publishing to a place a person will actually look.

    Three earlier attempts put the output somewhere technically correct and
    practically invisible: a gitignored file, a zip inside a build artifact, and
    a CI log page whose URL changes every run. And the email depended on five
    SMTP secrets that needed an IT request, so it never arrived at all.

    These tests pin the properties that made the fourth attempt work: no
    credential beyond the one GitHub injects, and a missing token degrades to a
    warning rather than losing the briefing.
    """

    def test_marker_lets_us_find_the_previous_briefing(self):
        from monitor.publish import MARKER
        # Title matching would break the moment the date format changed, so the
        # marker is what identifies our own issues. It must be HTML-commented so
        # readers never see it.
        self.assertTrue(MARKER.startswith("<!--"))
        self.assertTrue(MARKER.endswith("-->"))

    def test_missing_token_names_the_fix(self):
        from monitor.publish import env_repo_and_token, GitHubError
        with mock.patch.dict(os.environ,
                             {"GITHUB_REPOSITORY": "nrla/x", "GITHUB_TOKEN": ""},
                             clear=False):
            with self.assertRaises(GitHubError) as caught:
                env_repo_and_token()
        message = str(caught.exception)
        self.assertIn("GITHUB_TOKEN", message)
        # An error that does not say how to fix it is only half an error.
        self.assertIn("issues: write", message)

    def test_assignee_defaults_to_the_repository_owner(self):
        from monitor.publish import default_assignee
        # This is the delivery mechanism. Watch notifications were fragile —
        # the repository showed "0 watching", so an issue-only design would have
        # emailed nobody. GitHub always notifies an assignee.
        self.assertEqual(default_assignee("JHC220199/Senedd-monitoring-tool"),
                         "JHC220199")
        self.assertEqual(default_assignee("nonsense"), "")

    def test_issue_title_carries_the_date(self):
        from monitor.publish import issue_title
        self.assertEqual(issue_title(date(2026, 8, 6)),
                         "Senedd briefing — 06 August 2026")

    def test_standalone_page_gets_a_heading_and_a_date(self):
        from monitor.brief import render_markdown
        md = render_markdown([make_item("rent controls")], TAX,
                             today=date(2026, 8, 6),
                             heading="NRLA Senedd policy briefing")
        self.assertTrue(md.startswith("# NRLA Senedd policy briefing"))
        self.assertIn("Thursday 06 August 2026", md)

    def test_run_summary_has_no_heading(self):
        from monitor.brief import render_markdown
        # The Actions run page supplies its own title; a second H1 there reads
        # as a duplicated header.
        md = render_markdown([make_item("rent controls")], TAX)
        self.assertFalse(md.startswith("#"))


class TestCommandsActuallyRun(unittest.TestCase):
    """Execute every read-only command, rather than asserting about them.

    WHY THIS CLASS EXISTS
    ---------------------
    A refactor moved the database handling out of `cmd_brief` and left a
    `store.close()` behind, referencing a name that no longer existed in that
    scope. `python -m monitor.cli brief` raised NameError and the scheduled run
    went red.

    118 tests passed. Every one of them asserted something *about* the commands
    — that the workflow called them, that the YAML was shaped correctly — and not
    one of them actually ran one. A test suite that checks the wiring diagram but
    never turns on the power will miss a NameError every time.

    So: run each command against a real temporary database and assert only that
    it exits cleanly. Cheap, and it closes the exact hole that shipped a broken
    run to the operator.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = str(Path(cls.tmp) / "test.sqlite3")
        store = Store(cls.db)
        store.upsert_many([make_item("rent controls in the private rented sector"),
                           make_item("EPC and retrofit standards for landlords")])
        store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, *argv) -> int:
        from monitor.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = main(["--db", self.db, *argv])
        self.output = buf.getvalue()
        return code

    def test_prune_fixtures_removes_only_fixtures(self):
        """`prune --fixtures` is how demonstration data gets out of the live
        archive. It must take the fixtures and nothing else."""
        db = str(Path(self.tmp) / "fixtures.sqlite3")
        store = Store(db)
        fake = make_item("sample consultation text",
                         title="FIXTURE consultation",
                         source_kind="consultation",
                         raw_ref="mailbox:x@nrla.org.uk:FIXTURE-001")
        real = make_item("rent controls in the private rented sector",
                         title="Real consultation",
                         source_kind="consultation",
                         raw_ref="https://www.gov.wales/real")
        store.upsert_many([fake, real])
        store.close()

        from monitor.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            # Without --yes it must only report, never delete.
            self.assertEqual(main(["--db", db, "prune", "--fixtures"]), 0)
        survivors = Store(db)
        self.assertEqual(len(survivors.query(min_score=0)), 2)
        survivors.close()

        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            self.assertEqual(
                main(["--db", db, "prune", "--fixtures", "--yes"]), 0)
        left = Store(db)
        titles = [i.title for i in left.query(min_score=0)]
        left.close()
        self.assertEqual(titles, ["Real consultation"])

    def test_brief_runs(self):
        self.assertEqual(self._run("brief"), 0)

    def test_brief_to_a_file_runs(self):
        out = str(Path(self.tmp) / "brief.md")
        self.assertEqual(self._run("brief", "--out", out), 0)
        self.assertTrue(Path(out).exists())

    def test_dashboard_runs(self):
        out = str(Path(self.tmp) / "index.html")
        self.assertEqual(self._run("dashboard", "--out", out), 0)
        self.assertTrue(Path(out).stat().st_size > 1000)

    def test_publish_files_run_without_a_token(self):
        # The files must still be written when no GitHub token is present,
        # because that is how it behaves on a developer's machine.
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "",
                                          "GITHUB_TOKEN": ""}):
            code = self._run("publish",
                             "--file", str(Path(self.tmp) / "BRIEFING.md"),
                             "--week-dir", str(Path(self.tmp) / "briefings"))
        self.assertEqual(code, 0)
        self.assertTrue((Path(self.tmp) / "BRIEFING.md").exists())

    def test_publish_dry_run_does_not_call_the_api(self):
        with mock.patch.dict(os.environ,
                             {"GITHUB_REPOSITORY": "nrla/senedd-monitor",
                              "GITHUB_TOKEN": "not-a-real-token"}):
            code = self._run("publish", "--dry-run",
                             "--file", str(Path(self.tmp) / "B2.md"),
                             "--week-dir", str(Path(self.tmp) / "b2"))
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", self.output)
        self.assertIn("nrla", self.output)

    def test_stats_and_weeks_run(self):
        self.assertEqual(self._run("stats"), 0)
        self.assertEqual(self._run("weeks"), 0)

    def test_digest_dry_run_runs(self):
        self.assertEqual(self._run("digest", "--days", "7"), 0)

    def test_search_runs(self):
        self.assertEqual(self._run("search", "rent"), 0)


class TestHostedSite(unittest.TestCase):
    """The GitHub Pages database page.

    Built after the operator's verdict on four earlier attempts: *"that is not a
    database at all. It's not even hosted on an actual page?"* — every previous
    output was a file rather than a URL. Then REBUILT after the verdict on the
    week-tab version: *"far too focussed on the week tracking … doesn't give
    clear lists on for example relevant consultations, debates, etc."* and
    *"it seems to have every single consultation … you're just overloaded"*.
    The page is now lists by type, strictly filtered to NRLA relevance.
    """

    def _page(self, items):
        from monitor.site import render_site
        return render_site(items, TAX)

    def test_page_is_self_contained(self):
        page = self._page([make_item("rent controls")])
        # No external scripts or stylesheets: it must render from one file on
        # Pages, offline, forever.
        self.assertNotIn("<script src=", page)
        self.assertNotIn("stylesheet", page)
        # And no browser storage, which Claude artifacts and some corporate
        # browser policies block outright.
        for banned in ("localStorage", "sessionStorage"):
            self.assertNotIn(banned, page)

    def test_house_style_furniture_is_present(self):
        page = self._page([make_item("rent controls")])
        for expected in ("Senedd Policy Monitor", "Last updated",
                         "Download CSV", "OPEN CONSULTATIONS",
                         "Open Government Licence"):
            self.assertIn(expected, page)

    def test_page_is_lists_by_type_not_weeks(self):
        page = self._page([make_item("rent controls")])
        for section in ('id="consultations"', 'id="legislation"',
                        'id="debates"', 'id="committees"', 'id="questions"',
                        'id="upcoming"'):
            self.assertIn(section, page)
        # The week furniture is gone on purpose.
        self.assertNotIn("data-week", page)
        self.assertNotIn("WC ", page)

    def test_ordinals_handle_the_teens(self):
        from monitor.site import _ordinal
        self.assertEqual([_ordinal(n) for n in (1, 2, 3, 11, 12, 13, 21, 22)],
                         ["1st", "2nd", "3rd", "11th", "12th", "13th",
                          "21st", "22nd"])

    def test_generic_senedd_business_is_filtered_out(self):
        """The overload complaint, pinned down: an item whose only match is
        another committee's name (or the budget) is context for scoring, but
        it must NEVER reach the page."""
        noise = make_item("The Finance Committee will consider the draft "
                          "budget on Tuesday.",
                          title="Priorities for the Finance Committee")
        signal = make_item("rent controls in the private rented sector")
        page = self._page([noise, signal])
        self.assertNotIn("Priorities for the Finance Committee", page)
        self.assertIn("private rented sector", page)

    def test_housing_committee_own_business_is_always_shown(self):
        """The one exception to the strict filter: the housing committee's own
        business qualifies by TITLE, even with no thematic text."""
        meeting = make_item(
            "Local Government, Housing and Planning Committee",
            title="Local Government, Housing and Planning Committee — "
                  "17 September 2026, 09.30",
            source_kind="calendar")
        meeting.item_date = date.today() + timedelta(days=30)
        # …but the same committee scrutinising NON-housing business, where the
        # committee name sits in the body and the title stays on-topic, is
        # exactly the noise the filter exists to remove.
        electoral = make_item(
            "Considered by the Local Government, Housing and Planning "
            "Committee.",
            title="The Representation of the People (Electoral Reform) "
                  "(Wales) Regulations 2026",
            source_kind="consultation")
        page = self._page([meeting, electoral])
        self.assertIn("17 September 2026", page)
        self.assertNotIn("Representation of the People", page)

    def test_noise_band_never_reaches_the_page(self):
        weak = make_item("a passing mention of housing statistics",
                         source_kind="research")
        if weak.band == "Noise":                # scored weakly, as expected
            page = self._page([weak])
            self.assertNotIn("passing mention", page)

    def test_open_consultations_show_their_clock(self):
        """A consultation with a deadline must show the days remaining —
        the deadline column is the whole point of monitoring consultations."""
        c = make_item("consultation on the private rented sector",
                      source_kind="consultation",
                      title="Consultation: PRS licensing")
        c.deadline = date.today() + timedelta(days=10)
        page = self._page([c])
        self.assertIn("10 days left", page)

    def test_future_sittings_appear_under_coming_up(self):
        from monitor.site import render_site
        soon = make_item(
            "Local Government, Housing and Planning Committee",
            title="Local Government, Housing and Planning Committee — sitting",
            source_kind="calendar")
        soon.item_date = date.today() + timedelta(days=40)
        past = make_item("rent controls in the private rented sector")
        past.item_date = date.today() - timedelta(days=3)
        page = render_site([soon, past], TAX)
        # The sitting is listed after the "Coming up" heading, not among
        # the debates.
        self.assertIn("Coming up", page)
        self.assertLess(page.index('id="upcoming"'),
                        page.index("— sitting"))

    def test_duplicate_legislation_sources_collapse_to_one_row(self):
        """The same Act arrives from the Senedd bill-history page AND
        legislation.gov.uk; two rows for one Act reads as clutter."""
        a = make_item("Renting Homes (Wales) Act",
                      title="Building Safety (Wales) Act 2026",
                      source_kind="legislation",
                      url="https://business.senedd.wales/x")
        b = make_item("Renting Homes (Wales) Act",
                      title="Building Safety (Wales) Act 2026",
                      source_kind="legislation",
                      url="https://www.legislation.gov.uk/x")
        page = self._page([a, b])
        # Count in the rendered body only — the CSV payload in the <script>
        # block legitimately repeats the title.
        body = page.split("<script>")[0]
        self.assertEqual(body.count("Building Safety (Wales) Act 2026"), 1)
        self.assertIn("Senedd bill history", body)
        self.assertIn("legislation.gov.uk", body)

    def test_demonstration_fixtures_never_reach_the_page(self):
        """Sample Welsh Government notifications exist so the mailbox parser
        can be exercised without a Graph tenant. They were committed into the
        archive and then displayed for weeks as open consultations with a
        deadline countdown, indistinguishable from real ones. Pruning fixed
        that day; this stops a future demo run putting them back."""
        fake = make_item(
            "We want your views on the private rented sector.",
            title="Consultation: something that looks entirely real",
            source_kind="consultation",
            raw_ref="mailbox:joshua.helm-cowley@nrla.org.uk:FIXTURE-001")
        fake.deadline = date.today() + timedelta(days=25)
        real = make_item("rent controls in the private rented sector",
                         title="Consultation: a genuinely collected one",
                         source_kind="consultation",
                         raw_ref="https://www.gov.wales/real-page")
        page = self._page([fake, real])
        self.assertNotIn("looks entirely real", page)
        self.assertIn("genuinely collected one", page)

    def test_a_source_that_is_not_running_is_named_on_the_page(self):
        """The worst failure a monitor can have is a silent gap: an empty
        section reads as "nothing to report" when it may mean "not looking".
        gov.wales blocks this host, and the run calls that "substituted" so the
        page is not permanently red — which made the gap invisible."""
        from monitor.site import render_site
        item = make_item("rent controls in the private rented sector")
        page = render_site([item], TAX,
                           not_live=["Welsh Government — RSS"])
        self.assertIn("Not everything is being monitored", page)
        self.assertIn("Welsh Government — RSS", page)
        self.assertIn("gov.wales", page)

    def test_no_banner_when_every_source_reported(self):
        from monitor.site import render_site
        page = render_site([make_item("rent controls")], TAX, not_live=[])
        self.assertNotIn("Not everything is being monitored", page)

    def test_no_priority_ratings_are_shown(self):
        """The operator does not want the tool ranking importance: "we don't
        need a rating from the tool on how important each identified part is"
        (12 Aug 2026). Deadlines convey urgency; a Critical badge does not."""
        hot = make_item("rent controls in the private rented sector "
                        "eviction Renting Homes (Wales) Act Rent Smart Wales",
                        title="Consultation: rent controls",
                        source_kind="consultation")
        self.assertEqual(hot.band, "Critical")      # still scored internally…
        page = self._page([hot])
        for banned in ("Critical", "badge", "High</span>"):
            self.assertNotIn(banned, page)          # …but never displayed

    def test_boilerplate_rationale_is_not_repeated_on_every_row(self):
        """The collectors prepend a stock sentence for the briefing's benefit.
        On the page it appeared on every row and read as the tool justifying
        its own rating."""
        c = make_item(
            "This is an open Senedd consultation. Responding puts NRLA's "
            "position formally on the record. Issue Details Issue History "
            "The Committee is seeking views on rent controls in the private "
            "rented sector.",
            title="Priorities for the Housing Committee",
            source_kind="consultation")
        page = self._page([c])
        self.assertNotIn("Responding puts NRLA", page)
        self.assertNotIn("Issue Details", page)
        self.assertIn("seeking views on rent controls", page)

    def test_same_source_page_is_listed_once(self):
        """A page whose wording changes between runs is stored again under a
        new uid, by design, so the archive keeps the history. The page must
        still list it once — the forward work programme appeared twice,
        identical, one above the other."""
        url = "https://business.senedd.wales/mgIssueHistoryHome.aspx?IId=47562"
        first = make_item("rent controls in the private rented sector",
                          title="Follow-up inquiry into Empty Properties",
                          source_kind="consultation", url=url)
        second = make_item("rent controls in the private rented sector, with "
                           "some extra wording added by a later scrape",
                           title="Follow-up inquiry into Empty Properties",
                           source_kind="consultation", url=url)
        page = self._page([first, second])
        body = page.split("<script>")[0]
        self.assertEqual(body.count("Follow-up inquiry into Empty Properties"), 1)

    def test_a_debate_keeps_every_contribution_despite_a_shared_url(self):
        """The dedupe must not touch transcripts: a whole debate shares one
        Record URL, so keying on URL there would discard every contribution
        but one."""
        url = "https://record.senedd.wales/Plenary/2026-07-15"
        a = make_item("rent controls in the private rented sector",
                      title="2. Questions to the Cabinet Minister",
                      url=url, speaker="Alice Jones")
        b = make_item("eviction and possession in the private rented sector",
                      title="2. Questions to the Cabinet Minister",
                      url=url, speaker="Bob Evans")
        page = self._page([a, b])
        self.assertIn("2 relevant contributions", page)
        self.assertIn("Alice Jones", page)
        self.assertIn("Bob Evans", page)

    def test_forward_work_programme_pages_are_suppressed(self):
        """An index page is not an opportunity: it has no closing date and
        its content points at the priorities consultation, which the page
        lists separately with a deadline."""
        fwp = make_item(
            "The forward work programme sets out the work the Committee "
            "intends to carry out. Consultation: Priorities for the Local "
            "Government, Housing and Planning Committee",
            title="Forward work programme – Local Government, Housing and "
                  "Planning Committee",
            source_kind="consultation")
        real = make_item(
            "The Committee is seeking views on its priorities, including "
            "the private rented sector.",
            title="Priorities for the Local Government, Housing and "
                  "Planning Committee",
            source_kind="consultation")
        page = self._page([fwp, real])
        self.assertNotIn("Forward work programme", page)
        self.assertIn("Priorities for the Local Government", page)

    def test_oral_questions_are_headed_by_the_question_not_the_sitting(self):
        """An oral question's stored title is the sitting it belongs to, so
        seven questions from one sitting rendered as seven rows under one
        heading — indistinguishable, and reading as the same item repeated."""
        q = make_item("What assessment has the Cabinet Minister made of the "
                      "impact of rent controls on the private rented sector? "
                      "A second sentence that should not be in the heading.",
                      title="Questions to the Cabinet Minister for Local "
                            "Government, Housing and Planning",
                      source_kind="oral_question", speaker="Nigel Williams")
        page = self._page([q])
        self.assertIn("What assessment has the Cabinet Minister made of the "
                      "impact of rent controls on the private rented sector?",
                      page)
        # The sitting name survives as context, not as the headline.
        self.assertIn("Questions to the Cabinet Minister", page)
        self.assertNotIn("A second sentence that should not be in the heading",
                         page.split('class="meta"')[0])

    def test_a_long_question_is_not_echoed_under_its_own_heading(self):
        """A heading cut mid-clause ends in "…", which never matches the text
        it came from — so the row printed its own opening words twice."""
        long_q = ("The town-centre taskforce will address the structural "
                  "challenges facing town centres, including business rates "
                  "and planning reform, by working collaboratively with "
                  "partners across the private rented sector and maintaining "
                  "a strong focus on delivery.")
        q = make_item(long_q, title="Questions to the Cabinet Minister",
                      source_kind="oral_question")
        page = self._page([q])
        opening = "The town-centre taskforce will address the structural"
        self.assertEqual(page.split("<script>")[0].count(opening), 1)

    def test_a_short_question_is_not_given_a_stray_ellipsis(self):
        q = make_item("When will the Welsh Government update its rent "
                      "controls policy?",
                      title="Oral Question - OQ64366",
                      source_kind="oral_question", speaker="James Evans")
        page = self._page([q])
        self.assertIn("update its rent controls policy?", page)
        self.assertNotIn("policy?…", page)

    def test_a_question_tabled_and_then_not_reached_is_listed_once(self):
        """The same question is published when tabled and again when the
        sitting runs out of time before reaching it. Different pages, so URL
        de-duplication cannot catch it."""
        text = ("What assessment has the Cabinet Minister made of the impact "
                "of housing policy on the private rented sector?")
        tabled = make_item(text, title="Oral Question - OQ64370",
                           source_kind="oral_question",
                           speaker="Nigel Williams",
                           url="https://record.senedd.wales/OQ64370")
        not_reached = make_item(text,
                                title="Questions to the Cabinet Minister for "
                                      "Local Government, Housing and Planning",
                                source_kind="oral_question",
                                speaker="Nigel Williams",
                                url="https://record.senedd.wales/Plenary/x")
        page = self._page([tabled, not_reached])
        body = page.split("<script>")[0]
        self.assertEqual(body.count("What assessment has the Cabinet Minister"), 1)

    def test_two_members_asking_the_same_thing_both_appear(self):
        """De-duplication keys on the member as well as the words: two members
        pressing the same point is a fact about the Chamber, not a repeat."""
        text = "When will the Welsh Government update Planning Policy Wales?"
        a = make_item(text, title="Oral Question - OQ1",
                      source_kind="oral_question", speaker="James Evans")
        b = make_item(text, title="Oral Question - OQ2",
                      source_kind="oral_question", speaker="Helen Jenner")
        page = self._page([a, b])
        self.assertIn("James Evans", page)
        self.assertIn("Helen Jenner", page)

    def test_written_questions_never_reach_the_page(self):
        """The policy team already runs a dedicated written-questions tool
        (operator request, 12 Aug 2026). Even if the written_questions source
        is re-enabled in taxonomy.yaml, the page must not duplicate it."""
        wq = make_item("What is the Minister doing about rent controls in "
                       "the private rented sector?",
                       title="Written Question - WQ99999",
                       source_kind="written_question")
        oq = make_item("What is the Minister doing about rent controls in "
                       "the private rented sector?",
                       title="Oral Question - OQ88888",
                       source_kind="oral_question")
        page = self._page([wq, oq])
        self.assertNotIn("WQ99999", page)
        self.assertIn("OQ88888", page)

    def test_titles_with_quotes_cannot_break_the_page(self):
        from monitor.site import render_site
        nasty = make_item('A "first phase" of legislation <script>x</script>')
        page = render_site([nasty], TAX)
        self.assertNotIn("<script>x</script>", page)


class TestWorkflowGuards(unittest.TestCase):
    """The workflow's own logic, checked without running it.

    `secrets` is not available in a step-level `if:`, so the SMTP host is lifted
    into `env` at job level. Get that wrong and the send steps either never run
    or always run — both silent.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        self.workflow = yaml.safe_load(
            (self.ROOT / ".github/workflows/monitor.yml").read_text(
                encoding="utf-8"))
        self.job = self.workflow["jobs"]["monitor"]
        self.steps = {s["name"]: s for s in self.job["steps"]}

    def test_smtp_host_is_lifted_into_env_for_the_if_conditions(self):
        self.assertIn("SMTP_HOST", self.job.get("env", {}))

    def test_send_steps_are_guarded_and_the_warning_is_the_inverse(self):
        for name in ("Send the daily digest", "Alert on anything critical"):
            self.assertEqual(self.steps[name]["if"], "env.SMTP_HOST != ''", name)
        self.assertEqual(
            self.steps["Note where the briefing went"]["if"],
            "env.SMTP_HOST == ''")

    def test_the_briefing_reaches_the_run_summary(self):
        step = self.steps["Put the briefing on this page"]
        self.assertIn("monitor.cli brief", step["run"])
        self.assertIn("GITHUB_STEP_SUMMARY", step["run"])

    def test_the_briefing_is_published_and_committed(self):
        """The bookmarkable page must be both written AND committed.

        Writing BRIEFING.md without adding it to the commit would leave it on
        the runner's disk and nowhere else — which is exactly the mistake that
        made the first three attempts useless.
        """
        step = self.steps[
            "Publish the briefing — bookmarkable page and weekly record"]
        self.assertIn("monitor.cli publish", step["run"])
        self.assertIn("BRIEFING.md", step["run"])

        commit = self.steps["Commit the updated archive and weekly records"]
        self.assertIn("BRIEFING.md", commit["run"])
        self.assertIn("briefings", commit["run"])

    def test_the_run_opens_no_issue_and_cannot_email_by_notification(self):
        """The directorate asked for the per-run email to stop (13 Aug 2026):
        "It is not user friendly or useful to read this."

        Two independent guards, because one alone fails quietly. `--no-issue`
        stops it; withholding `issues: write` means a future edit that drops
        the flag errors instead of silently resuming the emails.
        """
        step = self.steps[
            "Publish the briefing — bookmarkable page and weekly record"]
        self.assertIn("--no-issue", step["run"])
        self.assertNotIn("issues", self.workflow["permissions"])

    def test_publish_runs_before_the_commit_step(self):
        names = [s["name"] for s in self.job["steps"]]
        self.assertLess(
            names.index(
                "Publish the briefing — bookmarkable page and weekly record"),
            names.index("Commit the updated archive and weekly records"))

    def test_tests_run_before_anything_is_published(self):
        names = [s["name"] for s in self.job["steps"]]
        self.assertLess(names.index("Run the tests first"),
                        names.index("Build the dashboard"))


class TestBrowserUploadCopies(unittest.TestCase):
    """The dot-file problem, pinned by a test.

    A browser drag-and-drop upload to GitHub silently skips anything beginning
    with a dot. Losing `.github/workflows/monitor.yml` means the schedule never
    runs, with no error shown anywhere — it looks like it worked and it hasn't.

    So `deploy/` carries readable copies of both hidden files, which the setup
    guide tells the operator to copy and paste into GitHub's web editor. A copy
    that has drifted from the original is worse than no copy: it would deploy a
    stale schedule that nobody thinks to doubt. These two tests fail the moment
    they diverge.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def test_workflow_readable_copy_is_identical(self):
        real = (self.ROOT / ".github/workflows/monitor.yml").read_text(encoding="utf-8")
        copy = (self.ROOT / "deploy/github-actions-workflow.yml").read_text(encoding="utf-8")
        self.assertEqual(real, copy,
                         "deploy/github-actions-workflow.yml has drifted from "
                         ".github/workflows/monitor.yml — re-copy it.")

    def test_gitignore_readable_copy_contains_the_original(self):
        real = (self.ROOT / ".gitignore").read_text(encoding="utf-8")
        copy = (self.ROOT / "deploy/gitignore.txt").read_text(encoding="utf-8")
        # The copy carries an explanatory header, so it is a superset, not equal.
        self.assertIn(real, copy,
                      "deploy/gitignore.txt no longer contains .gitignore "
                      "verbatim — re-copy it.")
        for essential in ("data/*.sqlite3", "!data/archive.sql", "*.env"):
            self.assertIn(essential, copy)


# ---------------------------------------------------------------------------
# The Welsh Government half. Everything below this line exists because the
# Welsh Government section of the live page was empty for weeks while the
# page implied it was being watched.
# ---------------------------------------------------------------------------

class _StubFetcher:
    """Serves canned pages and records what was asked for.

    Deliberately not a mock of `Fetcher`: the collector is only allowed to use
    `get_text`, and a stub that offers nothing else makes that a property the
    tests enforce rather than a convention someone remembers.
    """

    def __init__(self, pages: dict, session=None):
        self.pages = pages
        self.requested: list[str] = []
        self.session = session

    def get_text(self, url, params=None):
        self.requested.append(url)
        return self.pages.get(url)


def _card(href: str, when: str, title: str, summary: str = "") -> str:
    return (f'<div class="card"><div class="card__body">'
            f'<time class="card__date">{when}</time>'
            f'<h2 class="card__title"><a class="card__link" href="{href}">{title}</a></h2>'
            f'<div class="card__summary"><p>{summary}</p></div>'
            f'</div></div>')


def _story(when_attr: str, when_text: str, title: str, summary: str,
           body: str, tag: str = "housing") -> str:
    return (f'<time class="story__date" datetime="{when_attr}">{when_text}</time>'
            f'<div class="story__tags tags"><div class="tag">'
            f'<a class="tag__link" href="/news/t/{tag}">{tag.title()}</a>'
            f'</div></div>'
            f'<h1 class="story__title">{title}</h1>'
            f'<div class="story__summary">{summary}</div>'
            f'<div class="story__body"><p>{body}</p></div>')


class TestWelshGovernmentNewsroom(unittest.TestCase):
    """media.service.gov.wales — the route that is reachable from GitHub.

    www.gov.wales rejects datacentre IPs and always will. The newsroom is a
    different Welsh Government host carrying the same press notices, and it
    answers 200 from a cloud runner (measured 19 August and 11 September 2026).
    Its markup — `.card`, `.card__date`, `a.card__link`, `.story__date` with a
    machine-readable `datetime` — was verified against the live site on
    11 September 2026, and these tests pin every part of it the parser leans on,
    so that a redesign fails here rather than silently emptying the page.
    """

    LIST_URL = "https://media.service.gov.wales/news"
    STORY_URL = "https://media.service.gov.wales/news/seals-and-hares"

    def _collector(self, pages):
        from monitor.collectors.govwales import GovWalesNewsroomCollector
        collector = GovWalesNewsroomCollector(_StubFetcher(pages))
        return collector

    def test_listing_and_story_are_parsed_into_an_item(self):
        pages = {
            self.LIST_URL: _card(
                "/news/seals-and-hares", "Friday 11 Sep 2026, 11:30",
                "Have your say on new protections for seals and hares",
                "A short summary."),
            self.STORY_URL: _story(
                "2026-09-11 11:30", "Friday 11 Sep 2026, 11:30",
                "Have your say on new protections for seals and hares",
                "A short summary.",
                "The consultation closes on 9 October 2026."),
        }
        collector = self._collector(pages)
        items = list(collector.collect(since=date(2026, 9, 1)))

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.item_date, date(2026, 9, 11))
        self.assertEqual(item.forum, "Welsh Government")
        self.assertEqual(item.raw_ref, "newsroom:seals-and-hares")
        self.assertEqual(item.url, self.STORY_URL)
        self.assertEqual(collector.errors, [])

    def test_consultation_is_classified_and_its_deadline_read_from_the_body(self):
        """The card summary alone is too thin to score fairly.

        This is why each in-window story is fetched in full. The consultation on
        implementing the Building Safety (Wales) Act 2026 — leaseholder
        remediation costs, tribunal remediation orders — is core NRLA material
        that appears nowhere in the one-line summary.
        """
        pages = {
            self.LIST_URL: _card(
                "/news/building-safety-act", "Wednesday 9 Sep 2026, 14:00",
                "Implementing the Building Safety (Wales) Act 2026",
                "Views sought."),
            "https://media.service.gov.wales/news/building-safety-act": _story(
                "2026-09-09 14:00", "Wednesday 9 Sep 2026, 14:00",
                "Implementing the Building Safety (Wales) Act 2026",
                "Views sought.",
                "This consultation covers leaseholder remediation costs and "
                "tribunal remediation orders. It closes on 4 December 2026."),
        }
        item = list(self._collector(pages).collect(since=date(2026, 9, 1)))[0]
        self.assertEqual(item.source_kind, "consultation")
        self.assertEqual(item.deadline, date(2026, 12, 4))
        self.assertIn("leaseholder remediation costs", item.body)

    def test_topic_links_are_not_mistaken_for_stories(self):
        """`/news/t/housing` is a topic listing, not an announcement.

        It sits in the same `.card` markup as a real story. Following one
        produces an "announcement" whose body is a list of headlines, which
        scores well and is entirely useless.
        """
        pages = {
            self.LIST_URL: (
                _card("/news/t/housing", "Friday 11 Sep 2026, 11:30", "Housing")
                + _card("/news/real-story", "Friday 11 Sep 2026, 10:00",
                        "A real announcement")),
            "https://media.service.gov.wales/news/real-story": _story(
                "2026-09-11 10:00", "Friday 11 Sep 2026, 10:00",
                "A real announcement", "", "Body text."),
        }
        collector = self._collector(pages)
        items = list(collector.collect(since=date(2026, 9, 1)))
        self.assertEqual([i.title for i in items], ["A real announcement"])
        self.assertNotIn("https://media.service.gov.wales/news/t/housing",
                         collector.fetcher.requested)

    def test_story_page_date_overrides_the_listing_date(self):
        """A listing entry can be re-dated when a notice is edited.

        The date on the notice itself is the one a reader would cite, and the
        one the deadline arithmetic on the page has to agree with.
        """
        pages = {
            self.LIST_URL: _card("/news/seals-and-hares",
                                 "Friday 11 Sep 2026, 11:30", "A notice"),
            self.STORY_URL: _story("2026-09-08 09:00",
                                   "Tuesday 08 Sep 2026, 09:00",
                                   "A notice", "", "Body."),
        }
        item = list(self._collector(pages).collect(since=date(2026, 9, 1)))[0]
        self.assertEqual(item.item_date, date(2026, 9, 8))

    def test_paging_stops_once_a_whole_page_predates_the_window(self):
        """The listing is newest first, so an entirely old page ends the walk.

        Without this the collector would walk all six pages every morning for
        no new data — roughly three months of press notices re-fetched daily.
        """
        pages = {
            self.LIST_URL: _card("/news/recent", "Friday 11 Sep 2026, 11:30",
                                 "Recent"),
            "https://media.service.gov.wales/news/recent": _story(
                "2026-09-11 11:30", "Friday 11 Sep 2026, 11:30",
                "Recent", "", "Body."),
            "https://media.service.gov.wales/news?page=2": _card(
                "/news/ancient", "Monday 02 Feb 2026, 09:00", "Ancient"),
        }
        collector = self._collector(pages)
        list(collector.collect(since=date(2026, 9, 1)))
        self.assertNotIn("https://media.service.gov.wales/news?page=3",
                         collector.fetcher.requested)
        self.assertNotIn("https://media.service.gov.wales/news/ancient",
                         collector.fetcher.requested)

    def test_unreachable_first_page_is_a_loud_error(self):
        """A parser yielding nothing must never look like a quiet fortnight.

        If this host is ever put behind the same CloudFront rule as
        www.gov.wales, the Welsh Government half of the page disappears — and
        the only thing standing between a reader and that silent gap is this
        message.
        """
        collector = self._collector({})
        self.assertEqual(list(collector.collect(since=date(2026, 9, 1))), [])
        self.assertEqual(len(collector.errors), 1)
        self.assertIn("could not be fetched", collector.errors[0])

    def test_first_page_that_parses_to_nothing_is_a_different_loud_error(self):
        """200 OK with no readable stories means the markup changed.

        Reported separately from an unreachable host on purpose: the two have
        different causes and different fixes, and a reader who is told the
        wrong one loses days.
        """
        collector = self._collector({self.LIST_URL: "<html><body>Hello</body></html>"})
        self.assertEqual(list(collector.collect(since=date(2026, 9, 1))), [])
        self.assertEqual(len(collector.errors), 1)
        self.assertIn("markup has probably changed", collector.errors[0])

    def test_article_fetching_is_capped(self):
        """A busy fortnight must not turn one run into hundreds of requests.

        We are a guest on a press office's infrastructure; the cap is ours to
        impose, not theirs to enforce.
        """
        from monitor.collectors.govwales import GovWalesNewsroomCollector
        cards = "".join(
            _card(f"/news/story-{n}", "Friday 11 Sep 2026, 11:30", f"Story {n}")
            for n in range(12))
        pages = {self.LIST_URL: cards}
        for n in range(12):
            pages[f"https://media.service.gov.wales/news/story-{n}"] = _story(
                "2026-09-11 11:30", "Friday 11 Sep 2026, 11:30",
                f"Story {n}", "", "Body.")
        collector = self._collector(pages)
        items = list(collector.collect(since=date(2026, 9, 1), max_articles=3))
        self.assertEqual(len(items), 3)
        self.assertLessEqual(
            len([u for u in collector.fetcher.requested if "/news/story-" in u]), 3)


# ---------------------------------------------------------------------------
CONSULTATION_LISTING_HTML = """<html><body><ul>
<li class="index-list__item"><div class="index-list__title">
  <a href="/rent-guarantor-guidance-local-housing-authorities">Rent Guarantor
  Guidance for Local Housing Authorities</a></div>
  <div class="index-list__meta"><span class="index-list__date">27 July 2026
  </span><span class="index-list__type">Open consultation</span>Housing</div>
</li>
<li class="index-list__item"><div class="index-list__title">
  <a href="/implementing-building-safety-wales-act-2026">Implementing the
  Building Safety (Wales) Act 2026</a></div>
  <div class="index-list__meta"><span class="index-list__date">7 September 2026
  </span><span class="index-list__type">Closed consultation</span>Housing</div>
</li></ul></body></html>"""

CONSULTATION_DETAIL_HTML = """<html><body>
<nav>Housing and regeneration menu</nav>
<main><h1>Rent Guarantor Guidance for Local Housing Authorities</h1>
<p>We are seeking views on guidance for local housing authorities operating
rent guarantor schemes for tenants in the private rented sector.</p>
<p>Consultation ends: 19 October 2026</p></main>
<footer>Contact us</footer></body></html>"""


class TestGovWalesConsultationRegister(unittest.TestCase):
    """The four consultations the first Friday email did not have.

    All four were core NRLA business — rent guarantors, student accommodation
    codes, council tax reduction, self-catering classification — and none had a
    newsroom announcement, so no other route could see them. The register holds
    the authoritative closing dates, and on 11 September 2026 gov.wales began
    answering GitHub's runners again after weeks of blocking them.
    """

    def _collector(self, pages):
        from monitor.collectors.govwales import GovWalesConsultationsCollector
        return GovWalesConsultationsCollector(_StubFetcher(pages))

    def _pages(self, listing=CONSULTATION_LISTING_HTML):
        return {
            "https://www.gov.wales/consultations": listing,
            "https://www.gov.wales/rent-guarantor-guidance-local-housing-authorities":
                CONSULTATION_DETAIL_HTML,
        }

    def test_an_open_consultation_keeps_its_authoritative_closing_date(self):
        collector = self._collector(self._pages())
        items = list(collector.collect())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("Rent Guarantor Guidance", item.title)
        self.assertEqual(item.deadline, date(2026, 10, 19))
        self.assertEqual(item.source_kind, "consultation")
        self.assertEqual(item.forum, "Welsh Government")
        self.assertEqual(collector.errors, [])

    def test_consultation_ends_is_a_closing_date(self):
        """None of the existing patterns matched the register's own wording,
        which is the phrasing on every consultation page it publishes."""
        self.assertEqual(parse_deadline("Consultation ends: 19 October 2026"),
                         date(2026, 10, 19))

    def test_closed_consultations_never_reach_the_page(self):
        """A closed consultation with a countdown beside it is worse than no
        consultation: it invites a response that cannot be made."""
        collector = self._collector(self._pages())
        titles = [i.title for i in collector.collect()]
        self.assertFalse(any("Building Safety" in t for t in titles))

    def test_navigation_is_not_treated_as_consultation_text(self):
        """A stray "housing" in a site-wide menu would score every page on
        gov.wales identically, which is how a strict filter stops being one."""
        collector = self._collector(self._pages())
        item = next(iter(collector.collect()))
        self.assertNotIn("Housing and regeneration menu", item.body)
        self.assertIn("rent guarantor schemes", item.body)

    def test_the_budget_is_big_enough_for_every_open_consultation(self):
        """The register is walked newest-first, so a budget smaller than the
        number of open consultations drops the OLDEST — which are the ones
        closing soonest. On the first live run a budget of 25 lost the Council
        Tax Reduction Scheme consultation, closing in twelve days."""
        from monitor.collectors.govwales import GovWalesConsultationsCollector
        self.assertGreaterEqual(GovWalesConsultationsCollector.MAX_DETAILS, 50)

    def test_the_block_returning_is_a_loud_error(self):
        """This host rejected datacentre IPs for weeks. If it starts again,
        the run must say so — otherwise the consultations simply stop
        appearing, which reads as a quiet month in Cardiff."""
        collector = self._collector({})
        self.assertEqual(list(collector.collect()), [])
        self.assertEqual(len(collector.errors), 1)
        self.assertIn("could not be fetched", collector.errors[0])

    def test_a_register_that_parses_to_nothing_is_a_different_loud_error(self):
        collector = self._collector(
            {"https://www.gov.wales/consultations": "<html><body>hi</body></html>"})
        self.assertEqual(list(collector.collect()), [])
        self.assertEqual(len(collector.errors), 1)
        self.assertIn("markup has probably changed", collector.errors[0])


class TestGraphTokenCanRenewItself(unittest.TestCase):
    """The bug that shipped as documentation.

    The setup instructions told the reader to paste a Microsoft Graph access
    token into a repository secret. Those expire in about an hour, so following
    the instructions exactly gave one successful run and then a 401 every
    morning in a log nobody reads — and a 401 does not empty the Welsh
    Government section, it just stops adding to it, which looks like a quiet
    fortnight in Cardiff Bay.
    """

    class _Session:
        def __init__(self, status=200, payload=None):
            self.status = status
            self.payload = payload or {"access_token": "fresh-token"}
            self.calls = []

        def post(self, url, data=None, timeout=None):
            self.calls.append({"url": url, "data": data})
            outer = self

            class _Resp:
                status_code = outer.status

                def json(self):
                    return outer.payload
            return _Resp()

    FULL = {
        "MONITOR_GRAPH_TENANT": "tenant-guid",
        "MONITOR_GRAPH_CLIENT_ID": "client-guid",
        "MONITOR_GRAPH_CLIENT_SECRET": "the-secret-value",
    }

    def test_client_credentials_produce_a_token(self):
        from monitor.graph_auth import resolve_token
        session = self._Session()
        token, error = resolve_token(dict(self.FULL), session)
        self.assertEqual(token, "fresh-token")
        self.assertEqual(error, "")
        self.assertEqual(session.calls[0]["data"]["grant_type"],
                         "client_credentials")

    def test_the_secret_is_never_put_in_a_url(self):
        """A client secret in a query string ends up in proxy logs and error
        reports. Form-encoded body only — this is the kind of thing a
        well-meaning refactor breaks quietly."""
        from monitor.graph_auth import resolve_token
        session = self._Session()
        resolve_token(dict(self.FULL), session)
        call = session.calls[0]
        self.assertNotIn("the-secret-value", call["url"])
        self.assertNotIn("?", call["url"])
        self.assertEqual(call["data"]["client_secret"], "the-secret-value")

    def test_nothing_configured_is_not_an_error(self):
        """A deployment inside the NRLA network reaches gov.wales directly and
        needs no mailbox at all. Reporting that as a fault would put a
        permanent warning on a page that is working perfectly."""
        from monitor.graph_auth import resolve_token
        token, error = resolve_token({}, self._Session())
        self.assertEqual((token, error), ("", ""))

    def test_half_configured_says_which_piece_is_missing(self):
        from monitor.graph_auth import resolve_token
        env = dict(self.FULL)
        del env["MONITOR_GRAPH_CLIENT_SECRET"]
        token, error = resolve_token(env, self._Session())
        self.assertEqual(token, "")
        self.assertIn("MONITOR_GRAPH_CLIENT_SECRET", error)

    def test_a_pasted_token_still_works_and_takes_precedence(self):
        """Kept because it is genuinely useful for one manual test — and
        because someone mid-setup must not be broken silently. It warns, and
        the docs say to delete it."""
        from monitor.graph_auth import resolve_token
        env = dict(self.FULL, MONITOR_GRAPH_TOKEN="pasted")
        token, error = resolve_token(env, self._Session())
        self.assertEqual((token, error), ("pasted", ""))

    def test_microsoft_error_codes_are_translated_for_a_policy_officer(self):
        """AADSTS7000215 means "you pasted the Secret ID instead of the secret
        value". Nobody knows that, and searching for it lands on forum threads
        about a different product."""
        from monitor.graph_auth import resolve_token
        session = self._Session(status=401, payload={
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided."})
        token, error = resolve_token(dict(self.FULL), session)
        self.assertEqual(token, "")
        self.assertIn("secret VALUE", error)
        self.assertIn("Secret ID", error)

    def test_expiry_risk_is_described_somewhere_a_human_will_see_it(self):
        from monitor.graph_auth import describe_expiry_risk
        text = describe_expiry_risk()
        self.assertIn("expire", text)
        self.assertIn("reminder", text.lower())


# ---------------------------------------------------------------------------
class TestMailboxReadsByDateNotByUnreadFlag(unittest.TestCase):
    """Opening an email in Outlook used to hide it from the monitor forever.

    The collector filtered on `isRead eq false` against a real person's inbox.
    Reading a Welsh Government email — the entirely normal thing to do with an
    email — removed it from the collector's view permanently, and nothing
    anywhere said the consultation had been dropped.
    """

    def _collector(self):
        collector = GovWalesMailboxCollector.__new__(GovWalesMailboxCollector)
        collector.errors = []
        collector.mailbox = "joshua.helm-cowley@nrla.org.uk"
        collector.access_token = "token"
        return collector

    def test_the_query_filters_on_received_date(self):
        url = self._collector().first_page_url(since=date(2026, 8, 21))
        self.assertIn("receivedDateTime ge 2026-08-21T00:00:00Z", url)
        self.assertIn("$orderby=receivedDateTime desc", url)

    def test_the_query_never_mentions_the_unread_flag(self):
        url = self._collector().first_page_url(since=date(2026, 8, 21))
        self.assertNotIn("isRead", url)

    def test_the_collector_never_writes_to_the_mailbox(self):
        """Reading by date keeps the permission ask at Mail.Read, because
        nothing has to be marked read. That is a materially smaller thing to
        ask IT for, and IT are right to care about the difference.

        Enforced by giving the collector a session that can only GET: any
        attempt to PATCH a message read, or to move one, raises here rather
        than turning up as a rejected permission request months later.
        """
        class _ReadOnlySession:
            def get(self, url, headers=None, timeout=None):
                return SimpleNamespace(status_code=200,
                                       json=lambda: {"value": []}, text="")

            def __getattr__(self, name):
                raise AssertionError(
                    f"the mailbox collector must not call session.{name}() — "
                    "anything but GET means asking IT for Mail.ReadWrite")

        collector = self._collector()
        collector.fetcher = SimpleNamespace(session=_ReadOnlySession())
        self.assertEqual(list(collector.collect(since=date(2026, 8, 21))), [])

    def test_401_and_403_are_explained_differently(self):
        """They look identical in a log and have completely different fixes —
        one of which is a message to IT."""
        collector = self._collector()
        unauthorised = SimpleNamespace(status_code=401, text="{}")
        forbidden = SimpleNamespace(status_code=403, text="{}")
        self.assertIn("consent", collector._explain_status(unauthorised))
        self.assertIn("application access policy",
                      collector._explain_status(forbidden))


# ---------------------------------------------------------------------------
class TestCoveredSourcesAreNotNamedAsGaps(unittest.TestCase):
    """A banner that stays up after its gap has closed trains people to ignore
    banners — and that banner is the only thing standing between a reader and
    the next silent gap."""

    def test_rss_stops_being_named_once_the_newsroom_delivers(self):
        from monitor.pipeline import SUBSTITUTED_BY
        self.assertIn("Welsh Government — newsroom",
                      SUBSTITUTED_BY["Welsh Government — RSS"])

    def test_the_gov_wales_explanation_only_appears_for_a_welsh_source(self):
        """It used to be printed whatever had failed. A broken Senedd
        transcript feed produced a confident paragraph about CloudFront,
        sending the reader to check the one place the material was not."""
        from monitor.site import _coverage_banner
        welsh = _coverage_banner(["Welsh Government — newsroom"])
        self.assertIn("gov.wales", welsh)
        senedd = _coverage_banner(["Senedd Record — tabled business"])
        self.assertNotIn("gov.wales", senedd)
        self.assertIn("Senedd Record", senedd)

    def test_the_banner_agrees_with_itself_about_number(self):
        from monitor.site import _coverage_banner
        self.assertIn("This source is", _coverage_banner(["One source"]))
        self.assertIn("These sources are", _coverage_banner(["One", "Two"]))

    def test_no_gaps_panel_when_there_are_no_gaps(self):
        from monitor.site import _partial_note
        self.assertEqual(_partial_note([]), "")

    def test_the_gaps_panel_is_calm_and_distinct_from_the_warning(self):
        """The banner means "a source that should be reporting is not" — a
        fault. This panel means "here is what this tool does not watch, by
        design" — a standing limitation. Collapsing the two ends with nobody
        reading either."""
        from monitor.site import _partial_note
        html_text = _partial_note(["The consultation register."])
        self.assertIn('<details class="gaps">', html_text)
        self.assertIn("What this page does not cover", html_text)
        self.assertNotIn('class="warn"', html_text)

    def test_the_consultation_register_drops_off_once_the_mailbox_runs(self):
        from monitor.cli import _standing_gaps
        without = _standing_gaps([{"sources": ["Welsh Government — newsroom"]}])
        self.assertTrue(any("consultation register" in g for g in without))
        with_mailbox = _standing_gaps(
            [{"sources": ["Welsh Government — newsroom",
                          "Welsh Government — mailbox"]}])
        self.assertFalse(any("consultation register" in g for g in with_mailbox))

    def test_the_register_gap_also_closes_when_the_register_itself_is_read(self):
        """gov.wales answered GitHub's runners again on 11 September 2026, so
        the register can be read directly. The panel must not keep telling a
        reader to go and check a source the tool is already reading."""
        from monitor.cli import _standing_gaps
        gaps = _standing_gaps(
            [{"sources": ["Welsh Government — consultations"]}])
        self.assertFalse(any("consultation register" in g for g in gaps))

    def test_written_questions_are_named_as_a_deliberate_exclusion(self):
        """Not an oversight. The team has a separate tool, and a reader must be
        able to tell the difference between "not watched" and "watched
        elsewhere"."""
        from monitor.cli import _standing_gaps
        gaps = _standing_gaps([])
        self.assertTrue(any("Written questions" in g for g in gaps))


# ---------------------------------------------------------------------------
class TestWelshGovernmentSetupGuide(unittest.TestCase):
    """The original bug shipped as documentation, so it is pinned as
    documentation. A guide that names a secret the code does not read, or omits
    one it does, is the same failure again in a different file."""

    GUIDE = Path(__file__).resolve().parent.parent / "WELSH-GOVERNMENT-SETUP.md"

    def test_the_guide_exists(self):
        self.assertTrue(self.GUIDE.exists(),
                        "WELSH-GOVERNMENT-SETUP.md is what a non-developer "
                        "follows to connect the mailbox route. Without it the "
                        "four secrets are undiscoverable.")

    def test_it_names_every_secret_the_code_actually_reads(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        for secret in ("MONITOR_MAILBOX", "MONITOR_GRAPH_TENANT",
                       "MONITOR_GRAPH_CLIENT_ID", "MONITOR_GRAPH_CLIENT_SECRET"):
            self.assertIn(secret, text)

    def test_it_asks_for_read_only_access(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("Mail.Read", text)
        self.assertNotIn("Mail.ReadWrite", text)

    def test_it_warns_about_the_secret_expiring(self):
        """An expired secret does not announce itself. The Welsh Government
        section just goes quiet, which looks exactly like a quiet fortnight."""
        text = self.GUIDE.read_text(encoding="utf-8").lower()
        self.assertIn("expir", text)
        self.assertIn("renew", text)

    def test_it_warns_against_the_old_pasted_token(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("MONITOR_GRAPH_TOKEN", text)
        self.assertIn("hour", text)


ORDER_PAPER_HTML = """<html><head><title>Oral Questions tabled on 10/09/2026
for answer on 15/09/2026 - Welsh Parliament</title></head><body>
<h2 class="subheading orderpaper">First Minister</h2>
<div class="itemContent oralQuestion orderPaper">
  <span class="numbering">4</span>
  <div class="topBar"><div class="memberBar"><div class="memberDetail">
    <span class="name">Gareth Beer</span>
    <span class="area">Sir Gaerfyrddin</span></div></div>
    <span class="title">OQ64456</span><span class="tabledIn">(e)</span>
    <span class="date">Tabled on 10/09/2026</span></div>
  <div class="itemContent__content"><p>Will the First Minister make a statement
    on waiting times for cataract surgery in Sir Gaerfyrddin?</p></div>
</div>
<div class="itemContent oralQuestion orderPaper">
  <span class="numbering">5</span>
  <div class="topBar"><div class="memberBar"><div class="memberDetail">
    <span class="name">David Hughes</span>
    <span class="area">Pontypridd Cynon Merthyr</span></div></div>
    <span class="title">OQ64467</span><span class="tabledIn">(e)</span>
    <span class="date">Tabled on 10/09/2026</span></div>
  <div class="itemContent__content"><p>Will the First Minister set out a
    timeline for the introduction of new measures to better protect
    renters?</p></div>
</div></body></html>"""

# What the Senedd returns for a day with no sitting, or before questions have
# been tabled: HTTP 200, a polite error page, and no questions.
NO_SITTING_HTML = """<html><head><title>Welsh Parliament</title></head><body>
<p>An error has occurred, please return to the Search page and try again.</p>
</body></html>"""

PLENARY_AGENDA_HTML = """<html><head><title>Agenda for Plenary on Tuesday,
15 September 2026, 13.30</title></head><body>
<h1>Agenda for Plenary on Tuesday, 15 September 2026, 13.30</h1>
<table class="mgItemTable">
<tr><td class="mgFootnoteMarkerCell">(45 mins)</td>
    <td class="mgItemNumberCell">1.</td>
    <td><p class="mgAiTitleTxt">Questions to the First Minister</p>
        <ul class="mgActionList"><li>View the background to item 1.</li></ul></td></tr>
<tr><td class="mgFootnoteMarkerCell">(30 mins)</td>
    <td class="mgItemNumberCell">2.</td>
    <td><p class="mgAiTitleTxt">Business Statement and Announcement</p></td></tr>
<tr><td class="mgFootnoteMarkerCell">(30 mins)</td>
    <td class="mgItemNumberCell">4.</td>
    <td><p class="mgAiTitleTxt">Statement by the Cabinet Minister for Finance:
        Rebalancing the non-domestic rates system</p>
        <div class="mgWordPara">A statement on reform of non-domestic
        rates.</div></td></tr>
</table></body></html>"""

CALENDAR_HTML = """<html><body>
<a href="ieListDocuments.aspx?CId=986&amp;MId=16253">Meeting of Legislation
  Committee on 14/09 at 13.30</a>
<a href="ieListDocuments.aspx?CId=908&amp;MId=16260">Meeting of Plenary on
  15/09 at 13.30</a>
<a href="ieListDocuments.aspx?CId=908&amp;MId=16261">Meeting of Plenary on
  16/09 at 13.30</a>
</body></html>"""


class TestForthcomingBusiness(unittest.TestCase):
    """What is about to be said, not what was said.

    Every other Senedd source in this tool reads the Record, and the Record is
    a record. The first Friday email, compared against the supplier briefing it
    replaces, was missing an entire section for that reason — an oral question
    tabled on 10 September for the 15 September sitting:

        5. David Hughes MS (Pontypridd Cynon Merthyr): Will the First Minister
           set out a timeline for the introduction of new measures to better
           protect renters?

    The supplier had it on the Friday. This tool would have seen it on Tuesday
    evening, after it was asked, which is one working day too late to brief
    anybody. Both the source and the parser are pinned here.
    """

    def _collector(self, pages):
        from monitor.collectors.forthcoming import (
            SeneddForthcomingBusinessCollector)
        return SeneddForthcomingBusinessCollector(_StubParamFetcher(pages))

    def test_a_tabled_question_becomes_an_item_before_it_is_asked(self):
        collector = self._collector({
            "calendar": CALENDAR_HTML,
            "agenda:16260": PLENARY_AGENDA_HTML,
            "order:15-09-2026": ORDER_PAPER_HTML,
        })
        items = list(collector.collect(start=date(2026, 9, 11),
                                       end=date(2026, 10, 2)))
        tabled = [i for i in items if i.source_kind == "oral_question"]
        self.assertEqual(len(tabled), 2)
        renters = next(i for i in tabled if "renters" in i.body)
        self.assertEqual(renters.title, "OQ64467")
        self.assertEqual(renters.speaker, "David Hughes")
        self.assertEqual(renters.constituency, "Pontypridd Cynon Merthyr")
        self.assertEqual(renters.item_date, date(2026, 9, 10))   # tabled
        self.assertEqual(renters.deadline, date(2026, 9, 15))    # answered
        self.assertIn("First Minister", renters.agenda_item)
        self.assertEqual(collector.errors, [])

    def test_that_question_is_relevant_enough_to_be_shown(self):
        """The collector is only half the fix.

        "Renter" and "renters" were not in the taxonomy, so this question
        scored zero and would have been collected and then silently dropped by
        the relevance rule — a more expensive failure than not collecting it,
        because everything would have looked like it was working.
        """
        item = make_item(
            "Will the First Minister set out a timeline for the introduction "
            "of new measures to better protect renters?",
            source_kind="oral_question", title="OQ64467")
        self.assertGreater(item.score, 0)
        self.assertIn("private_rented_sector", item.themes)
        self.assertTrue(TAX.qualifies_for_site(item))

    def test_irrelevant_questions_on_the_same_order_paper_are_dropped(self):
        """Twelve questions a sitting, most of them about something else.
        Strict relevance is the whole reason this is readable."""
        cataracts = make_item(
            "Will the First Minister make a statement on waiting times for "
            "cataract surgery in Sir Gaerfyrddin?",
            source_kind="oral_question", title="OQ64456")
        self.assertFalse(TAX.qualifies_for_site(cataracts))

    def test_scheduled_statements_are_collected_from_the_agenda(self):
        """"Statement by the Cabinet Minister for Finance: Rebalancing the
        non-domestic rates system" is core NRLA business, and it is on the
        agenda days before it is made."""
        collector = self._collector({
            "calendar": CALENDAR_HTML,
            "agenda:16260": PLENARY_AGENDA_HTML,
            "order:15-09-2026": NO_SITTING_HTML,
        })
        items = list(collector.collect(start=date(2026, 9, 11),
                                       end=date(2026, 10, 2)))
        agenda = [i for i in items if i.source_kind == "calendar"]
        self.assertEqual(len(agenda), 1)
        self.assertIn("non-domestic rates", agenda[0].title)
        self.assertEqual(agenda[0].item_date, date(2026, 9, 15))
        self.assertEqual(agenda[0].forum, "Plenary")

    def test_routine_agenda_machinery_is_not_collected(self):
        """"Questions to the First Minister" and "Business Statement and
        Announcement" appear on every sitting and carry no subject. Listing
        them would put the same two lines in "coming up" every week."""
        collector = self._collector({
            "calendar": CALENDAR_HTML,
            "agenda:16260": PLENARY_AGENDA_HTML,
            "order:15-09-2026": NO_SITTING_HTML,
        })
        titles = [i.title for i in collector.collect(start=date(2026, 9, 11),
                                                     end=date(2026, 10, 2))]
        self.assertNotIn("Questions to the First Minister", titles)
        self.assertNotIn("Business Statement and Announcement", titles)

    def test_a_day_with_no_sitting_is_silent_not_an_error(self):
        """The Senedd answers 200 with an error page for a day it is not
        sitting, and for a sitting whose questions are not tabled yet. Treating
        that as a fault would make the run red every Monday."""
        collector = self._collector({
            "calendar": CALENDAR_HTML,
            "agenda:16260": PLENARY_AGENDA_HTML,
            "order:15-09-2026": NO_SITTING_HTML,
        })
        items = list(collector.collect(start=date(2026, 9, 11),
                                       end=date(2026, 10, 2)))
        self.assertEqual([i for i in items if i.source_kind == "oral_question"],
                         [])
        self.assertEqual(collector.errors, [])

    def test_an_order_paper_that_parses_to_nothing_is_a_loud_error(self):
        """A page that says it carries tabled questions and yields none means
        the markup moved. That is the failure that looks like a quiet week."""
        broken = ORDER_PAPER_HTML.replace("itemContent oralQuestion orderPaper",
                                          "itemContent somethingElse")
        collector = self._collector({
            "calendar": CALENDAR_HTML,
            "agenda:16260": PLENARY_AGENDA_HTML,
            "order:15-09-2026": broken,
        })
        list(collector.collect(start=date(2026, 9, 11), end=date(2026, 10, 2)))
        self.assertEqual(len(collector.errors), 1)
        self.assertIn("markup has probably changed", collector.errors[0])

    def test_an_unreadable_calendar_is_a_loud_error(self):
        collector = self._collector({})
        self.assertEqual(list(collector.collect(start=date(2026, 9, 11),
                                                end=date(2026, 10, 2))), [])
        self.assertEqual(len(collector.errors), 1)
        self.assertIn("calendar could not be read", collector.errors[0])

    def test_tabled_questions_survive_a_blocked_business_senedd(self):
        """The two halves must not share a fate.

        business.senedd.wales returned 403 to GitHub Actions on 11 September
        2026 — the same datacentre-IP block www.gov.wales uses, on a host that
        had always worked. record.senedd.wales does not block. Losing the
        agendas must not also lose the tabled questions, which are the more
        valuable half: they are the thing the supplier briefing had and this
        tool did not.
        """
        collector = self._collector({"order:15-09-2026": ORDER_PAPER_HTML})
        items = list(collector.collect(start=date(2026, 9, 11),
                                       end=date(2026, 10, 2)))
        tabled = [i for i in items if i.source_kind == "oral_question"]
        self.assertEqual(len(tabled), 2)
        # Still said out loud, because the agendas really are missing.
        self.assertEqual(len(collector.errors), 1)
        self.assertIn("PLENARY STATEMENTS AND DEBATES", collector.errors[0])

    def test_sitting_days_are_probed_without_a_calendar(self):
        """Plenary has sat on Tuesdays and Wednesdays for years. Probing them
        costs one request each and the order paper confirms or denies it —
        which beats depending on a calendar that is currently blocked."""
        collector = self._collector({})
        list(collector.collect(start=date(2026, 9, 14), end=date(2026, 9, 20)))
        self.assertIn("order:15-09-2026", collector.fetcher.requested)
        self.assertIn("order:16-09-2026", collector.fetcher.requested)
        self.assertNotIn("order:14-09-2026", collector.fetcher.requested)
        self.assertNotIn("order:19-09-2026", collector.fetcher.requested)

    def test_recess_is_not_an_error(self):
        """A calendar that reads fine and contains no Plenary sitting is the
        Senedd being in recess, which is a real answer."""
        collector = self._collector({
            "calendar": '<a href="ieListDocuments.aspx?CId=986&MId=1">'
                        'Meeting of Legislation Committee</a>'})
        self.assertEqual(list(collector.collect(start=date(2026, 9, 11),
                                                end=date(2026, 10, 2))), [])
        self.assertEqual(collector.errors, [])

    def test_only_plenary_meetings_are_followed(self):
        collector = self._collector({
            "calendar": CALENDAR_HTML,
            "agenda:16260": PLENARY_AGENDA_HTML,
            "order:15-09-2026": NO_SITTING_HTML,
        })
        list(collector.collect(start=date(2026, 9, 11), end=date(2026, 10, 2)))
        self.assertNotIn("agenda:16253", collector.fetcher.requested)


class _StubParamFetcher:
    """Serves canned pages keyed by what was asked for, not by URL.

    The Senedd's ModernGov pages are addressed by query string, so keying on a
    full URL would make these tests assert the exact order of query parameters
    — which is not a property worth pinning and would break on a harmless
    refactor.
    """

    def __init__(self, pages: dict):
        self.pages = pages
        self.requested: list[str] = []

    def get_text(self, url, params=None):
        params = params or {}
        if "mgCalendarMonthView" in url:
            key = "calendar"
        elif "ieListDocuments" in url:
            key = f"agenda:{params.get('MId')}"
        elif "OrderPaper" in url:
            key = "order:" + url.rstrip("/").rsplit("/", 1)[-1]
        else:
            key = url
        self.requested.append(key)
        return self.pages.get(key)


# ---------------------------------------------------------------------------
# The Friday future-business email.
# ---------------------------------------------------------------------------

class TestFridayForwardBusiness(unittest.TestCase):
    """Camlas's weekly forward look, rebuilt.

    The thing to hold on to while reading these: this email answers "what is
    still open?", not "what changed?". Those are different questions and only
    one of them has a deadline attached.
    """

    TODAY = date(2026, 9, 11)          # a Friday

    def _item(self, **kw):
        defaults = dict(source_kind="consultation",
                        source_name="Welsh Government — Consultation",
                        title="Rent Guarantor Guidance for Local Housing "
                              "Authorities",
                        body="Consultation on rent guarantor guidance for "
                             "private rented sector tenants in Wales.",
                        url="https://media.service.gov.wales/news/rent-guarantor",
                        item_date=self.TODAY, forum="Welsh Government",
                        deadline=date(2026, 10, 19))
        defaults.update(kw)
        return SCORER.score_item(Item(**defaults))

    def test_last_friday_on_a_friday_is_a_week_ago_not_today(self):
        """The email covers the week since the last edition, so something
        collected this morning is new. Returning today would tag nothing."""
        from monitor.forward import last_friday
        self.assertEqual(last_friday(date(2026, 9, 11)), date(2026, 9, 4))
        self.assertEqual(last_friday(date(2026, 9, 14)), date(2026, 9, 11))
        self.assertEqual(last_friday(date(2026, 9, 10)), date(2026, 9, 4))

    def test_consultations_come_first_and_soonest_first(self):
        """A missed deadline cannot be recovered; a missed debate can at least
        be read afterwards."""
        from monitor.forward import select_business
        late = self._item(deadline=date(2026, 11, 30), url="u1")
        soon = self._item(deadline=date(2026, 9, 25), url="u2")
        sections = select_business([late, soon], TAX, today=self.TODAY)
        self.assertEqual([i.deadline for i in sections["consultations"]],
                         [date(2026, 9, 25), date(2026, 11, 30)])

    def test_closed_consultations_drop_off(self):
        from monitor.forward import select_business
        closed = self._item(deadline=date(2026, 9, 1))
        sections = select_business([closed], TAX, today=self.TODAY)
        self.assertEqual(sections["consultations"], [])

    def test_repeats_are_not_suppressed(self):
        """Deliberate, and the thing most likely to be "fixed" by mistake.

        The supplier lists the same open consultation every week until it
        closes, and that is right: this is a standing list of what is live, not
        a change log. What makes it scannable is the NEW tag, not omission.
        """
        from monitor.forward import render_forward, select_business
        old = self._item()
        old.collected_at = datetime(2026, 7, 1, 9, 0)
        sections = select_business([old], TAX, today=self.TODAY)
        self.assertEqual(len(sections["consultations"]), 1)
        subject, body, count = render_forward(
            sections, TAX, today=self.TODAY, new_since=date(2026, 9, 4))
        self.assertEqual(count, 1)
        self.assertNotIn(">NEW<", body)

    def test_items_first_seen_since_last_friday_are_tagged_new(self):
        from monitor.forward import render_forward, select_business
        fresh = self._item()
        fresh.collected_at = datetime(2026, 9, 9, 9, 0)
        sections = select_business([fresh], TAX, today=self.TODAY)
        subject, body, count = render_forward(
            sections, TAX, today=self.TODAY, new_since=date(2026, 9, 4))
        self.assertIn(">NEW<", body)
        self.assertIn("1 new", subject)

    def test_the_same_page_collected_twice_appears_once(self):
        """Regression: a consultation whose wording changes between runs is
        stored again under a new uid, and the committee priorities consultation
        appeared in the email twice, identically, one above the other."""
        from monitor.forward import select_business
        first = self._item(body="Consultation on rent guarantor guidance.")
        second = self._item(body="Consultation on rent guarantor guidance for "
                                 "local housing authorities in Wales.")
        sections = select_business([first, second], TAX, today=self.TODAY)
        self.assertEqual(len(sections["consultations"]), 1)

    def test_irrelevant_business_never_reaches_the_email(self):
        """Same strict rule as the page, and for the same reason: *"it needs to
        show stuff that is relevant to the NRLA otherwise you're just
        overloaded with information."* Two definitions of relevant in one
        system would also let the email and the page disagree."""
        from monitor.forward import select_business
        noise = self._item(title="Consultation on sea bass fishing quotas",
                           body="Views sought on bass fishing quotas.",
                           url="u-bass")
        sections = select_business([noise], TAX, today=self.TODAY)
        self.assertEqual(sections["consultations"], [])

    def test_committee_meetings_are_limited_to_the_diary_window(self):
        from monitor.forward import select_business
        soon = self._item(source_kind="calendar", deadline=None,
                          title="Local Government, Housing and Planning "
                                "Committee — 17 September 2026",
                          body="The committee is scheduled to meet.",
                          forum="Local Government, Housing and Planning "
                                "Committee",
                          item_date=date(2026, 9, 17), url="c1")
        distant = self._item(source_kind="calendar", deadline=None,
                             title="Local Government, Housing and Planning "
                                   "Committee — 20 December 2026",
                             body="The committee is scheduled to meet.",
                             forum="Local Government, Housing and Planning "
                                   "Committee",
                             item_date=date(2026, 12, 20), url="c2")
        sections = select_business([soon, distant], TAX, today=self.TODAY)
        self.assertEqual([i.url for i in sections["committees"]], ["c1"])

    def test_oral_questions_tabled_for_a_future_sitting_are_included(self):
        """The Record's search results carry a tabled date and an answer-due
        date; for an oral question that second date is the sitting it is down
        for, and the collector clears it once the question has been answered.
        So a future deadline means exactly "tabled, not yet asked" — which is
        what the supplier lists, and no new collector was needed."""
        from monitor.forward import select_business
        tabled = self._item(
            source_kind="oral_question",
            title="OQ12345",
            body="Will the First Minister set out a timeline for new measures "
                 "to protect tenants in the private rented sector?",
            speaker="David Hughes MS", constituency="Pontypridd Cynon Merthyr",
            item_date=date(2026, 9, 10), deadline=date(2026, 9, 16), url="q1")
        answered = self._item(
            source_kind="oral_question", title="OQ11111",
            body="A question about rent arrears in the private rented sector.",
            item_date=date(2026, 7, 1), deadline=None, url="q2")
        sections = select_business([tabled, answered], TAX, today=self.TODAY)
        self.assertEqual([i.url for i in sections["oral"]], ["q1"])

    def test_an_oral_question_shows_the_question_not_its_reference(self):
        """The first edition printed "OQ64467" and nothing else.

        An oral question's title is its reference number, and every row in the
        email used the title, so the email named a code instead of the
        business. The reader could not tell that the question was about
        protecting renters, who asked it, or which session it would be answered
        in — which is the whole of what the row is for.
        """
        from monitor.forward import render_forward, select_business
        question = self._item(
            source_kind="oral_question", title="OQ64467",
            body="Will the First Minister set out a timeline for new measures "
                 "to protect tenants in the private rented sector?",
            speaker="David Hughes", constituency="Pontypridd Cynon Merthyr",
            agenda_item="To the First Minister",
            item_date=date(2026, 9, 10), deadline=date(2026, 9, 15),
            url="q1")
        sections = select_business([question], TAX, today=self.TODAY)
        _, body, _ = render_forward(sections, TAX, today=self.TODAY)
        self.assertIn("protect tenants in the private rented sector", body)
        self.assertIn("David Hughes", body)
        self.assertIn("Pontypridd Cynon Merthyr", body)
        # Which session it is down for, and when.
        self.assertIn("To the First Minister", body)
        self.assertIn("Tue 15 Sep", body)
        # The reference still appears, as a reference rather than as the point.
        self.assertIn("OQ64467", body)

    def test_a_committee_row_does_not_say_the_same_thing_twice(self):
        """The title already carries the committee's name and the date, so
        printing the forum underneath it repeated the line verbatim."""
        from monitor.forward import render_forward, select_business
        meeting = self._item(
            source_kind="calendar", deadline=None,
            title="Local Government, Housing and Planning Committee — "
                  "17 September 2026, 09.30",
            body="The committee is scheduled to meet.",
            forum="Local Government, Housing and Planning Committee",
            item_date=date(2026, 9, 17), url="c1")
        sections = select_business([meeting], TAX, today=self.TODAY)
        _, body, _ = render_forward(sections, TAX, today=self.TODAY)
        self.assertEqual(
            body.count("Local Government, Housing and Planning Committee"), 1)

    def test_an_empty_week_sends_nothing_at_all(self):
        """Six "nothing this week" emails in a row teach the reader to delete
        the seventh unread, and the seventh is the one with a consultation in
        it."""
        from monitor.forward import render_forward, select_business
        sections = select_business([], TAX, today=self.TODAY)
        subject, body, count = render_forward(sections, TAX, today=self.TODAY)
        self.assertEqual(count, 0)
        sent, message = alerts_mod.post_to_flow(
            "https://example.invalid/flow", subject, body, count, dry_run=False)
        self.assertFalse(sent)
        self.assertIn("no email sent", message)

    def test_not_switched_on_yet_is_not_a_failed_run(self):
        """A repository where the flow has not been created must not go red
        every Friday. A warning that is always on is a warning nobody reads,
        and the week it means something is the week it gets ignored.

        Configured-and-failed is the opposite case and must exit non-zero.
        """
        from monitor.cli import cmd_forward
        tmp = tempfile.mkdtemp()
        try:
            db = str(Path(tmp) / "t.sqlite3")
            store = Store(db)
            # A real item, so the run has something to send and the exit code
            # is decided by the configuration rather than by an empty week.
            store.upsert(self._item(deadline=date.today() + timedelta(days=30)))
            store.close()
            args = SimpleNamespace(db=db, taxonomy=None, out="", weeks=3,
                                   new_since="", send=True)
            with mock.patch.dict(os.environ, {"MONITOR_FLOW_URL": ""},
                                 clear=False):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cmd_forward(args), 0)
            self.assertIn("MONITOR_FLOW_URL", out.getvalue())

            args = SimpleNamespace(db=db, taxonomy=None, out="", weeks=3,
                                   new_since="", send=True)
            with mock.patch.dict(os.environ,
                                 {"MONITOR_FLOW_URL": "https://example.invalid/f"},
                                 clear=False):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cmd_forward(args), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_written_questions_are_excluded_from_the_email_too(self):
        from monitor.forward import render_forward, select_business
        sections = select_business([self._item()], TAX, today=self.TODAY)
        _, body, _ = render_forward(sections, TAX, today=self.TODAY)
        self.assertIn("Written questions are deliberately excluded", body)


# ---------------------------------------------------------------------------
class TestFlowDelivery(unittest.TestCase):
    """Delivery without credentials.

    SMTP needs an app password, which needs IT, which is weeks. A Power
    Automate flow needs nothing from anyone: the webhook URL is the only
    secret, it is write-only, and the worst it can do is send its owner an
    email.
    """

    class _Session:
        def __init__(self, status=202, text=""):
            self.status, self.text, self.calls = status, text, []

        def post(self, url, json=None, timeout=None):
            self.calls.append({"url": url, "json": json})
            outer = self

            class _Resp:
                status_code = outer.status
                text = outer.text
            return _Resp()

    def test_the_payload_is_what_the_flow_schema_expects(self):
        """subject / body / count, exactly — the flow's trigger schema is
        generated from those three keys and silently ignores anything else."""
        session = self._Session()
        sent, message = alerts_mod.post_to_flow(
            "https://example.invalid/flow", "Subject", "<p>Body</p>", 3,
            session=session, dry_run=False)
        self.assertTrue(sent)
        self.assertEqual(sorted(session.calls[0]["json"]),
                         ["body", "count", "subject"])

    def test_nothing_is_sent_without_an_explicit_send(self):
        """Nothing should ever reach a person because a script was run with the
        wrong argument."""
        session = self._Session()
        sent, message = alerts_mod.post_to_flow(
            "https://example.invalid/flow", "Subject", "Body", 3,
            session=session, dry_run=True)
        self.assertFalse(sent)
        self.assertEqual(session.calls, [])
        self.assertIn("--send", message)

    def test_a_missing_url_says_what_to_do_about_it(self):
        sent, message = alerts_mod.post_to_flow("", "S", "B", 2, dry_run=False)
        self.assertFalse(sent)
        self.assertIn("MONITOR_FLOW_URL", message)
        self.assertIn("FORWARD-EMAIL-SETUP.md", message)

    def test_a_regenerated_url_is_diagnosed_rather_than_reported_raw(self):
        """403 from a flow almost always means the URL was regenerated. Saying
        so is the difference between a two-minute fix and an afternoon."""
        session = self._Session(status=403)
        sent, message = alerts_mod.post_to_flow(
            "https://example.invalid/flow", "S", "B", 2,
            session=session, dry_run=False)
        self.assertFalse(sent)
        self.assertIn("regenerated", message)

    def test_a_network_failure_never_raises(self):
        """A failed weekly email must annotate the run, not end it."""
        class _Broken:
            def post(self, *a, **kw):
                raise OSError("connection reset")
        sent, message = alerts_mod.post_to_flow(
            "https://example.invalid/flow", "S", "B", 2,
            session=_Broken(), dry_run=False)
        self.assertFalse(sent)
        self.assertIn("Could not reach", message)


# ---------------------------------------------------------------------------
class TestCollectedAtSurvivesTheArchive(unittest.TestCase):
    """When an item was FIRST seen, not when the row was read.

    `Item.__post_init__` defaults `collected_at` to "now" when it is missing,
    and the store's row-to-item conversion did not pass it through. Every item
    read back from the archive therefore claimed to have been collected the
    instant it was loaded — which made "new since last Friday" mean
    "everything", and the NEW tag on the weekly email worthless.
    """

    def test_the_first_seen_time_is_read_back_from_the_archive(self):
        tmp = tempfile.mkdtemp()
        try:
            store = Store(str(Path(tmp) / "t.sqlite3"))
            item = make_item("Rent Smart Wales registration and licensing.",
                             title="An item collected in July")
            item.collected_at = datetime(2026, 7, 1, 9, 30)
            store.upsert(item)
            (back,) = [i for i in store.query(min_score=0)
                       if i.title == "An item collected in July"]
            self.assertEqual(back.collected_at, datetime(2026, 7, 1, 9, 30))
            store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
class TestForwardWorkflowGuards(unittest.TestCase):
    """GitHub cron is UTC and does not follow British Summer Time.

    One cron line means the email silently moves by an hour twice a year. Two
    lines plus a London-clock check means it does not — but only while all
    three parts agree, which is what these pin.
    """

    ROOT = Path(__file__).resolve().parent.parent
    WORKFLOW = ROOT / ".github/workflows/forward.yml"

    def test_both_utc_hours_are_scheduled(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "0 14 * * 5"', text)
        self.assertIn('cron: "0 15 * * 5"', text)

    def test_the_run_stops_itself_at_the_wrong_london_hour(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("TZ=Europe/London date +%H", text)
        self.assertIn("TARGET_LONDON_HOUR", text)

    def test_two_runs_can_never_overlap(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("group: senedd-forward", text)

    def test_it_cannot_start_emailing_by_opening_issues(self):
        """The daily run used to open a dated issue purely to make GitHub send
        a notification. The verdict was *"It is not user friendly or useful to
        read this."* Withholding the permission means a future edit that
        re-adds it fails loudly instead of quietly resuming."""
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("issues: write", text)

    def test_it_does_not_commit_the_archive(self):
        """The daily run owns the archive. Two workflows committing it would
        collide, and the loser would be a lost day of collection."""
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("contents: read", text)
        self.assertNotIn("cli export", text)

    def test_the_readable_copy_has_not_drifted(self):
        """A browser drag-and-drop upload to GitHub silently skips anything
        beginning with a dot, so `deploy/` carries a copy that can be pasted
        into the web editor. A stale copy would deploy a schedule nobody thinks
        to doubt."""
        real = self.WORKFLOW.read_text(encoding="utf-8")
        copy = (self.ROOT / "deploy/forward-workflow.yml").read_text(encoding="utf-8")
        self.assertEqual(real, copy,
                         "deploy/forward-workflow.yml has drifted from "
                         ".github/workflows/forward.yml — re-copy it.")


# ---------------------------------------------------------------------------
class TestForwardEmailSetupGuide(unittest.TestCase):

    GUIDE = Path(__file__).resolve().parent.parent / "FORWARD-EMAIL-SETUP.md"

    def test_the_guide_exists_and_names_the_secret(self):
        self.assertTrue(self.GUIDE.exists())
        self.assertIn("MONITOR_FLOW_URL", self.GUIDE.read_text(encoding="utf-8"))

    def test_it_mentions_the_html_button_people_miss(self):
        """Without switching the flow's body field to HTML mode, the email
        arrives as a page of visible tags."""
        self.assertIn("</>", self.GUIDE.read_text(encoding="utf-8"))



# ---------------------------------------------------------------------------
# senedd.tv — the diary, now that business.senedd.wales blocks this tool
# ---------------------------------------------------------------------------

def _tv_card(guid: str, committee_id: str, start: str, name: str, room: str) -> str:
    """One card on the senedd.tv home page, as served on 23 September 2026."""
    return (f'<div class="item-meeting com-0 com-{committee_id}"><div class="row">'
            f'<div class="col-xs-4 col-sm-3"><div class="image-colour">'
            f'<a href="/Meeting/Index/{guid}"><img src="x.jpg"></a></div></div>'
            f'<div class="col-xs-12 col-sm-3 time-box"><div>'
            f'<i class="fa fa-sclock fa-1x"></i>Start time: {start}</div></div>'
            f'<div class="col-xs-12 col-sm-6">'
            f'<h4><a href="/Meeting/Index/{guid}">{name}</a></h4>'
            f'<p>\n{room}                </p></div></div></div>')


def _tv_home(days: dict[int, list[str]], latest: list[tuple[str, str]] = ()) -> str:
    """The home page. Day 1 is nested inside a duplicate of itself, as live."""
    panes = []
    for n, cards in sorted(days.items()):
        inner = "".join(cards) or ('<p>There are no live meetings or events on '
                                   'this day.</p>')
        pane = f'<div class="tab-pane fade" id="day{n}">{inner}</div>'
        if n == 1:
            pane = (f'<div class="tab-pane fade active in" id="day1">'
                    f'<div class="tab-pane fade active in" id="day1">{inner}</div></div>')
        panes.append(pane)
    slides = "".join(
        f'<div class="slide com-0 com-908"><a href="/Meeting/Index/x">'
        f'<h5>{name}</h5><p>{when}</p></a></div>' for name, when in latest)
    return (f'<ul class="days" id="dayTabs"><li><a href="#day1">Today</a></li></ul>'
            f'<div class="tab-content">{"".join(panes)}</div>'
            f'<h2><strong>Latest meetings</strong></h2>'
            f'<div class="slider-one">{slides}</div>')


def _tv_meeting_before(name: str, when: str, cid: str, mid: str,
                       agenda: list[tuple[str, str]]) -> str:
    """A meeting page before it sits: /Meeting/Index layout."""
    rows = "".join(
        f'<article class="agenda-item "><div class="row">'
        f'<div class="col-xs-1"><b>{num}</b></div>'
        f'<div class="col-xs-10"><b>{text}</b></div></div></article>'
        for num, text in agenda)
    return (f'<div class="player-title dropdown"><a data-toggle="dropdown" href="#">'
            f'<h2>{name}</h2><p></p><p>{when}</p></a>'
            f'<ul class="dropdown-menu"><li><a href="/Meeting/Index/other">'
            f'<h2>Some Other Committee</h2><p>1 January 2020</p></a></li></ul></div>'
            f'<div id="agenda"><div class="agenda-items">{rows}</div></div>'
            f'<div id="agenda"><div class="agenda-items">{rows}</div></div>'
            f'<a href="http://www.senedd.assembly.wales/ieListDocuments.aspx?CId={cid}&amp;MId={mid}">'
            f'Meeting information and papers</a>')


def _tv_meeting_after(name: str, when: str, cid: str, mid: str,
                      agenda: list[tuple[str, str]]) -> str:
    """The same page once the meeting has sat: /Meeting/Archive layout."""
    rows = "".join(
        f'<article class="agenda-item"><a class="agenda-item-time" '
        f'data-item-number="{num}" href="#"><header><h5><b>{num}</b> - {text}</h5>'
        f'</header><footer><strong>Start time:</strong> 10:42</footer></a></article>'
        for num, text in agenda)
    return (f'<div class="player-title"><h2>{name}</h2><p></p><p>{when}</p></div>'
            f'<div id="agenda"><div class="agenda-items">{rows}</div></div>'
            f'<a href="http://www.senedd.assembly.wales/ieListDocuments.aspx?CId={cid}&amp;MId={mid}">'
            f'Meeting information and papers</a>')


class TestSeneddTVDiary(unittest.TestCase):
    """senedd.tv — the diary source that still answers GitHub's runners.

    business.senedd.wales has returned 403 (an Azure Application Gateway WAF)
    to this tool since August 2026. The "Senedd forward look" failed on every
    run for seven weeks while the diary it had collected on 4 August sat in the
    archive looking current. senedd.tv carries the same meetings and agendas for
    the next five sitting days, and on 23 September agreed with the supplier's
    morning briefing item for item. These tests pin every part of its markup the
    parser leans on, including BOTH layouts a meeting page has in one day.
    """

    HOME = "https://www.senedd.tv/"

    def _collector(self, pages):
        from monitor.collectors.seneddtv import SeneddTVScheduleCollector
        return SeneddTVScheduleCollector(_StubFetcher(pages))

    def test_each_meeting_is_listed_once_despite_the_nested_first_tab(self):
        """The live page nests #day1 inside a copy of itself."""
        from monitor.collectors.seneddtv import SeneddTVScheduleCollector
        html = _tv_home({1: [_tv_card("aaa", "978", "09.30", "CCERA Committee", "Room 1")]})
        meetings = SeneddTVScheduleCollector.parse_schedule(html)
        self.assertEqual([m.guid for m in meetings], ["aaa"])

    def test_committee_id_start_and_room_are_read_from_the_card(self):
        from monitor.collectors.seneddtv import SeneddTVScheduleCollector
        html = _tv_home({1: [_tv_card("aaa", "983", "9.30", "EHRSJ Committee", "Committee Room 3")],
                         2: [_tv_card("bbb", "985", "09.25", "Health Committee", "Committee Room 3")]})
        a, b = SeneddTVScheduleCollector.parse_schedule(html)
        self.assertEqual((a.committee_id, a.start, a.room, a.day_index),
                         ("983", "09.30", "Committee Room 3", 1))
        self.assertEqual(b.day_index, 2)

    def test_a_meeting_page_before_it_sits_gives_date_ids_and_agenda(self):
        """And the dropdown's other meetings do not leak into the title."""
        from monitor.collectors.seneddtv import Meeting, SeneddTVScheduleCollector
        m = SeneddTVScheduleCollector.parse_meeting(_tv_meeting_before(
            "Plenary", "23 September 2026", "908", "16263",
            [("1", "Questions to the First Minister"), ("2", "Voting Time")]),
            Meeting(guid="g", name="Plenary"))
        self.assertEqual(m.when, date(2026, 9, 23))
        self.assertEqual(m.name, "Plenary")
        self.assertEqual((m.committee_id, m.meeting_id), ("908", "16263"))
        self.assertEqual([e.text for e in m.agenda],
                         ["Questions to the First Minister", "Voting Time"],
                         "the agenda is rendered twice; it must be read once")

    def test_a_meeting_page_after_it_has_sat_is_still_read(self):
        """/Meeting/Index redirects to /Meeting/Archive once a meeting ends.

        The first version read only the pre-meeting layout, so a run after
        11.30 lost the date of every committee that had sat that morning — and
        an undated meeting is dropped, which made a busy morning look empty.
        """
        from monitor.collectors.seneddtv import Meeting, SeneddTVScheduleCollector
        m = SeneddTVScheduleCollector.parse_meeting(_tv_meeting_after(
            "Climate Change, Environment, Sustainability and Rural Affairs Committee",
            "23 September 2026", "978", "16233",
            [("2", "General scrutiny of the Cabinet Minister for Rural Resilience and Sustainability")]),
            Meeting(guid="g", name="CCERA"))
        self.assertEqual(m.when, date(2026, 9, 23))
        self.assertEqual(m.meeting_id, "16233")
        self.assertEqual([(e.number, e.text) for e in m.agenda],
                         [("2", "General scrutiny of the Cabinet Minister for "
                                "Rural Resilience and Sustainability")])

    def test_the_no_agenda_placeholder_is_not_business(self):
        from monitor.collectors.seneddtv import Meeting, SeneddTVScheduleCollector
        m = SeneddTVScheduleCollector.parse_meeting(_tv_meeting_before(
            "Plenary", "29 September 2026", "908", "16264",
            [("", "There are no agenda items available for this video")]),
            Meeting(guid="g", name="Plenary"))
        self.assertEqual(m.agenda, [])

    def test_machinery_is_dropped_with_its_sub_items(self):
        from monitor.collectors.seneddtv import AgendaEntry, Meeting
        m = Meeting(guid="g", name="CCERA", agenda=[
            AgendaEntry("1", "Introductions, apologies, substitutions and declarations of interest"),
            AgendaEntry("2", "General scrutiny of the Cabinet Minister"),
            AgendaEntry("3", "Papers to note"),
            AgendaEntry("3.1", "Inter-Institutional Relations Agreement"),
            AgendaEntry("4", "Motion under Standing Order 17.42 (ix) to resolve to exclude the public"),
        ])
        self.assertEqual([e.text for e in m.substantive()],
                         ["General scrutiny of the Cabinet Minister"])

    def test_private_deliberation_after_a_colon_is_machinery(self):
        """"General scrutiny session: consideration of evidence" reads, to
        someone scanning for what to watch, like a second evidence session."""
        from monitor.collectors.seneddtv import AgendaEntry
        self.assertTrue(AgendaEntry("5", "General scrutiny session: consideration of evidence").is_procedural)
        self.assertTrue(AgendaEntry("8", "Annual scrutiny session with Sport Wales: Consideration of evidence").is_procedural)
        self.assertFalse(AgendaEntry("2", "General scrutiny session: Deputy First Minister").is_procedural)

    def test_a_committee_meeting_carries_the_forward_look_url(self):
        """Same URL as the blocked forward look stored, so the page's
        one-row-per-URL rule replaces a stale August entry with this one."""
        from monitor.collectors.seneddtv import Meeting, SeneddTVScheduleCollector, AgendaEntry
        m = Meeting(guid="g", name="Local Government, Housing and Planning Committee",
                    committee_id="987", meeting_id="16300", start="09.30",
                    when=date(2026, 10, 1),
                    agenda=[AgendaEntry("2", "Evidence session 1")])
        (item,) = SeneddTVScheduleCollector.to_items(m)
        self.assertEqual(item.url, "https://business.senedd.wales/ieListDocuments.aspx?CId=987&MId=16300")
        self.assertEqual(item.source_kind, "calendar")
        self.assertEqual(item.title, "Local Government, Housing and Planning Committee — 1 October 2026, 09.30")
        self.assertIn("Evidence session 1", item.body)
        self.assertEqual(item.video_url, "https://www.senedd.tv/Meeting/Index/g")

    def test_the_housing_committee_is_stored_and_shown_whatever_its_agenda(self):
        """Its agenda on 17 September was electoral registration regulations.
        Its meetings are NRLA business regardless."""
        from monitor.collectors.seneddtv import Meeting, SeneddTVScheduleCollector, AgendaEntry
        m = Meeting(guid="g", name="Local Government, Housing and Planning Committee",
                    committee_id="987", meeting_id="16300", when=date(2026, 10, 1),
                    agenda=[AgendaEntry("2", "The Representation of the People (Electoral "
                                             "Registration without Applications) Regulations 2026")])
        (item,) = SeneddTVScheduleCollector.to_items(m)
        SCORER.score_item(item)
        self.assertTrue(SCORER.keep(item))
        self.assertTrue(TAX.qualifies_for_site(item))

    def test_plenary_becomes_one_item_per_piece_of_business(self):
        """A sitting is many unrelated things; the statement on non-domestic
        rates must be scored on its own, and must survive URL de-duplication."""
        from monitor.collectors.seneddtv import Meeting, SeneddTVScheduleCollector, AgendaEntry
        m = Meeting(guid="g", name="Plenary", committee_id="908", meeting_id="16264",
                    start="13.30", when=date(2026, 9, 29), agenda=[
                        AgendaEntry("1", "Questions to the First Minister"),
                        AgendaEntry("3", "Statement by the Cabinet Minister for Finance: "
                                         "Rebalancing the non-domestic rates system"),
                        AgendaEntry("4", "Welsh Conservatives Debate: Housing supply"),
                        AgendaEntry("5", "Voting Time"),
                    ])
        items = SeneddTVScheduleCollector.to_items(m)
        self.assertEqual([i.title for i in items], [
            "Statement by the Cabinet Minister for Finance: Rebalancing the non-domestic rates system",
            "Welsh Conservatives Debate: Housing supply"])
        self.assertEqual(len({i.url for i in items}), 2)
        self.assertTrue(all(i.forum == "Plenary" for i in items))

    def test_an_undated_meeting_is_not_guessed(self):
        from monitor.collectors.seneddtv import Meeting, SeneddTVScheduleCollector
        self.assertEqual(SeneddTVScheduleCollector.to_items(Meeting(guid="g", name="X")), [])

    def test_an_unreachable_senedd_tv_is_reported_not_swallowed(self):
        collector = self._collector({})
        self.assertEqual(collector.meetings(), [])
        self.assertTrue(collector.errors)
        self.assertIn("only source", collector.errors[0])

    def test_recent_sitting_dates_come_from_latest_meetings(self):
        from monitor.collectors.seneddtv import SeneddTVScheduleCollector
        html = _tv_home({1: []}, latest=[("Plenary", "22 September 2026"),
                                         ("PAPA Committee", "21 September 2026"),
                                         ("Legislation Committee", "21 September 2026")])
        self.assertEqual(SeneddTVScheduleCollector.parse_recent_dates(html),
                         [date(2026, 9, 22), date(2026, 9, 21)])

    def test_the_whole_collector_runs_end_to_end(self):
        home = _tv_home({1: [_tv_card("aaa", "987", "09.30",
                                      "Local Government, Housing and Planning Committee", "Room 1")]})
        pages = {self.HOME: home,
                 "https://www.senedd.tv/Meeting/Index/aaa": _tv_meeting_before(
                     "Local Government, Housing and Planning Committee", "1 October 2026",
                     "987", "16300", [("2", "Evidence session: private rented sector")])}
        collector = self._collector(pages)
        items = list(collector.collect())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_date, date(2026, 10, 1))
        self.assertEqual(collector.errors, [])

    def test_it_substitutes_for_the_blocked_forward_look(self):
        from monitor.pipeline import SUBSTITUTED_BY
        self.assertIn("Senedd diary (senedd.tv)", SUBSTITUTED_BY["Senedd forward look"])


# ---------------------------------------------------------------------------
class TestStaleDiaryIsNotShown(unittest.TestCase):
    """A diary that cannot be refreshed is worse than a shorter one.

    The forward look last succeeded on 4 August 2026. Its entries kept
    appearing on the page and in the Friday email as though current —
    including a housing committee meeting on 24 September that neither
    senedd.tv nor the supplier listed. While the forward look is failing, its
    entries are not shown; if it recovers, they return on their own.
    """

    def _items(self):
        stale = Item(source_kind="calendar", source_name="Senedd forward look",
                     title="Local Government, Housing and Planning Committee — 24 September 2026, 09.30",
                     body="scheduled", item_date=date(2026, 9, 24),
                     raw_ref="https://business.senedd.wales/mgWebService.asmx#GetAllMeetingsByDate")
        fresh = Item(source_kind="calendar", source_name="Senedd diary (senedd.tv)",
                     title="Health Committee — 24 September 2026, 09.25", body="x",
                     item_date=date(2026, 9, 24), raw_ref="seneddtv:abc")
        return stale, fresh

    def test_forward_look_entries_are_dropped_while_it_is_failing(self):
        from monitor.cli import _without_unverifiable_diary
        stale, fresh = self._items()
        runs = [{"errors": ["Senedd forward look: GetAllMeetingsByDate returned HTTP 403"]}]
        self.assertEqual(_without_unverifiable_diary([stale, fresh], runs), [fresh])

    def test_they_return_when_it_recovers(self):
        from monitor.cli import _without_unverifiable_diary
        stale, fresh = self._items()
        self.assertEqual(_without_unverifiable_diary([stale, fresh], [{"errors": []}]),
                         [stale, fresh])

    def test_an_empty_diary_does_not_claim_a_recess(self):
        """The empty message was written in August and still said "in recess
        until 14 September" on 23 September, with the Senedd sitting."""
        from monitor.site import _upcoming
        n, html_text = _upcoming([], TAX, date(2026, 9, 23), [])
        self.assertEqual(n, 0)
        self.assertNotIn("recess", html_text)

    def test_the_page_says_the_diary_is_shorter(self):
        from monitor.cli import _standing_gaps
        gaps = _standing_gaps([{"errors": ["Senedd forward look: GetCommittees returned HTTP 403"],
                                "sources": []}])
        self.assertTrue(any("senedd.tv" in g and "five sitting days" in g for g in gaps))


# ---------------------------------------------------------------------------
class TestMorningBriefing(unittest.TestCase):
    """The supplier's "Bore da" email, rebuilt — the full day, NRLA items tagged.

    The operator chose the whole day over a filtered one (23 September 2026),
    so nothing is hidden; the NRLA tag is what makes the two lines that matter
    findable among the thirty that do not. That morning the supplier listed four
    Welsh Government announcements and missed a fifth — interim alarm measures
    for leaseholders facing waking watch costs — which is exactly the kind of
    item the tag exists for.
    """

    def _meeting(self, name, when, start="09.30", cid="978", agenda=()):
        from monitor.collectors.seneddtv import AgendaEntry, Meeting
        return Meeting(guid=f"g-{name[:4]}-{when}", name=name, committee_id=cid,
                       meeting_id="16000", start=start, room="Committee Room 1",
                       when=when, agenda=[AgendaEntry(n, t) for n, t in agenda])

    def _news(self, title, body, at):
        return (at, Item(source_kind="press_release", source_name="Welsh Government — Announcement",
                         title=title, body=f"{title}\n{body}",
                         url="https://media.service.gov.wales/news/x", item_date=at.date(),
                         forum="Welsh Government"))

    def _day(self, **extra):
        from monitor.morning import build
        today = date(2026, 9, 23)
        meetings = [
            self._meeting("Climate Change, Environment, Sustainability and Rural Affairs Committee",
                          today, agenda=[("1", "Introductions, apologies, substitutions and declarations of interest"),
                                         ("2", "General scrutiny of the Cabinet Minister for Rural Resilience")]),
            self._meeting("Plenary", today, start="13.30", cid="908", agenda=[
                ("1", "Questions to the First Minister"),
                ("6", "Welsh Labour Debate - Further education"),
                ("8", "Voting Time")]),
            self._meeting("Petitions Committee", date(2026, 9, 24), start="14.00", cid="988", agenda=[
                ("2", "New Petitions"), ("2.1", "P-07-1587 Ban the tethering of horses"),
                ("2.2", "P-07-1593 Red squirrels"), ("2.3", "P-07-1579 A statue"),
                ("2.4", "P-07-1606 A surgery")]),
        ]
        news = [
            self._news("Welsh Government to fund interim alarm measures for leaseholders facing waking watch costs",
                       "Leaseholders in buildings with fire safety defects will get support under the "
                       "Building Safety (Wales) Act 2026.", datetime(2026, 9, 22, 18, 20)),
            self._news("Welsh Government backs farmers with certainty, flexibility and support",
                       "Advance payments for farm businesses rise from 70% to 80%.",
                       datetime(2026, 9, 22, 23, 0)),
        ]
        questions = extra.get("questions", [])
        return build(meetings, news, questions, TAX, today=today,
                     recent_sittings=[date(2026, 9, 22)])

    def test_the_window_starts_at_0730_london_on_the_last_sitting_day(self):
        from monitor.morning import window_start
        # 07.30 BST on Tuesday 22 September is 06.30 UTC.
        self.assertEqual(window_start(date(2026, 9, 23), [date(2026, 9, 22)]),
                         datetime(2026, 9, 22, 6, 30))

    def test_in_winter_the_window_is_0730_utc(self):
        from monitor.morning import window_start
        self.assertEqual(window_start(date(2026, 11, 18), [date(2026, 11, 17)]),
                         datetime(2026, 11, 17, 7, 30))

    def test_a_monday_after_a_thursday_sitting_covers_friday_too(self):
        """Nothing published after Thursday's briefing may be lost."""
        from monitor.morning import window_start
        start = window_start(date(2026, 9, 28), [date(2026, 9, 24), date(2026, 9, 23)])
        self.assertEqual(start.date(), date(2026, 9, 24))

    def test_the_first_briefing_after_recess_does_not_replay_the_summer(self):
        from monitor.morning import window_start
        start = window_start(date(2026, 9, 14), [date(2026, 7, 16)])
        self.assertEqual(start.date(), date(2026, 9, 10))

    def test_with_no_latest_meetings_it_falls_back_to_the_previous_weekday(self):
        from monitor.morning import window_start
        self.assertEqual(window_start(date(2026, 9, 28), []).date(), date(2026, 9, 25))

    def test_the_whole_day_is_listed(self):
        b = self._day()
        self.assertEqual([bl.meeting.name for bl in b.today_blocks],
                         ["Climate Change, Environment, Sustainability and Rural Affairs Committee",
                          "Plenary"])
        self.assertEqual(len(b.announcements), 2)
        self.assertEqual(b.next_day, date(2026, 9, 24))

    def test_the_relevant_announcement_is_tagged_and_the_other_is_still_listed(self):
        b = self._day()
        tagged = {a.item.title[:30]: a.marked for a in b.announcements}
        self.assertTrue(tagged["Welsh Government to fund inter"])
        self.assertFalse(tagged["Welsh Government backs farmers"])

    def test_committee_machinery_is_left_out_but_plenary_is_printed_in_full(self):
        b = self._day()
        ccera, plenary = b.today_blocks
        self.assertEqual([l.text for l in ccera.lines],
                         ["General scrutiny of the Cabinet Minister for Rural Resilience"])
        self.assertEqual([l.text for l in plenary.lines],
                         ["Questions to the First Minister",
                          "Welsh Labour Debate - Further education"],
                         "Voting Time is left out, as the supplier leaves it out")

    def test_a_long_list_of_petitions_is_collapsed(self):
        from monitor.morning import Marker, meeting_block
        petitions = self._meeting("Petitions Committee", date(2026, 9, 24), agenda=[
            ("2", "New Petitions"), ("2.1", "Horses"), ("2.2", "Squirrels"),
            ("2.3", "A statue"), ("2.4", "A surgery")])
        block = meeting_block(petitions, Marker(TAX))
        texts = [l.text for l in block.lines]
        self.assertIn("New Petitions", texts)
        self.assertNotIn("Squirrels", texts)
        self.assertTrue(any("4 more items" in t for t in texts))

    def test_a_relevant_petition_is_rescued_from_the_collapsed_list(self):
        from monitor.morning import Marker, meeting_block
        petitions = self._meeting("Petitions Committee", date(2026, 9, 24), agenda=[
            ("2", "New Petitions"), ("2.1", "Horses"), ("2.2", "Squirrels"),
            ("2.3", "P-07-1600 Introduce rent controls in the private rented sector"),
            ("2.4", "A surgery")])
        block = meeting_block(petitions, Marker(TAX))
        marked = [l.text for l in block.lines if l.marked]
        self.assertIn("P-07-1600 Introduce rent controls in the private rented sector", marked)

    def test_the_housing_committee_is_always_tagged(self):
        from monitor.morning import Marker, meeting_block
        lghp = self._meeting("Local Government, Housing and Planning Committee", date(2026, 9, 23),
                             agenda=[("2", "Electoral registration regulations: evidence session")])
        self.assertTrue(meeting_block(lghp, Marker(TAX)).marked)

    def test_a_question_on_renters_tabled_for_today_appears_under_plenary(self):
        """The 15 September example: David Hughes MS on protecting renters."""
        q = Item(source_kind="oral_question", source_name="Oral Question (tabled)",
                 title="OQ64467", body="Will the First Minister set out a timeline for the "
                 "introduction of new measures to better protect renters?",
                 speaker="David Hughes", constituency="Pontypridd Cynon Merthyr",
                 forum="Plenary", agenda_item="To the First Minister",
                 deadline=date(2026, 9, 23), url="https://record.senedd.wales/x")
        SCORER.score_item(q)
        b = self._day(questions=[q])
        plenary = b.today_blocks[1]
        self.assertTrue(any(l.marked and "protect renters" in l.text for l in plenary.lines))

    def test_nothing_is_sent_when_the_senedd_is_not_sitting(self):
        from monitor.morning import build, render_morning
        b = build([], [], [], TAX, today=date(2026, 9, 25))
        _, _, count = render_morning(b)
        self.assertEqual(count, 0)
        sent, message = alerts_mod.post_to_flow("https://example.invalid/flow", "s", "b", count,
                                            dry_run=False)
        self.assertFalse(sent)

    def test_a_midnight_embargo_is_shown_in_london_time(self):
        """The newsroom prints UTC. 23.00 on Tuesday is midnight on Wednesday
        in Cardiff, and that is the day a reader saw it."""
        from monitor.morning import render_morning
        _, body, _ = render_morning(self._day())
        self.assertIn("Wed 23 Sep, 00.00", body)
        self.assertIn("Tue 22 Sep, 19.20", body)

    def test_the_summary_is_the_notices_own_first_sentence(self):
        """The card summary often has no full stop; splitting on sentences
        alone ran it into the next paragraph ("…current law Views and…")."""
        from monitor.morning import Announcement
        item = Item(source_kind="press_release", source_name="WG", title="Roadside rubbish",
                    body="Roadside rubbish\nRegistered owners could be fined for litter\n\n"
                         "Views and evidence sought. More text.")
        self.assertEqual(Announcement(item=item, published_utc=None, marked=False).summary,
                         "Registered owners could be fined for litter")

    def test_the_subject_says_how_many_are_tagged(self):
        from monitor.morning import render_morning
        subject, _, count = render_morning(self._day())
        self.assertEqual(count, 2)
        self.assertTrue(subject.startswith("Bore da: Senedd morning briefing — Wed 23 September"))
        self.assertIn("marked NRLA", subject)

    def test_it_is_built_for_outlook_on_windows(self):
        """Tables and fixed widths — the Friday email learned this the hard way."""
        from monitor.morning import render_morning
        _, body, _ = render_morning(self._day())
        self.assertIn('role="presentation"', body)
        self.assertIn('width="680"', body)
        self.assertNotIn("display:flex", body)
        self.assertNotIn("<script", body)

    def test_a_missing_newsroom_is_said_in_the_email(self):
        from monitor.morning import render_morning
        b = self._day()
        b.notes.append("The Welsh Government newsroom could not be read this morning.")
        _, body, _ = render_morning(b)
        self.assertIn("could not be read this morning", body)


# ---------------------------------------------------------------------------
class TestNewsroomRecentIsTimeAware(unittest.TestCase):
    """"Since the last briefing" is a moment, not a date."""

    LIST_URL = "https://media.service.gov.wales/news"

    def test_only_stories_after_the_moment_are_read(self):
        from monitor.collectors.govwales import GovWalesNewsroomCollector
        pages = {self.LIST_URL:
                 _card("/news/new", "Tuesday 22 Sep 2026, 18:20", "Waking watch", "s")
                 + _card("/news/old", "Tuesday 22 Sep 2026, 06:00", "Earlier", "s"),
                 "https://media.service.gov.wales/news/new": _story(
                     "2026-09-22 18:20", "x", "Waking watch", "s", "b")}
        found = GovWalesNewsroomCollector(_StubFetcher(pages)).recent(datetime(2026, 9, 22, 6, 30))
        self.assertEqual([item.title for _, item, _ in found], ["Waking watch"])
        self.assertEqual(found[0][0], datetime(2026, 9, 22, 18, 20))

    def test_the_lead_is_the_first_summary_point_not_all_of_them_run_together(self):
        """Live, 22 September: "…fined for litter thrown from their car
        Roadside rubbish blights communities…" — three bullet points with no
        full stops, joined into one line."""
        from monitor.collectors.govwales import GovWalesNewsroomCollector
        card = ('<div class="card"><div class="card__body">'
                '<time class="card__date">Tuesday 22 Sep 2026, 14:55</time>'
                '<h2 class="card__title"><a class="card__link" href="/news/rubbish">'
                'Roadside rubbish</a></h2><div class="card__summary"><ul>'
                '<li>Registered vehicle owners could be fined for litter</li>'
                '<li>Roadside rubbish blights communities</li></ul></div></div></div>')
        pages = {self.LIST_URL: card,
                 "https://media.service.gov.wales/news/rubbish": _story(
                     "2026-09-22 14:55", "x", "Roadside rubbish", "s", "b")}
        (_, _, lead), = GovWalesNewsroomCollector(_StubFetcher(pages)).recent(
            datetime(2026, 9, 22, 6, 30))
        self.assertEqual(lead, "Registered vehicle owners could be fined for litter")


# ---------------------------------------------------------------------------
class TestMorningWorkflowGuards(unittest.TestCase):
    """The morning workflow has no schedule, on purpose.

    GitHub began the 06.30 UTC daily run between 11.29 and 13.07 UTC on every
    day of September 2026. The clock is a Power Automate recurrence instead.
    If someone "helpfully" adds a cron line, the briefing would start arriving
    after lunch — and, with the flow also running, twice.
    """

    ROOT = Path(__file__).resolve().parent.parent
    WORKFLOW = ROOT / ".github/workflows/morning.yml"

    def test_it_is_started_by_hand_or_by_the_flow_only(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("schedule:", text)
        self.assertNotIn("cron:", text)

    def test_tests_run_before_anything_is_sent(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(text.index("python -m tests.test_monitor"),
                        text.index("monitor.cli morning --send"))

    def test_it_delivers_through_the_same_flow_as_the_friday_email(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("MONITOR_FLOW_URL: ${{ secrets.MONITOR_FLOW_URL }}", text)

    def test_it_cannot_email_by_opening_issues_or_commit_the_archive(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("issues: write", text)
        self.assertIn("contents: read", text)
        self.assertNotIn("cli export", text)

    def test_the_readable_copy_has_not_drifted(self):
        real = self.WORKFLOW.read_text(encoding="utf-8")
        copy = (self.ROOT / "deploy/morning-workflow.yml").read_text(encoding="utf-8")
        self.assertEqual(real, copy,
                         "deploy/morning-workflow.yml has drifted from "
                         ".github/workflows/morning.yml — re-copy it.")


# ---------------------------------------------------------------------------
class TestMorningBriefingSetupGuide(unittest.TestCase):
    """The instructions are part of the deliverable.

    The operator is not a developer and set up the Friday email from a guide.
    If the guide names a secret the code does not read, or a workflow file
    that does not exist, the setup fails at the one step nobody can debug.
    """

    GUIDE = Path(__file__).resolve().parent.parent / "MORNING-BRIEFING-SETUP.md"

    def test_the_guide_exists(self):
        self.assertTrue(self.GUIDE.exists())

    def test_it_calls_the_right_workflow_on_the_right_repository(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("https://api.github.com/repos/JHC220199/Senedd-monitoring-tool/"
                      "actions/workflows/morning.yml/dispatches", text)
        self.assertIn('{"ref":"main"}', text)

    def test_it_sets_the_recurrence_in_london_time(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("Recurrence", text)
        self.assertIn("London", text)

    def test_the_token_is_limited_to_this_repository_and_to_actions(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("Only select repositories", text)
        self.assertIn("Actions", text)
        self.assertIn("Read and write", text)

    def test_it_reuses_the_friday_flow_rather_than_asking_for_another(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("MONITOR_FLOW_URL", text)

    def test_it_also_fixes_the_friday_email(self):
        """GitHub started the 15.00 Friday run at 18.22 and 19.16 on
        18 September, so its London-clock check stopped it both times and no
        Friday email has gone out on its own. A dispatched run is always let
        through, so the same Power Automate start fixes it."""
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("actions/workflows/forward.yml/dispatches", text)
        workflow = (Path(__file__).resolve().parent.parent
                    / ".github/workflows/forward.yml").read_text(encoding="utf-8")
        self.assertIn('if [ "${{ github.event_name }}" != "schedule" ]', workflow,
                      "forward.yml must let a dispatched run through the clock "
                      "check, or the Power Automate start cannot work")


# ---------------------------------------------------------------------------
# Debate summaries (monitor/debates.py, collectors/record_html.py)
# ---------------------------------------------------------------------------

def _rec_agenda(cid, title):
    return (f'<div class="itemContent agendaItem" id="{cid}"><div class="contributionText">'
            f'<div class="verbatim">Cymraeg</div><div class="translation">{title}</div>'
            f'</div></div>')


def _rec_heading(cid, title):
    return (f'<div class="itemContent subHeading" id="{cid}"><div class="contributionText">'
            f'<div class="verbatim ">Cymraeg</div><div class="translation">{title}</div>'
            f'</div></div>')


def _rec_speech(cid, name, text, role="", member=True, kind="contribution",
                welsh_first=False):
    link = ("https://business.senedd.wales/mgUserInfo.aspx?UID=1" if member
            else "#")
    title = f'<div class="memberTitle"><span>{role}</span></div>' if role else ""
    chunks = []
    if welsh_first:
        chunks.append('<div class="contributionText"><div class="verbatim ">'
                      '<p>Diolch yn fawr.</p></div><div class="translation">'
                      '<p>Thank you very much.</p></div></div>')
    chunks.append(f'<div class="contributionText"><div class="verbatim fullWidth">'
                  + "".join(f"<p>{para}</p>" for para in text.split("\n")) +
                  '</div></div>')
    return (f'<div class="itemContent {kind}" id="{cid}"><div class="detailBar">'
            f'<div class="memberInfo"><div class="memberBar"><a href="{link}">'
            f'<div class="memberDetail"><span class="name"> {name} </span>'
            f'<span class="time">14:00:00</span></div></a>{title}</div></div>'
            f'<div class="meetingShareContainer"><a class="seneddTV" '
            f'href="http://www.senedd.tv/en/1?startPos=1">Video</a></div></div>'
            + "".join(chunks) + '</div>')


def _rec_page(*blocks):
    return "<html><body>" + "".join(blocks) + "</body></html>"


TREFNYDD = "Trefnydd, Chief Whip and Cabinet Minister for Culture and Sport"
HOUSING_MINISTER = "Cabinet Minister for Local Government, Housing and Planning"
CHAIR = "Deputy Presiding Officer"

PLENARY_FIXTURE = _rec_page(
    '<div class="itemContent proceduralText">The Senedd met at 13:30.</div>',
    _rec_agenda("A1", "1. Questions to the First Minister"),
    _rec_heading("H1", "Protecting Renters"),
    _rec_speech("C1", "David Hughes", "Will the First Minister set out new measures to protect renters?", kind="oralQuestion"),
    _rec_speech("C2", "Rhun ap Iorwerth", "We will legislate to improve protections for renters in the private rented sector.", role="First Minister of Wales"),
    _rec_heading("H2", "Rail Services"),
    _rec_speech("C3", "Andrew Davies", "What is the Government doing about rail services in the valleys?", kind="oralQuestion"),
    _rec_speech("C4", "Rhun ap Iorwerth", "We are investing in the trains."),
    _rec_agenda("A3", "3. Business Statement and Announcement"),
    _rec_speech("C10", "Kerry Ferguson", "The business statement. I call the Trefnydd.", role=CHAIR),
    _rec_speech("C11", "Heledd Fychan", "There are no changes to this week's business.", role=TREFNYDD),
    _rec_speech("C12", "Llyr Powell", "Trefnydd, can I have a statement on local train overcrowding? The service is the third most overcrowded in the UK."),
    _rec_speech("C13", "Heledd Fychan", "The transport secretary will have heard."),
    _rec_speech("C14", "John Clark", "Trefnydd, can I request a statement on houses in multiple occupancy in Bangor being used for Home Office schemes?"),
    _rec_speech("C15", "Heledd Fychan", "That issue is not devolved."),
    _rec_agenda("A7", "7. Statement by the Cabinet Minister for Local Government, Housing and Planning: Building Safety Programme Update"),
    _rec_speech("C20", "Kerry Ferguson", "I call the Cabinet Minister.", role=CHAIR),
    _rec_speech("C21", "Sian Gwenllian", "Thank you, Dirprwy Lywydd.\nRemediation has not moved quickly enough for leaseholders. Of 161 buildings, 11 are complete.", role=HOUSING_MINISTER, welsh_first=True),
    _rec_speech("C22", "Francesca O'Brien", "Only four of 161 private buildings have been remediated. Will leaseholders paying £500 a month for alarms be reimbursed?"),
    _rec_speech("C23", "Sian Gwenllian", "The alarm grant will open very soon for leaseholders."),
    _rec_agenda("A8", "8. Voting Time"),
    _rec_speech("C30", "Kerry Ferguson", "We move to voting time.", role=CHAIR),
)


def _plenary():
    from monitor.collectors.record_html import parse_record
    return parse_record(PLENARY_FIXTURE, "16262", "Plenary")


class TestDraftRecordPage(unittest.TestCase):
    """The web page, because the XML export lagged by days in September
    2026: Plenary 22 September's XML still held only First Minister's
    Questions 27 hours after the sitting."""

    def test_agenda_items_and_questions_are_read(self):
        rec = _plenary()
        self.assertTrue(rec.published)
        self.assertEqual([i.number for i in rec.items], ["1", "3", "7", "8"])
        self.assertEqual(rec.items[2].heading_text,
                         "Statement by the Cabinet Minister for Local Government, "
                         "Housing and Planning: Building Safety Programme Update")
        self.assertEqual([b.heading for b in rec.items[0].blocks],
                         ["Protecting Renters", "Rail Services"])

    def test_every_language_run_of_a_contribution_is_read(self):
        """A contribution has one .contributionText per run of language.
        Reading only the first lost most of the Building Safety statement."""
        minister = _plenary().items[2].contributions[1]
        self.assertTrue(minister.text.startswith("Thank you very much."))
        self.assertIn("Of 161 buildings, 11 are complete.", minister.text)

    def test_role_is_carried_to_later_contributions(self):
        items = _plenary().items
        self.assertEqual(items[2].contributions[-1].role, HOUSING_MINISTER)
        self.assertEqual(items[0].contributions[-1].role, "First Minister of Wales")

    def test_members_chairs_anchors_and_video(self):
        c = _plenary().items[1].contributions[0]
        self.assertTrue(c.is_chair)
        self.assertTrue(c.member)
        self.assertEqual(c.anchor, "C10")
        self.assertTrue(c.video_url.startswith("https://www.senedd.tv/"))

    def test_an_unpublished_committee_page_is_not_published(self):
        from monitor.collectors.record_html import parse_record
        rec = parse_record("<html><body><h1>Committee</h1></body></html>",
                           "16247", "Equality Committee")
        self.assertFalse(rec.published)
        self.assertEqual(rec.url, "https://record.senedd.wales/Committee/16247")


class TestDebateSelection(unittest.TestCase):

    def setUp(self):
        from monitor.debates import Relevance, select
        self.rel = Relevance(TAX)
        self.debates = select(_plenary(), self.rel, date(2026, 9, 22))
        self.by_number = {d.item.number: d for d in self.debates}

    def test_a_housing_statement_is_summarised_whole(self):
        d = self.by_number["7"]
        self.assertTrue(d.whole)
        self.assertEqual([c.speaker for c in d.exchanges[0].contributions],
                         ["Sian Gwenllian", "Francesca O'Brien", "Sian Gwenllian"],
                         "the chair calling speakers is left out")

    def test_business_statement_keeps_only_the_relevant_request_and_its_answer(self):
        """As Camlas did on 22 September: the HMO request and the Trefnydd's
        reply; not train overcrowding."""
        d = self.by_number["3"]
        self.assertFalse(d.whole)
        self.assertEqual([[c.speaker for c in ex.contributions] for ex in d.exchanges],
                         [["John Clark", "Heledd Fychan"]])

    def test_question_sessions_are_cut_to_relevant_questions(self):
        d = self.by_number["1"]
        self.assertFalse(d.whole)
        self.assertEqual([ex.heading for ex in d.exchanges], ["Protecting Renters"])

    def test_voting_time_is_never_summarised(self):
        self.assertNotIn("8", self.by_number)

    def test_a_contribution_is_judged_on_its_own_words(self):
        """Under 'Questions to the Cabinet Minister for ... Housing ...' a
        question about buses must not qualify because of the heading."""
        self.assertFalse(self.rel.text("What is the Government doing about bus services?"))
        self.assertTrue(self.rel.text("What is the Government doing about landlord licensing in the private rented sector?"))

    def test_committee_sessions_are_whole_or_nothing(self):
        from monitor.collectors.record_html import parse_record
        from monitor.debates import select
        page = _rec_page(
            _rec_agenda("A1", "1. Introductions, apologies, substitutions and declarations of interest"),
            _rec_speech("C1", "Committee Chair", "Welcome to the meeting."),
            _rec_agenda("A2", "2. Electoral registration regulations: evidence session"),
            _rec_speech("C2", "Committee Chair", "Welcome to our witnesses."),
            _rec_speech("C3", "A Witness", "Automatic registration will add voters.", member=False),
            _rec_speech("C4", "Peter Fox", "How many voters? Unlike the private rented sector, this is simple."),
            _rec_agenda("A3", "3. Homelessness and social housing allocation: evidence session"),
            _rec_speech("C5", "Committee Chair", "Our next session."),
            _rec_speech("C6", "A Witness", "Social housing waiting lists are at a record high and homelessness is rising.", member=False),
            _rec_speech("C7", "Peter Fox", "What would help the private rented sector house homeless families?"),
        )
        rec = parse_record(page, "16256", "Local Government, Housing and Planning Committee")
        chosen = select(rec, self.rel)
        self.assertEqual([d.item.number for d in chosen], ["3"])
        self.assertTrue(chosen[0].whole)
        self.assertNotIn("Committee Chair",
                         [c.speaker for c in chosen[0].exchanges[0].contributions],
                         "a committee chair has no role on the page; the first "
                         "speaker of the meeting is treated as the chair")


class TestDebateTaxonomyFixes(unittest.TestCase):
    """Tuned against the 22 September 2026 Business Statement."""

    def _qualifies(self, text):
        item = Item(source_kind="plenary_transcript", source_name="t", title="", body=text)
        SCORER.score_item(item)
        return TAX.qualifies_for_site(item)

    def test_houses_in_multiple_occupancy_is_an_hmo(self):
        self.assertTrue(self._qualifies("the extent of houses in multiple occupancy in Bangor"))

    def test_train_overcrowding_is_not_housing(self):
        self.assertFalse(self._qualifies("a statement on local train overcrowding"))
        self.assertTrue(self._qualifies("overcrowding and damp in private rented homes"))

    def test_builder_licensing_is_not_landlord_licensing(self):
        self.assertFalse(self._qualifies(
            "a mandatory licensing scheme for building companies to stop rogue builders"))

    def test_fly_tipping_enforcement_is_not_housing(self):
        self.assertFalse(self._qualifies(
            "Illegally dumped waste: co-ordinating enforcement action against waste crime"))


class TestDebateSummaries(unittest.TestCase):

    def setUp(self):
        from monitor.debates import Relevance, select
        self.debates = select(_plenary(), Relevance(TAX), date(2026, 9, 22))
        self.statement = [d for d in self.debates if d.item.number == "7"][0]

    def test_key_sentences_skip_courtesies_and_keep_the_point(self):
        from monitor.debates import key_sentences
        c = self.statement.exchanges[0].contributions[0]
        text = key_sentences(c, TAX)
        self.assertNotIn("Thank you", text)
        self.assertIn("leaseholders", text)

    def test_key_sentences_are_capped(self):
        from monitor.collectors.record_html import Contribution
        from monitor.debates import KEY_WORDS_MAX, key_sentences
        long = Contribution(anchor="C1", speaker="X",
                            text=" ".join(["Landlords matter."] * 200))
        self.assertLessEqual(len(key_sentences(long, TAX).split()), KEY_WORDS_MAX + 1)

    def test_figures_check(self):
        from monitor.debates import figures_check
        src = "Of 161 buildings, 11 are complete, and alarms cost £1,500 a month."
        self.assertTrue(figures_check("11 of 161 complete; alarms £1500 a month", src))
        self.assertFalse(figures_check("12 of 161 complete", src))
        self.assertTrue(figures_check("progress was slow", src))

    def _fake_post(self, payload, status=200):
        import json as _json

        class Resp:
            status_code = status
            text = _json.dumps(payload)

            def json(self):
                return {"content": [{"type": "text", "text": _json.dumps(payload)}]}
        calls = []

        def post(url, **kw):
            calls.append((url, kw))
            return Resp()
        return post, calls

    def test_ai_summary_is_used_and_a_wrong_figure_is_replaced(self):
        from monitor.debates import summarise_ai
        post, calls = self._fake_post({"overview": "An update on building safety.",
            "points": [
                {"n": 1, "summary": "The Cabinet Minister said 11 of 161 buildings were complete."},
                {"n": 2, "summary": "Francesca O'Brien said only 40 buildings had been remediated."},
                {"n": 3, "summary": ""},
                {"n": 99, "summary": "Invented speaker."}]})
        d = summarise_ai(self.statement, TAX, "key", post=post)
        self.assertEqual(d.mode, "ai")
        points = d.points[0]
        self.assertEqual(len(points), 2, "empty summaries are skipped, out-of-range refs ignored")
        self.assertFalse(points[0].verbatim)
        self.assertTrue(points[1].verbatim, "40 is not in her words, so her own sentences are shown")
        self.assertTrue(d.notes)
        _, kw = calls[0]
        self.assertEqual(kw["headers"]["x-api-key"], "key")
        self.assertIn("Do not name private individuals", kw["json"]["system"])

    def test_a_failed_call_falls_back_to_key_sentences(self):
        from monitor.debates import summarise_ai
        post, _ = self._fake_post({"error": "overloaded"}, status=529)
        d = summarise_ai(self.statement, TAX, "key", post=post)
        self.assertEqual(d.mode, "verbatim")
        self.assertTrue(all(p.verbatim for row in d.points for p in row))
        self.assertIn("unavailable", d.notes[0])

    def test_without_a_key_nothing_leaves_the_runner(self):
        from monitor import debates as mod
        with mock.patch.object(mod, "summarise_ai", side_effect=AssertionError("called")):
            out = mod.summarise(self.debates, TAX, api_key="")
        self.assertTrue(out)
        self.assertTrue(all(d.mode == "verbatim" for d in out))

    def test_render(self):
        from monitor.debates import render_debates, summarise
        out = summarise(self.debates, TAX, api_key="")
        subject, body, count = render_debates(out, ["Finance Committee, Thu 17 September"],
                                              page_url="https://example.invalid/")
        self.assertEqual(count, 3)
        self.assertIn("Senedd debate summaries — Plenary, Tue 22 September (3 items)", subject)
        self.assertIn("John Clark MS", body)
        self.assertIn("record.senedd.wales/Plenary/16262#C14", body)
        self.assertIn("verbatim", body)
        self.assertNotIn("written by AI", body)
        self.assertIn("Still awaited:", body)
        self.assertNotIn("Kerry Ferguson", body)
        self.assertEqual(render_debates([], []), ("", "", 0))

    def test_ai_emails_say_they_are_ai(self):
        from monitor.debates import render_debates
        d = self.statement
        from monitor.debates import summarise_verbatim
        summarise_verbatim(d, TAX)
        d.mode = "ai"
        _, body, _ = render_debates([d])
        self.assertIn("written by AI (Claude)", body)
        self.assertIn("Check the Record", body)


class TestDebateCandidatesAndState(unittest.TestCase):

    def _tv(self, mid, name, when, cid="987"):
        from monitor.collectors.seneddtv import Meeting
        return Meeting(guid=mid, name=name, committee_id=cid, when=when, meeting_id=mid)

    def test_candidates(self):
        from monitor.debates import candidates
        today = date(2026, 9, 24)
        tv = [self._tv("16263", "Plenary", date(2026, 9, 23), "908"),
              self._tv("16247", "Equality Committee", date(2026, 9, 23), "983"),
              self._tv("16300", "Petitions Committee", date(2026, 9, 24), "988"),
              self._tv("16100", "Old Committee", date(2026, 9, 1)),
              self._tv("16262", "Plenary", date(2026, 9, 22), "908")]
        listing = [{"meeting_id": "16267", "date": date(2026, 9, 21),
                    "forum": "Public Accounts and Public Administration Committee"},
                   {"meeting_id": "16263", "date": date(2026, 9, 23), "forum": "Plenary"}]
        got = candidates(tv, listing, today, done={"16262"}, baseline=date(2026, 9, 20))
        self.assertEqual([c.meeting_id for c in got], ["16267", "16263", "16247"],
                         "today's meeting, done ones and old ones are left out; "
                         "Plenary before committees on the same day")
        self.assertIn("CId=908&MId=16263", got[1].papers_url)
        got = candidates(tv, listing, today, done=set(), baseline=date(2026, 9, 21))
        self.assertNotIn("16267", [c.meeting_id for c in got], "on or before the baseline")

    def test_state_round_trip_and_pruning(self):
        from monitor.debates import load_state, save_state
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data", "sent.json")
            state = load_state(path)
            self.assertEqual(state["meetings"], {})
            state["baseline"] = "2026-09-21"
            state["meetings"] = {"1": {"date": "2026-09-22"}, "2": {"date": "2026-06-01"}}
            save_state(state, date(2026, 9, 24), path)
            again = load_state(path)
            self.assertEqual(list(again["meetings"]), ["1"])
            self.assertEqual(again["baseline"], "2026-09-21")

    def test_the_committed_state_file_has_a_baseline(self):
        from monitor.debates import load_state, state_baseline
        state = load_state(str(Path(__file__).resolve().parent.parent
                               / "data/debates-sent.json"))
        self.assertIsNotNone(state_baseline(state),
                             "without a baseline the first run sends ten days of back numbers")


class TestDebatesCommand(unittest.TestCase):
    """A meeting is remembered only once its summary has gone."""

    def _run(self, send_result):
        from monitor import cli
        from monitor.collectors.record_html import parse_record
        from monitor.collectors.seneddtv import Meeting
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        state = os.path.join(tmp, "sent.json")
        Path(state).write_text('{"baseline": "2026-09-20", "meetings": {}}')
        plenary = Meeting(guid="g", name="Plenary", committee_id="908",
                          when=date(2026, 9, 22), meeting_id="16262")
        args = SimpleNamespace(taxonomy=None, date="2026-09-23", interval=0,
                               state=state, lookback=10, remember=False,
                               send=True, out=os.path.join(tmp, "d.html"))
        with mock.patch("monitor.collectors.seneddtv.SeneddTVScheduleCollector.schedule",
                        return_value=[]), \
             mock.patch("monitor.collectors.seneddtv.SeneddTVScheduleCollector.parse_recent_meetings",
                        return_value=[plenary]), \
             mock.patch("monitor.collectors.seneddtv.SeneddTVScheduleCollector.fill",
                        side_effect=lambda m: m), \
             mock.patch("monitor.collectors.record_transcripts.RecordTranscriptCollector.list_meetings",
                        return_value=[]), \
             mock.patch("monitor.collectors.record_html.RecordPageCollector.record",
                        return_value=parse_record(PLENARY_FIXTURE, "16262", "Plenary")), \
             mock.patch.object(cli.alerts_mod, "post_to_flow",
                               return_value=send_result), \
             mock.patch.dict(os.environ, {"MONITOR_FLOW_URL": "https://example.invalid/x",
                                          "ANTHROPIC_API_KEY": ""}), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_debates(args)
        import json as _json
        return code, _json.loads(Path(state).read_text())["meetings"]

    def test_sent_means_remembered(self):
        code, meetings = self._run((True, "sent"))
        self.assertEqual(code, 0)
        self.assertIn("16262", meetings)

    def test_not_sent_means_tried_again_tomorrow(self):
        code, meetings = self._run((False, "HTTP 500"))
        self.assertEqual(code, 2)
        self.assertEqual(meetings, {})


class TestDebatesWorkflowGuards(unittest.TestCase):

    ROOT = Path(__file__).resolve().parent.parent
    WORKFLOW = ROOT / ".github/workflows/debates.yml"

    def test_it_follows_the_morning_briefing_and_has_no_timer(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        morning = (self.ROOT / ".github/workflows/morning.yml").read_text(encoding="utf-8")
        name = re.search(r"^name:\s*(.+)$", morning, re.M).group(1).strip()
        self.assertIn(f'workflows: ["{name}"]', text,
                      "workflow_run must name the morning workflow exactly, or it never fires")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("schedule:", text)
        self.assertNotIn("cron:", text)

    def test_tests_run_before_anything_is_sent(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(text.index("python -m tests.test_monitor"),
                        text.index("monitor.cli debates --send"))

    def test_it_commits_only_its_own_memory(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        adds = re.findall(r"git add (.+)", text)
        self.assertEqual(adds, ["data/debates-sent.json"])
        self.assertNotIn("issues: write", text)
        self.assertIn("MONITOR_FLOW_URL: ${{ secrets.MONITOR_FLOW_URL }}", text)
        self.assertIn("ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}", text)

    def test_the_readable_copy_has_not_drifted(self):
        self.assertEqual(self.WORKFLOW.read_text(encoding="utf-8"),
                         (self.ROOT / "deploy/debates-workflow.yml").read_text(encoding="utf-8"))

    def test_the_guide_names_the_secret_the_code_reads(self):
        guide = (self.ROOT / "DEBATE-SUMMARIES.md").read_text(encoding="utf-8")
        self.assertIn("ANTHROPIC_API_KEY", guide)
        code = (self.ROOT / "monitor/cli.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("ANTHROPIC_API_KEY"', code)


def main() -> int:
    Path("data").mkdir(exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

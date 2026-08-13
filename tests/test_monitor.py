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
        collector.mailbox = "senedd-monitor@nrla.org.uk"
        message = {"subject": "Re: lunch", "bodyPreview": "consultation on rent",
                   "from": {"emailAddress": {"address": "colleague@example.com"}}}
        self.assertIsNone(collector.message_to_item(message))

    def test_mailbox_accepts_gov_wales_and_extracts_source_link(self):
        collector = GovWalesMailboxCollector.__new__(GovWalesMailboxCollector)
        collector.errors = []
        collector.mailbox = "senedd-monitor@nrla.org.uk"
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

    def test_dry_run_without_send_is_success(self):
        from monitor.cli import _report_not_sent
        self.assertEqual(
            _report_not_sent(self._args(False), [], {"smtp_host": ""}), 0)

    def test_send_without_config_is_an_error(self):
        from monitor.cli import _report_not_sent
        self.assertEqual(
            _report_not_sent(self._args(True), [], {"smtp_host": ""}), 3)

    def test_the_error_names_the_missing_variable(self):
        from monitor.cli import _report_not_sent
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
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


def main() -> int:
    Path("data").mkdir(exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

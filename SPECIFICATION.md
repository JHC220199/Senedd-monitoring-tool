# Senedd policy monitor — system specification

**Prepared for:** NRLA Policy Directorate
**Date:** 4th August 2026
**Status:** Design specification with working prototype
**Revision:** 4 — committee consultations added; dashboard rebuilt around actions
**Author:** Drafted with Claude for Joshua Helm-Cowley

---

## 1. What this is, in one page

We currently buy Senedd monitoring from Camlas as a weekly Word briefing, debate
notes, forward looks and ad-hoc consultation emails. This specification sets out
a system that replaces it, and a working prototype that already does most of the
job.

**The recommendation.** Build it. The Senedd publishes its full official Record
as XML under the Open Government Licence, which means the expensive part of
political monitoring — getting reliable, complete, timely text of everything said
and tabled — is free and machine-readable. The prototype consumes it today.

**What changes for the team.** Three things, and none of them is "you get the
same briefing, cheaper":

- **From weekly to same-day.** A written question tabled on Thursday reaches you
  on Thursday, not in the following week's document.
- **From a document to an archive.** Every contribution since the system started
  running becomes searchable in one place. "Everything anyone has said in the
  Senedd about rent controls" is a search box, not a request to a supplier.
- **From a narrative to a queue.** The system's job is not to tell you what
  happened. It is to tell you what you need to *do*, ordered by how urgent it is,
  with the closing dates at the top.

**What was verified, not assumed.** The prototype ran against live Senedd data on
4th August 2026: 1,473 items collected in about two minutes, scored, and reduced
to 488 in the archive and 440 on the dashboard — drawn from Plenary and committee
transcripts, written and oral questions, Acts and Welsh statutory instruments,
28 Senedd Bills and Acts with their full stage histories, and 36 forward-look
sittings. It
was then benchmarked against the Camlas briefing of 17th July 2026. It found
**13 of the 13 substantive items** that briefing reported.

It also found something the briefing did not. Written question **WQ99808**, tabled
by Dan Thomas MS on 16th July 2026 — *"Does the Welsh Government have plans to
bring in rent controls?"* — does not appear anywhere in the W29 briefing, which
covered that week and did list three other Dan Thomas written questions. On the
single most consequential issue facing our members, a question was tabled and we
were not told. That is the argument for this system in one line.

**What it will not do.** It will not write your briefing, form your view, or
decide what NRLA's position should be. It surfaces verbatim published text with a
link and a priority. Every judgement stays with the policy team, and that is
deliberate — see section 11.

---

## 2. What we buy today, and what we actually need

### The Camlas service, as evidenced by the W29 briefing

| Component | What it is | Replaced by |
|---|---|---|
| Weekly briefing (Word) | Headlines, then sections: Big Picture, Housing, Welfare, Planning, Economy, Local Government, Legislation Watch | Dashboard + digest email, generated on demand |
| Debate notes | Long verbatim extracts from Plenary and committees, hand-selected | Full Record ingested and scored; verbatim text with timestamped video links |
| Forward look | Table of scheduled Plenary debates, oral question rota, open consultations | Live forward look and a "closing soon" panel |
| Consultation emails | Ad-hoc notifications | Consultation tracking with parsed closing dates and countdowns |
| *Yr Wythnos* video roundup | A weekly video summary | **Not replaced.** See section 11. |
| Judgement and advice | A consultant's read on what matters politically | **Not replaced.** See section 11. |

### Where the current service is structurally weak

These are not criticisms of Camlas, who are producing a good weekly document.
They are the limits of any weekly-document format.

1. **Latency is up to seven days.** The briefing arrives on a Friday covering the
   week. A consultation that opens on Monday with a short window loses a week.
2. **Nothing is searchable.** A folder of Word documents cannot answer "when did
   the minister last commit to anything on rent data?"
3. **Coverage depends on a human reading everything.** WQ99808 shows what happens
   when it slips. The Senedd published 1,409 contributions in the sample window;
   no one reads all of that reliably, every week, forever.
4. **No deadline tracking.** Closing dates appear in a table but nothing counts
   down, and nothing chases.
5. **No audit trail.** If someone asks in 2028 why we did not respond to a 2026
   consultation, there is no record of what we were told and when.

### What the policy directorate actually needs

Ranked, because the design follows this order:

1. **Never miss a deadline.** Consultations, calls for evidence, committee
   evidence windows. Missing one is unrecoverable.
2. **Know within hours when something material happens** on rent controls,
   evictions, Rent Smart Wales, or when the NRLA is named.
3. **Be able to search everything ever said**, to brief quickly and to show a
   minister their own words.
4. **See what is coming**, far enough ahead to influence it.
5. **Have a shared view**, so the directorate is not dependent on one person's
   inbox.

---

## 3. What the system does

### 3.1 Collect

Seven sources, normalised into one item shape. Section 4 has the verified detail.
A live run on 4th August 2026 collected 1,493 items across six of the seven; the
seventh (gov.wales RSS) is unreachable from cloud hosts by design and its content
arrives via the mailbox route instead.

### 3.2 Score

Every item is scored on four factors, then banded. This is the part that decides
whether the tool gets used or muted, so it is described fully in section 5.

### 3.3 Route

| Band | Score | Channel | What it means |
|---|---|---|---|
| **Critical** | 130+ | Immediate email + dashboard | Material, usually time-limited. Look today. |
| **High** | 85–129 | Digest + dashboard | Substantive. Probably needs a position. |
| **Medium** | 50–84 | Digest + dashboard | Worth knowing, grouped and summarised. |
| **Low** | 25–49 | Dashboard only | Peripheral. Available if you want the full picture. |
| **Noise** | 0–24 | Archive only | Kept so the archive stays searchable and tuning is auditable. |

A direct NRLA mention always escalates to Critical and immediate, whatever else
matched.

### 3.3a The most valuable source: committee consultations and inquiries

Revisions 1–3 monitored what the Senedd had **already said**. They did not
monitor what its committees were **asking to be told**. The second is more
valuable, because it is a dated, open invitation to put NRLA's position on the
record — and it was missing entirely.

Two live items the system failed to surface, both from the Local Government,
Housing and Planning Committee:

- **Priorities for the Local Government, Housing and Planning Committee**
  (consultation 626, **closing 14 September 2026**) — the Committee asking
  organisations to name the top three issues it should prioritise for the whole
  Seventh Senedd. For a landlord body this is close to the highest-value single
  submission available in the term.
- **Follow-up inquiry into Empty Properties** (issue 47957) — revisiting the 2019
  ELGC report, covering residential empty properties, which runs directly into
  council tax premiums and enforcement powers.

Missing a debate means reading it late. Missing these means a four-year
opportunity passes. They live in the Senedd's ModernGov instance, in two places:
the **active consultations RSS** (`mgRss.aspx?f=76`, whose `<description>` carries
a structured `end date` — the reliable source for closing dates), and each
committee's **Current Work** issue links, which is where inquiries with no
consultation record hide.

### 3.4 Present

**The dashboard** is a single self-contained HTML file. No server, no build step,
no login, no npm tree, no framework to keep patched. It can sit in a SharePoint
document library and open in any browser, including from a phone, including
offline once downloaded.

Revision 4 rebuilt it. The first version was a ranked list of 440 items with a
numeric score on each, and the directorate's verdict was that it was "incredibly
difficult to look at and prioritise what needs doing". That was right: it was a
data view pretending to be a work view. Three specific faults, all now fixed:

| Fault | Fix |
|---|---|
| Answered "what is most relevant" rather than "what must I do" | Three zones: **Respond** (dated consultations), **Review** (developments), **Coming up** (sittings). The archive is collapsed. |
| One card per contribution — up to 48 for a single agenda item | Contributions grouped into one card per debate, listing speakers in speaking order |
| Raw scores on every card | Hidden by default behind a toggle |
| Procedural housekeeping scored High ("Introductions, apologies", "Papers to note") | Filtered at source via a policy-editable list |
| 17 cards headed "No closing date published" | Dated items get the section; undated ones become a short watch-list |

The surface now shows **5 consultations and 15 developments** where it previously
showed 440 undifferentiated items. Each Respond card states what it is, why it
matters in one sentence, and a suggested next step. It has:

- Four stat tiles: needs attention now, high priority, closing within seven days,
  items in view.
- A **closing soon** panel at the top, in date order, with days remaining, and
  items closing within seven days in red.
- Free-text search across every field, with match highlighting.
- Filter chips by policy area, a minimum-priority selector, a source selector,
  and sort by priority, date or closing date.
- Per item: priority band, score, speaker, role, forum, date, policy-area tags,
  the verbatim excerpt, a link to the source, a **timestamped Senedd.tv link that
  jumps to the moment the words were spoken**, and an expander for the full text.
- A **run-health banner**. If a source returned nothing, the page says so, in red,
  at the top. This matters more than it sounds: the failure mode that kills trust
  in monitoring is not a crash, it is a quiet week that was actually a broken
  feed.

**Email** in two flavours, both plain HTML that renders correctly in Outlook:

- **Immediate alert** — Critical only, rationed hard. Says what the item is, why
  it escalated, and links to source and video. If the NRLA has been named, the
  subject line says so.
- **Digest** — daily or weekly. Closing-soon table first, then items grouped by
  policy area. A quiet period is stated explicitly as a quiet period, so it is
  never confused with a broken run.

### 3.5 Search the archive

From the dashboard, or from the command line:

```
python -m monitor.cli search "rent control"
```

Full-text search across title, body, speaker and forum, over everything ever
collected.

---

## 4. Data sources — verified, 4th August 2026

Every row was tested. Where something does not work, that is recorded as a
finding rather than smoothed over, because these are the things that will
otherwise surprise whoever operates this.

| Source | Endpoint | Format | Status | Licence |
|---|---|---|---|---|
| **Record of Proceedings — transcripts** | `record.senedd.wales/XMLExport/Download?meetingID=<id>&xmlDownloadType=EnglishTranscript` | XML | **Works.** The core source. | OGL v3 |
| Record — questions not reached | same, `xmlDownloadType=QNR` | XML | **Works.** | OGL v3 |
| Record — votes | same, `xmlDownloadType=Votes` | XML | **Works.** Not yet used. | OGL v3 |
| Meeting index | `record.senedd.wales/XMLExport` | HTML | **Works**, with a caveat — see 4.1 | OGL v3 |
| **Tabled business** (written, oral, topical questions; statements of opinion) | `record.senedd.wales/Search/?query=<q>&type=<n>&start=<iso>&end=<iso>` | HTML | **Works**, with a caveat — see 4.2 | OGL v3 |
| **Acts of Senedd Cymru** | `legislation.gov.uk/asc/data.feed` | Atom | **Works.** Best-behaved source in the system. | OGL v3 |
| **Welsh statutory instruments** | `legislation.gov.uk/wsi/<year>/data.feed` | Atom | **Works.** | OGL v3 |
| **Forward look — scheduled business** | `business.senedd.wales/mgWebService.asmx` → `GetAllMeetingsByDate` | SOAP/XML | **Works.** Returned 96 scheduled meetings for Sept–Oct 2026 — see 4.3 | OGL v3 |
| Committee register | same service → `GetCommittees` | SOAP/XML | **Works.** 209 committees with IDs; fixes the incomplete dropdown | OGL v3 |
| Senedd calendar (legacy) | `business.senedd.wales/calJson.aspx` | JSON | **Broken — do not use.** Ignores its own date parameters — see 4.3 | OGL v3 |
| **Senedd Bills and Acts, with stages** | `senedd.wales/senedd-business/legislation/` → `business.senedd.wales/mgIssueHistoryHome.aspx?IId=<id>` | HTML | **Works.** 28 bills/Acts with full dated stage history — see 4.4 | OGL v3 |
| **Welsh Government announcements, statements, consultations** | `gov.wales/announcements/rss` | RSS | **Blocked from cloud hosts** — see 4.4 | OGL v3 |
| Welsh Government notifications | `gov.wales/subscribe/announcements` → shared mailbox → Microsoft Graph | Email | **Recommended production route** — see 4.5 | OGL v3 |
| **Senedd Research** | `research.senedd.wales/research-articles/` | HTML | **Works.** Impartial analysis of Welsh Government policy; partially closes the gov.wales gap and is a source the current briefing does not carry — see 4.6 | OGL v3 |

The Senedd documents its open data at `senedd.wales/help/open-data/`, publishes it
under the Open Government Licence v3.0, and lists the transcript XML library, the
legislation Crown XML, the ModernGov web service and the calendar JSON. It is a
genuinely good open-data offering; most legislatures do not match it.

### 4.1 Finding: the meeting index date filter is unreliable

The `XMLExport` listing accepts `Start` and `End` in `dd/mm/yyyy`, but only
behaves correctly when a specific committee is also selected:

```
SelectedCommitteeID=908&Start=01/07/2026&End=31/07/2026
    -> correct: Plenary sittings of 15 and 14 July 2026

SelectedCommitteeID=0&Start=15/06/2026&End=04/08/2026
    -> WRONG: returns March 2026 sixth-Senedd meetings

(no parameters)
    -> correct: the 16 most recent meetings, newest first
```

This bit during the build: a run asking for June–August 2026 silently backfilled
the wrong Senedd term. **The collector therefore always filters by date in Python
as well**, and never sends `SelectedCommitteeID=0` with a date range. A daily
incremental run uses the unfiltered listing (one request, always correct); a
historical backfill iterates committees individually.

A second wrinkle: the Senedd's own committee dropdown is incomplete. On 4th August
2026 it did not list the **Local Government, Housing and Planning Committee** —
our single most important committee — even though that committee had published a
transcript for 16th July 2026. The backfill therefore always also reads the
unfiltered listing, so a new committee is never missed because it has not yet
appeared in a dropdown.

### 4.2 Finding: the Record search needs the canonical URL form

The search form advertises title-case parameters and UK dates. Submitting those
returns an empty JavaScript shell. The form actually POSTs and then 302-redirects
to a canonical GET with **lowercase parameters and ISO dates**, which renders
complete results server-side. Only that form works.

`type` values, read from the form's own checkboxes:

```
-1 All          7 Written Question    4 Oral Question    3 Motions/Amendments
 6 Topical Q    1 Speeches            2 Transcripts      8 Statement of Opinion
13 QNR         15 Emergency Question
```

Note also that the Record's own search is a **substring** match: searching "rent"
returns "cur*rent*ly". We use it only as a cheap coarse net and let our own
word-boundary scorer make the relevance decision. Precision comes from our
taxonomy, not from their search box.

### 4.3 Finding, now fixed: use the SOAP service, not the calendar JSON

The forward look was the weakest part of revision 1. `calJson.aspx` returns HTTP
200 and valid JSON but **ignores its documented `fromdate`/`todate` parameters**
— identical output for 13–18 July and 1 September–1 October — and during recess
contained only building exhibitions. It produced no parliamentary business at
all, so Legislation Watch's sibling section was effectively dead.

The working answer was in the same open-data listing, undocumented: the ModernGov
SOAP service.

```
POST https://business.senedd.wales/mgWebService.asmx
SOAPAction: http://moderngov.co.uk/namespaces/GetAllMeetingsByDate
```

Two details make the difference between 0 results and 96, and both had to be
found by probing:

```
lCommitteeId = 0    -> returns NOTHING. Zero does not mean "all".
lCommitteeId = -1   -> returns ALL committees.

dates in dd/mm/yyyy -> correct, honours the range
dates in ISO        -> silently IGNORES the range, returns a 5,000-row cap
```

The ISO failure is the dangerous one: it returns a great deal of plausible data
while quietly answering a different question. That is worse than an error, and it
is the class of bug the test suite now guards explicitly.

**Verified 4 August 2026:** `-1` with `01/09/2026`–`31/10/2026` returned **96
scheduled meetings**, including the Local Government, Housing and Planning
Committee on 17 and 24 September and 1 October 2026. `GetCommittees` additionally
returns the full register of **209 committees** with IDs, which fixes the
incomplete-dropdown problem described in 4.1.

The forward look is no longer the least-proven component. It is live.

### 4.4 Finding, now fixed: the "WAF block" on Senedd hosts was our own fault

Revision 1 reported `senedd.wales` as blocked by the same CloudFront WAF as
gov.wales, and Legislation Watch was permanently empty as a result. That
diagnosis was wrong, and the way it was wrong is worth recording.

There were two independent bugs, and both were ours:

1. **A self-inflicted 403.** Our User-Agent ended with the token
   `python-requests`. That alone triggers CloudFront's managed bot rules.
   Verified against `senedd.cymru/deddfwriaeth/`:

   ```
   "NRLA-PolicyMonitor/1.0 (... +mailto:policy@nrla.org.uk)"  -> 200
   "NRLA-PolicyMonitor/1.0 python-requests"                   -> 403
   "python-requests/2.31.0"                                   -> 403
   ```

2. **A wrong URL.** `senedd.wales/senedd-business/bills-and-laws/` does not
   exist. The correct path is `senedd.wales/senedd-business/legislation/`.

The 403 masked the 404, which is why it took two passes: fixing the User-Agent
turned the error into a 404 and revealed the real problem underneath.

With both fixed, the bill collector reads the index (11 IDs on the English page,
28 across all index pages) and fetches each from
`business.senedd.wales/mgIssueHistoryHome.aspx?IId=<id>`, which returns the
English title plus the full dated stage history. Verified with IId 46141:
*Building Safety (Wales) Act 2026*, Stage 1 through Stage 4, Royal Assent
27 April 2026.

The User-Agent fix is a correction to our own request, not evasion. It stays
honest and contactable — organisation name and a real address — and deliberately
does **not** impersonate a browser. A test now enforces both halves of that: no
library tokens, and no Mozilla/Chrome strings.

**The generalisable lesson, and it is the most useful thing in this document:**
an HTTP 403 from a CDN is not evidence that anyone has decided to block you.
Check your own request first. Two of the three "blocked source" findings in
revision 1 were self-inflicted, and treating them as external constraints would
have meant permanently accepting a degraded system and possibly a needless
change of hosting.

### 4.4a What remains genuinely blocked

`gov.wales` returns 403 from this host **regardless of User-Agent** — tested with
a clean identifying UA, a minimal UA, and on `/announcements/rss`, `/rss.xml` and
`/consultations`. The Welsh-language `llyw.cymru` behaves the same way. This one
is a real IP-based restriction, and the mailbox route below is the answer.

### 4.6 Senedd Research: a substitute worth having in its own right

`research.senedd.wales` is reachable from anywhere and publishes the Senedd's own
impartial analysis of Welsh Government policy, bills and consultations — often
with more useful framing than the Government's own announcement. There is no RSS
feed (`/feed/` and `/rss/` both 404), so the article index is parsed on the
`/research-articles/<slug>/` URL pattern, which is derived from content and
therefore more stable than any CSS class.

Its article on the First Minister's 14th July legislation statement scores 335 —
Critical — and matches all four private-rented-sector themes, because it sets out
the rent-data requirement, the Rent Smart Wales enforcement changes and the
later-term rent and eviction measures in one place. Worth noting: an early cut of
six paragraphs per article scored it only 45 (Low), because Senedd Research
structures pieces by topic and housing appeared several sections in. Fourteen
paragraphs fixed it. Body-length limits on a scraped source are a scoring
decision, not just a storage one.

### 4.5 The mailbox route, and why it is better anyway

The Welsh Government offers an email subscription at
`gov.wales/subscribe/announcements`. Camlas's own consultation alerts are email
notifications. The recommended design subscribes a dedicated NRLA shared mailbox
and reads it via Microsoft Graph.

This is not a workaround. For a policy team it is the better architecture:

- **Sanctioned** — it is the mechanism the publisher provides.
- **Robust** — no WAF, no scraping, no parsing a CMS that can be redesigned.
- **Already licensed** — NRLA runs Microsoft 365; there is nothing to buy.
- **Auditable** — the mailbox becomes a permanent record of what we were notified
  of and when. If anyone ever asks why a deadline was missed, there is an answer.
- **Migration-friendly** — forward the existing Camlas alerts to the same mailbox
  during handover and nothing is lost in the transition.

It was tested end-to-end through the production code path with realistic
notifications, including a deliberately misdirected human email, which was
correctly refused by the sender allow-list rather than entering the archive.

---

## 5. The relevance model

This is where the intellectual work is. Collecting Senedd data is easy. Deciding
which twelve of the week's fourteen hundred contributions a policy officer should
read is the hard part, and getting it wrong in either direction kills the tool —
too much noise and it gets muted, too little and it gets distrusted.

The model is defined entirely in `config/taxonomy.yaml`, a heavily commented file
**designed to be edited by policy staff, not developers**. Change a weight, run
`rescore`, and see immediately what would have been flagged differently across the
whole archive. No deployment, no developer.

```
score = ( sum of matched theme weights
        + sum of matched entity boosts
        + sum of matched signal boosts )  x  source multiplier
```

### 5.1 Themes — what it is about

18 themes in six tiers. Weight reflects consequence for our members, not how
often the topic comes up.

| Weight | Meaning | Examples |
|---|---|---|
| 50 | Existential | Rent controls and rent data; evictions and possession |
| 45 | Direct regulatory | Rent Smart Wales and licensing |
| 40 | Direct impact | The private rented sector itself; tenancy law |
| 35 | Real cost | HMOs and standards; energy efficiency; building safety and leasehold; property taxation; second homes and short-term lets |
| 25 | Strong indirect | Housing supply; planning; homelessness |
| 15–20 | Context | Social housing; welfare; enforcement; budget; housing data |

A theme fires **once** at full weight however many of its terms match, but each
*distinct* theme adds its own weight. So an item about rent controls in the
private rented sector correctly outranks one about rent controls alone.

### 5.2 Entities — who is involved

Entity boosts **amplify** relevance; they never create it. An item matching no
theme scores zero however senior the speaker — otherwise every word the housing
minister said about ambulance waiting times would be flagged as housing business.

| Entity group | Boost | Notes |
|---|---|---|
| NRLA named | 60 | Always escalates to Critical and immediate |
| Housing minister (Siân Gwenllian) | 30 | The decision-maker on the Rental Bill |
| LGHP Committee and members | 30 | Chair: Carmelo Colasanto (Reform UK) |
| Opposition housing leads | 25 | Francesca O'Brien (Reform), Jayne Bryant (Lab), Peter Fox (Con), Jane Dodds (LD), Anthony Slaughter (Green) |
| First Minister and Cabinet | 20 | Rhun ap Iorwerth, Elin Jones |
| Sector bodies | 15 | Propertymark, Shelter Cymru, CHC, TPAS Cymru, Generation Rent |
| Delivery bodies | 15 | WRA, Residential Property Tribunal Wales, Audit Wales |
| Other committees | 10 | Finance, Legislation, Climate Change, Equality |

### 5.3 Signals — is something actually about to happen?

A vague expression of concern and a firm commitment to legislate are different
things and must score differently.

| Signal | Boost | Detects |
|---|---|---|
| Consultation open | 40 | "consultation", "call for evidence", "closing date", "respond by" |
| Legislative commitment | 35 | "we will legislate", "introduce a bill", "White Paper", "draft regulations" |
| Bill progress | 35 | "Stage 1–4", "Royal Assent", "commencement order", "coming into force" |
| Opportunity to influence | 30 | "oral evidence", "written evidence", "stakeholder engagement", "task and finish group" |
| Decision or commitment | 25 | "I can confirm", "I am announcing", "with effect from" |

### 5.4 Source multipliers

| Source | × | Rationale |
|---|---|---|
| Consultation | 1.4 | Time-limited; missing one is unrecoverable |
| Legislation | 1.3 | The hard edge of policy |
| Committee transcript | 1.15 | Detailed scrutiny |
| Plenary transcript | 1.1 | On the record, ministers answering |
| Written statement | 1.1 | Formal government position |
| Written/oral question | 1.0 | Baseline |
| Press release | 0.9 | Often restates a statement |
| Calendar | 0.8 | Scheduling, not substance |

### 5.5 Design decisions that matter

**Word-boundary matching.** "rent" must not match "current", "different",
"parent" or "apparent". A substring search on the Senedd Record produces hundreds
of false positives a week, and a tool that cries wolf gets muted within a month.
This is the most-tested property in the codebase.

**Themes and entities are disjoint.** During the build, "Rent Smart Wales"
appeared as both a theme term (45) and a delivery-body entity (15), scoring
twice. The visible symptom: a written question about Rent Smart Wales hate-crime
awareness training scored 85 and **outranked** *"Does the Welsh Government have
plans to bring in rent controls?"* at 75. A test now guards the whole taxonomy
against any such overlap, which caught a second instance ("Unnos") immediately.

**Vetoes with overrides.** Social-landlord business is monitored separately, so
`private_rented_sector` is vetoed by "registered social landlord". But an item
saying "standards among registered social landlords are higher than in the
private rented sector" is exactly the comparative material we most want, so an
unambiguous phrase overrides the veto.

**Consultations are never buried.** Any consultation touching a monitored theme
reaches the digest regardless of score, because the cost of missing a closing
date is asymmetric.

**Procedural furniture is excluded by configuration, not code.** The Record tags
each contribution with a type; type `I` is the bilingual-column explainer and the
"[R] indicates a declared interest" note, present in every transcript. It is
filtered via the taxonomy so the policy team can change their mind without a code
change.

### 5.6 Honest limitations of this model

It is a transparent, auditable, tunable keyword-and-entity model. It is not a
language model, and that is a deliberate choice — see section 11. Consequences:

- **It will miss novel phrasing.** If a minister invents a term for rent
  regulation that is not in the taxonomy, the item scores on its other themes
  only. Mitigation: monthly review of Low/Noise items, which is a 10-minute job
  with the dashboard's minimum-priority selector.
- **It cannot read sentiment or implication.** "I have no plans to bring forward
  rent controls" and "I will bring forward rent controls" score identically. The
  system tells you to read it; you read it. This is correct behaviour for a tool
  whose output may end up in a briefing to a chief executive.
- **Legislation is scored on title alone**, because the feeds carry little body
  text. The Building Safety (Wales) Act 2026 therefore banded Low despite its
  importance. Mitigation: raise the `legislation` multiplier, or fetch the
  explanatory notes. Noted as a phase 2 item.
- **The people list will go stale.** Every reshuffle, by-election and committee
  change needs an edit. `config/taxonomy.yaml` carries a `review_due` date set to
  14th September 2026 for exactly this reason. Note that the Senedd's own
  committee pages had not been updated for the Seventh Senedd as at 4th August
  2026, so the current entity list was verified from press reporting and **must be
  re-checked in September**.

---

## 6. Architecture

```
                    ┌──────────────────────────────────────────┐
  record.senedd     │  COLLECTORS  (one per source)            │
  .wales      ─────▶│    record_transcripts   record_search    │
  business.senedd   │    legislation          forward_look     │
  .wales      ─────▶│    govwales (RSS | mailbox via Graph)    │
  legislation       └────────────────────┬─────────────────────┘
  .gov.uk     ─────▶                     │  every collector emits the same Item
                                         ▼
  gov.wales         ┌──────────────────────────────────────────┐
  (via shared ─────▶│  RELEVANCE ENGINE                        │
   mailbox)         │  config/taxonomy.yaml — policy-editable   │
                    │  themes + entities + signals × source     │
                    └────────────────────┬─────────────────────┘
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │  STORE — one SQLite file                 │
                    │  items · FTS5 search · score_history     │
                    │  runs (incl. which sources failed)       │
                    └───────┬──────────────────────┬───────────┘
                            ▼                      ▼
              ┌─────────────────────┐   ┌────────────────────────┐
              │ DASHBOARD           │   │ EMAIL                  │
              │ one self-contained  │   │ immediate alert (Crit.) │
              │ HTML file →         │   │ daily/weekly digest     │
              │ SharePoint          │   │ → Outlook              │
              └─────────────────────┘   └────────────────────────┘
```

**Why one `Item` shape for everything.** Because a written question, a paragraph
of a Plenary speech, a consultation and a bill stage all become the same object,
the scorer, dashboard, alerting and archive each need to understand only one
thing. Adding a source means writing one collector. Nothing else changes.

**Why SQLite rather than a hosted database.** A single file that can be copied to
SharePoint, opened by a non-developer in DB Browser, and restored from OneDrive
version history is worth more to a policy team than a managed Postgres instance
nobody has credentials for. It also makes the whole system trivially portable if
NRLA later moves it in-house or to a different host.

**Why a single HTML file rather than a web app.** No server to patch, no
dependency tree to keep current, no login for colleagues to lose, works offline,
opens on a phone, and can be emailed to a member of staff on the way to a
committee appearance. The constraint that it must stay a single file is a feature.

### Repository layout

```
senedd-monitor/
├── config/taxonomy.yaml        ← the policy team's file. Start here.
├── monitor/
│   ├── models.py               Item: the one shape everything becomes
│   ├── relevance.py            scoring engine
│   ├── store.py                SQLite + FTS5 + score history
│   ├── pipeline.py             run orchestration and health reporting
│   ├── dashboard.py            single-file HTML renderer
│   ├── alerts.py               alert and digest email renderers
│   ├── cli.py                  command line interface
│   └── collectors/
│       ├── base.py             polite HTTP: throttling, backoff, ETags
│       ├── record_transcripts.py
│       ├── record_search.py
│       ├── legislation.py
│       ├── forward_look.py
│       └── govwales.py         both routes: RSS and mailbox
├── tests/test_monitor.py       52 tests, incl. a regression per bug found
├── tools/load_govwales_fixture.py
├── samples/govwales_notifications.json   ← delete at go-live
└── requirements.txt            requests, PyYAML, beautifulsoup4
```

Three dependencies. All are standard, widely used, and available in any Python
3.11 environment.

---

## 7. Access and deployment

The directorate needs a shared dashboard and email alerts. NRLA runs Microsoft
365, and section 4.4 established that gov.wales blocks cloud datacentre IPs.
Those three facts point clearly at one option.

### Recommended: scheduled task on NRLA infrastructure, publishing to SharePoint

```
  Windows Task Scheduler / cron on an existing NRLA server
      │  runs from an NRLA egress IP, so gov.wales is reachable directly
      ▼
  python -m monitor.cli collect --days 14
  python -m monitor.cli dashboard --out \\sharepoint\policy\monitor\index.html
  python -m monitor.cli alert  --send        (hourly on sitting days)
  python -m monitor.cli digest --send        (08.00 daily)
      │
      ▼
  SharePoint document library, synced or mapped
      → colleagues open index.html; no login beyond their normal SharePoint access
      → SQLite file sits alongside it, versioned by SharePoint automatically
```

**Why this is the recommendation.** It needs no new procurement, no new
infrastructure, no firewall change and no external hosting. It reaches gov.wales
directly. It puts outputs where the directorate already works. And the SQLite
file gets SharePoint's version history for free, which is a better backup story
than most bespoke systems manage.

### Alternative A: Azure, if NRLA prefers not to run it on-premises

An Azure Container App Job or Function on a timer trigger, with the SQLite file
on Azure Files and the dashboard pushed to SharePoint via Graph.

- **Cost:** roughly £5–£15 a month at this volume, plus storage. Small.
- **Catch:** gov.wales will 403 unless the deployment uses a NAT Gateway with a
  static outbound IP **and** that IP is allow-listed by the Welsh Government, or
  the mailbox route is used instead. The mailbox route is the cleaner answer and
  is recommended in either topology.

### Alternative B: GitHub Actions

Free for a private repo at this volume, and the schedule is version-controlled.
Same gov.wales limitation as Azure, plus the SQLite file has to live in the repo
or in artifact storage, which is awkward once the archive grows. Reasonable for a
pilot, not for the long run.

### Access model in practice

| Who | How | What they get |
|---|---|---|
| Policy directorate | SharePoint link to `index.html` | Full dashboard, search, archive |
| Policy lead | Immediate alerts + daily digest | Critical items within the hour |
| Wider team / comms | Weekly digest | Grouped summary, no dashboard needed |
| External adviser | Emailed HTML file | Works offline, no access grant needed |

Because the dashboard is one file with no credentials embedded, sharing it is a
file-permission question that SharePoint already answers. There is no separate
user management to build or maintain.

### Suggested schedule

| Job | Sitting weeks | Recess |
|---|---|---|
| `collect` | 07.00, 12.00, 17.00, 20.00 | 08.00 daily |
| `alert --send` | hourly, 08.00–20.00 | 09.00 daily |
| `digest --send` | 08.00 daily | 08.00 Monday only |
| `dashboard` | after every collect | after every collect |
| Backfill / integrity check | 02.00 Sunday | 02.00 Sunday |

Plenary sits Tuesday and Wednesday; committees mostly Wednesday and Thursday. The
Senedd rose for summer on 17th July 2026 and returns on **14th September 2026**.

---

## 8. Costs

| Item | Cost |
|---|---|
| Senedd Record XML, legislation.gov.uk, gov.wales content | £0 — Open Government Licence v3.0 |
| Software licences | £0 — Python and three open-source libraries |
| Hosting, recommended option (existing NRLA server) | £0 marginal |
| Hosting, Azure alternative | ~£5–£15/month |
| Shared mailbox | £0 — within existing Microsoft 365 |
| **Total recurring** | **£0–£180/year** |

Set-up and ongoing effort, which is the real cost:

| Activity | Effort |
|---|---|
| Deploy, schedule, connect mailbox, verify against a sitting week | 2–3 days, one-off |
| Taxonomy tuning with the policy team | 1 day, one-off, then quarterly |
| Ongoing operation | Effectively zero — it is a scheduled task |
| Reshuffle / committee updates | ~1 hour each time |
| Annual review | Half a day |

I have deliberately not compared this against the Camlas retainer, because I do
not know what we pay. That comparison should go into the business case, and it is
the one number that will actually decide this.

---

## 9. Risks and how they are handled

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **A source breaks and nobody notices** | Medium | **High** — the worst failure mode | Every collector reports failures; failed sources are persisted per run and rendered in red at the top of the dashboard; `collect` exits non-zero so a scheduler can raise an alert |
| Senedd changes the Record's HTML | Medium | Medium | Transcripts and legislation are XML/Atom and unaffected. The two HTML parsers key off data-derived markup, warn loudly when structure is missing, and are covered by fixture tests |
| gov.wales WAF blocks us | **Certain on cloud** | Medium | Mailbox route as primary; documented, tested, and independent of the WAF |
| ~~The forward look stays broken~~ | — | — | **Resolved.** Now on the ModernGov SOAP service, verified against real scheduled business |
| Misdiagnosing our own bug as an external block | **High — it already happened twice** | High | Regression tests on the User-Agent and on the SOAP parameters; the 403 log message now tells the operator to check their own request first |
| Taxonomy drifts out of date | High over time | Medium | `review_due` date in the config; quarterly review; `rescore` makes tuning instant and auditable |
| Over-alerting, tool gets muted | Medium | High | Critical rationed to 130+; immediate alerts are Critical only; word-boundary matching; `rescore` lets thresholds be tuned against real history before anyone is emailed |
| Under-alerting, something is missed | Medium | High | Deliberately wide seed queries and low store threshold; everything kept in the archive; monthly sweep of Low/Noise |
| Key-person dependency | Medium | Medium | Three dependencies, 52 tests, extensive comments, plain Python. Any competent developer can pick it up. Handover notes in `README.md` |
| Someone treats the output as advice | Medium | **High** | Verbatim text only, never generated summaries; every item links to source; section 11 states the boundary explicitly |

---

## 10. Data protection and responsible use

**Personal data.** The system processes the names, roles, constituencies and
published statements of Members of the Senedd and Welsh Ministers, acting in
their public capacity. This is material they have published themselves, in
Parliament, for the purpose of public scrutiny. It is a low-risk processing
activity, but it is processing, and it should be recorded as such.

Recommended before go-live:

- Add the monitor to NRLA's record of processing activities. Lawful basis:
  legitimate interests — representing our members' interests in the Welsh policy
  process. A short legitimate interests assessment is proportionate; a full DPIA
  is probably not, but that is the DPO's call, not mine.
- Set a retention position. The suggestion is to keep the archive indefinitely,
  because its value is precisely that it is historical, and because it contains
  only published parliamentary material.
- **Confirm the mailbox route with the DPO specifically.** The collector already
  restricts itself: it stores only subject, body and received date, ignores
  recipients, headers and attachments, and refuses any message from outside an
  allow-list of publisher domains. That last control was tested — a misdirected
  human email was correctly refused. But a shared mailbox reading via Graph is
  the one component that touches an NRLA mail store, so it deserves a specific
  sign-off.
- Scope the Entra ID application access policy to the single monitored mailbox.
  Without that policy, an application-permission `Mail.Read` grant can read every
  mailbox in the tenant, and IT will rightly refuse it.

**Being a good citizen of public infrastructure.** The Senedd publishes this data
under OGL v3 on the understanding it is used responsibly. The fetcher therefore
uses an honest, identifying User-Agent with a contact address, enforces a minimum
1.5-second gap between requests to any host, retries only transient failures with
exponential backoff, never retries a 4xx, and sends conditional requests so
re-runs are cheap for the publisher too. If we ever cause a problem, someone can
email us instead of blocking us.

We do not attempt to circumvent the gov.wales WAF. That is a deliberate position,
not an oversight.

**Attribution.** Senedd, Welsh Government and legislation.gov.uk content is
reproduced under the Open Government Licence v3.0, and the dashboard and both
email templates carry that attribution.

**Reputational care.** The system reproduces what people actually said, verbatim,
with a link and a timestamped video reference. It never paraphrases a minister or
an MS, and it never generates commentary. If NRLA quotes something the monitor
surfaced, the quotation is the published record and can be checked in one click.
That is a deliberate protection for the organisation as much as a design
convenience.

---

## 11. What this system deliberately does not do

Being clear about this is part of the design, not a caveat bolted on.

**It does not write the briefing.** Every word it presents is verbatim published
text. It does not summarise in its own words, and it does not use a language model
to condense a minister's answer. If it did, the paraphrase would eventually drift,
and a drifted paraphrase in a briefing to a chief executive or a committee is a
reputational risk that no amount of convenience justifies. The team gets the
actual words, ranked, with a link.

**It does not replace political judgement.** Camlas presumably offer a read on
what matters and why — which committee member is worth cultivating, what a
minister's phrasing signals, when to push. That is consultancy, and this is a
monitoring tool. If we bring monitoring in-house, we should be honest that we are
buying back the judgement with staff time, or retaining an adviser for it.

**It does not replace *Yr Wythnos*.** The weekly video roundup is a communications
product. Nothing here produces it.

**It does not cover local government.** The 22 Welsh councils make decisions that
hit landlords directly — Article 4 directions, licensing schemes, council tax
premiums, local development plans. That was scoped out of this phase and is the
strongest candidate for phase 3.

**It does not read Welsh-language contributions in Welsh.** It uses the English
transcript, which is the Senedd's own translation, and falls back to the bilingual
export. For monitoring that is correct. For anything quoted in Welsh, check the
original.

---

## 12. Pre-go-live verification

The prototype is verified against live data, but four things cannot be confirmed
until the Senedd is sitting. **Do not decommission Camlas before these pass.**

| # | Check | Why | When |
|---|---|---|---|
| 1 | ~~Re-test the calendar during a sitting week~~ **Done.** Replaced with the SOAP service, which returns real scheduled business (96 meetings, Sept–Oct 2026). Still worth spot-checking one agenda against the published Order Paper in September | Was the least-proven component; now live | ✅ 4th August 2026 |
| 2 | Run from an NRLA egress IP and confirm **gov.wales** returns 200. senedd.wales no longer needs this — it was a User-Agent problem, now fixed | Determines whether the RSS route is available at all, or whether the mailbox route is the only option | Before deployment |
| 3 | Connect the shared mailbox and confirm live Welsh Government notifications parse, with closing dates extracted correctly | The gov.wales half depends entirely on this | Before deployment |
| 4 | Verify the Seventh Senedd committee membership and ministerial names in `taxonomy.yaml` against the Senedd's own pages | Current list came from press reporting; senedd.wales had not updated | After 14th September 2026 |
| 5 | Run in parallel with Camlas for four sitting weeks and compare, item by item | The only honest test of completeness | Sept–Oct 2026 |
| 6 | Tune thresholds with the policy team using `rescore` against the real archive | Get the alert volume right before anyone is emailed | During parallel running |

Item 5 is the important one. Four weeks of parallel running, with someone
comparing the two outputs each week, is what turns "the prototype found 13 of 13"
into a decision anyone can defend.

---

## 13. Delivery plan

**Phase 1 — Deploy what exists (2–3 weeks)**
Deploy to NRLA infrastructure; connect the shared mailbox; schedule the jobs;
publish the dashboard to SharePoint; tune the taxonomy with the policy team;
begin parallel running.
*Delivers: same-day alerting, searchable archive, deadline tracking.*

**Phase 2 — Close the gaps (4–6 weeks)**
Fix the forward look against a real sitting week; add Senedd Bill stage tracking;
enrich legislation items with explanatory notes so they band correctly; add
committee inquiry and call-for-evidence detection; add the member register so
party and constituency are populated everywhere; add a simple triage workflow so
officers can mark items *action / monitor / not relevant* (the columns already
exist in the schema).
*Delivers: full Camlas parity, plus triage the current service cannot offer.*

**Phase 3 — Go beyond (as capacity allows)**
The 22 local authorities; Welsh media and stakeholder monitoring so we see
arguments forming rather than only proceedings; trend reporting from the archive
(who raises our issues, how often, and whether that is changing); a Power BI view
over the SQLite file for anyone who prefers it; automatic member-facing summaries
of confirmed changes.

Phase 1 is the decision. Everything after it is optional and can be judged on
whether phase 1 earns its keep.

---

## 14. Recommendation

Proceed to phase 1 and run in parallel with Camlas for four sitting weeks from
14th September 2026.

The evidence for this is not that the system is cheaper. It is that the Senedd
gives away, in machine-readable form, the raw material we are currently paying to
have read for us — and that when the prototype read it, it found a written
question on rent controls that the service we pay for did not report.

The honest caveats have narrowed to one. The forward look is now live and proven
against real scheduled business, and the Senedd "WAF block" turned out to be our
own User-Agent. What remains is that bringing monitoring in-house means buying
back political judgement with staff time — that is a real cost and should be
budgeted, not wished away. It is not a reason to wait.

---

## Sources

Verified 4th August 2026.

- [Senedd Open Data](https://senedd.wales/help/open-data/) — datasets, endpoints and OGL v3 licensing
- [Record of Proceedings XML export](https://record.senedd.wales/XMLExport)
- [The Record — search](https://record.senedd.wales/Search/)
- [Acts of Senedd Cymru — Atom feed](https://www.legislation.gov.uk/asc/data.feed)
- [Welsh Statutory Instruments 2026 — Atom feed](https://www.legislation.gov.uk/wsi/2026/data.feed)
- [Senedd business calendar JSON](https://business.senedd.wales/calJson.aspx)
- [GOV.WALES announcements](https://www.gov.wales/announcements) and its [RSS feed](https://www.gov.wales/announcements/rss)
- [GOV.WALES announcement subscriptions](https://www.gov.wales/subscribe/announcements)
- [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
- [A "first phase of legislation" — Senedd Research](https://research.senedd.wales/research-articles/a-first-phase-of-legislation-the-welsh-government-s-legislative-priorities/)
- [Implementing the Building Safety (Wales) Act 2026 — GOV.WALES](https://www.gov.wales/implementing-building-safety-wales-act-2026-html)
- [Evaluation of Rent Smart Wales — GOV.WALES](https://www.gov.wales/evaluation-rent-smart-wales-summary-html)
- Camlas, *W29 NRLA Weekly Briefing*, 17th July 2026 (supplied)

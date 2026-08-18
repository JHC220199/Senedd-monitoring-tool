# Does it actually catch things? — the W29 recall test

Everything else in this repository tests whether the code does what I told it
to. This tests something harder and more important: **whether the monitor sees
what a human expert saw.** A monitoring tool that quietly misses a consultation
is worse than no tool, because it replaces "I should check" with false comfort.

Run on 13 August 2026, against the archive as it stood that day.

## Method

The ground truth is the **Camlas NRLA Weekly Briefing for W29, 17 July 2026** —
the paid supplier product this tool is meant to replace. Every item the supplier
itemised for that week was checked against the archive for 13–19 July 2026.

One rule made the test honest: **fixture data counted as a miss.** Five Welsh
Government items were in the archive as demonstration samples
(`raw_ref` containing `FIXTURE`), loaded so the mailbox parser could be
exercised without a Microsoft Graph tenant. They had never been collected from a
live source, so for the purposes of "would this tool have told me?", they are
not evidence that it would.

## Result

| Supplier item | Captured live? |
|---|---|
| Debate: The First Supplementary Budget 2026-27 (Government lost 49–44) | **Yes** — 46 contributions |
| Statement by the First Minister: Legislation | **Yes** — 12 contributions |
| Community Right to Buy raised in that statement | **Yes** — 13 items mention it |
| Business Statement: Lis McLean MS on community right to buy | **Yes** — 5 contributions |
| Questions to the Cabinet Minister for Local Government, Housing and Planning | **Yes** — 38 contributions |
| Planning Policy Wales — James Evans MS, OQ64366 | **Yes** |
| Local government pension schemes — Stephen Senior MS | **Correctly dropped** (see below) |
| Written Statement: Outcome of the First Supplementary Budget | **No** — fixture only |
| Press release: first phase of legislation | **No** — fixture only |
| Council Tax Reduction Scheme consultation | **No** — fixture only |
| Consultation: Implementing the Building Safety (Wales) Act 2026 | **No** — fixture only |
| Finance Committee Report on the Supplementary Budget (laid 13 July) | **No** — no source covers it |
| Written questions and answers (Dan Thomas, Huw Thomas, Natasha Asghar) | **Out of scope by design** |
| Ken Skates elected Welsh Labour leader | **No** — party politics, no source covers it |

**Senedd-sourced business: 6 of 6 captured.** Everything the supplier reported
from the Chamber, this tool had, usually with more of the verbatim record than
the briefing carried.

**Welsh Government business: 0 of 4 captured live.** All four existed only as
demonstration fixtures. This is not a scoring problem — it is that gov.wales
blocks datacentre IP ranges, so the feed returns nothing from GitHub Actions
and never has.

## Three findings worth acting on

**1. The Welsh Government half has never run.** Confirmed by this test rather
than inferred. Every run records it, but as *substituted* rather than *failed*,
so the page stayed green and the gap was invisible. Welsh Government is where
most law affecting landlords actually starts, so this is the difference between
a useful tool and a partial one. Fixed on the page (a banner now names the dead
source) but only genuinely fixed by connecting the shared mailbox to Microsoft
Graph, or running the collector inside the NRLA network.

**2. Laid documents have no source at all.** The supplier's "Documents Laid
Before the Senedd" and "Laid Documents" sections have no counterpart here.
That is how the Council Tax Reduction Scheme consultation reached the supplier's
briefing, and it is a systematic blind spot, not a one-off miss. Worth a
collector.

**3. The strict filter behaved correctly, including where it stayed silent.**
Stephen Senior's question on local government pension schemes is genuinely
absent from the archive: it scored below the storage threshold because it has
nothing to do with the private rented sector. The supplier covers all of local
government; this tool covers the PRS. That is the filter working, not failing —
and it is the distinction the whole taxonomy exists to draw.

## Where the tool beat the supplier

Three influence opportunities appear on the monitor and **not** in the W29
briefing:

- **Priorities for the Local Government, Housing and Planning Committee** —
  an open consultation closing 14 September 2026, directly addressed to
  organisations like the NRLA.
- **Follow-up inquiry into Empty Properties** — a live committee inquiry taking
  written evidence.
- A **Senedd Research** briefing analysing the legislative programme.

Senedd committee consultations look like this tool's genuine edge: they are
published on committee pages rather than announced, so a weekly human roundup
can miss them, and they are exactly the items with a deadline attached.

## What this test does not prove

One week, and a quiet one — recess began on 17 July, so no Bill stages and
little committee activity. The real test is a sitting week; the Senedd returns
on **14 September 2026**, and this should be re-run against a W38 or W39
briefing then. The ground truth is also itself a commercial product with its own
omissions, so "the supplier had it and we didn't" is a strong signal while
"we had it and the supplier didn't" is only suggestive.

Re-running is cheap: the archive is permanent and `rescore` re-applies a tuned
taxonomy to history without a network call, so a filter change can be tested
against this same week immediately.

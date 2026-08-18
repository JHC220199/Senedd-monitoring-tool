# Senedd policy monitor

Automated political monitoring of Senedd Cymru and the Welsh Government for the
NRLA policy directorate. Replaces a weekly supplier briefing with same-day
alerting, a searchable archive and deadline tracking.

Read `SPECIFICATION.md` for the design, the verified data-source findings and the
deployment recommendation. This file is the operator's guide.

## Where to read it

Three places, none of which need any credential, any server or any IT request:

| Where | What it is |
|---|---|
| **[The live page](https://jhc220199.github.io/Senedd-monitoring-tool/)** | The tool itself: open consultations with their deadlines, bills, debates, questions. **Bookmark this.** Rebuilt every weekday morning. |
| **`BRIEFING.md`** in this repository | The same run written up as a short briefing. One permanent URL, always current. Works on a private repository — which GitHub Pages does not. |
| **`briefings/2026-W32.md`** | The permanent weekly record, committed on every run. |

### What is NOT being monitored

**The Welsh Government feed is not running.** gov.wales blocks datacentre IP
ranges, so from GitHub Actions the feed returns nothing — consultations, written
statements and announcements published there are invisible to this tool. Every
run records it and the live page now carries a banner naming it, because an empty
section must not be mistaken for a quiet week.

To fix it, one of:

- connect the shared mailbox `joshua.helm-cowley@nrla.org.uk` to Microsoft Graph and
  set `MONITOR_MAILBOX` and `MONITOR_GRAPH_TOKEN` as repository secrets, so the
  gov.wales notification emails become the feed (`--govwales-route mailbox`); or
- run `collect` from inside the NRLA network, where gov.wales is reachable.

**Laid documents have no source.** Papers laid before the Senedd are not
collected, which `VALIDATION.md` identifies as a systematic blind spot.

Demonstration fixtures used to stand in for the Welsh Government feed and were
displayed as real open consultations. They have been removed
(`prune --fixtures`), and fixture-sourced rows can no longer reach the page even
if a future demo run re-adds them.

### How well does it work?

`VALIDATION.md` records a recall test against the Camlas W29 supplier briefing:
**6 of 6 Senedd items captured, 0 of 4 Welsh Government items**, plus three
influence opportunities the supplier's briefing missed. Re-run it against a
sitting week after 14 September 2026.

### Nothing emails you, and that is deliberate

Earlier versions opened a dated GitHub issue on every run and assigned it to the
repository owner, because an assigned issue is the one way to make GitHub deliver
a digest with no SMTP server and no IT request. It worked — and it produced an
email after every successful run, which the directorate asked to stop on
13 August 2026: *"It is not user friendly or useful to read this."*

So `publish` now runs with `--no-issue`, and the workflow no longer requests the
`issues: write` permission. Two guards rather than one: if a later edit drops the
flag, the run fails instead of quietly resuming the emails.

If a daily email is ever wanted, the SMTP route still exists — set
`MONITOR_SMTP_HOST`, `MONITOR_SMTP_USER`, `MONITOR_SMTP_PASS`, `MONITOR_FROM`
and `MONITOR_TO`, and the formatted digest sends from an NRLA address.

**If a run went green and something looks wrong, read `TROUBLESHOOTING.md`.**

---

## Get it running in five minutes

```bash
pip install -r requirements.txt

# Collect the last two weeks, score it, and build the dashboard
python -m monitor.cli collect --days 14 --dashboard out/index.html

# Open out/index.html in a browser. That's it.
```

To see the Welsh Government half working without a live mailbox:

```bash
python3 tools/load_govwales_fixture.py --i-will-prune-this-afterwards
python -m monitor.cli dashboard --out out/index.html

# ALWAYS clean up before committing, or demo consultations reach the live page:
python -m monitor.cli prune --fixtures --yes
python -m monitor.cli export --out data/archive.sql
```

> **This writes into the archive.** Five fixture items once sat on the live page
> for weeks as open consultations, complete with a deadline countdown, because
> nobody pruned them. The page now refuses to display fixture-sourced rows, but
> clean up anyway — the briefing and the CSV do not have that guard.

---

## Commands

| Command | What it does |
|---|---|
| `collect --days N` | Fetch, score and store. `--dashboard PATH` also rebuilds the page. Exits non-zero if any source returned nothing, so a scheduler can raise an alert. |
| `dashboard --out PATH` | Rebuild the dashboard from the archive without fetching. |
| `brief` | The same briefing as markdown. Piped into `$GITHUB_STEP_SUMMARY` by the workflow, so it appears on the Actions run page — no download and no email needed. |
| `publish` | Writes `BRIEFING.md` and `briefings/YYYY-Wnn.md`. The workflow passes `--no-issue`, so no GitHub issue is opened and nothing is emailed. `--dry-run` to see what it would do. |
| `digest --days N [--send]` | Build the periodic digest. **Dry run by default.** |
| `alert [--send]` | Email unnotified Critical items only. **Dry run by default.** |
| `search "rent control"` | Full-text search the whole archive. |
| `rescore` | Re-apply the current taxonomy to everything already collected. No network. |
| `stats` | Archive size, band distribution, upcoming deadlines, recent run health. |
| `weeks` | Every week in the archive, with counts. The index for the weekly record. |
| `week [2026-W29]` | One week's business. Accepts `current`, `last`, or an ISO week. `--snapshot` also freezes the HTML. |
| `snapshots` | Backfill a permanent HTML page for every complete week. |
| `prune --source X --yes` | Remove a source kind from the archive, for overlap with other tools. |
| `prune --fixtures --yes` | Remove demonstration fixture items. Run this after any `load_govwales_fixture.py` demo. |

Add `--govwales-route mailbox` to `collect` when running anywhere gov.wales
blocks (any cloud host). Without it, the unreachable RSS feed is reported as a
failed source and the dashboard banner goes red on every single run — and a
warning that is always on is a warning nobody reads.

Global options: `--db PATH`, `--taxonomy PATH`, `--interval SECONDS`, `-v`.

`--send` is required for anything to leave the building. Nothing should ever be
emailed to a distribution list because a script was run with the wrong argument.

---

## The weekly record

The archive stores items; it is *read* by week. ISO weeks, Monday to Sunday,
labelled `2026-W29` — which lines up with the supplier's own W29 numbering, so a
historical comparison is file against file rather than a judgement call.

```bash
python -m monitor.cli weeks                 # index of every week, with counts
python -m monitor.cli week last             # last complete week
python -m monitor.cli week 2026-W29         # any specific week
python -m monitor.cli snapshots             # freeze a page per complete week
```

Snapshots land in `out/weeks/2026-W29.html`. Each is **permanent and fixed** —
re-scoring the archive later does not change one. That is the audit trail: if
someone asks in 2028 why NRLA did not respond to a 2026 consultation, the answer
is a file with a date on it, not a database query whose answer has since moved.

The current week is skipped when backfilling, because freezing an unfinished week
produces a record that is wrong by Friday.

Retention is indefinite by design. The archive holds only published
parliamentary material, its whole value is that it is historical, and a full
Senedd term of weekly snapshots is smaller than one of the Word briefings they
replace.

---

## Switching sources off

`config/taxonomy.yaml` has a `sources` section of plain on/off switches. A source
set to `false` is never fetched, so it costs no requests.

**Written questions are off**, because the policy directorate has a separate tool
that tracks them. Duplicate coverage is worse than a gap: two systems reporting
the same written question means two people each think the other is handling it.

Switching a source off stops new items arriving. To clear out what is already
there:

```bash
python -m monitor.cli prune --source written_question --yes
```

Oral and topical questions stay on — they are asked in the Chamber and appear in
the transcripts anyway, so excluding them would leave holes in debates.

---

## Tuning what gets flagged

**`config/taxonomy.yaml` is the policy team's file.** It is heavily commented and
designed to be edited without touching code.

```bash
# 1. Edit config/taxonomy.yaml — change a weight, add a term, add a new MS
# 2. Re-apply it to everything already collected
python -m monitor.cli rescore
# 3. See what changed
python -m monitor.cli stats
```

`rescore` needs no network and takes seconds, so you can tune against six months
of real history before anyone gets an email. Every change is logged to the
`score_history` table, so a tuning decision can always be explained later.

Rules of thumb:

- Team says "this should have been flagged" → raise a theme weight, or add the
  phrase they used to that theme's terms.
- Team says "this is noise" → add an `exclude_if` term to the theme.
- Reshuffle, by-election or committee change → update `entities`.
- Never delete a theme. Set its weight to `0` to mute it, so historical scores
  stay comparable.
- Keep `themes` and `entities` terms **disjoint**. A term in both is counted
  twice and quietly distorts priorities. A test enforces this — run it.

---

## Scheduling

**To run this daily on GitHub, read `SETUP-GITHUB.md`** — the workflow is already
at `.github/workflows/monitor.yml` and goes live on push. `deploy/AUTOMATION.md`
compares that against running it on an NRLA server, which is the only route that
reaches gov.wales.

**No command line, no paid account: read `SETUP-GITHUB-BROWSER-ONLY.md`.** GitHub
Free includes unlimited private repositories, so nothing here needs paying for.
The one trap is that a browser upload silently skips `.github` and `.gitignore` —
and a missing `.github` means the schedule never runs, with no error shown. That
guide handles it; `deploy/github-actions-workflow.yml` and `deploy/gitignore.txt`
are readable copies of the two hidden files, kept identical by a test.

The archive persists as `data/archive.sql` — plain SQL text, committed after every
run, with the SQLite binary gitignored and rebuilt from it. `git log -p
data/archive.sql` then shows exactly what the monitor found each day.

Scheduling is left to the host on purpose: cron, Windows Task Scheduler, an Azure
timer trigger or a GitHub Actions cron are all easier for IT to reason about than
a bespoke scheduler inside the app.

Sitting weeks (Plenary sits Tuesday and Wednesday; committees Wednesday and
Thursday):

```cron
0 7,12,17,20 * * 1-5   cd /opt/senedd-monitor && python -m monitor.cli collect --days 14 --dashboard /sharepoint/policy/monitor/index.html
0 8-20      * * 1-5    cd /opt/senedd-monitor && python -m monitor.cli alert --send
0 8         * * 1-5    cd /opt/senedd-monitor && python -m monitor.cli digest --days 1 --send
0 2         * * 0      cd /opt/senedd-monitor && python -m monitor.cli collect --days 30
```

Recess: drop to one `collect` and one digest a day. The Senedd rose on 17th July
2026 and returns **14th September 2026**.

---

## Email configuration

Set these in the environment, not in a file:

```bash
export MONITOR_FROM="joshua.helm-cowley@nrla.org.uk"
export MONITOR_TO="first.last@nrla.org.uk,policy@nrla.org.uk"
export MONITOR_SMTP_HOST="smtp.office365.com"
export MONITOR_SMTP_PORT="587"
export MONITOR_SMTP_USER="joshua.helm-cowley@nrla.org.uk"
export MONITOR_SMTP_PASS="..."          # use a secret store, not a shell profile
```

For the gov.wales mailbox route:

```bash
export MONITOR_MAILBOX="joshua.helm-cowley@nrla.org.uk"
export MONITOR_GRAPH_TOKEN="..."        # app-only token, Mail.Read
```

**Scope the Graph permission.** Apply an Exchange application access policy
restricting the app registration to that single mailbox. Without it, an
application-permission `Mail.Read` grant can read every mailbox in the tenant,
and IT will rightly refuse it.

---

## Reading the dashboard

The page is organised as work, not as data. Three zones, in priority order:

- **Respond** — open consultations and inquiries with a closing date, soonest
  first. Each card says what it is, why it matters, and a suggested next step.
  Underneath, a short watch-list of open items whose closing date has not been
  published yet.
- **Review** — what has happened that a policy officer should know about. **One
  card per debate, not one per speaker** — the first version produced up to 48
  cards for a single agenda item.
- **Coming up** — scheduled sittings. Committee papers usually appear about two
  weeks before.
- **Search everything** — the full archive, collapsed by default.

Relevance scores are **hidden by default**. They are a means of sorting, not a
measure anyone should act on, and putting "148.5" on a card invites comparisons
that are not valid across sources. Tick "show relevance scores" under "How this
was produced" if you are tuning the taxonomy.
- **A red banner means a source we depend on failed.** Treat the page as
  incomplete. A blue banner means a source returned nothing but its content
  arrives another way, so nothing is missing.
- **"Watch this moment"** jumps to the exact point in the Senedd.tv recording.
- Search accepts multiple words; every word must appear somewhere in the item.

---

## Tests

```bash
python -m tests.test_monitor        # 109 tests, no pytest needed
python -m pytest tests/ -q          # if you prefer pytest
```

The `TestRegressions` class is worth reading before changing a collector. Each
test there corresponds to a real bug that live data produced during the build,
and the docstrings explain what went wrong and why it mattered — including two
that silently produced *wrong* results rather than errors:

- The XMLExport listing returns March 2026 meetings when given "All committees"
  plus a date range, so a run asking for June–August silently backfilled the
  wrong Senedd term.
- A lowercase-only CSS class regex missed the Record's camelCase
  `.searchResult` containers, so the parser fell back to scraping flattened page
  text and ingested the site footer and member dropdown into every item — making
  an unrelated question about an agricultural loan scheme score as Critical
  housing business.

---

## Known limitations

Full list in `SPECIFICATION.md` sections 5.6 and 11. The ones that will bite an
operator first:

1. **Only gov.wales is genuinely blocked.** It returns 403 from cloud hosts
   regardless of User-Agent (tested with clean, minimal and default UAs, and on
   `llyw.cymru`). Run from the NRLA network, or use the mailbox route. Every
   Senedd host — `record.senedd.wales`, `business.senedd.wales`, `senedd.wales`,
   `senedd.cymru` — works fine.
2. **Never put a library token in the User-Agent.** `python-requests` in the UA
   triggers CloudFront bot rules and returns 403 on senedd.wales and
   senedd.cymru. This masqueraded as an external WAF block for a whole revision.
   See the comment on `USER_AGENT` in `collectors/base.py`; a test enforces it.
3. **The forward look needs dd/mm/yyyy dates and `lCommitteeId=-1`.** ISO dates
   make the SOAP service ignore the range and return 5,000 rows of everything;
   `lCommitteeId=0` returns nothing. Both failure modes are silent. Tests cover
   them.
4. **The entity list was verified from press reporting**, because senedd.wales had
   not updated its committee pages for the Seventh Senedd as at 4th August 2026.
   Re-check after 14th September 2026. `taxonomy.yaml` carries a `review_due` date.
5. **Legislation is scored largely on title**, so terse Act names need to be in
   the `named_welsh_legislation` theme. Adding a newly introduced Bill to that
   list is the single highest-value maintenance task in the taxonomy.
6. **Nothing is summarised by a language model.** Every word presented is the
   verbatim published record. This is deliberate, not a limitation to fix.

---

## Before go-live

Delete the demonstration fixture, which exists only to exercise the mailbox route
without a live tenant:

```bash
rm -rf samples/ tools/load_govwales_fixture.py
```

Then work through the verification table in `SPECIFICATION.md` section 12. Do not
decommission the existing service until items 1–5 pass, including four sitting
weeks of parallel running.

---

## Licensing and attribution

Senedd Cymru data is reproduced under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
Welsh Government content and legislation.gov.uk content are reproduced under the
same licence. The dashboard and both email templates carry this attribution.

The fetcher identifies itself honestly, spaces requests at least 1.5 seconds
apart per host, retries only transient failures, and sends conditional requests.
Please do not remove any of that. It does not attempt to circumvent the gov.wales
WAF, and it should stay that way.

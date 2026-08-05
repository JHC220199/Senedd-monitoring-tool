# Running this on GitHub

The fastest route to the monitor running daily, unattended, with a database that
persists — and it needs no IT ticket.

---

## What you get, and what you don't

**You get:** the full pipeline daily, a database that accumulates permanently, a
readable audit trail of what was found each day, the weekly records, and the
dashboard as a downloadable file. Runs on a schedule whether anyone is looking
or not.

**You don't get gov.wales.** GitHub's runners are datacentre hosts and gov.wales
returns HTTP 403 to those regardless of what you send — verified on every path.
Every Senedd source works fine, which is the large majority of the value. For
Welsh Government announcements and consultations, either set up the shared
mailbox route (step 5) or accept the gap knowingly.

**Cost:** each run uses roughly 3 minutes of runner time, so about 200 minutes a
month. GitHub includes a monthly allowance of Actions minutes on paid plans and
charges beyond it; the rates changed in January 2026, so check the current figure
for your plan on GitHub's pricing page rather than trusting a number here. Public
repositories are unlimited and free — but **make this repository private**, since
the archive is a curated view of NRLA's policy interests.

---

## Setup

**1. Create a private repository**

`nrla/senedd-monitor`, private. Do not initialise it with a README.

**2. Push this project into it**

```bash
unzip senedd-monitor.zip && cd senedd-monitor
git init -b main
git add -A
git commit -m "Senedd policy monitor"
git remote add origin git@github.com:nrla/senedd-monitor.git
git push -u origin main
```

The workflow is already at `.github/workflows/monitor.yml`, so it is live as soon
as you push. `.gitignore` is set up so the SQLite binary stays out and
`data/archive.sql` goes in.

**3. Add the email secrets**

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `MONITOR_FROM` | `senedd-monitor@nrla.org.uk` |
| `MONITOR_TO` | `first.last@nrla.org.uk` — comma-separate several |
| `MONITOR_SMTP_HOST` | `smtp.office365.com` |
| `MONITOR_SMTP_USER` | `senedd-monitor@nrla.org.uk` |
| `MONITOR_SMTP_PASS` | app password for a dedicated service account |

If you skip these, nothing is emailed — `send()` stays in dry-run mode unless an
SMTP host is present, so a half-configured repo cannot accidentally mail a
distribution list. **The run now says so on its own summary page** rather than
going quietly green: see `TROUBLESHOOTING.md`. The briefing itself appears on the
run page whether or not email is configured, so the tool is usable from day one.

**4. Prove it works**

Actions → "Senedd policy monitor" → Run workflow. It takes a few minutes. Check:

- the job summary shows archive statistics
- a `senedd-dashboard-1` artifact is attached
- a commit appeared touching `data/archive.sql`

If the run is red, read the log. The most likely cause on a first run is a typo
in a secret name.

**5. Optional but worth it: the Welsh Government mailbox**

Subscribe a shared mailbox to `gov.wales/subscribe/announcements`, then add:

| Secret | Value |
|---|---|
| `MONITOR_MAILBOX` | `senedd-monitor@nrla.org.uk` |
| `MONITOR_GRAPH_TOKEN` | app-only token, `Mail.Read` |

**Scope the Graph permission** with an Exchange application access policy
restricting the app registration to that one mailbox. Without it the grant can
read every mailbox in the tenant, and IT will rightly refuse it.

---

## Living with it

**Read the archive's history.** This is the part worth knowing about:

```bash
git log -p data/archive.sql        # exactly what the monitor found, day by day
git log --oneline data/archive.sql # when the archive changed at all
```

During recess there will be gaps in the commit log. That is correct — the export
is deterministic, so identical data produces no commit. **No commit means a quiet
day, not a broken run.** To tell the difference, look at whether the Actions run
happened at all.

**Tune what gets flagged** by editing `config/taxonomy.yaml` and pushing. Then:

```bash
python -m monitor.cli restore --from data/archive.sql
python -m monitor.cli rescore
python -m monitor.cli export --out data/archive.sql
```

**Watch that it is still running.** Enable Actions failure notifications
(Settings → Notifications). A silent scheduler failure looks exactly like a quiet
fortnight in the Senedd, and that is the failure mode that destroys trust in a
monitoring system.

One caveat specific to GitHub: **scheduled workflows are disabled automatically
after 60 days of repository inactivity.** Commits from the monitor itself count,
so an active archive keeps it alive — but during a long recess with no new
business there may be no commits. Check in after any quiet stretch of more than
a month.

---

## If you outgrow it

The reason to move to an NRLA server later is gov.wales, not GitHub's limits.
`deploy/run-daily.sh` and `deploy/run-daily.ps1` are ready for that, and the
archive moves across as one file.

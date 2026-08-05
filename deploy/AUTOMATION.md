# Making this run without anyone prompting it

Three routes, in the order you should consider them. All three are prepared —
nothing here needs writing from scratch.

---

## Not an option: hosting it inside Claude

A scheduled Cowork task was tried and **removed at the directorate's request**,
for the right reason: each firing is a fresh session with no memory, so there is
no database. That makes it a daily search, not a monitoring tool. "When did the
minister last commit to anything on rent data" only works if the data persists.

Recorded here so nobody proposes it again as a shortcut.

---

## Recommended for speed: GitHub Actions (about 15 minutes, no IT involvement)

**Full instructions: `../SETUP-GITHUB.md`.** The workflow is already in place at
`.github/workflows/monitor.yml`, so it goes live the moment you push.

A private repo runs the **full** pipeline daily and commits the archive back
after every run, so it accumulates permanently and "new since last run" works.

The archive is committed as **plain SQL text** (`data/archive.sql`), not the
SQLite binary. The binary is gitignored and rebuilt from the SQL on each run.
That decision is measured, not assumed — see `monitor/archive_io.py`. The
headline benefit is not size but that `git log -p data/archive.sql` shows exactly
what the monitor found each day, with a signed timestamp. For an audit trail
that is better than anything a database gives you.

Two GitHub-specific things to know:

- **It cannot reach gov.wales.** The runners are datacentre hosts. Set up the
  shared mailbox route for the Welsh Government half, or accept the gap knowingly.
- **Scheduled workflows are disabled after 60 days of repository inactivity.**
  The monitor's own commits count as activity, but during a long recess there may
  be none. Check in after any quiet stretch longer than a month.

---

## Properly: NRLA infrastructure (the recommendation)

See `run-daily.sh` (Linux/macOS, cron) or `run-daily.ps1` (Windows, Task
Scheduler), plus `monitor.env.example` for configuration.

This is the recommended option for one reason that nothing else can match: run
from an NRLA egress IP and **gov.wales is reachable directly**. Full coverage,
the archive on your own storage, outputs written straight into SharePoint, and
no external dependency at all.

Ask IT for: a small Linux VM or a Windows service account, Python 3.11, outbound
HTTPS, and a writable path in a SharePoint document library. That is the whole
request.

---

## Whichever route you pick, do this one thing

**Point something at the exit code.** `collect` exits non-zero when a source it
depends on returns nothing. Without a check on that, a silent scheduler failure
looks exactly like a quiet fortnight in the Senedd — and that is the failure mode
that destroys trust in a monitoring system.

The cheapest version is a free dead-man's-switch ping at the end of the run; see
the note at the foot of `run-daily.sh`. If the run stops happening at all, you
get told.

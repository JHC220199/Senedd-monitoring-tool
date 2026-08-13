# "It ran, and I got nothing"

This is the first thing almost everyone hits, and it was a design fault rather
than anything you did wrong. Read this page first.

## What actually happened on your first run

The run **worked**. It is worth being precise about how well, because the
symptom made it look like a failure:

- It collected **257 records** from the Senedd sources.
- It scored them, found **5 open consultations with closing dates** and **15
  developments worth reviewing**.
- It committed the archive to `data/archive.sql`, so the history is accumulating
  as intended.
- It took 4m 51s and exited green.

And you saw none of it. Two separate faults, both mine:

**1. The dashboard was written somewhere nobody would look.** It went to
`out/index.html`, which is gitignored, and then into a build artifact — a zip
file behind three clicks at the foot of a log page. Technically produced.
Practically invisible. Nobody is doing that every morning.

**2. The email went quietly nowhere.** Sending requires SMTP credentials, and
until they exist the code deliberately refuses to send, so that a half-configured
repository cannot mail a distribution list by accident. That part is right. What
was wrong is that it then printed one line into a log and **exited green**. A
successful run that emailed nothing and a successful run that had nothing to say
looked identical. That is the exact failure this project keeps warning about: it
looks like it worked, and it hasn't.

## What changed — and the third attempt that was still wrong

**First fix: the briefing went onto the Actions run page.** Better than a zip in
an artifact, but still wrong, and the verdict was again fair: *"this tool has
still not produced any accessible dashboard or anything for me to actually view
the output of it."*

A CI log page is not a dashboard. Its URL changes every run, you cannot bookmark
it, and nobody opens an Actions tab to read a policy briefing. Three attempts in
a row, I had chosen places that were technically outputs rather than places a
person would actually go.

**Second fix — a real page, plus two written records:**

| Where | What it is |
|---|---|
| **[The live page](https://jhc220199.github.io/Senedd-monitoring-tool/)** | The tool. Open consultations with deadlines, bills, debates, questions. **Bookmark it.** |
| **`BRIEFING.md`** | The same run as a short written briefing, always current. Works on a private repository, unlike GitHub Pages. |
| **`briefings/2026-Wnn.md`** | The permanent weekly record. |

All of these use `GITHUB_TOKEN`, which GitHub injects into every workflow run.
There is nothing to configure, because a system that depends on a credential
nobody has yet is not a system.

## "Why have the emails stopped?"

Because they were asked to stop, on 13 August 2026: *"whenever the monitor
successfully runs I get this email. Please get rid of this. It is not user
friendly or useful to read this."*

The email was a GitHub notification, not something this project sent. Each run
opened a dated issue and assigned it to the repository owner — GitHub always
notifies an issue's assignee, which was the only way to deliver a digest with no
SMTP server and no IT request. Solving "nothing arrives" that way created
"something arrives every single morning whether or not it is worth reading".

`publish` now runs with `--no-issue`, and the workflow no longer asks for
`issues: write`, so re-adding the step by accident fails rather than quietly
resuming the emails. A test asserts both.

**If you want a daily email back**, set the five `MONITOR_SMTP_*` secrets and the
formatted digest sends from an NRLA address — a real email, not a notification
about an issue. If those credentials are set but sending fails, the run goes
**red** and names the missing variable rather than shrugging.

### Stray issues from before the change

Any briefing issues opened by earlier runs are still in the repository's Issues
tab, and any still assigned to you may keep appearing in your GitHub
notifications. Closing them stops that; nothing in the tool reads them.

---

# Two things to fix on your repository

## 1. It is public. It should not be.

`github.com/JHC220199/Senedd-monitoring-tool` is currently public, which means
anyone can read:

- **which Senedd business the NRLA considers important, and how highly** — the
  taxonomy weights and the scored archive together are a fairly precise map of
  the NRLA's policy priorities and where it is likely to intervene
- the archive of what has been collected and flagged, updated daily
- your work-in-progress configuration

None of it is confidential in the sense of being secret, and none of it is
embarrassing. But a competitor organisation, a journalist or an opposing
interest group reading NRLA's private prioritisation of Welsh housing policy is a
foreseeable and avoidable outcome, and it is not something to discover after the
fact.

**Fix it:** Settings → scroll to the bottom, "Danger Zone" → **Change
visibility** → Make private. It takes ten seconds, keeps every commit and the
Actions history, and does not affect the schedule. GitHub Free includes unlimited
private repositories, so this costs nothing.

One consequence to know about: Actions minutes on private repositories draw on
your plan's monthly allowance, where public repositories are unlimited. This runs
about 3 minutes per run, roughly 200 minutes a month. Check your plan's current
allowance — the rates changed in January 2026 and I would rather you read the
live figure than trust one from me. If it is tight, drop to three runs a week by
editing the two `cron:` lines.

## 2. Your email address was published in it

Five files contained `joshua.helm-cowley@nrla.org.uk` as an example value, and on
a public repository that put it on the open web where address harvesters look.
That was careless of me — an example should never have been a real person's
address. The updated package replaces all five with `first.last@nrla.org.uk`.

The role address `policy@nrla.org.uk` remains in one place on purpose: the
`User-Agent` the fetcher sends, so that a Senedd administrator noticing the
traffic can contact someone. That is the polite convention for automated
collection and should stay.

Your actual recipients go in **repository secrets**, never in the code. Secrets
are write-only once saved — GitHub will not display them again, they do not
appear in the repository, and they are masked in logs.

---

# Fixing this specific repository

You do not need to start over. Replace the changed files and re-run.

**The changed files are:**

```
monitor/brief.py          NEW — the briefing as markdown
monitor/cli.py            the `brief` command, and loud email failures
monitor/dashboard.py      zone logic extracted so both formats agree
tests/test_monitor.py     109 tests now
.github/workflows/monitor.yml     the run-page briefing and the email guard
deploy/github-actions-workflow.yml   readable copy of the above
README.md  SETUP-GITHUB.md  TROUBLESHOOTING.md
```

**Via GitHub Desktop** (easiest): unzip the new package over your local folder,
overwriting. Desktop shows the changed files. Write a commit message, Commit,
Push.

**Via the browser:** for each file above, open it in the repository, click the
pencil icon, select all, paste the new contents, Commit. Remember that
`.github/workflows/monitor.yml` is reachable by clicking through the folders in
the repository even though drag-and-drop will not upload it — editing an existing
file has none of the hidden-file problem.

Then: Actions → Senedd policy monitor → **Run workflow**. Wait about four
minutes, click into the run, and the briefing will be on the page.

---

# Other things that look like failures but are not

**"Welsh Government — RSS" shows as collected another way.** Correct and
expected. GitHub's runners are datacentre hosts and gov.wales returns 403 to
them. It is not a bug and not something to work around — the site is entitled to
refuse automated traffic. Senedd coverage is unaffected. To add the Welsh
Government half, use the shared-mailbox route (`MONITOR_MAILBOX`) or run on an
NRLA server; `deploy/AUTOMATION.md` covers both.

**No commit after a run.** Expected during recess. The archive export is
deterministic, so an unchanged archive produces a byte-identical file and
therefore no commit. Nothing new found means nothing to record.

**A briefing that says "nothing above the reporting threshold".** The Senedd rose
on 17 July 2026 and returns on **14 September 2026**. A quiet August is real, and
the briefing states it in words rather than showing an empty page — precisely so
you can tell a quiet period from a broken run.

**The run is red on the "Run the tests first" step.** That is the guard working.
A broken collector must not publish a misleading dashboard. Read the failure —
the test names describe real bugs found during the build.

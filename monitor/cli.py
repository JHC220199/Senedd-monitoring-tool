"""Command line interface.

    python -m monitor.cli collect  --days 14        Fetch, score and store
    python -m monitor.cli dashboard --out out/index.html
    python -m monitor.cli brief                     The briefing as markdown
    python -m monitor.cli digest   [--send]         Build (and optionally send)
    python -m monitor.cli alert    [--send]         Critical items only
    python -m monitor.cli forward  [--send]         Friday future business
    python -m monitor.cli search   "rent control"   Query the archive
    python -m monitor.cli rescore                   Re-apply a tuned taxonomy
    python -m monitor.cli stats                     Archive health
    python -m monitor.cli weeks                     List every week in the archive
    python -m monitor.cli week [2026-W29]           One week's business
    python -m monitor.cli snapshots                 Freeze a page per complete week
    python -m monitor.cli prune --source written_question   Remove a source
    python -m monitor.cli export --out data/archive.sql     Archive as SQL text
    python -m monitor.cli restore --from data/archive.sql   Rebuild from SQL

Scheduling is intentionally left to the host — cron, Task Scheduler, an Azure
Function timer or a GitHub Actions cron all work, and every one of them is
easier for NRLA's IT to reason about than a bespoke scheduler baked into the
application.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta

from . import alerts as alerts_mod
from .collectors.base import Fetcher
from .dashboard import render as render_dashboard
from .graph_auth import resolve_token
from .pipeline import Pipeline
from .relevance import Scorer, Taxonomy
from .store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _pipeline(args) -> tuple[Pipeline, Store, Taxonomy]:
    tax = Taxonomy.load(args.taxonomy)
    store = Store(args.db)
    fetcher = Fetcher(min_interval=args.interval)

    # The mailbox route is only attempted when a mailbox is named. A deployment
    # inside the NRLA network reaches gov.wales directly and should never talk
    # to Microsoft at all — asking it to would produce a confusing failure for
    # a feature that deployment does not use.
    mailbox = os.environ.get("MONITOR_MAILBOX", "")
    graph_token = ""
    if mailbox:
        graph_token, graph_error = resolve_token(os.environ, fetcher.session)
        if graph_error:
            # Printed, not raised. The reader is a policy officer looking at a
            # run log, and this sentence tells them what to do next.
            print(f"\n  Welsh Government mailbox route not available: "
                  f"{graph_error}\n")

    pipe = Pipeline(store, taxonomy=tax, fetcher=fetcher,
                    mailbox=mailbox,
                    graph_token=graph_token,
                    govwales_route=getattr(args, "govwales_route", None)
                    or os.environ.get("MONITOR_GOVWALES_ROUTE", "auto"))
    return pipe, store, tax


def cmd_collect(args) -> int:
    pipe, store, tax = _pipeline(args)
    report = pipe.run(lookback_days=args.days)

    print(f"\nRun {report.run_id} finished in {report.duration}")
    print(f"  collected {report.collected} · stored {report.stored} "
          f"· new {report.new_items}")
    for source, count in sorted(report.per_source.items(), key=lambda x: -x[1]):
        print(f"    {count:>5}  {source}")
    if report.errors:
        print(f"\n  {len(report.errors)} note(s):")
        for err in report.errors:
            print(f"    - {err}")
    if report.sources_substituted:
        print(f"\n  Expected-empty (covered by another route): "
              f"{', '.join(sorted(set(report.sources_substituted)))}")
    if report.sources_failed:
        print(f"\n  SOURCES THAT RETURNED NOTHING: "
              f"{', '.join(sorted(set(report.sources_failed)))}")
        print("  The dashboard will flag this run as incomplete.")
        if any("RSS" in s for s in report.sources_failed):
            print("  If this is the gov.wales RSS route on a cloud host, that is "
                  "expected: gov.wales blocks datacentre IPs regardless of "
                  "User-Agent. Re-run with --govwales-route mailbox to stop it "
                  "being reported as a failure.")

    if args.dashboard:
        _write_dashboard(store, tax, args.dashboard, report)
    store.close()
    return 0 if report.healthy else 2


def _write_dashboard(store: Store, tax: Taxonomy, path: str, report=None) -> None:
    items = store.query(min_score=float(tax.thresholds.get("dashboard_minimum", 25)))
    html_text = render_dashboard(
        items, tax, report=report, stats=store.stats(),
        # Real response windows: consultations, calls for evidence, answer dates.
        deadlines=store.upcoming_deadlines(60, exclude_kinds=["calendar"]),
        # Scheduled sittings, shown separately so they cannot bury the above.
        upcoming=store.upcoming_deadlines(60, include_kinds=["calendar"]))
    from pathlib import Path
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    print(f"\nDashboard written to {out} ({len(items)} items, "
          f"{out.stat().st_size / 1024:.0f} KB)")


def cmd_dashboard(args) -> int:
    tax = Taxonomy.load(args.taxonomy)
    store = Store(args.db)
    runs = store.last_runs(1)
    report = None
    if runs:
        from .pipeline import RunReport
        from datetime import datetime as dt
        r = runs[0]
        report = RunReport(
            run_id=r["run_id"],
            started_at=dt.fromisoformat(r["started_at"]),
            finished_at=dt.fromisoformat(r["finished_at"]),
            collected=r["collected"], stored=r["stored"],
            errors=r["errors"], sources_attempted=r["sources"],
            sources_failed=r.get("sources_failed", []),
            sources_substituted=r.get("sources_substituted", []))
    _write_dashboard(store, tax, args.out, report)
    store.close()
    return 0


def _last_report(store: Store):
    """Rebuild the last run's health report from the archive, or None."""
    runs = store.last_runs(1)
    if not runs:
        return None
    from .pipeline import RunReport
    from datetime import datetime as dt
    r = runs[0]
    return RunReport(
        run_id=r["run_id"],
        started_at=dt.fromisoformat(r["started_at"]),
        finished_at=dt.fromisoformat(r["finished_at"]),
        collected=r["collected"], stored=r["stored"],
        errors=r["errors"], sources_attempted=r["sources"],
        sources_failed=r.get("sources_failed", []),
        sources_substituted=r.get("sources_substituted", []))


def _briefing_markdown(args) -> str:
    """Render the briefing once, so every destination shows the same thing.

    `brief`, `publish` and the repository's BRIEFING.md all go through here. The
    alternative — each building its own view — is how a dashboard and an email
    end up disagreeing about what is outstanding.
    """
    from .brief import render_markdown
    tax = Taxonomy.load(args.taxonomy)
    store = Store(args.db)
    try:
        # Same inputs as the HTML dashboard, deliberately.
        items = store.query(
            min_score=float(tax.thresholds.get("dashboard_minimum", 25)))
        return render_markdown(
            items, tax, report=_last_report(store),
            # Scheduled sittings only — consultations already arrive via the
            # items, and mixing the sitting calendar into "Respond" is the bug
            # that buried a real deadline under a dozen routine meetings.
            upcoming=store.upcoming_deadlines(args.deadline_days,
                                              include_kinds=["calendar"]),
            dashboard_note=getattr(args, "note", ""),
            heading=getattr(args, "heading", ""))
    finally:
        store.close()


FORWARD_LOOK_REF = "mgWebService.asmx"


def _forward_look_failing(runs: list[dict]) -> bool:
    """Is the ModernGov forward look on business.senedd.wales failing?

    Read from the latest run's errors, which are prefixed with the source's
    label. The source is optional now that senedd.tv substitutes for it, so it
    no longer appears in `sources_failed` — but it still records why.
    """
    errors = (runs[0].get("errors") or []) if runs else []
    return any(str(e).startswith("Senedd forward look:") for e in errors)


def _without_unverifiable_diary(items: list, runs: list[dict]) -> list:
    """Drop diary entries that no working source can currently vouch for.

    The forward look last succeeded on 4 August 2026. Everything it collected
    then stayed in the archive and kept appearing on the page and in the
    Friday email as though current — including a Local Government, Housing and
    Planning Committee meeting on 24 September that neither senedd.tv nor the
    supplier's own briefing listed. A diary that cannot be refreshed is worse
    than a shorter one, because it is believed.

    Only while the forward look is failing. If business.senedd.wales lets the
    runners back in, its entries are current again and return on their own.
    """
    if not _forward_look_failing(runs):
        return items
    return [i for i in items
            if not (i.source_kind == "calendar"
                    and FORWARD_LOOK_REF in (i.raw_ref or ""))]


def _standing_gaps(runs: list[dict]) -> list[str]:
    """What this tool does not watch, by design — for the page's own panel.

    Standing limitations, not faults. A reader who can see the list can judge
    what the page is silent about; a reader who cannot will reasonably assume
    that an empty section means nothing happened.

    The mailbox gaps are listed only while the mailbox route is not running.
    `per_source` is not persisted in the runs table, but `sources` — the list
    of sources attempted — is, so that is what the presence test uses.
    """
    gaps = [
        "Written statements laid before the Senedd, unless they were also "
        "issued as a press announcement.",
        "Documents laid before the Senedd. There is no collector for the laid "
        "documents register yet; it is how the Council Tax Reduction Scheme "
        "consultation reached the supplier's briefing and not this page.",
        "Written questions. These are deliberately excluded — the team's "
        "dedicated Westminster and Senedd written-questions tool tracks them.",
    ]

    # The diary. Listed while the ModernGov forward look is failing, which the
    # run records as an error against its label — the source itself is now
    # optional, so it no longer appears in sources_failed.
    if _forward_look_failing(runs):
        gaps.insert(0,
                    "Committee meetings and Plenary business more than about a "
                    "week ahead. business.senedd.wales blocks this tool, so the "
                    "diary comes from senedd.tv, which lists the next five "
                    "sitting days and broadcast meetings only. The Friday "
                    "email's diary is correspondingly shorter than three weeks.")

    attempted = set(runs[0].get("sources") or []) if runs else set()
    covered = any("mailbox" in s.lower() or "consultations" in s.lower()
                  for s in attempted)
    if not covered:
        gaps.insert(0,
                    "The Welsh Government consultation register at "
                    "gov.wales/consultations, which holds the authoritative "
                    "closing dates. Announcements that launch a consultation "
                    "ARE captured from the Welsh Government newsroom, so most "
                    "are seen — but confirm the deadline on gov.wales before "
                    "relying on it.")
    return gaps


def cmd_site(args) -> int:
    """Build the hosted database page that GitHub Pages serves.

    The answer to "that is not a database at all — it's not even hosted on an
    actual page?". Everything before this was a file: gitignored HTML, a build
    artifact, a CI log page, a markdown document. This writes `docs/index.html`,
    which GitHub Pages publishes at a real URL, in the same house style as the
    other NRLA monitors.
    """
    from pathlib import Path
    from .site import render_site

    tax = Taxonomy.load(args.taxonomy)
    store = Store(args.db)
    try:
        # No score floor here: the page applies the strict relevance rule
        # itself (`Taxonomy.qualifies_for_site`), and that rule protects
        # low-scoring consultations that a raw score cut-off would drop.
        items = store.query(min_score=0, limit=5000)

        # A source that returns nothing is only good news if it is genuinely
        # quiet. gov.wales returns nothing because it blocks this host, and the
        # pipeline records that as "substituted" so the run is not permanently
        # red — which is right for the run, and wrong for the reader, who
        # cannot tell an empty section from an unmonitored one. Both failed and
        # substituted sources are named on the page.
        runs = store.last_runs(limit=1)
        items = _without_unverifiable_diary(items, runs)
        not_live: list[str] = []
        if runs:
            not_live = sorted(set(runs[0].get("sources_failed") or [])
                              | set(runs[0].get("sources_substituted") or []))
            # Recess is a real, expected silence and says so on its own.
            not_live = [s for s in not_live if "transcript" not in s.lower()]

        page = render_site(items, tax,
                           repo=os.environ.get("GITHUB_REPOSITORY", ""),
                           not_live=not_live,
                           gaps=_standing_gaps(runs))
    finally:
        store.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    # GitHub Pages runs Jekyll by default, which silently ignores files and
    # folders beginning with an underscore. Nothing here starts with one today,
    # but a future asset might, and the failure is invisible.
    (out.parent / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Site written to {out} ({len(items)} items, "
          f"{out.stat().st_size / 1024:.0f} KB)")
    return 0


def cmd_publish(args) -> int:
    """Publish the briefing somewhere a person will actually read it.

    THREE DESTINATIONS, NO CREDENTIALS
    ----------------------------------
    Every previous "here is where to read it" was somewhere nobody would go: a
    gitignored file, a zip inside a build artifact, then a CI log page whose URL
    changes every run. And the email needed five SMTP secrets that require an IT
    request, so it never arrived at all.

    This uses GITHUB_TOKEN, which GitHub injects into every workflow run. There
    is nothing to configure:

      BRIEFING.md            bookmarkable, always current, renders on private repos
      briefings/2026-Wnn.md  the permanent weekly record
      a GitHub issue         which GitHub emails to watchers — this is the digest
    """
    from pathlib import Path
    from . import publish as pub
    from .weekly import iso_week

    args.heading = "NRLA Senedd policy briefing"
    text = _briefing_markdown(args)
    written: list[str] = []

    if not args.issue_only:
        Path(args.file).write_text(text, encoding="utf-8")
        written.append(args.file)

        week_dir = Path(args.week_dir)
        week_dir.mkdir(parents=True, exist_ok=True)
        week_path = week_dir / f"{iso_week(date.today())}.md"
        week_path.write_text(text, encoding="utf-8")
        written.append(str(week_path))

        for path in written:
            print(f"Wrote {path}")

    if args.no_issue:
        print("Issue skipped (--no-issue).")
        return 0

    try:
        repo, token = pub.env_repo_and_token()
    except pub.GitHubError as error:
        # Not fatal: the two files above are already written and committed, so
        # the briefing is still readable. Only the email half is missing.
        print(f"Not opening an issue: {error}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("::warning title=Briefing not emailed::"
                  "The issue could not be opened, so no notification email was "
                  "sent. BRIEFING.md is still up to date.")
        return 0

    title = args.title or pub.issue_title()
    if args.assign == "none":
        assignees = []
    else:
        assignees = [a.strip() for a in
                     (args.assign or pub.default_assignee(repo)).split(",")
                     if a.strip()]

    body = (text + "\n\n---\n\n<sub>You are receiving this because this "
            "briefing is assigned to you. The same briefing is always at "
            f"[BRIEFING.md](https://github.com/{repo}/blob/main/"
            f"{args.file}), and every past one is under "
            f"[briefings/](https://github.com/{repo}/tree/main/"
            f"{args.week_dir}).</sub>\n")

    if args.dry_run:
        print(f"DRY RUN — would open an issue in {repo}:\n  {title}\n"
              f"  assigned to: {', '.join(assignees) or '(nobody)'}\n"
              f"  {len(body)} characters")
        return 0

    issue = pub.publish_issue(repo, token, title, body,
                              close_previous=not args.keep_previous,
                              assignees=assignees)
    print(f"Opened issue #{issue['number']}: {issue['html_url']}")

    if issue.get("assignment_failed"):
        # Say it out loud: an unassigned briefing may email nobody, which is the
        # silent-nothing failure this whole design exists to prevent.
        message = (f"Could not assign the briefing to "
                   f"{', '.join(assignees)} — it was opened unassigned, so it "
                   f"may not have emailed anyone. Check that the account has "
                   f"repository access, or use --assign.")
        print(f"WARNING: {message}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=Briefing not assigned::{message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Briefing published::{issue['html_url']}")
    return 0


def cmd_brief(args) -> int:
    """The briefing as markdown, for the GitHub job summary or a terminal.

    This is the answer to "the workflow went green but I have nothing to read".
    The HTML dashboard is gitignored and only reachable as a build artifact;
    markdown written to $GITHUB_STEP_SUMMARY appears on the run page itself.
    """
    text = _briefing_markdown(args)
    if args.out:
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Briefing written to {args.out} ({len(text)} characters)")
    else:
        print(text)
    # No store to close here: _briefing_markdown owns the connection and closes
    # it in a finally block. A leftover `store.close()` on this line shipped a
    # NameError that no test caught, because every test asserted what the
    # workflow *calls* and none of them actually called it. See
    # TestCommandsActuallyRun.
    return 0


def _email_config() -> dict:
    return {
        "sender": os.environ.get("MONITOR_FROM", "joshua.helm-cowley@nrla.org.uk"),
        "smtp_host": os.environ.get("MONITOR_SMTP_HOST", ""),
        "smtp_port": int(os.environ.get("MONITOR_SMTP_PORT", "587")),
        "username": os.environ.get("MONITOR_SMTP_USER", ""),
        "password": os.environ.get("MONITOR_SMTP_PASS", ""),
    }


def _report_not_sent(args, recipients: list[str], config: dict) -> int:
    """Say precisely why no email left the building, and fail if asked to send.

    WHY THIS IS LOUD
    ----------------
    The first live GitHub Actions run finished green having emailed nothing,
    printed "Not sent (dry run, or SMTP not configured)" into a log nobody
    opens, and exited 0. The operator's conclusion — that the tool did not
    work — was correct in every way that matters.

    A dry run must stay the default, so a mis-run script cannot mail a
    distribution list. But `--send` is an explicit instruction, and failing to
    carry it out is an error, not a quiet note. So: name the missing variable,
    emit a GitHub annotation that surfaces on the run page, and exit non-zero.
    """
    missing = []
    if not config["smtp_host"]:
        missing.append("MONITOR_SMTP_HOST")
    if not recipients:
        missing.append("MONITOR_TO")

    if not args.send:
        print("Dry run — nothing sent. Add --send to email it.")
        return 0

    detail = ", ".join(missing) if missing else "the SMTP server refused it"
    message = (f"--send was requested but no email could be sent: "
               f"{detail} not set. "
               f"Add the missing repository secrets under "
               f"Settings > Secrets and variables > Actions.")
    print(f"ERROR: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Surfaces as a red annotation at the top of the run page, where it
        # cannot be mistaken for a successful send.
        print(f"::error title=No email sent::{message}")
    return 3


def cmd_digest(args) -> int:
    pipe, store, tax = _pipeline(args)
    items = pipe.items_for_digest(since_days=args.days)
    # The digest's "Closing soon" table is for things needing a response, not
    # for the sitting calendar.
    deadlines = store.upcoming_deadlines(args.deadline_days,
                                         exclude_kinds=["calendar"])

    subject, html_body, text_body = alerts_mod.render_digest(
        items, deadlines, tax,
        period_label=f"the last {args.days} days",
        dashboard_url=args.dashboard_url)

    if args.out:
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(html_body, encoding="utf-8")
        print(f"Digest written to {args.out}")

    print(f"\nSubject: {subject}")
    print(f"Items: {len(items)} · deadlines: {len(deadlines)}")

    config = _email_config()
    recipients = [r.strip() for r in (args.to or
                  os.environ.get("MONITOR_TO", "")).split(",") if r.strip()]
    sent = alerts_mod.send(subject, html_body, text_body,
                           recipients=recipients, dry_run=not args.send,
                           **config)
    store.close()
    if sent:
        print(f"Sent to {len(recipients)} "
              f"{'recipient' if len(recipients) == 1 else 'recipients'}.")
        return 0
    return _report_not_sent(args, recipients, config)


def cmd_alert(args) -> int:
    pipe, store, tax = _pipeline(args)
    items = pipe.items_for_alert()
    if not items:
        print("No unnotified Critical items. Nothing to alert on.")
        store.close()
        return 0

    subject, html_body, text_body = alerts_mod.render_alert(items, tax)
    print(f"Subject: {subject}")
    for item in items:
        print(f"  [{item.score:>6.1f}] {item.title[:88]}")

    if args.out:
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(html_body, encoding="utf-8")
        print(f"Alert written to {args.out}")

    config = _email_config()
    recipients = [r.strip() for r in (args.to or
                  os.environ.get("MONITOR_TO", "")).split(",") if r.strip()]
    sent = alerts_mod.send(subject, html_body, text_body,
                           recipients=recipients, dry_run=not args.send,
                           **config)

    if sent:
        store.mark_notified([i.uid for i in items])
        print("Sent, and items marked as notified.")
        store.close()
        return 0

    # Items deliberately stay unnotified, so a later successful run still
    # reports them. A failed alert must not silently consume its own contents.
    print("Items remain unnotified — they will be included in the next "
          "successful alert.")
    store.close()
    return _report_not_sent(args, recipients, config)


def cmd_forward(args) -> int:
    """The Friday future-business email — what is scheduled, not what happened.

    Replaces the forward-look half of the Camlas weekly briefing. Delivered
    through a Power Automate flow rather than SMTP, so there is no credential
    anywhere: the script POSTs a subject and a body, and the operator's own
    flow sends the mail. See FORWARD-EMAIL-SETUP.md.
    """
    from pathlib import Path
    from .forward import last_friday, render_forward, select_business

    tax = Taxonomy.load(args.taxonomy)
    store = Store(args.db)
    try:
        # No score floor: the page's strict rule is applied inside
        # select_business, and it deliberately rescues low-scoring
        # consultations that a raw cut-off would drop.
        items = _without_unverifiable_diary(
            store.query(min_score=0, limit=5000), store.last_runs(limit=1))
        today = date.today()
        since = (date.fromisoformat(args.new_since) if args.new_since
                 else last_friday(today))
        sections = select_business(items, tax, today=today,
                                   weeks_ahead=args.weeks)
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        page_url = (f"https://{repo.split('/')[0].lower()}.github.io/"
                    f"{repo.split('/')[1]}/") if "/" in repo else ""
        review = None
        if not getattr(args, "no_review", False):
            # The week in review: this week's Records and Welsh Government
            # notices, read live. A failure here must not cost the team the
            # future-business half, so it is caught and reported.
            from .weekly_review import build as build_review, render_review
            try:
                wr = build_review(today, tax,
                                  Fetcher(min_interval=getattr(args, "interval", 1.5)),
                                  news_state=getattr(args, "news_state", ""))
                review = render_review(wr)
                print(f"Week in review: {len(wr.entries)} entries, "
                      f"{len(wr.changes)} political changes, "
                      f"{'sat' if wr.sat else 'did not sit'}"
                      + (f"; awaiting {'; '.join(wr.pending)}" if wr.pending else ""))
            except Exception as exc:        # noqa: BLE001
                print(f"Week in review could not be built: {exc}")
                _summary_note("WARNING", f"**The week in review could not be "
                              f"built**, so this Friday email has future "
                              f"business only. {exc}")
        subject, html_body, count = render_forward(
            sections, tax, today=today, new_since=since, page_url=page_url,
            review=review)
    finally:
        store.close()

    print(f"Subject: {subject}")
    for name, rows in sections.items():
        print(f"  {len(rows):>3}  {name}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_body, encoding="utf-8")
        print(f"Written to {out}")

    flow_url = os.environ.get("MONITOR_FLOW_URL", "")
    sent, message = alerts_mod.post_to_flow(
        flow_url, subject, html_body, count, dry_run=not args.send)
    print(message)

    if sent or count == 0 or not args.send:
        return 0

    # Not configured yet is not a failure. The same reasoning as the SMTP guard
    # in the daily workflow: a repository where the feature has not been turned
    # on must not go red every week, because a warning that is always on is a
    # warning nobody reads — and the one week it means something is the week it
    # gets ignored.
    if not flow_url:
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write("\n> [!NOTE]\n> **The Friday future-business email is "
                         "not switched on yet.** Five minutes of setup, no "
                         "Azure and no IT: see `FORWARD-EMAIL-SETUP.md`.\n")
        return 0

    # Configured, asked to send, and it did not go. That IS a real failure and
    # the run must say so — silence here is how a weekly email stops arriving
    # without anyone noticing for a month.
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"\n> [!WARNING]\n> **The Friday future-business email "
                     f"was not sent.** {message}\n")
    return 2


def cmd_morning(args) -> int:
    """The morning briefing — what the Senedd is doing today.

    Replaces the supplier's "Bore da" email. Started at 07.30 London time by a
    Power Automate flow rather than by GitHub's timer, which ran five to six
    hours late on every day of September 2026. Delivered through the same
    Power Automate HTTP flow as the Friday email. See MORNING-BRIEFING-SETUP.md.
    """
    from pathlib import Path
    from .collectors.govwales import GovWalesNewsroomCollector, GovWalesRSSCollector
    from .collectors.seneddtv import SeneddTVScheduleCollector
    from .morning import (build, london_today, merge_announcements,
                          render_morning, window_start)

    tax = Taxonomy.load(args.taxonomy)
    today = date.fromisoformat(args.date) if args.date else london_today()
    fetcher = Fetcher(min_interval=args.interval)

    tv = SeneddTVScheduleCollector(fetcher)
    meetings = tv.meetings()
    for err in tv.errors:
        print(f"  senedd.tv: {err}")

    if not meetings and tv.errors:
        # Cannot tell "not sitting" from "cannot see". Saying nothing would be
        # the quiet failure this whole tool is built to avoid, so the run goes
        # red, which is what makes GitHub tell its owner.
        print("\nsenedd.tv could not be read, so there is no way to know whether "
              "the Senedd is sitting today. Nothing sent.")
        _summary_note("WARNING", "**No morning briefing today: senedd.tv could "
                      "not be read.** " + " ".join(tv.errors))
        return 2

    todays = [m for m in meetings if m.when == today]
    if not todays:
        print(f"No broadcast Senedd business on {today:%A %-d %B} — the Senedd is "
              "not sitting, so nothing is sent. This is deliberate.")
        return 0

    recent = SeneddTVScheduleCollector.parse_recent_dates(tv.home_html)
    since = window_start(today, recent)
    # Two sources, because neither is complete. The gov.wales feed is the only
    # one with written statements; the newsroom has press notices the feed
    # sometimes lacks, and a short summary line. Merged by title.
    feed = GovWalesRSSCollector(fetcher)
    newsroom = GovWalesNewsroomCollector(fetcher)
    news = merge_announcements(
        feed.recent(since, max_articles=args.max_articles),
        newsroom.recent(since, max_articles=args.max_articles))

    store = Store(args.db)
    try:
        questions = [i for i in store.query(min_score=0, limit=5000)
                     if i.source_kind == "oral_question" and i.deadline == today]
    finally:
        store.close()

    briefing = build(meetings, news, questions, tax, today=today,
                     recent_sittings=recent)
    if feed.errors and newsroom.errors:
        briefing.notes.append(
            "Neither the gov.wales feed nor the Welsh Government newsroom could "
            "be read this morning, so the Welsh Government section is empty.")
    elif feed.errors:
        briefing.notes.append(
            "The gov.wales announcements feed could not be read this morning, "
            "so written statements may be missing from the Welsh Government "
            "section.")
    elif newsroom.errors:
        briefing.notes.append(
            "The Welsh Government newsroom could not be read this morning, so "
            "the Welsh Government section may be incomplete.")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    page_url = (f"https://{repo.split('/')[0].lower()}.github.io/"
                f"{repo.split('/')[1]}/") if "/" in repo else ""
    subject, html_body, count = render_morning(briefing, page_url=page_url)

    print(f"Subject: {subject}")
    print(f"  {len(briefing.today_blocks):>3}  meetings today")
    print(f"  {len(briefing.announcements):>3}  Welsh Government announcements "
          f"since {since:%a %d %b %H:%M} UTC")
    print(f"  {len(briefing.next_blocks):>3}  meetings on "
          f"{briefing.next_day or 'the next sitting day'}")
    print(f"  {briefing.marked_count:>3}  marked NRLA")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_body, encoding="utf-8")
        print(f"Written to {out}")

    flow_url = os.environ.get("MONITOR_FLOW_URL", "")
    sent, message = alerts_mod.post_to_flow(
        flow_url, subject, html_body, count, dry_run=not args.send)
    print(message)

    if sent or not args.send:
        return 0
    if not flow_url:
        _summary_note("NOTE", "**The morning briefing is not switched on yet.** "
                      "It uses the same Power Automate flow as the Friday "
                      "email: see `MORNING-BRIEFING-SETUP.md`.")
        return 0
    _summary_note("WARNING", f"**The morning briefing was not sent.** {message}")
    return 2


def cmd_debates(args) -> int:
    """Debate summaries — what was said in the Senedd, the morning after.

    Replaces the supplier's post-debate notes. Runs straight after the
    morning briefing (debates.yml is started by it finishing), reads the
    draft Record of every meeting that has happened and not yet been done,
    and sends one email of the relevant debates. See DEBATE-SUMMARIES.md.
    """
    from pathlib import Path
    from .collectors.record_html import RecordPageCollector
    from .collectors.record_transcripts import RecordTranscriptCollector
    from .collectors.seneddtv import SeneddTVScheduleCollector
    from .debates import (Relevance, candidates, load_state, render_debates,
                          save_state, select, state_baseline, summarise)
    from .morning import london_today

    tax = Taxonomy.load(args.taxonomy)
    today = date.fromisoformat(args.date) if args.date else london_today()
    fetcher = Fetcher(min_interval=args.interval)
    state = load_state(args.state)
    done = set(state["meetings"])

    tv = SeneddTVScheduleCollector(fetcher)
    tv.schedule()
    recent = SeneddTVScheduleCollector.parse_recent_meetings(tv.home_html)
    recent = [tv.fill(m) for m in recent
              if m.when and m.when < today and (today - m.when).days <= args.lookback]
    listing = RecordTranscriptCollector(fetcher, taxonomy=tax).list_meetings()
    todo = candidates(recent, listing, today, done, state_baseline(state),
                      lookback=args.lookback)
    if not todo:
        print("No meetings waiting to be summarised — nothing to do.")
        return 0

    pages = RecordPageCollector(fetcher)
    rel = Relevance(tax)
    found, pending, finished = [], [], {}
    for cand in todo:
        record = pages.record(cand.meeting_id, cand.forum)
        if record is None or not record.published:
            print(f"  waiting   {cand.label} — Record not published yet")
            pending.append(cand.label)
            continue
        chosen = select(record, rel, cand.when)
        for d in chosen:
            d.papers_url = cand.papers_url
        print(f"  read      {cand.label} — {len(record.items)} items, "
              f"{len(chosen)} relevant")
        found.extend(chosen)
        finished[cand.meeting_id] = {
            "date": cand.when.isoformat() if cand.when else "",
            "forum": cand.forum, "done": today.isoformat(),
            "relevant": len(chosen)}
    for err in tv.errors + pages.errors:
        print(f"  note: {err}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    print(f"Summarising {len(found)} item(s) with "
          f"{'AI summaries (Claude)' if api_key else 'key sentences (no API key set)'}")
    debates = summarise(found, tax, api_key=api_key)

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    page_url = (f"https://{repo.split('/')[0].lower()}.github.io/"
                f"{repo.split('/')[1]}/") if "/" in repo else ""
    subject, html_body, count = render_debates(debates, pending, page_url=page_url)
    if count:
        print(f"Subject: {subject}")
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html_body, encoding="utf-8")
            print(f"Written to {out}")

    flow_url = os.environ.get("MONITOR_FLOW_URL", "")
    sent, message = alerts_mod.post_to_flow(
        flow_url, subject, html_body, count, dry_run=not args.send)
    if count == 0:
        message = ("Nothing relevant in the meetings read — no email sent, "
                   "deliberately.")
    print(message)

    # Remember a meeting only once its summary has gone (or there was nothing
    # to send). If the email failed, tomorrow's run tries the same meetings.
    if args.send and (sent or count == 0) or args.remember:
        state["meetings"].update(finished)
        save_state(state, today, args.state)
        print(f"Recorded {len(finished)} meeting(s) as done in {args.state}")

    if sent or not args.send or count == 0:
        return 0
    if not flow_url:
        _summary_note("NOTE", "**Debate summaries are not switched on yet.** "
                      "They use the same Power Automate flow as the Friday "
                      "email: see `FORWARD-EMAIL-SETUP.md`.")
        return 0
    _summary_note("WARNING", f"**The debate summaries were not sent.** {message}")
    return 2


def cmd_news(args) -> int:
    """Political news and press release alerts — an email when something big
    happens, or the Welsh Government publishes something relevant.

    Started every hour in office hours by Power Automate (news.yml). The two
    parts are independent: if the news feeds are down, press releases still
    go out, and the reverse. See NEWS-ALERTS-SETUP.md.
    """
    from datetime import datetime as _dt
    from .news import load_state, save_state

    now = _dt.utcnow().replace(microsecond=0)
    state = load_state(args.state)
    fetcher = Fetcher(min_interval=args.interval)
    codes = [_news_part(args, state, now, fetcher),
             _press_part(args, state, now, fetcher)]
    if not args.test:
        save_state(state, args.state)
    return max(codes)


def _write_out(path: str, html_body: str) -> None:
    from pathlib import Path
    if path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_body, encoding="utf-8")


def _send(args, subject: str, html_body: str, count: int, what: str) -> tuple[bool, int]:
    """Post one email; returns (sent, exit code)."""
    if count == 0:
        print(f"{what}: nothing new — no email sent, deliberately.")
        return False, 0
    flow_url = os.environ.get("MONITOR_FLOW_URL", "")
    sent, message = alerts_mod.post_to_flow(flow_url, subject, html_body, count,
                                            dry_run=not args.send)
    print(message)
    if sent or not args.send:
        return sent, 0
    if not flow_url:
        _summary_note("NOTE", f"**{what} are not switched on yet.** They use the "
                      "same Power Automate flow as the Friday email.")
        return False, 0
    _summary_note("WARNING", f"**The {what.lower()} email was not sent.** {message}")
    return False, 2


def _news_part(args, state: dict, now, fetcher) -> int:
    from .collectors.news import FEEDS, NewsCollector
    from .news import (minister_change, new_stories, remember, render_news,
                       test_stories)

    source = NewsCollector(fetcher)
    headlines = source.headlines()
    ministers = source.ministers()
    for err in source.errors:
        print(f"  note: {err}")

    if not headlines and len(source.errors) >= len(FEEDS):
        print("None of the news feeds could be read, so nothing can be said "
              "about today's news.")
        _summary_note("WARNING", "**News alerts: none of the news feeds could "
                      "be read.** " + " ".join(source.errors))
        return 2

    if args.test:
        # Proves the whole path — feeds, Power Automate, the mailbox — without
        # touching the memory, so a test can never swallow a real alert.
        stories = test_stories(headlines, now)
        subject, html_body, count = render_news(stories, None, test=True)
        if not count:
            print("Test: no political changes in the feeds from the last "
                  "fortnight, so no test news alert can be built.")
            return 0
        _write_out(args.out, html_body)
        print(f"Test subject: {subject}")
        return _send(args, subject, html_body, count, "News alerts")[1]

    if not state.get("initialised"):
        # The first run learns what is already out there, so it does not send
        # an alert about every story of the past week.
        remember(state, headlines, [], ministers, now)
        print(f"News, first run: noted {len(headlines)} current headlines and "
              f"{len(ministers or {})} ministers. Nothing sent — alerts start "
              "from the next run.")
        return 0

    stories = new_stories(headlines, state, now)
    change = minister_change(state, ministers)
    print(f"{len(headlines)} headlines read, {len(stories)} new political "
          f"change(s){', ministers changed' if change else ''}.")
    for story in stories:
        print(f"  {story.label}: {story.first.title} ({len(story.matches)} outlet(s))")

    subject, html_body, count = render_news(stories, change)
    if count:
        _write_out(args.out, html_body)
        print(f"Subject: {subject}")
    sent, code = _send(args, subject, html_body, count, "News alerts")

    # Remember what was read only once any alert about it has gone, so a
    # failed send is retried on the next run rather than lost.
    if (args.send and (sent or count == 0)) or args.remember:
        remember(state, headlines, stories, ministers, now)
    return code


def _press_part(args, state: dict, now, fetcher) -> int:
    from .collectors.govwales import GovWalesNewsroomCollector, GovWalesRSSCollector
    from .morning import Marker, merge_announcements
    from .press import look_since, remember, render_press, select

    since = now - timedelta(days=14) if args.test else look_since(state, now)
    room = GovWalesNewsroomCollector(fetcher)
    feed = GovWalesRSSCollector(fetcher)
    # Newsroom first: its notices carry the bullet-point summary Camlas quote.
    # The feed adds written statements, which the newsroom does not publish.
    announcements = merge_announcements(room.recent(since, max_articles=20),
                                        feed.recent(since, max_articles=20))
    errors = room.errors + feed.errors
    for err in errors:
        print(f"  note: {err}")
    if room.errors and feed.errors:
        _summary_note("WARNING", "**Press release alerts: the Welsh Government "
                      "could not be read.** " + " ".join(errors))
        return 2

    marker = Marker(Taxonomy.load(args.taxonomy))
    if args.test:
        releases = select(announcements, marker, state, test=True)
        subject, html_body, count = render_press(releases, test=True)
        if not count:
            print("Test: no relevant Welsh Government notices in the last "
                  "fortnight, so no test press alert can be built.")
            return 0
        _write_out(args.press_out, html_body)
        print(f"Test subject: {subject}")
        return _send(args, subject, html_body, count, "Press release alerts")[1]

    if not state.get("press_checked"):
        remember(state, announcements, now)
        print(f"Press releases, first run: noted {len(announcements)} recent "
              "Welsh Government notices. Nothing sent — alerts start from the "
              "next run.")
        return 0

    releases = select(announcements, marker, state)
    print(f"{len(announcements)} Welsh Government notices since "
          f"{since:%a %d %b %H:%M} UTC, {len(releases)} new and relevant.")
    for r in releases:
        print(f"  {r.label}: {r.item.title}")
    subject, html_body, count = render_press(releases)
    if count:
        _write_out(args.press_out, html_body)
        print(f"Subject: {subject}")
    sent, code = _send(args, subject, html_body, count, "Press release alerts")
    if (args.send and (sent or count == 0)) or args.remember:
        remember(state, announcements, now)
    return code


def _summary_note(kind: str, text: str) -> None:
    """Annotate the Actions run page, where 'did it go?' gets asked."""
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"\n> [!{kind}]\n> {text}\n")


def cmd_search(args) -> int:
    store = Store(args.db)
    results = store.search(args.expression, limit=args.limit)
    print(f"{len(results)} result(s) for {args.expression!r}\n")
    for item in results:
        when = item.item_date.strftime("%d %b %Y") if item.item_date else "undated"
        print(f"[{item.score:>6.1f}] {item.band:<8} {when}  {item.title[:70]}")
        if item.speaker:
            print(f"           {item.speaker} · {item.forum}")
        print(f"           {item.excerpt[:150]}")
        if item.url:
            print(f"           {item.url}")
        print()
    store.close()
    return 0


def cmd_rescore(args) -> int:
    tax = Taxonomy.load(args.taxonomy)
    store = Store(args.db)
    changed = store.rescore_all(Scorer(tax))
    print(f"Re-scored the archive. {changed} item(s) changed band or score.")
    stats = store.stats()
    for band, count in sorted(stats["bands"].items()):
        print(f"  {band:<10} {count}")
    store.close()
    return 0


def cmd_weeks(args) -> int:
    """List every week in the archive. The index for the weekly record."""
    from .weekly import week_index
    store = Store(args.db)
    weeks = week_index(store, min_score=args.min_score)
    if not weeks:
        print("No dated items in the archive yet. Run `collect` first.")
        store.close()
        return 0
    print(f"{len(weeks)} week(s) in the archive\n")
    print(f"{'Week':<11} {'Dates':<26} {'Items':>6} {'Crit':>5} {'High':>5} "
          f"{'Cons':>5} {'Closing':>8}")
    print("-" * 74)
    for w in weeks[:args.limit]:
        dates = f"{w['starts'].strftime('%d %b')} – {w['ends'].strftime('%d %b %Y')}"
        print(f"{w['label']:<11} {dates:<26} {w['items']:>6} {w['critical']:>5} "
              f"{w['high']:>5} {w['consultations']:>5} {w['closing']:>8}")
    store.close()
    return 0


def cmd_week(args) -> int:
    """One week's business, and optionally its permanent snapshot."""
    from .weekly import current_week, previous_week, week_summary, write_week_snapshot
    tax = Taxonomy.load(args.taxonomy)
    store = Store(args.db)

    label = args.week
    if label in (None, "current"):
        label = current_week()
    elif label == "last":
        label = previous_week(current_week())

    try:
        summary = week_summary(store, label, tax, min_score=args.min_score)
    except (ValueError, IndexError):
        print(f"Could not read '{label}'. Use the ISO form, e.g. 2026-W29, "
              f"or 'current' / 'last'.")
        store.close()
        return 1

    print(f"\n{summary['title']}  ({label})")
    print("=" * 62)
    print(f"  {len(summary['items'])} items · {len(summary['critical'])} critical "
          f"· {len(summary['high'])} high · "
          f"{len(summary['consultations'])} consultation(s)")
    if summary["sitting_days"]:
        print("  Sitting days: " + ", ".join(
            d.strftime("%a %d %b") for d in summary["sitting_days"]))
    else:
        print("  No sitting days in this week (recess, or no records published).")

    if summary["closed_this_week"]:
        print("\n  WINDOWS CLOSING THIS WEEK")
        for item in summary["closed_this_week"]:
            print(f"    {item.deadline}  {item.title[:62]}")

    for tier in sorted(summary["by_tier"]):
        entries = sorted(summary["by_tier"][tier], key=lambda i: -i.score)
        print(f"\n  {tier.upper()}  ({len(entries)})")
        for item in entries[:8]:
            who = item.speaker or item.source_name
            print(f"    [{item.band:<8}] {who[:22]:<22} {item.title[:52]}")

    if not summary["items"]:
        print("\n  Nothing recorded in this week. During recess that is the "
              "correct record of a quiet week, not a gap in the data.")

    if args.out or args.snapshot:
        path = write_week_snapshot(store, label, tax,
                                   out_dir=args.out or "out/weeks",
                                   min_score=args.min_score)
        print(f"\n  Snapshot written to {path}")

    store.close()
    return 0


def cmd_snapshots(args) -> int:
    """Freeze a permanent page for every complete week in the archive."""
    from .weekly import backfill_snapshots
    tax = Taxonomy.load(args.taxonomy)
    store = Store(args.db)
    written = backfill_snapshots(store, tax, out_dir=args.out,
                                 min_score=args.min_score)
    print(f"Wrote {len(written)} weekly snapshot(s) to {args.out}")
    for path in written[:12]:
        print(f"  {path}")
    if len(written) > 12:
        print(f"  ... and {len(written) - 12} more")
    print("\nThe current, incomplete week is skipped on purpose: freezing it "
          "would create a permanent record that is wrong by Friday.")
    store.close()
    return 0


FIXTURE_MARKER = "FIXTURE"


def cmd_prune(args) -> int:
    """Remove items from the archive: a whole source kind, or demo fixtures.

    For overlap with other NRLA tools. Switching a source off in taxonomy.yaml
    stops new items arriving; this clears out what is already there.

    `--fixtures` exists because demonstration data reached the live page and
    was presented as real. `tools/load_govwales_fixture.py` writes Welsh
    Government sample notifications into the archive so the mailbox parser can
    be exercised without a Microsoft Graph tenant. Those rows were committed
    into data/archive.sql and then displayed for weeks as open consultations,
    complete with a deadline countdown — indistinguishable from live data. A
    wrong deadline is worse than a missing one, so they come out.
    """
    store = Store(args.db)

    if getattr(args, "fixtures", False):
        before = store.conn.execute(
            "SELECT COUNT(*) FROM items WHERE raw_ref LIKE ?",
            (f"%{FIXTURE_MARKER}%",)).fetchone()[0]
        if before == 0:
            print("No fixture-sourced items in the archive.")
            store.close()
            return 0
        if not args.yes:
            print(f"{before} fixture-sourced item(s) would be permanently "
                  f"removed.\nRe-run with --yes to go ahead.")
            store.close()
            return 0
        for row in store.conn.execute(
                "SELECT title, raw_ref FROM items WHERE raw_ref LIKE ?",
                (f"%{FIXTURE_MARKER}%",)):
            print(f"  removing {row[0][:64]}  ({row[1]})")
        store.conn.execute("DELETE FROM items WHERE raw_ref LIKE ?",
                           (f"%{FIXTURE_MARKER}%",))
        store.conn.commit()
        print(f"Removed {before} fixture-sourced item(s).")
        return_code = 0
        store.close()
        return return_code
    before = store.conn.execute(
        "SELECT COUNT(*) FROM items WHERE source_kind = ?",
        (args.source,)).fetchone()[0]
    if before == 0:
        print(f"No items with source_kind '{args.source}' in the archive.")
        store.close()
        return 0

    if not args.yes:
        print(f"{before} item(s) with source_kind '{args.source}' would be "
              f"permanently removed.\nRe-run with --yes to go ahead.")
        store.close()
        return 0

    store.conn.execute("DELETE FROM items WHERE source_kind = ?", (args.source,))
    store.conn.commit()
    print(f"Removed {before} item(s) of source_kind '{args.source}'.")
    print("Switch the source off in taxonomy.yaml too, or the next run will "
          "collect them again.")
    store.close()
    return 0


def cmd_export(args) -> int:
    """Write the archive as plain SQL text, for git-hosted state."""
    from .archive_io import export_sql
    store = Store(args.db)
    path, rows = export_sql(store, args.out)
    size = path.stat().st_size / 1024
    print(f"Exported {rows} row(s) to {path} ({size:.0f} KB)")
    print("Plain SQL, deterministically ordered: git deltas it efficiently, the "
          "diff is readable, and an unchanged archive produces no commit.")
    store.close()
    return 0


def cmd_restore(args) -> int:
    """Rebuild the database from a SQL export, search index included."""
    from .archive_io import restore_sql
    try:
        rows, indexed = restore_sql(args.source, args.db, replace=not args.append)
    except FileNotFoundError as exc:
        print(f"{exc}\nOn a first run this is expected — the archive starts empty.")
        return 0
    print(f"Restored {rows} item(s); {indexed} indexed for search.")
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    stats = store.stats()
    print(f"Archive: {stats['total']} items, "
          f"{stats['earliest']} to {stats['latest']}\n")
    print("By priority band:")
    for band, count in sorted(stats["bands"].items(), key=lambda x: -x[1]):
        print(f"  {band or '(none)':<12} {count}")
    print("\nBy source:")
    for kind, count in sorted(stats["sources"].items(), key=lambda x: -x[1]):
        print(f"  {kind:<24} {count}")
    print("\nUpcoming deadlines:")
    for item in store.upcoming_deadlines(60)[:15]:
        days = (item.deadline - date.today()).days
        print(f"  {item.deadline}  ({days:>3}d)  {item.title[:66]}")
    print("\nRecent runs:")
    for run in store.last_runs(5):
        flag = "OK " if not run["errors"] else f"{len(run['errors'])} note(s)"
        print(f"  {run['started_at'][:16]}  collected {run['collected']:<5} "
              f"stored {run['stored']:<5} {flag}")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="monitor", description="NRLA Senedd policy monitor")
    parser.add_argument("--db", default="data/monitor.sqlite3")
    parser.add_argument("--taxonomy", default=None)
    parser.add_argument("--interval", type=float, default=1.5,
                        help="minimum seconds between requests to a host")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--govwales-route", choices=["auto", "mailbox", "rss"], default=None,
        help="how gov.wales content reaches this deployment. Use 'mailbox' when "
             "running anywhere gov.wales blocks (any cloud host), so a failed RSS "
             "fetch is not reported as a broken source. Default: auto.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="fetch, score and store")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--dashboard", default=None,
                   help="also write the dashboard to this path")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("dashboard", help="rebuild the dashboard from the archive")
    p.add_argument("--out", default="out/index.html")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("brief",
                       help="the briefing as markdown (for the Actions run page)")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--deadline-days", type=int, default=60)
    p.add_argument("--out", default="",
                   help="write to a file; default prints to stdout so it can "
                        "be piped into $GITHUB_STEP_SUMMARY")
    p.add_argument("--note", default="",
                   help="one line appended at the foot, e.g. where to find "
                        "the full dashboard")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("site",
                       help="build docs/index.html, the page GitHub Pages hosts")
    p.add_argument("--out", default="docs/index.html")
    p.set_defaults(func=cmd_site)

    p = sub.add_parser(
        "publish",
        help="write BRIEFING.md and open the briefing issue GitHub emails")
    p.add_argument("--days", type=int, default=21)
    p.add_argument("--deadline-days", type=int, default=60)
    p.add_argument("--file", default="BRIEFING.md",
                   help="the always-current bookmarkable page")
    p.add_argument("--week-dir", default="briefings",
                   help="permanent per-week copies")
    p.add_argument("--title", default="",
                   help="issue title; defaults to today's date")
    p.add_argument("--note", default="")
    p.add_argument("--no-issue", action="store_true",
                   help="write the files but do not open an issue")
    p.add_argument("--issue-only", action="store_true",
                   help="open the issue but do not write the files")
    p.add_argument("--assign", default="",
                   help="GitHub username(s) to assign, comma-separated. "
                        "GitHub always emails an assignee regardless of their "
                        "watch setting, which is why this is the delivery "
                        "mechanism. Defaults to the repository owner; "
                        "'none' to assign nobody.")
    p.add_argument("--keep-previous", action="store_true",
                   help="do not close the previous briefing issue")
    p.add_argument("--dry-run", action="store_true",
                   help="say what would be opened, and open nothing")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("digest", help="build the periodic digest")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--deadline-days", type=int, default=60)
    p.add_argument("--out", default="out/digest.html")
    p.add_argument("--to", default="")
    p.add_argument("--dashboard-url", default="")
    p.add_argument("--send", action="store_true",
                   help="actually send the email (default is dry run)")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("alert", help="alert on unnotified Critical items")
    p.add_argument("--out", default="out/alert.html")
    p.add_argument("--to", default="")
    p.add_argument("--send", action="store_true")
    p.set_defaults(func=cmd_alert)

    p = sub.add_parser("forward",
                       help="the Friday future-business email")
    p.add_argument("--out", default="out/forward.html")
    p.add_argument("--weeks", type=int, default=3,
                   help="how far ahead the diary sections look")
    p.add_argument("--new-since", default="",
                   help="ISO date; anything collected on or after it is "
                        "tagged NEW. Defaults to the previous Friday.")
    p.add_argument("--send", action="store_true",
                   help="actually POST to the Power Automate flow")
    p.add_argument("--no-review", action="store_true",
                   help="future business only, without the week in review")
    p.add_argument("--news-state", default="data/news-state.json",
                   help="the news alerts' memory, for this week's political changes")
    p.set_defaults(func=cmd_forward)

    p = sub.add_parser("morning",
                       help="the morning briefing, on sitting days")
    p.add_argument("--out", default="out/morning.html")
    p.add_argument("--date", default="",
                   help="ISO date to brief on, for testing. Defaults to today "
                        "in London.")
    p.add_argument("--max-articles", type=int, default=30,
                   help="cap on Welsh Government notices read in full")
    p.add_argument("--send", action="store_true",
                   help="actually POST to the Power Automate flow")
    p.set_defaults(func=cmd_morning)

    p = sub.add_parser("debates",
                       help="summaries of relevant debates, the morning after")
    p.add_argument("--out", default="out/debates.html")
    p.add_argument("--date", default="",
                   help="ISO date to run as, for testing. Defaults to today "
                        "in London.")
    p.add_argument("--state", default="data/debates-sent.json",
                   help="which meetings have already been summarised")
    p.add_argument("--lookback", type=int, default=10,
                   help="days back to look for meetings")
    p.add_argument("--remember", action="store_true",
                   help="record the meetings as done even without --send")
    p.add_argument("--send", action="store_true",
                   help="actually POST to the Power Automate flow")
    p.set_defaults(func=cmd_debates)

    p = sub.add_parser("news", help="political news and press release alerts")
    p.add_argument("--out", default="out/news.html")
    p.add_argument("--press-out", default="out/press.html")
    p.add_argument("--state", default="data/news-state.json")
    p.add_argument("--remember", action="store_true",
                   help="record what was read even without --send")
    p.add_argument("--test", action="store_true",
                   help="send a labelled test alert of the latest political "
                        "changes; remembers nothing")
    p.add_argument("--send", action="store_true",
                   help="actually POST to the Power Automate flow")
    p.set_defaults(func=cmd_news)

    p = sub.add_parser("search", help="full-text search the archive")
    p.add_argument("expression")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("rescore", help="re-apply the taxonomy to the archive")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("stats", help="archive and run health")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("weeks", help="list every week in the archive")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--min-score", type=float, default=25)
    p.set_defaults(func=cmd_weeks)

    p = sub.add_parser("week", help="one week's business")
    p.add_argument("week", nargs="?", default="last",
                   help="ISO week (2026-W29), or 'current' / 'last'. "
                        "Default: last complete week.")
    p.add_argument("--min-score", type=float, default=25)
    p.add_argument("--snapshot", action="store_true",
                   help="also write the permanent HTML snapshot")
    p.add_argument("--out", default=None, help="snapshot directory")
    p.set_defaults(func=cmd_week)

    p = sub.add_parser("snapshots",
                       help="freeze a page per complete week (backfill)")
    p.add_argument("--out", default="out/weeks")
    p.add_argument("--min-score", type=float, default=25)
    p.set_defaults(func=cmd_snapshots)

    p = sub.add_parser("export", help="write the archive as SQL text")
    p.add_argument("--out", default="data/archive.sql")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("restore", help="rebuild the database from a SQL export")
    p.add_argument("--from", dest="source", default="data/archive.sql")
    p.add_argument("--append", action="store_true",
                   help="keep existing rows instead of replacing them")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("prune", help="remove a source kind, or demo fixtures")
    p.add_argument("--source",
                   help="source_kind to remove, e.g. written_question")
    p.add_argument("--fixtures", action="store_true",
                   help="remove demonstration fixture items (raw_ref FIXTURE)")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

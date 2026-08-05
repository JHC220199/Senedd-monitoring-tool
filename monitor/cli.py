"""Command line interface.

    python -m monitor.cli collect  --days 14        Fetch, score and store
    python -m monitor.cli dashboard --out out/index.html
    python -m monitor.cli brief                     The briefing as markdown
    python -m monitor.cli digest   [--send]         Build (and optionally send)
    python -m monitor.cli alert    [--send]         Critical items only
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
    pipe = Pipeline(store, taxonomy=tax, fetcher=fetcher,
                    mailbox=os.environ.get("MONITOR_MAILBOX", ""),
                    graph_token=os.environ.get("MONITOR_GRAPH_TOKEN", ""),
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
    store.close()
    return 0


def _email_config() -> dict:
    return {
        "sender": os.environ.get("MONITOR_FROM", "senedd-monitor@nrla.org.uk"),
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


def cmd_prune(args) -> int:
    """Remove every item of a given source kind from the archive.

    For overlap with other NRLA tools. Switching a source off in taxonomy.yaml
    stops new items arriving; this clears out what is already there.
    """
    store = Store(args.db)
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

    p = sub.add_parser("prune", help="remove a source kind from the archive")
    p.add_argument("--source", required=True,
                   help="source_kind to remove, e.g. written_question")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

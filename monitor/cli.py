"""Command line interface.

    python -m monitor.cli collect  --days 14        Fetch, score and store
    python -m monitor.cli dashboard --out out/index.html
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

    sent = alerts_mod.send(
        subject, html_body, text_body,
        sender=os.environ.get("MONITOR_FROM", "senedd-monitor@nrla.org.uk"),
        recipients=[r.strip() for r in (args.to or
                    os.environ.get("MONITOR_TO", "")).split(",") if r.strip()],
        smtp_host=os.environ.get("MONITOR_SMTP_HOST", ""),
        smtp_port=int(os.environ.get("MONITOR_SMTP_PORT", "587")),
        username=os.environ.get("MONITOR_SMTP_USER", ""),
        password=os.environ.get("MONITOR_SMTP_PASS", ""),
        dry_run=not args.send)
    print("Sent." if sent else "Not sent (dry run, or SMTP not configured).")
    store.close()
    return 0


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

    sent = alerts_mod.send(
        subject, html_body, text_body,
        sender=os.environ.get("MONITOR_FROM", "senedd-monitor@nrla.org.uk"),
        recipients=[r.strip() for r in (args.to or
                    os.environ.get("MONITOR_TO", "")).split(",") if r.strip()],
        smtp_host=os.environ.get("MONITOR_SMTP_HOST", ""),
        smtp_port=int(os.environ.get("MONITOR_SMTP_PORT", "587")),
        username=os.environ.get("MONITOR_SMTP_USER", ""),
        password=os.environ.get("MONITOR_SMTP_PASS", ""),
        dry_run=not args.send)

    if sent:
        store.mark_notified([i.uid for i in items])
        print("Sent, and items marked as notified.")
    else:
        print("Not sent (dry run, or SMTP not configured). "
              "Items remain unnotified.")
    store.close()
    return 0


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

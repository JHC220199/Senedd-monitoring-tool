#!/usr/bin/env python3
"""Load the gov.wales notification fixture through the real mailbox collector.

Purpose
-------
gov.wales blocks direct HTTP access from cloud hosts (CloudFront WAF, HTTP 403),
so the Welsh Government half of the monitor cannot be demonstrated live from a
sandbox. This script feeds a set of realistic notification emails through
`GovWalesMailboxCollector.message_to_item` — the same code path that will run
in production against Microsoft Graph — so the parsing, classification,
deadline extraction, sender allow-listing and scoring are all genuinely
exercised.

The fixture content is real published Welsh Government material, not invented
examples. See samples/govwales_notifications.json for the provenance of each
item.

    python3 tools/load_govwales_fixture.py

Delete samples/ and this script once the live mailbox route is connected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.collectors.govwales import GovWalesMailboxCollector   # noqa: E402
from monitor.relevance import Scorer, Taxonomy                     # noqa: E402
from monitor.store import Store                                    # noqa: E402


FIXTURE = Path(__file__).resolve().parent.parent / "samples" / "govwales_notifications.json"


def main() -> int:
    messages = json.loads(FIXTURE.read_text(encoding="utf-8"))

    tax = Taxonomy.load()
    scorer = Scorer(tax)
    store = Store("data/monitor.sqlite3")

    collector = GovWalesMailboxCollector.__new__(GovWalesMailboxCollector)
    collector.errors = []
    collector.mailbox = "senedd-monitor@nrla.org.uk"

    stored = skipped = 0
    print(f"Loading {len(messages)} fixture notification(s) through the "
          f"production mailbox parser.\n")

    for message in messages:
        item = collector.message_to_item(message)
        if item is None:
            skipped += 1
            sender = (message.get("from", {}).get("emailAddress", {})
                      .get("address", "unknown"))
            print(f"  SKIPPED  {message['id']}  sender not allow-listed "
                  f"({sender})")
            print(f"           {message['subject'][:70]}")
            continue

        scorer.score_item(item)
        if not scorer.keep(item):
            skipped += 1
            print(f"  DROPPED  {message['id']}  scored below the store "
                  f"threshold ({item.score})")
            continue

        store.upsert(item)
        stored += 1
        deadline = (item.deadline.strftime("%d %B %Y") if item.deadline
                    else "no closing date published")
        print(f"  STORED   {item.score:>6.1f}  {item.band:<8}  {item.source_kind}")
        print(f"           {item.title[:74]}")
        print(f"           closes: {deadline}")
        print(f"           themes: {[tax.theme_label(t) for t in item.themes][:4]}")
        print(f"           link:   {item.url}")
        print()

    print(f"Stored {stored}, skipped {skipped}.")
    print("\nUpcoming deadlines now tracked:")
    for item in store.upcoming_deadlines(120):
        from datetime import date
        days = (item.deadline - date.today()).days
        print(f"  {item.deadline}  ({days:>3}d)  {item.title[:66]}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

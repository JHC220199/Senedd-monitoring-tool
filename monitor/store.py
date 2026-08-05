"""Persistence: a single SQLite file.

SQLite is chosen deliberately over a hosted database. The whole point of this
system is that it should be cheap to run, easy to hand over, and impossible to
lose. A single file that can be copied to SharePoint, opened in DB Browser by a
non-developer, and restored from a OneDrive version history is worth more to a
policy team than a managed Postgres instance nobody has credentials for.

Two things the schema does that matter:

* Full-text search over title and body via FTS5. This is what makes the archive
  genuinely useful — "everything anyone has ever said in the Senedd about rent
  controls" becomes one query. It is the capability the current service does not
  provide at all: a folder of Word documents cannot be searched this way.

* Change detection on re-score. When the taxonomy is tuned, existing items can
  be re-scored in place without re-fetching anything, and `score_history`
  records what changed. That makes tuning decisions auditable, which matters if
  the team ever needs to explain why something was or was not escalated.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

from .models import Item


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    uid             TEXT PRIMARY KEY,
    source_kind     TEXT NOT NULL,
    source_name     TEXT,
    title           TEXT,
    body            TEXT,
    url             TEXT,
    item_date       TEXT,
    speaker         TEXT,
    speaker_role    TEXT,
    speaker_id      TEXT,
    party           TEXT,
    constituency    TEXT,
    forum           TEXT,
    agenda_item     TEXT,
    meeting_id      TEXT,
    video_url       TEXT,
    deadline        TEXT,
    score           REAL,
    band            TEXT,
    channel         TEXT,
    themes          TEXT,
    tiers           TEXT,
    entities        TEXT,
    signals         TEXT,
    matched_terms   TEXT,
    force_alert     INTEGER DEFAULT 0,
    collected_at    TEXT,
    raw_ref         TEXT,
    notified_at     TEXT,          -- when an alert/digest included this item
    reviewed_by     TEXT,          -- policy officer who triaged it
    review_note     TEXT,
    review_status   TEXT           -- e.g. 'action', 'monitor', 'not relevant'
);

CREATE INDEX IF NOT EXISTS idx_items_date    ON items(item_date DESC);
CREATE INDEX IF NOT EXISTS idx_items_score   ON items(score DESC);
CREATE INDEX IF NOT EXISTS idx_items_band    ON items(band);
CREATE INDEX IF NOT EXISTS idx_items_channel ON items(channel);
CREATE INDEX IF NOT EXISTS idx_items_deadline ON items(deadline);
CREATE INDEX IF NOT EXISTS idx_items_notified ON items(notified_at);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, body, speaker, forum, themes,
    content='items', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body, speaker, forum, themes)
    VALUES (new.rowid, new.title, new.body, new.speaker, new.forum, new.themes);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, speaker, forum, themes)
    VALUES ('delete', old.rowid, old.title, old.body, old.speaker, old.forum, old.themes);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, speaker, forum, themes)
    VALUES ('delete', old.rowid, old.title, old.body, old.speaker, old.forum, old.themes);
    INSERT INTO items_fts(rowid, title, body, speaker, forum, themes)
    VALUES (new.rowid, new.title, new.body, new.speaker, new.forum, new.themes);
END;

CREATE TABLE IF NOT EXISTS score_history (
    uid         TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    old_score   REAL,
    new_score   REAL,
    old_band    TEXT,
    new_band    TEXT,
    taxonomy_version TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT,
    finished_at TEXT,
    collected   INTEGER,
    stored      INTEGER,
    errors      TEXT,
    sources     TEXT,
    sources_failed TEXT,     -- persisted so a rebuilt dashboard still reports
                             -- an incomplete run. Without this, rebuilding the
                             -- page from the archive silently claimed every
                             -- source had succeeded.
    sources_substituted TEXT -- empty-but-expected sources (e.g. gov.wales RSS
                             -- when the mailbox route is in use). Persisted for
                             -- the same reason: a rebuild must report the same
                             -- health state as the original run.
);
"""


class Store:
    def __init__(self, path: str | Path = "data/monitor.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- writing ----------------------------------------------------------

    def upsert(self, item: Item) -> bool:
        """Insert an item, or update its score if the taxonomy has changed.

        Returns True if this is a genuinely new item — which is what drives
        alerting. Re-running the collectors must never re-alert on business the
        team has already seen.
        """
        row = item.to_row()
        existing = self.conn.execute(
            "SELECT score, band, notified_at FROM items WHERE uid = ?",
            (item.uid,)).fetchone()

        if existing is None:
            columns = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            self.conn.execute(
                f"INSERT INTO items ({columns}) VALUES ({placeholders})", row)
            self.conn.commit()
            return True

        # Known item. Update the score if it moved, and log the change.
        if abs((existing["score"] or 0) - item.score) > 0.05 \
                or existing["band"] != item.band:
            self.conn.execute("""
                UPDATE items SET score = :score, band = :band, channel = :channel,
                       themes = :themes, tiers = :tiers, entities = :entities,
                       signals = :signals, matched_terms = :matched_terms,
                       force_alert = :force_alert
                 WHERE uid = :uid
            """, row)
            self.conn.execute("""
                INSERT INTO score_history
                    (uid, changed_at, old_score, new_score, old_band, new_band,
                     taxonomy_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (item.uid, datetime.now().isoformat(), existing["score"],
                  item.score, existing["band"], item.band, ""))
            self.conn.commit()
        return False

    def upsert_many(self, items: Iterable[Item]) -> tuple[int, int]:
        new = total = 0
        for item in items:
            total += 1
            if self.upsert(item):
                new += 1
        return new, total

    def mark_notified(self, uids: Iterable[str]) -> None:
        stamp = datetime.now().isoformat()
        self.conn.executemany(
            "UPDATE items SET notified_at = ? WHERE uid = ? AND notified_at IS NULL",
            [(stamp, u) for u in uids])
        self.conn.commit()

    def record_run(self, run_id: str, started: datetime, finished: datetime,
                   collected: int, stored: int, errors: list[str],
                   sources: list[str],
                   sources_failed: list[str] | None = None,
                   sources_substituted: list[str] | None = None) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO runs
                (run_id, started_at, finished_at, collected, stored, errors,
                 sources, sources_failed, sources_substituted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, started.isoformat(), finished.isoformat(), collected,
              stored, json.dumps(errors), json.dumps(sources),
              json.dumps(sorted(set(sources_failed or []))),
              json.dumps(sorted(set(sources_substituted or [])))))
        self.conn.commit()

    # -- reading ----------------------------------------------------------

    def _to_item(self, row: sqlite3.Row) -> Item:
        def split(value: str | None) -> list[str]:
            return [p for p in (value or "").split("; ") if p]

        return Item(
            source_kind=row["source_kind"], source_name=row["source_name"] or "",
            title=row["title"] or "", body=row["body"] or "",
            url=row["url"] or "",
            item_date=date.fromisoformat(row["item_date"]) if row["item_date"] else None,
            speaker=row["speaker"] or "", speaker_role=row["speaker_role"] or "",
            speaker_id=row["speaker_id"] or "", party=row["party"] or "",
            constituency=row["constituency"] or "", forum=row["forum"] or "",
            agenda_item=row["agenda_item"] or "", meeting_id=row["meeting_id"] or "",
            video_url=row["video_url"] or "",
            deadline=date.fromisoformat(row["deadline"]) if row["deadline"] else None,
            score=row["score"] or 0.0, band=row["band"] or "",
            channel=row["channel"] or "", themes=split(row["themes"]),
            tiers=split(row["tiers"]), entities=split(row["entities"]),
            signals=split(row["signals"]), matched_terms=split(row["matched_terms"]),
            force_alert=bool(row["force_alert"]),
            raw_ref=row["raw_ref"] or "",
        )

    def query(self, since: date | None = None, min_score: float = 0,
              channels: list[str] | None = None,
              unnotified_only: bool = False,
              limit: int = 2000) -> list[Item]:
        sql = ["SELECT * FROM items WHERE score >= ?"]
        params: list = [min_score]
        if since:
            sql.append("AND (item_date IS NULL OR item_date >= ?)")
            params.append(since.isoformat())
        if channels:
            sql.append(f"AND channel IN ({','.join('?' * len(channels))})")
            params.extend(channels)
        if unnotified_only:
            sql.append("AND notified_at IS NULL")
        sql.append("ORDER BY score DESC, item_date DESC LIMIT ?")
        params.append(limit)
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [self._to_item(r) for r in rows]

    def search(self, expression: str, limit: int = 200) -> list[Item]:
        """Full-text search across the whole archive."""
        rows = self.conn.execute("""
            SELECT items.* FROM items
              JOIN items_fts ON items_fts.rowid = items.rowid
             WHERE items_fts MATCH ?
             ORDER BY rank
             LIMIT ?
        """, (expression, limit)).fetchall()
        return [self._to_item(r) for r in rows]

    def upcoming_deadlines(self, within_days: int = 60,
                           include_kinds: list[str] | None = None,
                           exclude_kinds: list[str] | None = None,
                           min_score: float = 0) -> list[Item]:
        """Items with a live deadline, soonest first.

        `include_kinds` / `exclude_kinds` exist because "a consultation closes on
        7 September" and "the Business Committee meets on 15 September" are not
        the same kind of fact. Mixing them buried the one genuinely actionable
        item under a dozen routine sittings — see the two-panel split in
        dashboard.py.
        """
        from datetime import timedelta
        today = date.today()
        sql = ["""SELECT * FROM items
                   WHERE deadline IS NOT NULL AND deadline >= ? AND deadline <= ?
                     AND score >= ?"""]
        params: list = [today.isoformat(),
                        (today + timedelta(days=within_days)).isoformat(),
                        min_score]
        if include_kinds:
            sql.append(f"AND source_kind IN ({','.join('?' * len(include_kinds))})")
            params.extend(include_kinds)
        if exclude_kinds:
            sql.append(f"AND source_kind NOT IN ({','.join('?' * len(exclude_kinds))})")
            params.extend(exclude_kinds)
        sql.append("ORDER BY deadline ASC, score DESC")
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [self._to_item(r) for r in rows]

    def stats(self) -> dict:
        cur = self.conn.execute("""
            SELECT COUNT(*) AS n,
                   MIN(item_date) AS earliest,
                   MAX(item_date) AS latest
              FROM items
        """).fetchone()
        bands = {r["band"]: r["n"] for r in self.conn.execute(
            "SELECT band, COUNT(*) AS n FROM items GROUP BY band")}
        sources = {r["source_kind"]: r["n"] for r in self.conn.execute(
            "SELECT source_kind, COUNT(*) AS n FROM items GROUP BY source_kind")}
        return {"total": cur["n"], "earliest": cur["earliest"],
                "latest": cur["latest"], "bands": bands, "sources": sources}

    def last_runs(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["errors"] = json.loads(d["errors"] or "[]")
            d["sources"] = json.loads(d["sources"] or "[]")
            d["sources_failed"] = json.loads(d.get("sources_failed") or "[]")
            d["sources_substituted"] = json.loads(
                d.get("sources_substituted") or "[]")
            out.append(d)
        return out

    def iter_all(self) -> Iterator[Item]:
        for row in self.conn.execute("SELECT * FROM items"):
            yield self._to_item(row)

    def rescore_all(self, scorer) -> int:
        """Re-apply the current taxonomy to the whole archive.

        Cheap (no network) and safe to run after any taxonomy edit. This is what
        makes the YAML genuinely editable by policy staff: change a weight, run
        `rescore`, and see immediately what would have been flagged differently
        over the last six months.
        """
        changed = 0
        with closing(self.conn.cursor()):
            for item in list(self.iter_all()):
                before = (item.score, item.band)
                scorer.score_item(item)
                if (item.score, item.band) != before:
                    changed += 1
                self.upsert(item)
        return changed

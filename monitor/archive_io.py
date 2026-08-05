"""Export and restore the archive as plain SQL text.

WHY THIS EXISTS — a real flaw in the first GitHub Actions workflow
-----------------------------------------------------------------
That workflow committed `data/monitor.sqlite3` back to the repository after each
run, so the archive would accumulate. The idea was right; the mechanism was
wrong, and it would have become a problem months in rather than immediately —
which is the worst kind.

Git cannot delta a SQLite binary — it stores a fresh compressed copy on every
commit — whereas it deltas text efficiently.

MEASURED, not assumed. 30 simulated daily commits, archive growing ~40 items a
day from a 1.1 MB base, both formats in isolated repositories, `git gc` run:

    committing the binary     1.5 MB of git history
    committing the SQL text   396 KB of git history
                              -> 3.7x

An earlier version of this note claimed the binary approach would reach ~30 GB
over a Senedd term. That was wrong, and worth recording as wrong: extrapolating
the measurement gives roughly 48 MB against 13 MB at 1,000 commits. Large, not
catastrophic. The gap does widen as the database grows, because the binary cost
scales with file size on every commit while the text cost scales with what
actually changed — but do not repeat the 30 GB figure, it was invented.

The size saving is therefore real but modest. The two reasons that actually
justify this format are the ones below.

Committing a **plain SQL text dump**:

* Keeps git history proportionate to what changed rather than to file size.
* The diff is *readable*, and this is the main argument. `git log -p data/archive.sql` shows exactly what the
  monitor found each day. For an audit trail that is a genuine bonus, not just a
  storage trick — "what did we know, and when did we know it" becomes a git
  question with a signed timestamp on the answer.
* Rows are written in a deterministic order, so a re-run with no new data
  produces a byte-identical file and therefore no commit at all.

The FTS5 index is deliberately NOT exported. It is derived data — several times
the size of the content it indexes, and its shadow tables do not restore
cleanly. `restore()` rebuilds it from the triggers instead, which takes under a
second and is guaranteed consistent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .store import Store


# Real tables, in dependency-free order. FTS tables and their shadow tables
# (items_fts, items_fts_data, items_fts_idx, items_fts_docsize,
# items_fts_config) are excluded on purpose — see the module docstring.
EXPORTED_TABLES = ("items", "score_history", "runs")

HEADER = """\
-- NRLA Senedd monitor — archive export
--
-- Plain SQL so that git can delta it and a human can read the diff. Restore
-- with:  python -m monitor.cli restore --from <this file>
--
-- The full-text search index is not included: it is derived data and is rebuilt
-- on restore. Rows are ordered deterministically, so an unchanged archive
-- exports byte-identically and produces no git commit.
--
-- Tables: {tables}
-- Rows:   {rows}
--
-- NOTE: deliberately no generation timestamp. An earlier version had one, which
-- meant every export differed by one line even when the data was identical — so
-- the scheduler committed every single day regardless, exactly the churn this
-- format exists to avoid. Git already records when each commit happened, with a
-- better timestamp than we could write.

PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;
"""

FOOTER = "\nCOMMIT;\nPRAGMA foreign_keys = ON;\n"

# Deterministic ordering per table. Anything that makes the byte output stable
# run-to-run, so unchanged data does not churn the diff.
ORDER_BY = {
    "items": "uid",
    "score_history": "uid, changed_at",
    "runs": "run_id",
}


def _literal(value) -> str:
    """Render one Python value as a SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def export_sql(store: Store, path: str | Path) -> tuple[Path, int]:
    """Write the archive to a SQL text file. Returns (path, row count)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    body: list[str] = []
    total = 0

    for table in EXPORTED_TABLES:
        try:
            cursor = store.conn.execute(
                f"SELECT * FROM {table} ORDER BY {ORDER_BY[table]}")
        except sqlite3.OperationalError:
            continue                       # table absent in an older archive
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        if not rows:
            continue

        body.append(f"\n-- {table} ({len(rows)} rows)\n")
        column_list = ", ".join(columns)
        for row in rows:
            values = ", ".join(_literal(row[c]) for c in columns)
            body.append(f"INSERT INTO {table} ({column_list}) VALUES ({values});\n")
        total += len(rows)

    header = HEADER.format(tables=", ".join(EXPORTED_TABLES), rows=total)
    out.write_text(header + "".join(body) + FOOTER, encoding="utf-8")
    return out, total


def restore_sql(path: str | Path, db_path: str | Path,
                replace: bool = True) -> tuple[int, int]:
    """Rebuild a database from a SQL export.

    Returns (rows restored, FTS rows indexed).

    `Store.__init__` creates the schema and the FTS triggers, so replaying the
    INSERTs populates the search index as a side effect — no separate rebuild
    step, and no possibility of the index disagreeing with the content.
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"no export found at {source}")

    store = Store(db_path)
    try:
        if replace:
            # Order matters only in that items must go last, so the FTS delete
            # triggers have their content rows to work with.
            for table in ("score_history", "runs", "items"):
                try:
                    store.conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass
            store.conn.commit()

        store.conn.executescript(source.read_text(encoding="utf-8"))
        store.conn.commit()

        rows = store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        indexed = store.conn.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0]

        # If the counts disagree the triggers did not fire — rebuild explicitly
        # rather than leaving a silently broken search index.
        if indexed != rows:
            store.conn.execute(
                "INSERT INTO items_fts(items_fts) VALUES('rebuild')")
            store.conn.commit()
            indexed = store.conn.execute(
                "SELECT COUNT(*) FROM items_fts").fetchone()[0]

        return rows, indexed
    finally:
        store.close()

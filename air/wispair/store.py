"""Durable state for the Air node — SQLite at ~/.wispair/state.db.

Five tables, all serving one goal: **no silent data loss across a run that
dies half-way**. The Air runs unattended 20 hours a day with nobody watching
the logs, so every stage commits before the next one starts.

The flow, and why it's split the way it is:

    reader pushes  ──►  items          (durable the instant they arrive)
    run summarizes ──►  summaries      (+ items marked summarized)
    run extracts   ──►  reminder_queue (+ hash ledger dedupes)
    reader polls   ──►  reminder_queue (marked delivered on ack)
    Pro pulls      ──►  summaries      (marked acked, never deleted on read)

Note the deliberate split between `cursors` and `items.summarized_at`. The
original design had one cursor doing both jobs and the rule "advance it only
after the summary is written" — which is correct but costs a full re-read of
every window whose summary failed. Because items are stored durably the moment
they arrive, the read cursor can safely advance at push time (nothing can be
lost — it's on disk), while `summarized_at` independently tracks what the model
has actually processed. A failed run therefore re-summarizes exactly the items
it missed, without the reader having to re-scan Mail for them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL,          -- 'email' | 'messages'
    ts            REAL    NOT NULL,          -- unix seconds, when it arrived in the world
    dedupe_key    TEXT    NOT NULL,
    payload       TEXT    NOT NULL,          -- the one-line rendering handed to the model
    received_at   REAL    NOT NULL,          -- when WE saw it
    summarized_at REAL,                      -- NULL until a run covers it
    UNIQUE(source, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_items_pending ON items(source, summarized_at, ts);

CREATE TABLE IF NOT EXISTS summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,              -- when the summary was produced
    source       TEXT NOT NULL,
    window_start REAL NOT NULL,
    window_end   REAL NOT NULL,
    item_count   INTEGER NOT NULL,
    text         TEXT NOT NULL,
    acked        INTEGER NOT NULL DEFAULT 0  -- 1 once the Pro confirms receipt
);
CREATE INDEX IF NOT EXISTS idx_summaries_ts ON summaries(ts);

CREATE TABLE IF NOT EXISTS reminder_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    due_ts       REAL,                       -- NULL = no due date given
    dedupe_hash  TEXT NOT NULL UNIQUE,       -- the ledger; see reminder_hash()
    created_at   REAL NOT NULL,
    delivered_at REAL,                       -- set when the reader app confirms
    source       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS cursors (
    source TEXT PRIMARY KEY,
    ts     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostics (
    source  TEXT PRIMARY KEY,
    ts      REAL NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at   REAL,
    ok         INTEGER,
    detail     TEXT NOT NULL DEFAULT ''
);
"""


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """One connection per operation.

    The scheduler runs on its own thread while FastAPI serves requests, and a
    shared connection would need `check_same_thread=False` plus our own
    locking. Per-operation connections are cheap here (this database sees a few
    dozen writes an hour, not a few thousand a second) and sidestep the whole
    class of cross-thread bugs.
    """
    config.ensure_state_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL so a long-running read (the Pro pulling summaries) can't block the
    # scheduler's writes, and vice versa.
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with _db() as conn:
        conn.executescript(SCHEMA)
    config.DB_PATH.chmod(0o600)


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------

def add_items(source: str, items: list[dict[str, Any]]) -> int:
    """Store pushed reader items. Returns the count of genuinely new ones.

    Duplicates are expected and silently dropped — the readers deliberately
    re-scan the last 24 hours on every pass, so the same email arrives many
    times over its lifetime. `INSERT OR IGNORE` against the UNIQUE constraint
    is the whole dedupe mechanism.
    """
    if not items:
        return 0
    now = time.time()
    rows = [
        (source, float(it["ts"]), str(it["dedupe_key"]), str(it["payload"]), now)
        for it in items
    ]
    with _db() as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO items(source, ts, dedupe_key, payload, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        return conn.total_changes - before


def pending_items(source: str, limit: int) -> list[sqlite3.Row]:
    """Unsummarized items for a source, oldest first.

    Oldest-first matters: if a burst overflows `limit`, the truncation should
    drop the newest (which the next run picks up in 3 hours) rather than the
    oldest (which would then never be summarized at all).
    """
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM items WHERE source = ? AND summarized_at IS NULL "
            "ORDER BY ts ASC LIMIT ?",
            (source, limit),
        ).fetchall()


def mark_summarized(item_ids: list[int]) -> None:
    if not item_ids:
        return
    now = time.time()
    with _db() as conn:
        conn.executemany(
            "UPDATE items SET summarized_at = ? WHERE id = ?",
            [(now, i) for i in item_ids],
        )


def oldest_pending_ts(source: str) -> float | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT MIN(ts) AS t FROM items WHERE source = ? AND summarized_at IS NULL",
            (source,),
        ).fetchone()
    return row["t"] if row and row["t"] is not None else None


def prune_items(older_than_days: int = 30) -> int:
    """Drop summarized items past their usefulness.

    Only summarized ones — an unsummarized item is still owed to the user no
    matter how old it is.
    """
    cutoff = time.time() - older_than_days * 86400
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM items WHERE summarized_at IS NOT NULL AND ts < ?", (cutoff,)
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------

def add_summary(source: str, text: str, window_start: float,
                window_end: float, item_count: int) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO summaries(ts, source, window_start, window_end, item_count, text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), source, window_start, window_end, item_count, text),
        )
        return int(cur.lastrowid or 0)


def summaries_since(since: float, include_acked: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM summaries WHERE ts > ?"
    if not include_acked:
        sql += " AND acked = 0"
    sql += " ORDER BY ts ASC"
    with _db() as conn:
        return [dict(r) for r in conn.execute(sql, (since,)).fetchall()]


def ack_summaries(through_ts: float) -> int:
    """Mark everything up to `through_ts` as received by the Pro.

    Ack marks, it does not delete — the Pro may be asleep for hours and a
    delete-on-read would lose a summary to any dropped response. Pruning is a
    separate, much later step.
    """
    with _db() as conn:
        cur = conn.execute(
            "UPDATE summaries SET acked = 1 WHERE ts <= ? AND acked = 0", (through_ts,)
        )
        return cur.rowcount


def prune_summaries(older_than_days: int = 14) -> int:
    cutoff = time.time() - older_than_days * 86400
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM summaries WHERE acked = 1 AND ts < ?", (cutoff,)
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------

def reminder_hash(title: str, due_ts: float | None) -> str:
    """Dedupe key for a to-do.

    Bucketed to the DAY, not the exact second: the same email seen in two
    overlapping windows can easily produce "Reply to Sarah about the invoice"
    with due times a few minutes apart, and the user must never get the same
    reminder twice. Title is normalized for case and whitespace for the same
    reason — small models rephrase punctuation run to run.
    """
    norm = " ".join(title.lower().split())
    day = "none" if due_ts is None else time.strftime("%Y-%m-%d", time.localtime(due_ts))
    return hashlib.sha256(f"{norm}|{day}".encode()).hexdigest()[:32]


def enqueue_reminder(title: str, due_ts: float | None, source: str = "") -> bool:
    """Queue a to-do for the reader app to create. False if it's a duplicate."""
    h = reminder_hash(title, due_ts)
    with _db() as conn:
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO reminder_queue(title, due_ts, dedupe_hash, created_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, due_ts, h, time.time(), source),
        )
        return conn.total_changes > before


def pending_reminders(limit: int = 50) -> list[dict[str, Any]]:
    with _db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, title, due_ts FROM reminder_queue WHERE delivered_at IS NULL "
                "ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        ]


def ack_reminders(ids: list[int]) -> int:
    """Mark reminders the Swift app confirms it actually created.

    Only the app can say this — Python has no route to EventKit — so an
    unacked row means "not in Reminders.app yet" and stays in the queue. The
    hash ledger is separate and permanent, so re-queueing can't duplicate.
    """
    if not ids:
        return 0
    now = time.time()
    with _db() as conn:
        cur = conn.executemany(
            "UPDATE reminder_queue SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
            [(now, i) for i in ids],
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# Cursors / diagnostics / runs
# --------------------------------------------------------------------------

def get_cursor(source: str) -> float:
    with _db() as conn:
        row = conn.execute("SELECT ts FROM cursors WHERE source = ?", (source,)).fetchone()
    return float(row["ts"]) if row else 0.0


def set_cursor(source: str, ts: float) -> None:
    """Monotonic — never moves backwards.

    A reader restart, a clock adjustment, or a push that happens to carry only
    older items must not rewind the cursor and cause a re-scan storm.
    """
    with _db() as conn:
        conn.execute(
            "INSERT INTO cursors(source, ts) VALUES (?, ?) "
            "ON CONFLICT(source) DO UPDATE SET ts = MAX(ts, excluded.ts)",
            (source, ts),
        )


def all_cursors() -> dict[str, float]:
    with _db() as conn:
        return {r["source"]: r["ts"] for r in conn.execute("SELECT * FROM cursors")}


def set_diagnostic(source: str, payload: dict[str, Any]) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO diagnostics(source, ts, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET ts = excluded.ts, payload = excluded.payload",
            (source, time.time(), json.dumps(payload)),
        )


def all_diagnostics() -> dict[str, Any]:
    with _db() as conn:
        return {
            r["source"]: {"ts": r["ts"], **json.loads(r["payload"])}
            for r in conn.execute("SELECT * FROM diagnostics")
        }


def start_run() -> int:
    with _db() as conn:
        cur = conn.execute("INSERT INTO runs(started_at) VALUES (?)", (time.time(),))
        return int(cur.lastrowid or 0)


def end_run(run_id: int, ok: bool, detail: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE runs SET ended_at = ?, ok = ?, detail = ? WHERE id = ?",
            (time.time(), 1 if ok else 0, detail[:2000], run_id),
        )


def last_run() -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def counts() -> dict[str, int]:
    with _db() as conn:
        q = lambda s: int(conn.execute(s).fetchone()[0])  # noqa: E731
        return {
            "items_total": q("SELECT COUNT(*) FROM items"),
            "items_pending": q("SELECT COUNT(*) FROM items WHERE summarized_at IS NULL"),
            "summaries_total": q("SELECT COUNT(*) FROM summaries"),
            "summaries_unacked": q("SELECT COUNT(*) FROM summaries WHERE acked = 0"),
            "reminders_pending": q(
                "SELECT COUNT(*) FROM reminder_queue WHERE delivered_at IS NULL"),
            "reminders_created": q(
                "SELECT COUNT(*) FROM reminder_queue WHERE delivered_at IS NOT NULL"),
        }

"""A durable record of every message the attention runner has acted, or would have acted, on.

The primary key is the source message id, so a restart, a re-sync or a second tick can
never produce a second action for the same message: `claim` is an INSERT that succeeds
exactly once.

The row is written BEFORE the effect (state `claimed`) and finished after it. A row still
`claimed` after a crash is promoted to `unknown`, never retried: the reminder may or may
not exist, and a blind retry could create a twin. The project's existing reminder
protocol makes the same choice.

Its own SQLite file under ~/.moe/attention/ (0600), not assistant.db: this keeps the
measurement tools and the pure planner free of the live assistant store.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

STATES = ("shadow", "claimed", "created", "failed", "unknown", "capped", "already_on_file", "undone")
STALE_CLAIM_S = 300


class Ledger:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fresh = not path.exists()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if fresh:
            os.chmod(path, 0o600)
        self._lock = threading.Lock()
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS actions (
                source_id   TEXT PRIMARY KEY,
                state       TEXT NOT NULL,
                decided_at  REAL NOT NULL,
                finished_at REAL,
                title       TEXT,
                due_ts      REAL,
                event_ts    REAL,
                detail      TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def recover(self, now: float | None = None) -> int:
        """Promote claims abandoned by a crash to `unknown`. Never retried."""
        now = now if now is not None else time.time()
        with self._lock:
            cur = self._db.execute(
                "UPDATE actions SET state='unknown', finished_at=?, "
                "detail=json_set(detail,'$.recovered',1) "
                "WHERE state='claimed' AND decided_at < ?", (now, now - STALE_CLAIM_S))
            return cur.rowcount

    def purge_content(self, older_than: float, now: float | None = None) -> int:
        """Drop the message-derived text (title, quote, reasons) from rows decided before
        `older_than`, keeping `source_id`, state, times and the creation identity.

        The runner never looks at a message older than its lookback again, so the words are of no
        further use, while the id and state are what keep that message from being acted on twice.
        A `created` reminder that has not yet come due keeps its title: Undo matches the live
        reminder against it. Idempotent."""
        now = now if now is not None else time.time()
        with self._lock:
            cur = self._db.execute(
                "UPDATE actions SET title=NULL, "
                "detail=json_remove(detail,'$.quote','$.why','$.when','$.result.error') "
                "WHERE decided_at<? AND (state!='created' OR due_ts IS NULL OR due_ts<?) "
                "AND (title IS NOT NULL OR json_extract(detail,'$.quote') IS NOT NULL "
                "OR json_extract(detail,'$.why') IS NOT NULL OR json_extract(detail,'$.result.error') IS NOT NULL)",
                (older_than, now))
            return cur.rowcount

    def claim(self, source_id: str, *, title: str | None = None, due_ts: float | None = None,
              event_ts: float | None = None, now: float | None = None,
              detail: dict | None = None) -> bool:
        """True exactly once per message. False means someone already decided it.

        `detail` is written with the claim, so a row a crash leaves `claimed` (and `recover`
        turns `unknown`) still carries what later decisions match against."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO actions(source_id,state,decided_at,title,due_ts,event_ts,detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (source_id, "claimed", now if now is not None else time.time(), title, due_ts, event_ts,
                 json.dumps(detail or {}, sort_keys=True)))
            return cur.rowcount == 1

    def finish(self, source_id: str, state: str, detail: dict | None = None,
               now: float | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        with self._lock:
            self._db.execute(
                "UPDATE actions SET state=?, finished_at=?, detail=? WHERE source_id=?",
                (state, now if now is not None else time.time(),
                 json.dumps(detail or {}, sort_keys=True), source_id))

    def get(self, source_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM actions WHERE source_id=?", (source_id,)).fetchone()
        return _row(row) if row else None

    def known(self, source_ids: list[str]) -> set[str]:
        found: set[str] = set()
        for i in range(0, len(source_ids), 500):          # stay under SQLite's variable limit
            chunk = source_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                found.update(r[0] for r in self._db.execute(
                    f"SELECT source_id FROM actions WHERE source_id IN ({marks})", chunk))
        return found

    def count_since(self, since_ts: float, states: tuple[str, ...] = ("created",),
                    exclude: str | None = None) -> int:
        """Rows decided since `since_ts` in any of `states`. `exclude` leaves one message out,
        so a cap check does not count the claim the message under decision has just made."""
        marks = ",".join("?" * len(states))
        with self._lock:
            return self._db.execute(
                f"SELECT COUNT(*) FROM actions WHERE decided_at>=? AND state IN ({marks}) "
                "AND source_id IS NOT ?", (since_ts, *states, exclude)).fetchone()[0]

    def rows_relevant(self, since_ts: float, upcoming_from: float, states: tuple[str, ...]) -> list[dict]:
        """Rows in any of `states` decided since `since_ts`, or whose event (or, with no clock,
        due time) is still at or after `upcoming_from`, however long ago they were decided.
        Oldest first."""
        marks = ",".join("?" * len(states))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM actions WHERE (decided_at>=? OR COALESCE(event_ts,due_ts)>=?) "
                f"AND state IN ({marks}) ORDER BY decided_at", (since_ts, upcoming_from, *states)).fetchall()
        return [_row(r) for r in rows]

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM actions ORDER BY decided_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    def meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def mark_live(self, at: float) -> None:
        """Record the switch into live mode as ONE step: the baseline and the mode it belongs to.

        If the baseline were stamped alone, the first pass would see a mode change and move the
        baseline forward to its own clock, dropping every message that arrived in between."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for key, value in (("enabled_at", repr(at)), ("last_mode", "live")):
                    self._db.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def meta_setdefault(self, key: str, value: str) -> str:
        """The stored value for `key`, writing `value` first only if there is none. Two callers
        racing to set it both get the one that won."""
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (key, value))
            return self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _row(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["detail"] = json.loads(out.get("detail") or "{}")
    return out

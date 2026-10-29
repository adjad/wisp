"""The mail-memory queue and the reminders it found, in one local SQLite file.

IMPORTANT rules:
- No mail bodies are stored. A message row keeps its index metadata (sender,
  subject, dates, flags); a suggestion keeps the short verbatim quote it rests on.
- The queue is the unit of work: one row per message, `pending -> done |
  skipped | failed`. A tick handles one row, so a crash, a sleep or a user's
  turn loses at most one model call.
- A row is keyed by its RFC Message-ID when Mail has one, so a rebuilt Mail
  index (new ROWIDs) refreshes the ROWID instead of queueing the mail twice.
- Dismissed and accepted suggestions are never offered again, even if the same
  email is extracted again.
- Only important suggestions reach the Brief (`importance.judge`); the rest are
  kept with the reason they were held back, for tuning.
- Profile-fact candidates (profile_facts.py) are a working pile like `items`:
  each keeps its short quote, never the body. They reach the user only through
  facts.db, as proposed facts, and "forget everything from mail" clears them.
- The file is created 0600 in a 0700 directory, like Wisp's other caches.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from service.paths import MOE_DIR

DB_PATH = MOE_DIR / "mail_memory" / "mail_memory.db"
MAX_ATTEMPTS = 2
RETRY_S = 3600

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS messages(
         key TEXT PRIMARY KEY, rowid INTEGER NOT NULL, account TEXT NOT NULL DEFAULT '',
         box TEXT NOT NULL, ts REAL NOT NULL, sent_ts REAL NOT NULL DEFAULT 0,
         conversation_id INTEGER NOT NULL DEFAULT 0, subject TEXT NOT NULL DEFAULT '',
         sender TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL DEFAULT '',
         bulk INTEGER NOT NULL DEFAULT 0, tier INTEGER NOT NULL,
         state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
         next_at REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
         rfc_message_id TEXT NOT NULL DEFAULT '', category INTEGER NOT NULL DEFAULT -1,
         updated REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS messages_work ON messages(state, tier, ts)",
    """CREATE TABLE IF NOT EXISTS items(
         id INTEGER PRIMARY KEY, key TEXT NOT NULL, what TEXT NOT NULL, date_phrase TEXT NOT NULL,
         due TEXT NOT NULL, precision TEXT NOT NULL, evidence TEXT NOT NULL,
         suspicious INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'new',
         created REAL NOT NULL, decided REAL, reminder TEXT NOT NULL DEFAULT '',
         extractor TEXT NOT NULL, important INTEGER NOT NULL DEFAULT 0, why TEXT NOT NULL DEFAULT '',
         UNIQUE(key, what, due))""",
    "CREATE INDEX IF NOT EXISTS items_open ON items(state, due)",
    "CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS fact_candidates(
         id INTEGER PRIMARY KEY, key TEXT NOT NULL, kind TEXT NOT NULL, grp TEXT NOT NULL,
         value TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', quote TEXT NOT NULL,
         sent_ts REAL NOT NULL, extractor TEXT NOT NULL, created REAL NOT NULL,
         UNIQUE(key, kind, grp))""",
    "CREATE INDEX IF NOT EXISTS fact_candidates_group ON fact_candidates(kind, grp)",
)
# Columns added after the first schema, for files made by earlier builds of this branch.
_ADDED = (
    ("messages", "rfc_message_id", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "category", "INTEGER NOT NULL DEFAULT -1"),
    ("items", "important", "INTEGER NOT NULL DEFAULT 0"),
    ("items", "why", "TEXT NOT NULL DEFAULT ''"),
    # '' (not yet looked at), done, skipped (can't carry a fact), pending (the call
    # was dropped for a turn), failed.
    ("messages", "facts_state", "TEXT NOT NULL DEFAULT ''"),
    # The Mail account a candidate came from, so an account's removal takes it along.
    ("fact_candidates", "account", "TEXT NOT NULL DEFAULT ''"),
)


def _key(row: dict) -> str:
    """The RFC Message-ID when known; else Mail's own id; else the ROWID."""
    if row.get("rfc_message_id"):
        return row["rfc_message_id"]
    if row.get("message_id"):
        return f"mail:{row['message_id']}"
    return f"row:{row['rowid']}"


class Store:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        os.chmod(self.path, 0o600)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            for statement in _SCHEMA:
                self._db.execute(statement)
            for table, column, kind in _ADDED:
                have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    # --- meta -----------------------------------------------------------------------

    def meta(self, name: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, name: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta(name,value) VALUES(?,?) "
                             "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))

    # --- the queue ------------------------------------------------------------------

    def upsert_index(self, rows: list[dict], tier_of, now: float | None = None) -> dict:
        """Merge one index window. New messages enter the queue with `tier_of(row)`
        (None = not worth a model call: recorded as skipped). Known messages keep
        their state; only their ROWID and metadata are refreshed."""
        now = time.time() if now is None else now
        added = skipped = 0
        # Committed in chunks, so Accept and Dismiss on the loop never wait behind a
        # 6,000-row merge running in a worker thread.
        for start in range(0, len(rows), self.MERGE_CHUNK):
            a, s = self._merge(rows[start:start + self.MERGE_CHUNK], tier_of, now)
            added += a
            skipped += s
        return {"added": added, "skipped": skipped, "seen": len(rows)}

    MERGE_CHUNK = 250

    def _merge(self, rows: list[dict], tier_of, now: float) -> tuple[int, int]:
        added = skipped = 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    key = _key(row)
                    have = self._db.execute("SELECT state FROM messages WHERE key=?", (key,)).fetchone()
                    if have:
                        self._db.execute(
                            "UPDATE messages SET rowid=?, box=?, subject=?, sender=?, sender_name=?, "
                            "bulk=?, category=?, updated=? WHERE key=?",
                            (row["rowid"], row["box"], row["subject"], row["sender"], row["sender_name"],
                             int(row["bulk"]), row.get("category", -1), now, key))
                        continue
                    tier = tier_of(row)
                    state = "pending" if tier is not None else "skipped"
                    added += tier is not None
                    skipped += tier is None
                    self._db.execute(
                        "INSERT INTO messages(key,rowid,account,box,ts,sent_ts,conversation_id,subject,sender,"
                        "sender_name,bulk,tier,state,note,rfc_message_id,category,updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, row["rowid"], row["account"], row["box"], row["ts"], row["sent_ts"],
                         row["conversation_id"], row["subject"], row["sender"], row["sender_name"],
                         int(row["bulk"]), 99 if tier is None else tier, state,
                         "" if tier is not None else "prefilter", row.get("rfc_message_id", ""),
                         row.get("category", -1), now))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return added, skipped

    def exclude(self, rfc_ids, note: str = "gmail_tab") -> int:
        """Retire pending messages by RFC Message-ID (Gmail's Promotions, Social and
        Forums tabs), so a tab list that arrives after the first index still counts."""
        ids = sorted(set(rfc_ids))
        done = 0
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                done += self._db.execute(
                    f"UPDATE messages SET state='skipped', note=?, updated=? WHERE state='pending' "
                    f"AND rfc_message_id IN ({','.join('?' * len(chunk))})",
                    (note, time.time(), *chunk)).rowcount
        return done

    def next_pending(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        row = self._db.execute(
            "SELECT * FROM messages WHERE state='pending' AND next_at<=? ORDER BY tier, ts DESC LIMIT 1",
            (now,)).fetchone()
        return dict(row) if row else None

    def finish(self, key: str, state: str, note: str = "", rfc_message_id: str = "") -> None:
        assert state in ("done", "skipped")
        with self._lock:
            self._db.execute("UPDATE messages SET state=?, note=?, updated=?, "
                             "rfc_message_id=CASE WHEN ?='' THEN rfc_message_id ELSE ? END WHERE key=?",
                             (state, note[:80], time.time(), rfc_message_id, rfc_message_id, key))

    def fail(self, key: str, note: str, now: float | None = None) -> None:
        """Retry once, an hour later; then give up on this message."""
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "UPDATE messages SET attempts=attempts+1, note=?, updated=?, next_at=?, "
                "state=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'pending' END WHERE key=?",
                (note[:80], now, now + RETRY_S, MAX_ATTEMPTS, key))

    def message(self, key: str) -> dict | None:
        row = self._db.execute("SELECT * FROM messages WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    # --- suggestions ------------------------------------------------------------------

    def add_items(self, key: str, items: list[dict], extractor: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        added = 0
        with self._lock:
            for it in items:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO items(key,what,date_phrase,due,precision,evidence,suspicious,"
                    "state,created,extractor,important,why) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, it["what"], it["date_phrase"], it["due"], it["precision"], it["evidence"],
                     int(bool(it.get("suspicious"))), "new", now, extractor,
                     int(bool(it.get("important"))), str(it.get("why") or "")[:40]))
                added += cur.rowcount
        return added

    def suggestions(self, today: str, *, horizon: str, limit: int = 3) -> list[dict]:
        """Open, important, unsuspicious suggestions due from `today` through `horizon`
        (ISO dates), soonest first, joined with their email's sender and subject."""
        rows = self._db.execute(
            "SELECT i.*, m.sender, m.sender_name, m.subject, m.ts, m.box, m.rfc_message_id FROM items i "
            "JOIN messages m ON m.key=i.key WHERE i.state='new' AND i.suspicious=0 AND i.important=1 "
            "AND i.due>=? AND i.due<=? "
            "ORDER BY i.due, i.id LIMIT ?", (today, horizon, limit)).fetchall()
        return [dict(r) for r in rows]

    def item(self, item_id: int) -> dict | None:
        row = self._db.execute(
            "SELECT i.*, m.sender, m.sender_name, m.subject, m.ts, m.box, m.rfc_message_id FROM items i "
            "JOIN messages m ON m.key=i.key WHERE i.id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def decide(self, item_id: int, state: str, reminder: str = "", *, from_state: str = "new") -> bool:
        """new (or a claimed `accepting`) -> accepted | dismissed, once. False when
        it was already decided or claimed by someone else."""
        assert state in ("accepted", "dismissed") and from_state in ("new", "accepting")
        with self._lock:
            cur = self._db.execute("UPDATE items SET state=?, decided=?, reminder=? WHERE id=? AND state=?",
                                   (state, time.time(), reminder[:200], item_id, from_state))
        return cur.rowcount == 1

    def claim(self, item_id: int) -> bool:
        """new -> accepting, atomically, so one Add runs per suggestion even on a
        double click; a dismiss can't land while it runs."""
        with self._lock:
            return self._db.execute("UPDATE items SET state='accepting' WHERE id=? AND state='new'",
                                    (item_id,)).rowcount == 1

    def release(self, item_id: int) -> None:
        """An Add that failed before writing anything hands the suggestion back."""
        with self._lock:
            self._db.execute("UPDATE items SET state='new' WHERE id=? AND state='accepting'", (item_id,))

    def expire(self, today: str) -> int:
        """Suggestions whose day has passed are never offered."""
        with self._lock:
            return self._db.execute("UPDATE items SET state='expired' WHERE state='new' AND due<?",
                                    (today,)).rowcount

    # --- profile-fact candidates -------------------------------------------------------

    FACT_STATES = ("done", "skipped", "pending", "failed")

    def set_facts_state(self, key: str, state: str) -> None:
        assert state in self.FACT_STATES
        with self._lock:
            self._db.execute("UPDATE messages SET facts_state=? WHERE key=?", (state, key))

    def next_facts_pending(self, eligible, limit: int = 50) -> dict | None:
        """A read message whose fact pass hasn't run (mail read before this pass
        existed, or a call a turn interrupted), newest first. Messages `eligible`
        rejects are marked skipped on the way, so each is looked at once."""
        rows = self._db.execute(
            "SELECT * FROM messages WHERE state='done' AND facts_state IN ('','pending') "
            "ORDER BY tier, ts DESC LIMIT ?", (limit,)).fetchall()
        for row in rows:
            row = dict(row)
            if eligible(row):
                return row
            self.set_facts_state(row["key"], "skipped")
        return None

    def add_candidates(self, key: str, sent_ts: float, kept: list[dict], extractor: str,
                       now: float | None = None, *, account: str = "") -> list[tuple[str, str]]:
        """Record one email's candidates; returns the (kind, group) pairs it touched."""
        now = time.time() if now is None else now
        with self._lock:
            for c in kept:
                self._db.execute(
                    "INSERT OR IGNORE INTO fact_candidates(key,kind,grp,value,role,quote,sent_ts,extractor,"
                    "created,account) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (key, c["kind"], c["group"], c["value"], c["role"], c["quote"], sent_ts, extractor, now,
                     account))
        return list(dict.fromkeys((c["kind"], c["group"]) for c in kept))

    def candidate_groups(self) -> list[tuple[str, str]]:
        return [(r["kind"], r["grp"]) for r in self._db.execute(
            "SELECT DISTINCT kind, grp FROM fact_candidates ORDER BY kind, grp")]

    def candidates(self, kind: str, group: str) -> list[dict]:
        """One group's evidence, joined with its email's Message-ID and subject."""
        rows = self._db.execute(
            "SELECT c.*, m.subject, CASE WHEN m.rfc_message_id!='' THEN m.rfc_message_id ELSE c.key END source_id "
            "FROM fact_candidates c LEFT JOIN messages m ON m.key=c.key WHERE c.kind=? AND c.grp=? "
            "ORDER BY c.sent_ts DESC, c.id", (kind, group)).fetchall()
        return [{**dict(r), "subject": r["subject"] or ""} for r in rows]

    def newer_slot_value(self, kind: str, group: str, sent_ts: float) -> bool:
        """Whether a newer email names a different value for this slot kind. Two
        values from the same moment: the one that sorts last wins, every time."""
        return self._db.execute(
            "SELECT 1 FROM fact_candidates WHERE kind=? AND grp!=? AND (sent_ts>? OR (sent_ts=? AND grp>?)) LIMIT 1",
            (kind, group, sent_ts, sent_ts, group)).fetchone() is not None

    def clear_candidates(self, *, account: str | None = None) -> int:
        with self._lock:
            if account is None:
                return self._db.execute("DELETE FROM fact_candidates").rowcount
            return self._db.execute("DELETE FROM fact_candidates WHERE account=?", (account,)).rowcount

    def candidate_accounts(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT DISTINCT account FROM fact_candidates")}

    def fact_stats(self) -> dict:
        passes = {r["facts_state"] or "unread": r["n"] for r in self._db.execute(
            "SELECT facts_state, COUNT(*) n FROM messages WHERE state='done' GROUP BY facts_state")}
        kinds = {r["kind"]: r["n"] for r in self._db.execute(
            "SELECT kind, COUNT(DISTINCT grp) n FROM fact_candidates GROUP BY kind")}
        return {"passes": passes, "candidates": kinds}

    def stats(self) -> dict:
        queue = {r["state"]: r["n"] for r in self._db.execute(
            "SELECT state, COUNT(*) n FROM messages GROUP BY state")}
        items = {r["state"]: r["n"] for r in self._db.execute(
            "SELECT state, COUNT(*) n FROM items GROUP BY state")}
        suspicious = self._db.execute("SELECT COUNT(*) FROM items WHERE suspicious=1").fetchone()[0]
        why = {r["why"] or "unjudged": r["n"] for r in self._db.execute(
            "SELECT why, COUNT(*) n FROM items GROUP BY why")}
        return {"queue": queue, "items": items, "suspicious": suspicious, "why": why,
                "last_index": self.meta("last_index"), "last_tick": self.meta("last_tick")}

    def health(self) -> dict:
        """Why reading Mail last failed (a reason code and when), or None. Never mail text."""
        error = self.meta("last_error")
        at, ok = self.meta("last_error_at"), self.meta("last_ok_at")
        return {"last_error": error or None, "last_error_at": float(at) if error and at else None,
                "last_ok_at": float(ok) if ok else None}


_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store

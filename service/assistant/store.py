"""Commitments store — the keystone of the assistant layer.

Every connector (calendar today; mail/manual later) normalizes what it finds
into a *commitment*: a thing with a time. One table, one shape, one rendering
pipeline. SQLite at ~/.moe/assistant.db, stdlib only, a lock around writes —
same single-user model as service/memory/store.py.

`when_ts` is the canonical sortable moment (epoch seconds): an event's start or
an assignment's due time. `kind` carries the semantics. Reminders are deduped
via notify_log so a restart never re-fires a stage that already went out.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from service.paths import STATE_DIR

DB_PATH = STATE_DIR / "assistant.db"

# scripts/wisp_testdata.py seeds fake commitments ("Dentist Follow-up",
# "Team Standup", "Take the laundry out"…) into this same live DB under this
# source. Seeding is relative to the moment it ran, so a forgotten `seed-db`
# keeps producing plausible-looking events on real days — which is exactly how
# a daily brief ends up announcing an 11:15 dentist appointment that doesn't
# exist, and how the reminder engine fires notifications for it. It reads as
# the model hallucinating; it isn't, the data really is in the store.
#
# So seed rows are INVISIBLE to every read path unless WISP_QA_SEED=1 is set in
# the backend's environment. QA still gets the fixtures on demand; a stale seed
# can never again reach a brief, a notification, or an answer.
QA_SEED_SOURCE = "wisp_seed"


def _seed_clause() -> str:
    """SQL fragment hiding QA seed rows. Checked per-call (not at import) so
    flipping the env var doesn't need a rebuild of the packaged app."""
    if os.environ.get("WISP_QA_SEED") == "1":
        return ""
    return f" AND source <> '{QA_SEED_SOURCE}'"

# NOTE on uniqueness: keyed on (source, source_id, when_ts), NOT (source,
# source_id) alone. EventKit gives every occurrence of a RECURRING event the
# SAME eventIdentifier — deduping on source_id alone collapsed all occurrences
# of a recurring event into a single row (only the last-synced occurrence
# survived, which looked like "sees the far-future one, missing the near one").
# Pairing with when_ts makes each occurrence its own commitment.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS commitments (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,      -- 'calendar' | 'mail' | 'manual'
    source_id   TEXT,               -- stable upstream id (dedupe/update key)
    kind        TEXT NOT NULL,      -- 'event' | 'meeting' | 'assignment' | 'exam' | 'reminder'
    title       TEXT NOT NULL,
    context     TEXT,               -- calendar name, course, sender (NOT necessarily a person)
    organizer   TEXT,               -- real meeting organizer (only set when it's NOT the user)
    account     TEXT,               -- which linked account this came from (e.g. calendar's
                                     -- EKSource title: "iCloud", "Gmail work@x.com") — lets
                                     -- questions be scoped to one account once more than one
                                     -- is linked; NULL/empty when there's just one (the common
                                     -- case today)
    when_ts     REAL,               -- epoch seconds; start (events) or due (assignments)
    all_day     INTEGER DEFAULT 0,
    location    TEXT,
    url         TEXT,
    status      TEXT DEFAULT 'active',   -- 'active' | 'done' | 'dismissed'
    confidence  REAL DEFAULT 1.0,        -- 1.0 deterministic; <1.0 LLM-extracted
    created_at  REAL,
    updated_at  REAL,
    UNIQUE(source, source_id, when_ts)
);
CREATE TABLE IF NOT EXISTS notify_log (
    commitment_id TEXT,
    stage         TEXT,          -- e.g. 'T-1d', 'T-30m', 'due'
    sent_at       REAL,
    PRIMARY KEY (commitment_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_commit_when ON commitments(status, when_ts);
"""

class AssistantStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()
        self._migrate_unique_constraint()
        self._migrate_organizer_column()
        self._migrate_account_column()

    def _migrate_organizer_column(self) -> None:
        """Purely additive — no rebuild needed, unlike the constraint migration."""
        cols = [r["name"] for r in
                self._db.execute("PRAGMA table_info(commitments)").fetchall()]
        if "organizer" not in cols:
            with self._lock:
                self._db.execute("ALTER TABLE commitments ADD COLUMN organizer TEXT")
                self._db.commit()

    def _migrate_account_column(self) -> None:
        """Purely additive, same as organizer above."""
        cols = [r["name"] for r in
                self._db.execute("PRAGMA table_info(commitments)").fetchall()]
        if "account" not in cols:
            with self._lock:
                self._db.execute("ALTER TABLE commitments ADD COLUMN account TEXT")
                self._db.commit()

    def _migrate_unique_constraint(self) -> None:
        """One-time upgrade for DBs created before the (source, source_id,
        when_ts) uniqueness fix — rebuilds the table under the new constraint
        without losing existing rows (recurring-event duplicates just start
        working correctly on the next sync)."""
        cols = [r["name"] for r in
                self._db.execute("PRAGMA table_info(commitments)").fetchall()]
        if not cols:
            return
        # A UNIQUE(...) table constraint creates an auto-index with sql=NULL in
        # sqlite_master (not text we can grep) — inspect its actual columns via
        # PRAGMA index_info instead.
        has_new_constraint = False
        for idx in self._db.execute("PRAGMA index_list(commitments)").fetchall():
            if not idx["unique"]:
                continue
            info_cols = [c["name"] for c in
                        self._db.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
            if "when_ts" in info_cols:
                has_new_constraint = True
                break
        if has_new_constraint:
            return
        with self._lock:
            self._db.executescript("""
                ALTER TABLE commitments RENAME TO commitments_old_migrating;
            """)
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO commitments SELECT * FROM commitments_old_migrating")
            self._db.executescript("DROP TABLE commitments_old_migrating;")
            self._db.commit()

    # --- writes -----------------------------------------------------------
    def sync_source(self, source: str, items: list[dict]) -> int:
        """Replace the active set for `source` with `items` (each a commitment
        dict with at least kind/title/when_ts and a stable source_id).

        Upserts by (source_id, when_ts) — NOT source_id alone — so a moved time
        updates in place and keeps its notify_log; active rows for this source
        that vanished upstream are dropped. Manual commitments (a different
        source) are never touched.

        Keying on the pair (rather than source_id alone) is required because
        EventKit gives every occurrence of a RECURRING event the same
        eventIdentifier; deduping by source_id alone collapsed all of a
        recurring series into one row. Diffing is done in Python (fetch once,
        compare in memory) rather than a SQL tuple IN(...), which is fragile
        across SQLite versions.
        """
        now = time.time()
        with self._lock:
            existing = self._db.execute(
                "SELECT id, source_id, when_ts FROM commitments WHERE source=?",
                (source,)).fetchall()
            existing_by_key = {(r["source_id"], r["when_ts"]): r["id"] for r in existing}

            seen_keys: set[tuple] = set()
            for it in items:
                sid = str(it.get("source_id") or uuid.uuid4().hex)
                when_ts = it.get("when_ts")
                key = (sid, when_ts)
                seen_keys.add(key)
                cid = existing_by_key.get(key) or uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO commitments "
                    "(id, source, source_id, kind, title, context, organizer, account, when_ts, "
                    " all_day, location, url, status, confidence, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source, source_id, when_ts) DO UPDATE SET "
                    "  kind=excluded.kind, title=excluded.title, context=excluded.context, "
                    "  organizer=excluded.organizer, account=excluded.account, "
                    "  all_day=excluded.all_day, location=excluded.location, "
                    "  url=excluded.url, updated_at=excluded.updated_at, "
                    "  status=CASE WHEN commitments.status='dismissed' THEN 'dismissed' ELSE 'active' END",
                    (cid, source, sid, it.get("kind", "event"), it.get("title", "(untitled)"),
                     it.get("context"), it.get("organizer"), it.get("account"), when_ts,
                     int(bool(it.get("all_day"))),
                     it.get("location"), it.get("url"), "active",
                     float(it.get("confidence", 1.0)), now, now))

            # drop rows for this source whose (source_id, when_ts) vanished
            # upstream (event deleted, or an occurrence's time changed — the old
            # occurrence row is pruned once the new one is inserted above).
            stale_ids = [rid for key, rid in existing_by_key.items() if key not in seen_keys]
            for rid in stale_ids:
                self._db.execute("DELETE FROM commitments WHERE id=?", (rid,))
                self._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (rid,))
            self._db.commit()
        return len(items)

    def add_manual(self, title: str, when_ts: float, kind: str = "reminder",
                   context: str | None = None) -> dict:
        cid = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO commitments (id, source, source_id, kind, title, context, "
                "when_ts, status, confidence, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, "manual", cid, kind, title, context, when_ts, "active", 1.0, now, now))
            self._db.commit()
        return self.get(cid)

    def delete(self, cid: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM commitments WHERE id=?", (cid,))
            self._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
            self._db.commit()
        return cur.rowcount > 0

    def set_status(self, cid: str, status: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE commitments SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), cid))
            self._db.commit()
        return cur.rowcount > 0

    def mark_notified(self, cid: str, stage: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO notify_log (commitment_id, stage, sent_at) "
                "VALUES (?,?,?)", (cid, stage, time.time()))
            self._db.commit()

    def already_notified(self, cid: str, stage: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM notify_log WHERE commitment_id=? AND stage=?",
            (cid, stage)).fetchone()
        return row is not None

    # --- reads ------------------------------------------------------------
    def get(self, cid: str) -> dict | None:
        row = self._db.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None

    def next_active(self, now: float | None = None) -> dict | None:
        now = now if now is not None else time.time()
        row = self._db.execute(
            "SELECT * FROM commitments WHERE status='active' AND when_ts >= ?"
            + _seed_clause() +
            " ORDER BY when_ts ASC LIMIT 1", (now - 300,)).fetchone()  # 5-min grace
        return dict(row) if row else None

    def upcoming(self, now: float | None = None, days: int = 7) -> list[dict]:
        now = now if now is not None else time.time()
        rows = self._db.execute(
            "SELECT * FROM commitments WHERE status='active' AND when_ts >= ? AND when_ts <= ?"
            + _seed_clause() +
            " ORDER BY when_ts ASC", (now - 300, now + days * 86400)).fetchall()
        return [dict(r) for r in rows]

    def history(self, now: float | None = None, days: int = 365) -> list[dict]:
        """Past active commitments — the mirror of `upcoming`, most-recent
        first. Safe to widen: `due_reminders` only ever reads `active_future`
        (when_ts >= now), so past rows synced in here can never trigger a
        stale reminder no matter how far back `days` reaches."""
        now = now if now is not None else time.time()
        rows = self._db.execute(
            "SELECT * FROM commitments WHERE status='active' AND when_ts < ? AND when_ts >= ?"
            + _seed_clause() +
            " ORDER BY when_ts DESC", (now - 300, now - days * 86400)).fetchall()
        return [dict(r) for r in rows]

    def active_future(self, now: float | None = None, horizon_days: int = 30) -> list[dict]:
        """All active commitments from now out to the horizon — the reminder
        engine's working set. Same 5-min grace as next_active/upcoming: a
        "reminder"-kind commitment's only stage fires AT when_ts (lead 0), so
        by the time the 30s-interval scheduler tick sees it, when_ts has
        already slipped into the past — without the grace it'd be filtered
        out here before due_reminders' own 60s grace window ever runs.
        """
        now = now if now is not None else time.time()
        rows = self._db.execute(
            "SELECT * FROM commitments WHERE status='active' AND when_ts >= ? AND when_ts <= ?"
            + _seed_clause() +
            " ORDER BY when_ts ASC", (now - 300, now + horizon_days * 86400)).fetchall()
        return [dict(r) for r in rows]


# module-level singleton
assistant_store = AssistantStore()

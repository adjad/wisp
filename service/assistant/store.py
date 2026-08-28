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

from service.paths import MOE_DIR

DB_PATH = MOE_DIR / "assistant.db"

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


# --- cross-source dedupe ---------------------------------------------------
# One real-world commitment can legitimately land in this table twice, under
# two different sources, because Wisp writes it to a system app that then syncs
# it back: add_reminder() inserts a source='manual' row AND asks the app to
# mirror it into Reminders.app, whose next RemindersWriter.sync() posts it back
# as source='reminders'. sync_source() upserts scoped `WHERE source=?` (by
# design — sources are independent replace-sets), so it structurally cannot see
# the manual twin. Both rows then surface from every read: listed twice by
# get_upcoming, and notified twice by the reminder engine.
#
# The two rows aren't identical (different ids, different source_ids), so the
# only thing that identifies them as the same commitment is what the user
# actually sees: the title and the time. Hence dedupe on read by
# (normalized title, when_ts to the minute).
#
# Minute granularity is required, not just tolerance: EKReminder stores a due
# date as [.year,.month,.day,.hour,.minute] components (RemindersWriter.create),
# so a manual reminder created at 16:00:37 comes back from Reminders.app as
# 16:00:00. Exact-second matching would miss exactly the duplicates this exists
# to collapse. A minute is also far tighter than any two genuinely distinct
# commitments that happen to share a title.
_SOURCE_RANK = {"manual": 0, "reminders": 1, "calendar": 2}
_SOURCE_RANK_OTHER = 5
_SOURCE_RANK_SEED = 9      # QA fixtures never shadow a real row


def _dedupe_key(row: dict) -> tuple[str, int] | None:
    """Identity of the underlying commitment, or None for rows that can't be
    compared (no title / no time) — those are always kept as-is."""
    title = " ".join((row.get("title") or "").split()).casefold()
    when = row.get("when_ts")
    if not title or when is None:
        return None
    return (title, int(when // 60))


def _dedupe_rank(row: dict) -> tuple:
    """Which row of a duplicate group survives. Deterministic and *stable over
    time*, which matters more than which source wins: the survivor's id is the
    key notify_log is written under, so a survivor that changed as rows came
    and went would re-fire an already-sent reminder under the new id.

    'manual' wins for exactly that reason — for a Wisp-created reminder the
    manual row is inserted first (add_reminder writes it before publishing the
    mirror request), so it is the row that has been around long enough to carry
    the notify_log, and it stays the winner for the whole round trip through
    Reminders.app. Ties break on created_at then id, so the ordering is total.
    """
    source = row.get("source") or ""
    if source == QA_SEED_SOURCE:
        rank = _SOURCE_RANK_SEED
    else:
        rank = _SOURCE_RANK.get(source, _SOURCE_RANK_OTHER)
    return (rank, row.get("created_at") or 0.0, row.get("id") or "")

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

    # --- dedupe -----------------------------------------------------------
    def _collapse(self, rows: list[dict]) -> list[dict]:
        """Collapse duplicate-across-sources rows, preserving the caller's sort
        order (a group takes the position of its first member — every member
        shares a when_ts to the minute, so this never reorders anything).

        The survivor carries `duplicate_ids`/`duplicate_sources` (only present
        when it actually shadowed something) so a caller that mutates a
        commitment — cancel_event — can act on the whole group instead of
        deleting one row and watching its twin re-appear on the next sync.
        """
        groups: dict[tuple, list[dict]] = {}
        slots: list = []                     # dict = passthrough, tuple = group key
        for r in rows:
            key = _dedupe_key(r)
            if key is None:
                slots.append(r)
                continue
            if key not in groups:
                groups[key] = []
                slots.append(key)
            groups[key].append(r)

        out: list[dict] = []
        for slot in slots:
            if isinstance(slot, dict):
                out.append(slot)
                continue
            members = groups[slot]
            if len(members) == 1:
                out.append(members[0])
                continue
            members = sorted(members, key=_dedupe_rank)
            winner = dict(members[0])
            winner["duplicate_ids"] = [m["id"] for m in members[1:]]
            winner["duplicate_sources"] = [m["source"] for m in members[1:]]
            out.append(winner)
        return out

    def duplicate_ids(self, cid: str) -> list[str]:
        """Ids of other rows describing the same commitment as `cid` (same
        normalized title, same minute) — the group `_collapse` would fold
        together, looked up from one member instead of from a result set."""
        row = self._db.execute(
            "SELECT id, title, when_ts FROM commitments WHERE id=?", (cid,)).fetchone()
        if row is None:
            return []
        key = _dedupe_key(dict(row))
        if key is None:
            return []
        lo = key[1] * 60.0
        cands = self._db.execute(
            "SELECT id, title, when_ts FROM commitments "
            "WHERE id <> ? AND when_ts >= ? AND when_ts < ?", (cid, lo, lo + 60.0)).fetchall()
        return [r["id"] for r in cands if _dedupe_key(dict(r)) == key]

    # --- writes -----------------------------------------------------------
    def sync_source(self, source: str, items: list[dict]) -> int:
        """Replace the active set for `source` with `items` (each a commitment
        dict with at least kind/title/when_ts and a stable source_id).

        Upserts by (source_id, when_ts) — NOT source_id alone — so a moved time
        updates in place and keeps its notify_log; active rows for this source
        that vanished upstream are dropped. Manual commitments (a different
        source) are never touched — including the manual twin of a reminder
        Wisp itself mirrored into Reminders.app, which is why that pair has to
        be reconciled on read instead (see `_collapse`).

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

    def update_schedule(self, ids: list[str], when_ts: float,
                        title: str | None = None) -> int:
        """Move one collapsed commitment group to a new time.

        A Wisp-created reminder can have both a ``manual`` row and a mirrored
        ``reminders`` row.  Updating only the visible survivor would split the
        pair and make the old Apple Reminders copy reappear, so callers pass
        the survivor plus its ``duplicate_ids`` and this updates the group in
        one transaction.  Notification stages are cleared because they belong
        to the old schedule and must be eligible to fire at the new one.
        """
        unique_ids = list(dict.fromkeys(str(cid) for cid in ids if cid))
        if not unique_ids:
            return 0
        now = time.time()
        changed = 0
        with self._lock:
            for cid in unique_ids:
                if title is None:
                    cur = self._db.execute(
                        "UPDATE commitments SET when_ts=?, updated_at=? WHERE id=?",
                        (float(when_ts), now, cid))
                else:
                    cur = self._db.execute(
                        "UPDATE commitments SET title=?, when_ts=?, updated_at=? WHERE id=?",
                        (title, float(when_ts), now, cid))
                changed += cur.rowcount
                self._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
            self._db.commit()
        return changed

    def set_status(self, cid: str, status: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE commitments SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), cid))
            self._db.commit()
        return cur.rowcount > 0

    def mark_notified(self, cid: str, stage: str) -> None:
        """Record that `stage` fired — for this row AND for every duplicate of
        it (see `duplicate_ids`).

        Marking the whole group is what makes collapsing safe on the
        notification side. Reads only ever hand the engine the group's
        survivor, so only the survivor's id gets marked; if that row is later
        deleted (cancel_event, or an upstream item vanishing from a sync) its
        notify_log rows go with it, the twin stops being shadowed, and a
        reminder the user already received would fire a second time under the
        twin's id. Writing the stage to the whole group is cheap, idempotent,
        and immunizes whichever member ends up surviving.
        """
        now = time.time()
        ids = [cid] + self.duplicate_ids(cid)
        with self._lock:
            self._db.executemany(
                "INSERT OR IGNORE INTO notify_log (commitment_id, stage, sent_at) "
                "VALUES (?,?,?)", [(i, stage, now) for i in ids])
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
        # LIMIT 1 would return an arbitrary member of a duplicate group (they
        # share when_ts, so SQLite's tie-break decides) — take a small window
        # and collapse it so the chip shows the same row the lists do.
        rows = self._db.execute(
            "SELECT * FROM commitments WHERE status='active' AND when_ts >= ?"
            + _seed_clause() +
            " ORDER BY when_ts ASC LIMIT 50", (now - 300,)).fetchall()  # 5-min grace
        items = self._collapse([dict(r) for r in rows])
        return items[0] if items else None

    def upcoming(self, now: float | None = None, days: int = 7) -> list[dict]:
        now = now if now is not None else time.time()
        rows = self._db.execute(
            "SELECT * FROM commitments WHERE status='active' AND when_ts >= ? AND when_ts <= ?"
            + _seed_clause() +
            " ORDER BY when_ts ASC", (now - 300, now + days * 86400)).fetchall()
        return self._collapse([dict(r) for r in rows])

    def recently_added(self, since_ts: float, limit: int = 20) -> list[dict]:
        """Commitments Wisp FIRST SAW after `since_ts`, newest-first.

        Keyed on `created_at`, not `updated_at`, and that distinction is the
        whole reason this works: the sync upsert above refreshes `updated_at`
        for every row on every sync (it's in the DO UPDATE SET), so ordering by
        it would just return whatever synced last — i.e. everything. Notably
        `created_at` is NOT in that DO UPDATE SET, so it survives re-syncs and
        genuinely means "when this reminder/event first appeared".

        Deliberately not date-filtered on `when_ts`: a reminder created five
        minutes ago for next month is exactly the kind of NEW thing this is
        meant to surface, and its event date says nothing about that.
        """
        rows = self._db.execute(
            "SELECT * FROM commitments WHERE status='active' AND created_at >= ?"
            + _seed_clause() +
            " ORDER BY created_at DESC LIMIT ?", (since_ts, limit)).fetchall()
        return self._collapse([dict(r) for r in rows])

    def active_between(self, start_ts: float | None = None,
                       end_ts: float | None = None) -> list[dict]:
        """Active commitments in an optional half-open time range.

        Bulk reminder operations need local calendar-day boundaries and a
        genuine all-time view. Combining ``upcoming`` and ``history`` leaves a
        five-minute seam around now and makes "all" depend on arbitrary
        horizons, so keep this selection in the store and reuse normal dedupe.
        """
        where = ["status='active'"]
        params: list[float] = []
        if start_ts is not None:
            where.append("when_ts >= ?")
            params.append(float(start_ts))
        if end_ts is not None:
            where.append("when_ts < ?")
            params.append(float(end_ts))
        rows = self._db.execute(
            "SELECT * FROM commitments WHERE " + " AND ".join(where)
            + _seed_clause() + " ORDER BY when_ts ASC", params).fetchall()
        return self._collapse([dict(r) for r in rows])

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
        return self._collapse([dict(r) for r in rows])

    def active_future(self, now: float | None = None, horizon_days: int = 30) -> list[dict]:
        """All active commitments from now out to the horizon — the reminder
        engine's working set, deduped like every other read path so a
        commitment that exists under two sources notifies once rather than
        twice. Same 5-min grace as next_active/upcoming: a
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
        return self._collapse([dict(r) for r in rows])


# module-level singleton
assistant_store = AssistantStore()

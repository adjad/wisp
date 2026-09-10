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
import json
import math
from contextlib import contextmanager
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from service.assistant.recovery import recovery_guidance
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
_COMMITMENTS_SCHEMA = """
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
"""
_NOTIFY_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_log (
    commitment_id TEXT,
    stage         TEXT,          -- e.g. 'T-1d', 'T-30m', 'due'
    sent_at       REAL,
    PRIMARY KEY (commitment_id, stage)
);
"""

class AssistantStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path = path.expanduser().resolve()
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        try:
            with self._db:
                # Lock before inspecting schema so simultaneous startups cannot
                # make competing migration decisions. executescript() would
                # implicitly commit and must not be used anywhere in this path.
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(_COMMITMENTS_SCHEMA)
                self._db.execute(_NOTIFY_SCHEMA)
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_events (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE, target TEXT NOT NULL,
                    created_at REAL NOT NULL, last_attempt_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','ack','superseded')),
                    acknowledged_at REAL, expires_at REAL, claim_token TEXT, result TEXT
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_schedule_versions (
                    commitment_id TEXT PRIMARY KEY, version TEXT NOT NULL
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_completion (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, completed_at REAL NOT NULL
                )""")
                self._migrate_unique_constraint()
                cols = {r["name"] for r in self._db.execute("PRAGMA table_info(commitments)")}
                for column in ("organizer", "account"):
                    if column not in cols:
                        self._db.execute(f"ALTER TABLE commitments ADD COLUMN {column} TEXT")
                if self._has_interrupted_migration():
                    self._recover_migration_rows()
                # The old index follows the renamed table and is dropped with
                # it. Create the replacement only after migration/recovery.
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_commit_when ON commitments(status, when_ts)")
                self._migrate_calendar_results()
        except BaseException:
            self._db.close()
            raise

    def _has_interrupted_migration(self) -> bool:
        return self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='commitments_old_migrating'"
        ).fetchone() is not None

    def _migrate_unique_constraint(self) -> None:
        """Rebuild legacy uniqueness inside the constructor's transaction."""
        # A UNIQUE(...) table constraint creates an auto-index with sql=NULL in
        # sqlite_master (not text we can grep) — inspect its actual columns via
        # PRAGMA index_info instead.
        has_new_constraint = False
        has_old_constraint = False
        for idx in self._db.execute("PRAGMA index_list(commitments)").fetchall():
            if not idx["unique"]:
                continue
            index_name = idx["name"].replace('"', '""')
            info_cols = [c["name"] for c in
                        self._db.execute(f'PRAGMA index_info("{index_name}")').fetchall()]
            if (len(info_cols) == 3 and set(info_cols) == {"source", "source_id", "when_ts"}
                    and not idx["partial"]):
                has_new_constraint = True
            if len(info_cols) == 2 and set(info_cols) == {"source", "source_id"}:
                has_old_constraint = True
        if has_new_constraint and not has_old_constraint:
            return
        if self._has_interrupted_migration():
            raise RuntimeError(
                recovery_guidance(
                    self.path,
                    "both legacy tables exist; the live table still uses the old constraint",
                ))
        self._db.execute("ALTER TABLE commitments RENAME TO commitments_old_migrating")
        self._db.execute(_COMMITMENTS_SCHEMA)
        self._recover_migration_rows()

    def _recover_migration_rows(self) -> None:
        """Copy by name, verify every source row, then retire the old table.

        Older versions could commit the rename before failing the copy. Later
        startups may have added live rows. Keep those rows, accept identical
        copies, and roll back on conflicts rather than choosing which to lose.
        Only the two known additive columns may default to NULL.
        """
        columns = [r["name"] for r in self._db.execute("PRAGMA table_info(commitments)")]
        old_columns = {r["name"] for r in self._db.execute(
            "PRAGMA table_info(commitments_old_migrating)")}
        missing = set(columns) - old_columns - {"organizer", "account"}
        if missing or old_columns - set(columns):
            raise RuntimeError(recovery_guidance(
                self.path, "the interrupted migration has unrecognized columns"))
        # TEXT PRIMARY KEY permits NULL in SQLite. Such rows have no stable
        # identity for recognizing a partial copy; EXISTS could otherwise count
        # one live row as preserving multiple identical originals.
        if (self._db.execute(
                "SELECT 1 FROM commitments_old_migrating WHERE id IS NULL LIMIT 1").fetchone()
                and self._db.execute(
                    "SELECT 1 FROM commitments WHERE id IS NULL LIMIT 1").fetchone()):
            raise RuntimeError(recovery_guidance(
                self.path, "live and interrupted-migration rows have overlapping NULL IDs"))
        # Quote identifiers even though historical column names are fixed.
        quoted = ['"' + name.replace('"', '""') + '"' for name in columns]
        expressions = [f"old.{name}" if column in old_columns else "NULL"
                       for column, name in zip(columns, quoted)]
        source_count = self._db.execute(
            "SELECT COUNT(*) FROM commitments_old_migrating").fetchone()[0]
        live_count = self._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        try:
            copied = self._db.execute(
                f"INSERT INTO commitments ({', '.join(quoted)}) "
                f"SELECT {', '.join(expressions)} FROM commitments_old_migrating AS old "
                "WHERE NOT EXISTS (SELECT 1 FROM commitments AS live WHERE live.id IS old.id)"
            ).rowcount
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(recovery_guidance(
                self.path, "live and interrupted-migration rows conflict")) from exc
        matches = " AND ".join(f"live.{name} IS {expression}"
                               for name, expression in zip(quoted, expressions))
        preserved = self._db.execute(
            "SELECT COUNT(*) FROM commitments_old_migrating AS old WHERE EXISTS "
            f"(SELECT 1 FROM commitments AS live WHERE {matches})"
        ).fetchone()[0]
        final_count = self._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        if preserved != source_count or final_count != live_count + copied:
            raise RuntimeError(recovery_guidance(
                self.path, "automatic migration could not prove that every row was preserved"))
        self._db.execute("DROP TABLE commitments_old_migrating")

    def _migrate_calendar_results(self) -> None:
        # Prior versions wrote ok/error without a status. Only upgrade terminal
        # results whose strict types and consistency prove their meaning. Keep
        # ambiguous historical data intact; it must never authorize re-execution.
        for row in self._db.execute("SELECT id,kind,result FROM assistant_events WHERE result IS NOT NULL").fetchall():
            if row["kind"] not in {"create_calendar_event", "delete_calendar_event"}:
                continue
            try:
                result = json.loads(row["result"])
                if not isinstance(result, dict) or "status" in result:
                    continue
                status = "succeeded" if result.get("ok") is True else "failed"
                if result.get("ok") is False and result.get("error") == "Wisp was interrupted; native Calendar outcome is unknown":
                    status = "unknown"
                candidate = {**result, "status": status}
                canonical = self.calendar_result(row["kind"], candidate)
            except (ValueError, TypeError):
                continue
            self._db.execute("UPDATE assistant_events SET result=? WHERE id=?",
                             (json.dumps(canonical, sort_keys=True, allow_nan=False), row["id"]))

    @contextmanager
    def _write_transaction(self):
        # sqlite3's connection context manager does not begin a transaction for
        # SELECT. Lock the database before validating any read-modify-write
        # decision; the Python lock alone protects only this one connection.
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            yield

    @staticmethod
    def calendar_payload(kind: str, payload: dict) -> dict:
        if not isinstance(kind, str) or not isinstance(payload, dict) or kind not in {"create_calendar_event", "delete_calendar_event"}:
            raise ValueError("invalid Calendar payload")
        fields = ({"type", "action_id", "title", "when_ts", "duration_min", "location"}
                  if kind == "create_calendar_event" else {"type", "action_id", "source_id", "when_ts"})
        if set(payload) != fields or payload.get("type") != kind:
            raise ValueError("Calendar payload fields do not match the action")
        for field in fields - {"when_ts", "duration_min"}:
            if not isinstance(payload[field], str) or (field != "location" and not payload[field].strip()):
                raise ValueError("invalid Calendar payload text")
        when = payload["when_ts"]
        try:
            valid_time = type(when) in (int, float) and math.isfinite(when) and when > 0
        except OverflowError:
            valid_time = False
        if not valid_time:
            raise ValueError("invalid Calendar time")
        if kind == "create_calendar_event":
            duration = payload["duration_min"]
            if type(duration) is not int or not 1 <= duration <= 10080:
                raise ValueError("invalid Calendar duration")
        return {**payload, "when_ts": float(when)}

    @staticmethod
    def calendar_result(kind: str, result: dict) -> dict:
        if not isinstance(result, dict):
            raise ValueError("Calendar result must be an object")
        required = {"ok", "status", "error"}
        allowed = required | ({"source_id"} if kind == "create_calendar_event" else set())
        if not required <= set(result) or not set(result) <= allowed:
            raise ValueError("invalid Calendar result fields")
        if type(result["ok"]) is not bool or not isinstance(result["error"], str):
            raise ValueError("Calendar result requires a Boolean ok and string error")
        expected = {"succeeded"} if result["ok"] else {"failed", "unknown"}
        if not isinstance(result["status"], str) or result["status"] not in expected:
            raise ValueError("Calendar result status contradicts ok")
        if (result["ok"] and result["error"]) or (not result["ok"] and not result["error"].strip()):
            raise ValueError("Calendar result error contradicts ok")
        if result["ok"] and kind == "create_calendar_event":
            if not isinstance(result.get("source_id"), str) or not result["source_id"].strip():
                raise ValueError("successful creation requires native source_id")
        elif "source_id" in result:
            raise ValueError("unexpected native source_id")
        return dict(result)

    # Event payloads are immutable. A retry carries the original identity and
    # content, even if the producer has since recomposed its display text.
    def enqueue_event(self, payload: dict, *, dedupe_key: str | None = None,
                      target: dict | None = None, expires_at: float | None = None,
                      reminder_snapshot: dict | None = None) -> dict | None:
        """Persist an event, or return None without writes for a stale reminder snapshot."""
        kind = payload.get("type")
        if not isinstance(kind, str) or not kind:
            raise ValueError("event type is required")
        clean = {k: v for k, v in payload.items() if k != "event_id"}
        key = dedupe_key or uuid.uuid4().hex
        with self._write_transaction():
            # Preparation happens outside the write transaction. Reject every
            # changed member before either superseding an event or inserting
            # one; the next scheduler tick can prepare the replacement.
            if reminder_snapshot is not None:
                if not reminder_snapshot:
                    return None
                member = next(iter(reminder_snapshot.values()))
                identity = _dedupe_key(member)
                if identity is None:
                    return None
                current = self._db.execute(
                    "SELECT id,title,when_ts FROM commitments WHERE status='active' AND when_ts >= ? AND when_ts < ?"
                    + _seed_clause(), (identity[1] * 60, (identity[1] + 1) * 60)).fetchall()
                if {r["id"] for r in current if _dedupe_key(dict(r)) == identity} != set(reminder_snapshot):
                    return None
                for cid, expected in reminder_snapshot.items():
                    row = self._db.execute(
                        "SELECT title,kind,context,when_ts,status FROM commitments WHERE id=?", (cid,)).fetchone()
                    if (row is None or {**dict(row), "generation": self.reminder_generation(cid)} != expected):
                        return None
            if target and target.get("type") == "reminder":
                for old in self._db.execute("SELECT * FROM assistant_events WHERE kind='reminder' AND state='pending'").fetchall():
                    previous = json.loads(old["target"])
                    if (previous.get("identity") == target.get("identity")
                            and set(previous.get("stages", [])) < set(target.get("stages", []))):
                        self._db.execute("UPDATE assistant_events SET state='superseded' WHERE id=?", (old["id"],))
            self._db.execute(
                "INSERT OR IGNORE INTO assistant_events "
                "(id,kind,payload,dedupe_key,target,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, kind, json.dumps(clean, sort_keys=True), key,
                 json.dumps(target or {}, sort_keys=True), time.time(), expires_at))
            row = self._db.execute(
                "SELECT * FROM assistant_events WHERE dedupe_key=?", (key,)).fetchone()
            if row["kind"] != kind:
                raise ValueError("dedupe key belongs to another event kind")
            return self._event(row)

    @staticmethod
    def _event(row) -> dict:
        return {**dict(row), "payload": json.loads(row["payload"]),
                "target": json.loads(row["target"]),
                "result": json.loads(row["result"]) if row["result"] else None}

    def event(self, event_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM assistant_events WHERE id=?", (event_id,)).fetchone()
            return self._event(row) if row else None

    def event_by_key(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM assistant_events WHERE dedupe_key=?", (key,)).fetchone()
            return self._event(row) if row else None

    def reminder_generation(self, cid: str) -> str:
        row = self._db.execute("SELECT version FROM assistant_schedule_versions WHERE commitment_id=?", (cid,)).fetchone()
        return row[0] if row else ""

    def _reminder_targets(self, target: dict, payload: dict) -> list[str]:
        # Use captured per-row schedules: native Reminders truncates seconds,
        # while a rescheduled row must never inherit the old stage receipt.
        # Older versions could pair a stale primary payload with fresh target
        # schedules, even within one minute. Bind the primary exactly while
        # still allowing its native twins to have different seconds.
        if (payload.get("when_ts") != target.get("when_ts")
                or payload.get("when_ts") != target.get("schedules", {}).get(payload.get("commitment_id"))):
            return []
        ids = []
        for cid, expected in target.get("schedules", {}).items():
            row = self._db.execute("SELECT title,when_ts,status FROM commitments WHERE id=?", (cid,)).fetchone()
            if (row and row["status"] == "active" and row["when_ts"] == expected
                    and row["when_ts"] is not None and int(row["when_ts"] // 60) == target["identity"][1]
                    and self.reminder_generation(cid) == target.get("generations", {}).get(cid, "")
                    and " ".join(row["title"].split()).casefold() == target["identity"][0]):
                ids.append(cid)
        return ids

    def _invalidate_reminders(self) -> None:
        for row in self._db.execute("SELECT id,target,payload FROM assistant_events WHERE kind='reminder' AND state='pending'").fetchall():
            target = json.loads(row["target"])
            if target.get("type") == "reminder" and not self._reminder_targets(target, json.loads(row["payload"])):
                self._db.execute("UPDATE assistant_events SET state='superseded' WHERE id=?", (row["id"],))

    def pending_events(self) -> list[dict]:
        with self._write_transaction():
            self._invalidate_reminders()
            return [self._event(r) for r in self._db.execute(
                "SELECT * FROM assistant_events WHERE state='pending' ORDER BY created_at,id")]

    def event_attempt(self, event_id: str) -> bool:
        with self._write_transaction():
            self._invalidate_reminders()
            return bool(self._db.execute(
                "UPDATE assistant_events SET attempts=attempts+1,last_attempt_at=? "
                "WHERE id=? AND state='pending'", (time.time(), event_id)).rowcount)

    def completion(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM assistant_completion WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def acknowledge_event(self, event_id: str, kind: str) -> bool:
        """Commit receipt and completion together; mismatched/stale targets never
        finalize a different reminder schedule. Repeated valid receipts succeed."""
        with self._write_transaction():
            row = self._db.execute("SELECT * FROM assistant_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise KeyError("unknown event")
            if row["kind"] != kind:
                raise ValueError("event kind does not match")
            if row["state"] == "ack":
                return True
            if row["state"] == "superseded":
                return True  # Receipt retry is a no-op, never a future stage.
            if row["state"] != "pending":
                raise ValueError("invalid event state")
            target = json.loads(row["target"])
            if target.get("type") == "calendar":
                if row["result"] is None:
                    raise ValueError("native result required before acknowledgement")
                self.calendar_result(kind, json.loads(row["result"]))
            now = time.time()
            if target.get("type") == "reminder":
                targets = self._reminder_targets(target, json.loads(row["payload"]))
                if not targets:
                    self._db.execute("UPDATE assistant_events SET state='superseded' WHERE id=?", (event_id,))
                    return True
                for cid in targets:
                    self._db.executemany("INSERT OR IGNORE INTO notify_log VALUES (?,?,?)",
                                         [(cid, stage, now) for stage in target["stages"]])
            elif target.get("type") == "brief":
                self._db.execute(
                    "INSERT INTO assistant_completion VALUES ('daily_brief',?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value), "
                    "completed_at=excluded.completed_at", (target["date"], now))
            elif target.get("type") == "scheduled_unknown":
                changed = self._db.execute(
                    "UPDATE scheduled_sends SET notified_at=COALESCE(notified_at,?) "
                    "WHERE id=? AND status='unknown'", (now, target["id"])).rowcount
                if not changed:
                    raise ValueError("scheduled notice is not in unknown state")
            self._db.execute("UPDATE assistant_events SET state='ack',acknowledged_at=? WHERE id=?",
                             (now, event_id))
            return True

    def claim_calendar_action(self, event_id: str, kind: str, action_id: str, payload: dict) -> dict:
        proposed = self.calendar_payload(kind, payload)
        with self._write_transaction():
            row = self._db.execute("SELECT * FROM assistant_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise KeyError("unknown action")
            stored = self.calendar_payload(row["kind"], json.loads(row["payload"]))
            if (row["kind"] != kind or json.loads(row["target"]).get("type") != "calendar"
                    or not isinstance(action_id, str) or stored["action_id"] != action_id
                    or row["dedupe_key"] != "action:" + action_id or stored != proposed):
                raise ValueError("action identity or payload does not match")
            identity = {"event_id": event_id, "action_id": action_id, "kind": kind, "payload": stored}
            if row["result"]:
                return {**identity, "execute": False, "recorded": True,
                        "result": self.calendar_result(kind, json.loads(row["result"]))}
            if row["claim_token"]:
                return {**identity, "execute": False, "recorded": False,
                        "error": "native outcome unknown; do not retry the action"}
            if row["state"] != "pending":
                raise ValueError("action is not pending")
            if row["expires_at"] is None or row["expires_at"] < time.time():
                result = {"ok": False, "status": "failed", "error": "action expired before native execution"}
                self._db.execute("UPDATE assistant_events SET result=?,state='ack',acknowledged_at=? WHERE id=?",
                                 (json.dumps(result, sort_keys=True), time.time(), event_id))
                return {**identity, "execute": False, "recorded": True, "result": result}
            token = uuid.uuid4().hex
            self._db.execute("UPDATE assistant_events SET claim_token=? WHERE id=?", (token, event_id))
            return {**identity, "execute": True, "recorded": False, "claim_token": token}

    def complete_calendar_action(self, event_id: str, kind: str, token: str, result: dict) -> bool:
        """Reconcile the successful native receipt and action in one transaction.
        Late receipts are valid; a timeout cannot prove native failure."""
        result = self.calendar_result(kind, result)
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        with self._write_transaction():
            row = self._db.execute("SELECT * FROM assistant_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise KeyError("unknown action")
            target = json.loads(row["target"])
            if (row["kind"] != kind or target.get("type") != "calendar"
                    or not token or row["claim_token"] != token):
                raise ValueError("action identity or claim does not match")
            payload = self.calendar_payload(kind, json.loads(row["payload"]))
            if row["result"]:
                if row["result"] != encoded:
                    raise ValueError("action already has a different result")
                return True
            if result.get("ok") is True:
                if kind == "create_calendar_event":
                    source_id = result.get("source_id")
                    if not isinstance(source_id, str) or not source_id:
                        raise ValueError("successful creation requires native source_id")
                    now = time.time()
                    self._db.execute(
                        "INSERT INTO commitments (id,source,source_id,kind,title,when_ts,location,"
                        "status,confidence,created_at,updated_at) VALUES (?,?,?,'event',?,?,?,'active',1,?,?) "
                        "ON CONFLICT(source,source_id,when_ts) DO NOTHING",
                        (uuid.uuid4().hex, "calendar", source_id, payload["title"], payload["when_ts"],
                         payload.get("location", ""), now, now))
                elif kind == "delete_calendar_event":
                    self._db.execute(
                        "UPDATE commitments SET status='dismissed',updated_at=? "
                        "WHERE source='calendar' AND source_id=? AND when_ts=?",
                        (time.time(), payload["source_id"], payload["when_ts"]))
            self._db.execute("UPDATE assistant_events SET result=? WHERE id=?", (encoded, event_id))
            return True

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
        version = uuid.uuid4().hex
        with self._write_transaction():
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
                self._db.execute("INSERT INTO assistant_schedule_versions VALUES (?,?) "
                                 "ON CONFLICT(commitment_id) DO UPDATE SET version=excluded.version", (cid, version))
                self._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
            # Retire every pending completion referencing the changed group
            # in the same transaction as its revision and notification reset.
            for event in self._db.execute("SELECT id,target FROM assistant_events WHERE kind='reminder' AND state='pending'").fetchall():
                target = json.loads(event["target"])
                if set(target.get("schedules", {})) & set(unique_ids):
                    self._db.execute("UPDATE assistant_events SET state='superseded' WHERE id=?", (event["id"],))
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

    def reminder_snapshot(self, now: float | None = None, horizon_days: int = 30) -> list[dict]:
        """Capture payload inputs, twin schedules and revisions in one SELECT.

        Each twin retains its own timestamp (native sync can truncate seconds).
        The private snapshot is checked again atomically when enqueueing.
        """
        now = now if now is not None else time.time()
        with self._lock:
            rows = self._db.execute(
                "SELECT commitments.*, COALESCE(v.version, '') AS generation FROM commitments "
                "LEFT JOIN assistant_schedule_versions v ON v.commitment_id=commitments.id "
                "WHERE status='active' AND when_ts >= ? AND when_ts <= ?"
                + _seed_clause() + " ORDER BY when_ts ASC",
                (now - 300, now + horizon_days * 86400)).fetchall()
        members = {r["id"]: {k: r[k] for k in ("title", "kind", "context", "when_ts", "status", "generation")}
                   for r in rows}
        candidates = self._collapse([dict(r) for r in rows])
        for c in candidates:
            c["reminder_snapshot"] = {cid: members[cid] for cid in [c["id"]] + c.get("duplicate_ids", [])}
        return candidates

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

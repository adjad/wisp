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
import weakref
import hashlib
from dataclasses import dataclass
from pathlib import Path

from service.publication_authority import (Authority, LIVE_CLOCK, Witness, revocation,
    PublicationLease, PublicationUnavailable, owned_write, assert_mutation_allowed)
from service.assistant.recovery import recovery_guidance
from service.paths import MOE_DIR

# Most undated reminders one native snapshot may carry. The app sends at most this many
# plus the TRUE total, so the backend can tell a complete list from a truncated one.
UNDATED_MAX = 200
# Sanity bounds on what a native read may declare, so a malformed or hostile client cannot
# crash the sync with an absurd number or fill the store with huge strings.
UNDATED_TOTAL_MAX = 1_000_000
UNDATED_ID_MAX = 200
UNDATED_TITLE_MAX = 500
UNDATED_CONTEXT_MAX = 200


class UndatedSnapshot:
    """One validated native read of the incomplete Apple reminders that have no due date."""

    __slots__ = ("rows", "total", "truncated")

    def __init__(self, rows: list[tuple[str, str, str | None, float | None]], total: int,
                 truncated: bool):
        self.rows = rows
        self.total = total
        self.truncated = truncated


def parse_undated_snapshot(raw, total) -> UndatedSnapshot | None:
    """Validate the optional ``undated`` list and its ``undated_total`` from a sync post.

    Returns None for anything not wholly valid: a missing or non-list payload, any entry
    that is not an object with a non-blank string ``source_id``, a string ``title`` and a
    string-or-null ``context``, or a total that is not an integer (booleans are not) at
    least as large as the list. An optional ``created_ts`` (EventKit creationDate) is kept
    when it is a usable time and dropped otherwise: a missing or bad creation date makes the
    reminder's age unknown, which never makes it stale, so it is no reason to reject a read. None means "leave the stored list alone": it must read as
    stale afterwards, never as a fresh answer. A blank title is kept as "(untitled)".
    More than UNDATED_MAX entries are cut to the first UNDATED_MAX and reported truncated.
    """
    if not isinstance(raw, list) or isinstance(total, bool) or not isinstance(total, int):
        return None
    if total < len(raw) or total > UNDATED_TOTAL_MAX:
        return None
    from service.assistant.stale_reminders import valid_created_ts
    rows: dict[str, tuple[str, str, str | None, float | None]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        source_id, title, context = entry.get("source_id"), entry.get("title"), entry.get("context")
        if (not isinstance(source_id, str) or not source_id.strip() or not isinstance(title, str)
                or not (context is None or isinstance(context, str))):
            return None
        # Key on the stored (capped) id so two long ids sharing a prefix merge instead of
        # colliding on the table's UNIQUE source_id.
        sid = source_id.strip()[:UNDATED_ID_MAX]
        rows[sid] = (sid, title.strip()[:UNDATED_TITLE_MAX] or "(untitled)",
                     (context or None) and context[:UNDATED_CONTEXT_MAX],
                     valid_created_ts(entry.get("created_ts")))
    kept = list(rows.values())[:UNDATED_MAX]
    # Truncated means items were left out: the app declared more than it sent, or this side
    # had to cut the list at the cap. Merging a duplicate id is not truncation.
    return UndatedSnapshot(kept, max(total, len(rows)),
                           total > len(raw) or len(rows) > UNDATED_MAX)

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
# Rows the user created in Wisp itself; never an authoritative replace-set.
MANUAL_SOURCE = "manual"


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


# Native Calendar writes the app performs through the claimed event bridge.
CALENDAR_KINDS = frozenset({"create_calendar_event", "delete_calendar_event", "move_calendar_event",
                            "cancel_calendar_event"})


def _dedupe_key(row: dict) -> tuple[str, int, bool] | None:
    """Identity of the underlying commitment, or None for rows that can't be
    compared (no title / no time) — those are always kept as-is."""
    title = " ".join((row.get("title") or "").split()).casefold()
    when = row.get("when_ts")
    if not title or when is None:
        return None
    return (title, int(when // 60), bool(row.get('all_day')))


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
    due_date    TEXT,               -- floating reminder date; never an alert time
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

class _BehaviorReceiptHead:
    """Shared by live connections to one database; never restored from disk."""
    def __init__(self):
        self.authority = Authority("native-reminders-permission", owner=self)
        self.metadata = None
        self.expires_monotonic = 0.0


_behavior_receipt_heads = weakref.WeakValueDictionary()
_behavior_receipt_registry_lock = threading.RLock()


def _behavior_reminders_receipt(value):
    if (type(value) is not dict or value.get("available") is not True
            or value.get("syncing") is not False):
        return None
    diagnostics = value.get("diagnostics")
    if (type(diagnostics) is not dict or diagnostics.get("complete", True) is not True
            or diagnostics.get("partial", False) is not False):
        return None
    last, started = value.get("last_sync"), value.get("snapshot_started_at")
    if any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in (last, started)):
        return None
    coverage = tuple(diagnostics.get(key) for key in ("coverage_start", "coverage_end"))
    if any(t is not None and (type(t) not in (int, float) or not math.isfinite(t)) for t in coverage):
        return None
    return (last, started, *coverage)


_delivery_issuer = object()


_BEHAVIOR_RECEIPTS_SCHEMA = """CREATE TABLE IF NOT EXISTS assistant_behavior_action_receipts (
    owner_id TEXT NOT NULL, nudge_id TEXT NOT NULL, action_index INTEGER NOT NULL CHECK(action_index>=0),
    commitment_id TEXT NOT NULL UNIQUE, receipt_state TEXT NOT NULL CHECK(receipt_state IN ('active','consumed')),
    type TEXT, kind TEXT, title TEXT, context TEXT, when_ts REAL, end_ts REAL, created_at REAL,
    PRIMARY KEY(owner_id,nudge_id,action_index),
    CHECK((receipt_state='active' AND type IS NOT NULL AND kind IS NOT NULL
           AND type IN ('add_reminder','add_hold') AND kind IN ('reminder','event')
           AND ((type='add_reminder' AND kind='reminder' AND end_ts IS NULL) OR (type='add_hold' AND kind='event'))
           AND title IS NOT NULL AND context IS NOT NULL AND when_ts IS NOT NULL AND created_at IS NOT NULL)
       OR (receipt_state='consumed' AND type IS NULL AND kind IS NULL AND title IS NULL AND context IS NULL
           AND when_ts IS NULL AND end_ts IS NULL AND created_at IS NULL))
)"""
_BEHAVIOR_RECEIPT_PAYLOAD = ("type","kind","title","context","when_ts","end_ts","created_at")


def behavior_owner_id(home):
    """One canonical home encoding, outside publication; never source permission."""
    canonical = Path(home).expanduser().resolve()
    return hashlib.sha256(b"wisp.behavior-owner.v1\0"+os.fsencode(canonical)).hexdigest()


@dataclass(frozen=True,eq=False,repr=False)
class PreparedNudgeWrite:
    store: object
    runtime: object
    data: str
    binding: object
    issuer: object

    def __reduce_ex__(self, protocol):
        raise TypeError("Prepared delivery writes cannot restore authority")


@dataclass(frozen=True,eq=False,repr=False)
class CommittedNudge:
    plan: PreparedNudgeWrite
    event_id: str
    payload: str
    pending: bool
    issuer: object

    def __reduce_ex__(self, protocol):
        raise TypeError("Delivery receipts cannot restore authority")


class AssistantStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        assert_mutation_allowed()
        path = path.expanduser().resolve()
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._delivery_writer = Authority("assistant-nudge-writer",owner=self)
        self._delivery_writer.publish()
        self._nudge_plans = weakref.WeakSet()
        self._nudge_receipts = weakref.WeakSet()
        self._transaction_write: bool | None = None
        self._behavior_receipt_stages = []
        # Resolve aliases once, outside transaction locks/SQL. A fresh process
        # has no head and cannot recover native permission from persisted JSON.
        try:
            identity_stat = path.stat()
            identity = (identity_stat.st_dev, identity_stat.st_ino)
        except OSError:
            identity = ("path", str(path))
        with revocation(), _behavior_receipt_registry_lock:
            head = _behavior_receipt_heads.get(identity)
            if head is None:
                head = _BehaviorReceiptHead()
                _behavior_receipt_heads[identity] = head
            self._behavior_receipt_head = head
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
                event_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(assistant_events)")}
                if "claimed_at" not in event_cols:
                    self._db.execute("ALTER TABLE assistant_events ADD COLUMN claimed_at REAL")
                if "claim_order" not in event_cols:
                    self._db.execute("ALTER TABLE assistant_events ADD COLUMN claim_order INTEGER")
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assistant_events_claim_order "
                    "ON assistant_events(claim_order)")
                next_order = self._db.execute(
                    "SELECT COALESCE(MAX(claim_order),0) FROM assistant_events").fetchone()[0]
                for old in self._db.execute(
                    "SELECT id FROM assistant_events WHERE claim_token IS NOT NULL "
                    "AND claim_order IS NULL AND kind IN ('create_reminder','update_reminder',"
                    "'complete_reminder','delete_reminder') "
                    "ORDER BY COALESCE(claimed_at,created_at),rowid").fetchall():
                    next_order += 1
                    self._db.execute("UPDATE assistant_events SET claim_order=? WHERE id=?",
                                     (next_order, old["id"]))
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_schedule_versions (
                    commitment_id TEXT PRIMARY KEY, version TEXT NOT NULL
                )""")
                had_reminder_terminals = self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='assistant_reminder_terminals'").fetchone() is not None
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_reminder_terminals (
                    source_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    recorded_at REAL NOT NULL, action_id TEXT NOT NULL
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_reminder_writes (
                    source_id TEXT PRIMARY KEY, recorded_at REAL NOT NULL,
                    action_id TEXT NOT NULL
                )""")
                if not had_reminder_terminals:
                    self._migrate_reminder_terminals()
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_reminder_verified (
                    source_id TEXT PRIMARY KEY, recorded_at REAL NOT NULL,
                    claim_order INTEGER NOT NULL DEFAULT 0
                )""")
                verified_cols = {r["name"] for r in
                                 self._db.execute("PRAGMA table_info(assistant_reminder_verified)")}
                if "claim_order" not in verified_cols:
                    self._db.execute(
                        "ALTER TABLE assistant_reminder_verified "
                        "ADD COLUMN claim_order INTEGER NOT NULL DEFAULT 0")
                for table in ("assistant_reminder_terminals", "assistant_reminder_writes"):
                    self._db.execute(
                        "INSERT INTO assistant_reminder_verified(source_id,recorded_at) "
                        f"SELECT source_id,recorded_at FROM {table} WHERE true "
                        "ON CONFLICT(source_id) DO UPDATE SET recorded_at=MAX("
                        "assistant_reminder_verified.recorded_at,excluded.recorded_at)")
                self._migrate_reminder_verified_orders()
                # Incomplete Apple reminders with NO due date. Not commitments: nothing is
                # scheduled, so they live apart from `commitments` and no time-windowed
                # query (notifications, overdue, schedules) can ever see them.
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_undated_reminders (
                    source_id TEXT PRIMARY KEY, title TEXT NOT NULL, context TEXT,
                    synced_at REAL NOT NULL
                )""")
                # EventKit creationDate, used only to age undated reminders (stale_reminders.py).
                undated_cols = {r["name"] for r in
                                self._db.execute("PRAGMA table_info(assistant_undated_reminders)")}
                if "created_ts" not in undated_cols:
                    self._db.execute("ALTER TABLE assistant_undated_reminders ADD COLUMN created_ts REAL")
                # The user's answer to "keep or delete this old undated reminder?". Only
                # "keep" is stored: a verified Delete removes the reminder itself.
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_stale_reminder_decisions (
                    source_id TEXT PRIMARY KEY, decision TEXT NOT NULL CHECK (decision IN ('keep')),
                    title TEXT NOT NULL, decided_at REAL NOT NULL
                )""")
                # One row describing the undated list as a whole: which native snapshot it
                # came from and whether it is complete. Written in the same transaction as
                # the Reminders receipt, so a list is "current" only when both agree.
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_undated_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1), snapshot_started_at REAL,
                    status TEXT NOT NULL, total INTEGER NOT NULL, stored INTEGER NOT NULL,
                    recorded_at REAL NOT NULL
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS assistant_completion (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, completed_at REAL NOT NULL
                )""")
                self._migrate_unique_constraint()
                cols = {r["name"] for r in self._db.execute("PRAGMA table_info(commitments)")}
                for column in ("organizer", "account", "due_date"):
                    if column not in cols:
                        self._db.execute(f"ALTER TABLE commitments ADD COLUMN {column} TEXT")
                if self._has_interrupted_migration():
                    self._recover_migration_rows()
                # The old index follows the renamed table and is dropped with
                # it. Create the replacement only after migration/recovery.
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_commit_when ON commitments(status, when_ts)")
                self._migrate_calendar_results()
                # Auxiliary table leaves the lossless commitments recovery format intact.
                self._db.execute("""CREATE TABLE IF NOT EXISTS calendar_event_ends (
                    commitment_id TEXT PRIMARY KEY, end_ts REAL NOT NULL
                )""")
                # Invitee count and whether the user organizes the event, from the app's
                # Calendar sync. organizer_is_user NULL = unknown. No row = not synced yet.
                self._db.execute("""CREATE TABLE IF NOT EXISTS calendar_event_invitees (
                    commitment_id TEXT PRIMARY KEY, invitee_count INTEGER NOT NULL,
                    organizer_is_user INTEGER
                )""")
                # Whether the event's calendar accepts changes from this Mac (EventKit's
                # allowsContentModifications): 0 for a subscribed or read-only calendar. Its
                # own table, not a new column, so a build from before it (which inserts into
                # calendar_event_invitees without naming columns) still syncs after a rollback.
                # No row = the app didn't say (older app).
                self._db.execute("""CREATE TABLE IF NOT EXISTS calendar_event_writable (
                    commitment_id TEXT PRIMARY KEY, writable INTEGER NOT NULL
                )""")
                self._db.execute(_BEHAVIOR_RECEIPTS_SCHEMA)
                self._validate_behavior_receipt_schema()
                self._db.execute("""CREATE TABLE IF NOT EXISTS today_source_sync (
                    source TEXT PRIMARY KEY, payload TEXT NOT NULL
                )""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS today_tasks (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL
                )""")
                self._db.execute("CREATE INDEX IF NOT EXISTS idx_today_task_day ON today_tasks(day)")
                # When the task was first saved; NULL for tasks made before this column.
                # Only hide_stale_tasks reads it (was it planned after a reminder hid?).
                task_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(today_tasks)")}
                if "created_at" not in task_cols:
                    self._db.execute("ALTER TABLE today_tasks ADD COLUMN created_at REAL")
                self._db.execute("""CREATE TABLE IF NOT EXISTS today_preferences (
                    day TEXT NOT NULL, timezone TEXT NOT NULL, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(day, timezone)
                )""")
                from service.discovery.store import migrate
                migrate(self._db)
                # Read-only projection keeps floating reminder dates in the
                # current local civil day after travel, without rewriting user
                # rows or changing timed indexes. UNION keeps the normal timed
                # branch available to SQLite's existing range index.
                from service.assistant.verified_reminders import due_timestamp, valid_due_date
                def civil_index(raw, value, timezone=None):
                    if not valid_due_date(value):
                        return raw
                    if timezone is None:
                        return due_timestamp({'all_day': True, 'due_date': value})
                    from datetime import datetime, timedelta
                    from zoneinfo import ZoneInfo
                    civil = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone))
                    return (civil + timedelta(days=1, minutes=-1)).timestamp()
                self._db.create_function('wisp_civil_index', 2, civil_index)
                self._db.create_function('wisp_civil_index', 3, civil_index)
                columns = [row['name'] for row in self._db.execute('PRAGMA table_info(commitments)')]
                self._schedule_columns = tuple(columns)
                projected = ','.join('wisp_civil_index(when_ts,due_date) AS when_ts' if col == 'when_ts'
                                     else '"' + col + '"' for col in columns)
                self._db.execute('CREATE TEMP VIEW scheduled_commitments AS '
                    'SELECT * FROM commitments WHERE all_day=0 OR due_date IS NULL '
                    'UNION ALL SELECT ' + projected + ' FROM commitments WHERE all_day=1 AND due_date IS NOT NULL')
        except BaseException:
            self._db.close()
            raise

    def _validate_behavior_receipt_schema(self):
        # This new, solely owned schema has no historical alternative forms.
        # Finding its name is insufficient: malformed constraints must fail
        # explicitly rather than silently misattribute cleanup ownership.
        actual = self._db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                                  "AND name='assistant_behavior_action_receipts'").fetchone()
        expected = _BEHAVIOR_RECEIPTS_SCHEMA.replace("CREATE TABLE IF NOT EXISTS","CREATE TABLE",1)
        def normalized(sql):
            # Preserve whitespace inside literals/quoted identifiers: a
            # malformed CHECK for 'acti ve' must not normalize to 'active'.
            result,quote = [],None
            for char in sql:
                if quote is not None:
                    result.append(char)
                    if char==quote:
                        quote = None
                elif char in {"'",'"'}:
                    result.append(char)
                    quote = char
                elif not char.isspace():
                    result.append(char)
            return "".join(result)
        if actual is None or normalized(actual[0])!=normalized(expected):
            raise RuntimeError("Incompatible behavior-action receipt schema; database preserved")
        columns = self._db.execute("PRAGMA table_info(assistant_behavior_action_receipts)").fetchall()
        names = [row["name"] for row in columns]
        if (names!=["owner_id","nudge_id","action_index","commitment_id","receipt_state",*_BEHAVIOR_RECEIPT_PAYLOAD]
                or tuple(row["pk"] for row in columns[:3])!=(1,2,3)):
            raise RuntimeError("Incompatible behavior-action receipt identity; database preserved")
        unique_cid = False
        for index in self._db.execute("PRAGMA index_list(assistant_behavior_action_receipts)").fetchall():
            quoted = index["name"].replace('"','""')
            indexed = [row["name"] for row in self._db.execute(f'PRAGMA index_info("{quoted}")')]
            unique_cid |= bool(index["unique"] and not index["partial"] and indexed==["commitment_id"])
        if not unique_cid:
            raise RuntimeError("Behavior-action receipt CID uniqueness is unavailable; database preserved")

    @staticmethod
    def _behavior_item_matches(current, receipt, end):
        return (current is not None and current["id"]==receipt["commitment_id"]
            and current["source"]==MANUAL_SOURCE and current["source_id"]==receipt["commitment_id"]
            and current["status"]=="active" and current["updated_at"]==current["created_at"]
            and all(current[key]==receipt[key] for key in ("kind","title","context","when_ts","created_at"))
            and end==receipt["end_ts"])

    def assert_behavior_cleanup_available(self):
        """Cleanup owns its durable commit and never joins a caller's unit."""
        assert_mutation_allowed()
        with revocation(),self._lock:
            if self._db.in_transaction:
                raise PublicationUnavailable("Behavior cleanup cannot join a caller transaction")

    def drop_behavior_events(self):
        """Durably erase learned nudge events without joining a caller unit."""
        assert_mutation_allowed()
        with revocation(),self._lock:
            self.assert_behavior_cleanup_available()
            with self._write_transaction():
                count = self._db.execute("DELETE FROM assistant_events WHERE kind='nudge' "
                                         "AND dedupe_key LIKE 'behavior:%'").rowcount
            return int(count or 0)

    def forget_behavior_actions(self, owner_id):
        """Erase exact owned items; persist only scrubbed suppression identities.

        Consumed IDs also exclude legacy cleanup after interruption/restart.
        They cannot grant source access, recreate an item or publish an event.
        """
        assert_mutation_allowed()
        if type(owner_id) is not str or len(owner_id)!=64 or any(c not in "0123456789abcdef" for c in owner_id):
            raise ValueError("A canonical behavior owner identity is required")
        with revocation(),self._lock:
            self.assert_behavior_cleanup_available()
            return self._forget_behavior_actions_owned(owner_id)

    def _forget_behavior_actions_owned(self, owner_id):
        # Called only while the ordinary outer revocation and Store lock are
        # held, with no preexisting transaction. No connection/callback escapes.
        removed,covered,kept = [],[],0
        with self._write_transaction():
            # Any exact durable claim supersedes a weaker legacy ledger,
            # including claims belonging to a different home. Read only opaque
            # IDs here; cleanup and payload consumption stay owner-scoped.
            covered = [row[0] for row in self._db.execute(
                "SELECT commitment_id FROM assistant_behavior_action_receipts ORDER BY commitment_id")]
            receipts = self._db.execute("SELECT * FROM assistant_behavior_action_receipts WHERE owner_id=? "
                                       "ORDER BY nudge_id,action_index",(owner_id,)).fetchall()
            for receipt in receipts:
                cid = receipt["commitment_id"]
                current = self._db.execute("SELECT * FROM commitments WHERE id=?",(cid,)).fetchone()
                end = self._db.execute("SELECT end_ts FROM calendar_event_ends WHERE commitment_id=?",(cid,)).fetchone()
                if receipt["receipt_state"]=="consumed":
                    kept += int(current is not None)
                    continue
                if self._behavior_item_matches(current,receipt,end[0] if end is not None else None):
                    deleted = self._db.execute("DELETE FROM commitments WHERE id=?",(cid,))
                    if deleted.rowcount!=1:
                        raise PublicationUnavailable("Owned behavior item was not deleted")
                    deleted_end = self._db.execute("DELETE FROM calendar_event_ends WHERE commitment_id=?",(cid,))
                    if deleted_end.rowcount!=int(end is not None):
                        raise PublicationUnavailable("Owned behavior end was not deleted")
                    self._db.execute("DELETE FROM notify_log WHERE commitment_id=?",(cid,))
                    removed.append({"type":receipt["type"],"title":receipt["title"],"when_ts":receipt["when_ts"]})
                elif current is not None:
                    kept += 1
                changed = self._db.execute("UPDATE assistant_behavior_action_receipts SET receipt_state='consumed',"
                    "type=NULL,kind=NULL,title=NULL,context=NULL,when_ts=NULL,end_ts=NULL,created_at=NULL "
                    "WHERE owner_id=? AND nudge_id=? AND action_index=? AND receipt_state='active'",
                    (owner_id,receipt["nudge_id"],receipt["action_index"]))
                if changed.rowcount!=1:
                    raise PublicationUnavailable("Behavior-action consumption did not persist")
        return {"removed":removed,"kept":kept,"covered":tuple(covered)}

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
        Only known nullable additive columns may default to NULL.
        """
        columns = [r["name"] for r in self._db.execute("PRAGMA table_info(commitments)")]
        old_columns = {r["name"] for r in self._db.execute(
            "PRAGMA table_info(commitments_old_migrating)")}
        missing = set(columns) - old_columns - {"organizer", "account", "due_date"}
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

    def _migrate_reminder_terminals(self) -> None:
        """Recover durable terminal watermarks from pre-table native receipts."""
        for row in self._db.execute(
            "SELECT kind,payload,result,created_at,acknowledged_at FROM assistant_events "
            "WHERE kind IN ('complete_reminder','delete_reminder') AND result IS NOT NULL "
            "ORDER BY created_at").fetchall():
            try:
                payload = json.loads(row["payload"])
                result = self.calendar_result(row["kind"], json.loads(row["result"]), payload)
                if result.get("ok") is not True:
                    continue
                source_id = payload["source_id"]
                recorded_at = row["acknowledged_at"] or time.time()
                self._db.execute(
                    "INSERT INTO assistant_reminder_terminals VALUES (?,?,?,?) "
                    "ON CONFLICT(source_id) DO UPDATE SET status=excluded.status,"
                    "recorded_at=excluded.recorded_at,action_id=excluded.action_id "
                    "WHERE excluded.recorded_at > assistant_reminder_terminals.recorded_at",
                    (source_id, "done" if row["kind"] == "complete_reminder" else "dismissed",
                     recorded_at, payload["action_id"]))
            except (KeyError, TypeError, ValueError):
                continue

    def _migrate_reminder_verified_orders(self) -> None:
        """Backfill action order without treating delayed receipt arrival as action order."""
        for row in self._db.execute(
            "SELECT kind,payload,result,claim_order,created_at,acknowledged_at "
            "FROM assistant_events WHERE claim_order IS NOT NULL AND result IS NOT NULL "
            "AND kind IN ('create_reminder','update_reminder','complete_reminder','delete_reminder')"
        ).fetchall():
            try:
                payload = json.loads(row["payload"])
                result = self.calendar_result(row["kind"], json.loads(row["result"]), payload)
                if result.get("ok") is not True:
                    continue
                source_id = result["source_id"] if row["kind"] == "create_reminder" else payload["source_id"]
                self._db.execute(
                    "INSERT INTO assistant_reminder_verified(source_id,recorded_at,claim_order) "
                    "VALUES (?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                    "claim_order=MAX(assistant_reminder_verified.claim_order,excluded.claim_order)",
                    (source_id, row["acknowledged_at"] or row["created_at"], row["claim_order"]))
            except (KeyError, TypeError, ValueError):
                continue

    def _nudge_bucket(self, minute):
        rows = self._db.execute(
            "SELECT * FROM commitments WHERE status='active' AND when_ts>=? AND when_ts<? ORDER BY id LIMIT 1001",
            (minute,minute+60)).fetchall()
        if len(rows)>1000:
            raise PublicationUnavailable("Delivery uniqueness coverage is incomplete")
        return [dict(row) for row in rows]

    def prepare_nudge_write(self, runtime, nudge_json, event, actions, prior, now, expires_at):
        """Prepare only immutable values and exact owned-SQL consistency facts."""
        owner_id = behavior_owner_id(runtime.home)
        if owner_id!=runtime._owner_id:
            raise PublicationUnavailable("Runtime home identity changed")
        nudge = json.loads(nudge_json)
        if (event.get("type")!="nudge" or event.get("nudge_id")!=nudge["id"]
                or len(actions)>8 or type(now) not in (int,float) or not math.isfinite(now)):
            raise ValueError("A bounded canonical nudge is required")
        units,performed,buckets,owned,receipts,cid_receipts = [],[],{},{},{},{}
        planned = set()
        prior = {row["idx"]:row for row in prior}
        with self._lock:
            if self._db.in_transaction:
                raise PublicationUnavailable("Delivery cannot join a caller transaction")
            for action in actions:
                idx = action["idx"]
                previous = prior.get(idx)
                if previous is not None and (previous["undone_at"] is not None or not previous.get("commitment_id")):
                    continue
                cid = previous["commitment_id"] if previous is not None else action["id"]
                receipt = self._db.execute("SELECT * FROM assistant_behavior_action_receipts "
                    "WHERE owner_id=? AND nudge_id=? AND action_index=?",(owner_id,nudge["id"],idx)).fetchone()
                claim = self._db.execute("SELECT * FROM assistant_behavior_action_receipts WHERE commitment_id=?",(cid,)).fetchone()
                receipts[str(idx)] = dict(receipt) if receipt is not None else None
                cid_receipts[cid] = dict(claim) if claim is not None else None
                if (claim is not None and (claim["owner_id"],claim["nudge_id"],claim["action_index"])!=(owner_id,nudge["id"],idx)
                        or receipt is not None and receipt["commitment_id"]!=cid):
                    raise PublicationUnavailable("Behavior-action ownership identity conflicts")
                existing = self._db.execute("SELECT * FROM commitments WHERE id=?",(cid,)).fetchone()
                end = self._db.execute("SELECT end_ts FROM calendar_event_ends WHERE commitment_id=?",(cid,)).fetchone()
                actual_end = end[0] if end is not None else None
                owned[cid] = dict(row=dict(existing) if existing is not None else None,end=actual_end)
                if receipt is not None:
                    if receipt["receipt_state"]=="consumed":
                        continue
                    if any(receipt[key]!=action[key] for key in ("type","kind","title","context","when_ts","end_ts")):
                        raise PublicationUnavailable("Immutable behavior-action receipt changed")
                    if not self._behavior_item_matches(existing,receipt,actual_end):
                        continue
                    if previous is not None and (previous["commitment_id"]!=cid
                            or previous["created_at"]!=receipt["created_at"]
                            or any(previous[key]!=receipt[key] for key in ("type","title","when_ts"))):
                        raise PublicationUnavailable("Runtime receipt does not match its Store ownership")
                    performed.append(dict(nudge_id=nudge["id"],idx=idx,type=receipt["type"],commitment_id=cid,
                        title=receipt["title"],when_ts=receipt["when_ts"],created_at=receipt["created_at"]))
                    continue
                if previous is not None:
                    # A retry can advertise Undo only for a real surviving,
                    # untouched local item. A deleted/completed/edited item is
                    # never recreated or adopted merely from the old ledger.
                    if (existing is not None and existing["source"]==MANUAL_SOURCE and existing["source_id"]==cid
                            and existing["status"]=="active" and existing["updated_at"]==existing["created_at"]
                            and existing["created_at"]==previous["created_at"] and actual_end==action["end_ts"]
                            and existing["context"] in {action["context"],action["context"].rsplit(" [",1)[0]}
                            and all(existing[key]==action[key] for key in ("kind","title","when_ts"))):
                        performed.append(dict(nudge_id=nudge["id"],idx=idx,type=previous["type"],
                            commitment_id=previous["commitment_id"],title=previous["title"],
                            when_ts=previous["when_ts"],created_at=previous["created_at"]))
                    continue
                if existing is not None:
                    # No retrospective attribution from a deterministic CID
                    # or matching context. Only a real old ledger (above) or
                    # the atomic Store receipt can establish ownership.
                    continue
                minute = int(action["when_ts"]//60)*60
                bucket = buckets.setdefault(str(minute),self._nudge_bucket(minute))
                duplicate_key = minute,action["title"].casefold()
                if duplicate_key in planned or any(" ".join(str(row["title"] or "").split()).casefold()==action["title"].casefold()
                       for row in bucket):
                    continue
                units.append(action)
                planned.add(duplicate_key)
                performed.append(dict(nudge_id=nudge["id"],idx=idx,type=action["type"],
                    commitment_id=cid,title=action["title"],when_ts=action["when_ts"],created_at=now))
            key = "behavior:"+nudge["id"]
            old_event = self._db.execute("SELECT * FROM assistant_events WHERE dedupe_key=?",(key,)).fetchone()
            old_event = dict(old_event) if old_event is not None else None
        indices = {row["idx"] for row in performed}
        for idx,action in enumerate(event.get("actions") or []):
            action["auto"] = idx in indices
        event["auto_performed"] = [dict(type=row["type"],title=row["title"],when_ts=row["when_ts"],
                                          action_index=row["idx"]) for row in performed]
        event["can_undo"] = bool(performed)
        event_json = json.dumps(event,sort_keys=True,allow_nan=False)
        if len(event_json.encode())>65536:
            raise PublicationUnavailable("Delivery event exceeds its bound")
        if old_event is not None and (old_event["kind"]!="nudge" or old_event["payload"]!=event_json):
            raise PublicationUnavailable("Immutable delivery identity has changed")
        data = json.dumps(dict(nudge_id=nudge["id"],nudge_json=nudge_json,event_json=event_json,
            event_id=old_event["id"] if old_event else uuid.uuid4().hex,key=key,units=units,performed=performed,
            buckets=buckets,owned=owned,receipts=receipts,cid_receipts=cid_receipts,owner_id=owner_id,
            old_event=old_event,now=now,expires_at=expires_at),
            sort_keys=True,allow_nan=False)
        if len(data.encode())>262144:
            raise PublicationUnavailable("Delivery preparation exceeds its bound")
        binding = owned_write(self._delivery_writer,self,runtime,hashlib.sha256(data.encode()).hexdigest())
        plan = PreparedNudgeWrite(self,runtime,data,binding,_delivery_issuer)
        self._nudge_plans.add(plan)
        return plan

    def _require_nudge_write(self, lease, plan, runtime):
        if (type(lease) is not PublicationLease or type(plan) is not PreparedNudgeWrite
                or plan.issuer is not _delivery_issuer or plan.store is not self or plan.runtime is not runtime
                or plan not in self._nudge_plans
                or not lease.authorizes(plan.binding,authority=self._delivery_writer,owner=self,
                    context=runtime,key=hashlib.sha256(plan.data.encode()).hexdigest())):
            raise PublicationUnavailable("Exact active owned delivery capability required")
        return json.loads(plan.data)

    def commit_nudge_actions(self, lease, plan, runtime):
        """Fixed owned write; never expose a connection or run a caller callback."""
        data = self._require_nudge_write(lease,plan,runtime)
        with self._lock:
            if self._db.in_transaction:
                raise PublicationUnavailable("Delivery cannot join a caller transaction")
            try:
                self._db.execute("BEGIN IMMEDIATE")
                for minute,expected in data["buckets"].items():
                    if self._nudge_bucket(int(minute))!=expected:
                        raise PublicationUnavailable("Delivery uniqueness changed")
                for cid,expected in data["owned"].items():
                    row = self._db.execute("SELECT * FROM commitments WHERE id=?",(cid,)).fetchone()
                    end = self._db.execute("SELECT end_ts FROM calendar_event_ends WHERE commitment_id=?",(cid,)).fetchone()
                    if dict(row=dict(row) if row is not None else None,end=end[0] if end is not None else None)!=expected:
                        raise PublicationUnavailable("Owned action changed")
                for idx,expected in data["receipts"].items():
                    row = self._db.execute("SELECT * FROM assistant_behavior_action_receipts "
                        "WHERE owner_id=? AND nudge_id=? AND action_index=?",(data["owner_id"],data["nudge_id"],int(idx))).fetchone()
                    if (dict(row) if row is not None else None)!=expected:
                        raise PublicationUnavailable("Behavior-action receipt changed")
                for cid,expected in data["cid_receipts"].items():
                    row = self._db.execute("SELECT * FROM assistant_behavior_action_receipts WHERE commitment_id=?",(cid,)).fetchone()
                    if (dict(row) if row is not None else None)!=expected:
                        raise PublicationUnavailable("Behavior-action CID claim changed")
                for action in data["units"]:
                    self._db.execute(
                        "INSERT INTO commitments(id,source,source_id,kind,title,context,when_ts,status,confidence,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,'active',1.0,?,?)",
                        (action["id"],MANUAL_SOURCE,action["id"],action["kind"],action["title"],action["context"],
                         action["when_ts"],data["now"],data["now"]))
                    if action["end_ts"] is not None:
                        self._db.execute("INSERT INTO calendar_event_ends(commitment_id,end_ts) VALUES(?,?)",
                                         (action["id"],action["end_ts"]))
                    self._db.execute("INSERT INTO assistant_behavior_action_receipts "
                        "(owner_id,nudge_id,action_index,commitment_id,receipt_state,type,kind,title,context,when_ts,end_ts,created_at) "
                        "VALUES(?,?,?,?,'active',?,?,?,?,?,?,?)",
                        (data["owner_id"],data["nudge_id"],action["idx"],action["id"],action["type"],action["kind"],
                         action["title"],action["context"],action["when_ts"],action["end_ts"],data["now"]))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
        return tuple(data["performed"])

    def commit_nudge_event(self, lease, plan, runtime):
        data = self._require_nudge_write(lease,plan,runtime)
        with self._lock:
            if self._db.in_transaction:
                raise PublicationUnavailable("Delivery cannot join a caller transaction")
            try:
                self._db.execute("BEGIN IMMEDIATE")
                old = self._db.execute("SELECT * FROM assistant_events WHERE dedupe_key=?",(data["key"],)).fetchone()
                if (dict(old) if old is not None else None)!=data["old_event"]:
                    raise PublicationUnavailable("Delivery event changed")
                if old is None:
                    self._db.execute("INSERT INTO assistant_events(id,kind,payload,dedupe_key,target,created_at,expires_at) "
                        "VALUES(?,'nudge',?,?,'{}',?,?)",(data["event_id"],data["event_json"],data["key"],data["now"],data["expires_at"]))
                row = self._db.execute("SELECT id,payload,state FROM assistant_events WHERE dedupe_key=?",(data["key"],)).fetchone()
                if row is None or row["payload"]!=data["event_json"]:
                    raise PublicationUnavailable("Canonical event was not persisted")
                result = CommittedNudge(plan,row["id"],row["payload"],row["state"]=="pending",_delivery_issuer)
                self._db.commit()
                self._nudge_receipts.add(result)
                return result
            except BaseException:
                self._db.rollback()
                raise

    def validate_committed_nudge(self, lease, result, runtime):
        if (type(result) is not CommittedNudge or result.issuer is not _delivery_issuer
                or result not in self._nudge_receipts):
            raise PublicationUnavailable("An actual committed delivery receipt is required")
        self._require_nudge_write(lease,result.plan,runtime)
        return result

    @contextmanager
    def transaction(self, *, write: bool = True):
        """Serialize a coherent database unit, with nested rollback savepoints.

        Discovery repositories may compose operations on this connection. Do
        not commit/rollback the yielded connection or call legacy methods that
        commit independently. All write decisions must start with write=True;
        a read transaction must not be upgraded across concurrent connections.
        """
        # The outer order also covers composed/nested receipt writes. No
        # source lock can be held while waiting for a publication to finish.
        with revocation(), self._lock:
            nested = self._db.in_transaction
            if nested and write and self._transaction_write is False:
                raise RuntimeError("Cannot upgrade a read transaction to a write transaction")
            previous_mode = self._transaction_write
            savepoint = "unit_" + uuid.uuid4().hex
            self._db.execute(f"SAVEPOINT {savepoint}" if nested else
                             "BEGIN IMMEDIATE" if write else "BEGIN")
            if not nested:
                self._transaction_write = write
            frame = {"receipt": None}
            self._behavior_receipt_stages.append(frame)
            try:
                yield self._db
                if nested:
                    self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
                    if len(self._behavior_receipt_stages) > 1 and frame["receipt"] is not None:
                        self._behavior_receipt_stages[-2]["receipt"] = frame["receipt"]
                else:
                    candidate = frame["receipt"]
                    if candidate is not None:
                        row = self._db.execute("SELECT payload FROM today_source_sync WHERE source='reminders'").fetchone()
                        if row is None or row["payload"] != candidate[0]:
                            candidate = None
                    self._db.commit()
                    # Only a successfully committed actual native receipt can
                    # arm the process-local permission head. No SQL in checks.
                    if candidate is not None:
                        _, metadata, deadline = candidate
                        now = time.time()
                        if all(0 <= now-t < 1200 for t in metadata[:2]) and time.monotonic() < deadline:
                            self._behavior_receipt_head.metadata = metadata
                            self._behavior_receipt_head.expires_monotonic = deadline
                            self._behavior_receipt_head.authority.publish()
            except BaseException:
                # A failed attempted receipt never revives an older/pending
                # permission, even when its SQL savepoint is rolled back.
                if frame["receipt"] is not None:
                    self._behavior_receipt_head.authority.withdraw()
                    self._behavior_receipt_head.metadata = None
                    for previous in self._behavior_receipt_stages:
                        previous["receipt"] = None
                if nested:
                    self._db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._db.rollback()
                raise
            finally:
                self._behavior_receipt_stages.pop()
                self._transaction_write = previous_mode

    @contextmanager
    def _write_transaction(self):
        with self.transaction():
            yield

    def prepare_behavior_source_publication_witness(self, source, snapshot) -> Witness | None:
        """Only current native Reminders permission metadata, never task content.

        Copied DTO fields must match both current SQL and a process-issued
        successfully committed native receipt. Reads never renew its lifetime.
        """
        if source != "reminders":
            return None
        with revocation(), self._lock:
            metadata = _behavior_reminders_receipt(snapshot)
            head = self._behavior_receipt_head
            if metadata is None or metadata != head.metadata or self._db.in_transaction:
                return None
            row = self._db.execute("SELECT payload FROM today_source_sync WHERE source='reminders'").fetchone()
            if row is None or metadata != _behavior_reminders_receipt(json.loads(row["payload"])):
                return None
            return Witness.combine(head.authority.token(clock=LIVE_CLOCK,
                expires_wall=min(metadata[:2])+1200, expires_monotonic=head.expires_monotonic))

    def schedule_relation(self, timezone: str) -> str:
        """Trusted SQL relation for an explicit display zone's floating dates.

        Timed rows retain their stored epochs; calendar all-day occupancy also
        keeps its existing interval contract. Only date-only reminders project.
        """
        from zoneinfo import ZoneInfo
        ZoneInfo(timezone)
        quoted_zone = "'" + timezone.replace("'", "''") + "'"
        columns = self._schedule_columns
        projected = ','.join(
            f'wisp_civil_index(when_ts,due_date,{quoted_zone}) AS when_ts' if col == 'when_ts'
            else '"' + col + '"' for col in columns)
        return '(SELECT * FROM commitments WHERE all_day=0 OR due_date IS NULL UNION ALL SELECT ' + projected + ' FROM commitments WHERE all_day=1 AND due_date IS NOT NULL)'

    def today_snapshot(self, day: str, timezone: str) -> dict:
        from service.assistant.today import day_bounds
        lo, hi = day_bounds(day, timezone)
        relation = self.schedule_relation(timezone)
        with self._lock, self._db:
            self._db.execute("BEGIN")  # coherent reads across other store connections
            tasks = [json.loads(r["payload"]) for r in self._db.execute(
                "SELECT payload FROM today_tasks WHERE day=? OR "
                "(json_extract(payload,'$.status')='active' AND "
                "json_extract(payload,'$.pinned_start') < ? AND "
                "json_extract(payload,'$.pinned_start') + json_extract(payload,'$.duration_minutes') * 60 > ?) "
                "ORDER BY id", (day, hi, lo))]
            row = self._db.execute("SELECT payload,revision FROM today_preferences WHERE day=? AND timezone=?",
                                   (day, timezone)).fetchone()
            sources = {r["source"]: json.loads(r["payload"]) for r in
                       self._db.execute("SELECT source,payload FROM today_source_sync")}
            # Do not use display dedupe: separate events can share a title/start.
            # Rows with an unknown end stay in (even all-day ones that started
            # earlier) so the planner sees them and refuses to call that time free.
            commitments = [dict(r) for r in self._db.execute(
                f"SELECT c.*, e.end_ts FROM {relation} c LEFT JOIN calendar_event_ends e ON e.commitment_id=c.id "
                "WHERE status='active' AND when_ts < ? AND "
                "((source='calendar' AND (end_ts > ? OR (end_ts=when_ts AND when_ts >= ?) OR end_ts IS NULL)) "
                "OR (source IN ('reminders','manual') AND when_ts >= ?))",
                (hi, lo, lo, lo))]
        fixed = [c for c in commitments if c["source"] == "calendar" or
                 (c["source"] == "manual" and c["kind"] in {"event", "meeting"})]
        deadlines = [c for c in commitments if c not in fixed]
        commitments = fixed + self._collapse(deadlines)
        return dict(tasks=tasks, commitments=commitments, sources=sources,
                    preferences=json.loads(row["payload"]) if row else
                        dict(start_minute=540, end_minute=1080, not_before=None),
                    revision=row["revision"] if row else 0)

    def today_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT payload FROM today_tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def today_active_tasks_before(self, day: str) -> list[dict]:
        """Active tasks still planned for a day before `day` (ISO dates sort as
        text): what the morning carry-forward moves onto today."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM today_tasks WHERE day < ? AND "
                "json_extract(payload,'$.status')='active' ORDER BY day, id", (day,)).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def today_save_task(self, values: dict, *, task_id: str | None = None, revision: int | None = None) -> dict:
        from service.assistant.today import RevisionConflict, validate_task
        with self._write_transaction():
            if task_id is not None:
                row = self._db.execute("SELECT payload,revision FROM today_tasks WHERE id=?", (task_id,)).fetchone()
                if row is None:
                    raise KeyError("Task no longer exists")
                if type(revision) is not int or row["revision"] != revision:
                    raise RevisionConflict("This task changed. Refresh before editing it again.")
                old = json.loads(row["payload"])
                task = validate_task({**old, **values, "id": task_id, "revision": revision + 1})
            else:
                if self._db.execute("SELECT COUNT(*) FROM today_tasks").fetchone()[0] >= 10000:
                    raise ValueError("Task limit reached")
                task = validate_task({"kind": "task", "priority": 2, "status": "active", **values,
                                      "id": uuid.uuid4().hex, "revision": 1})
            saved = time.time()
            # created_at is set on insert only; an edit or carry keeps the original.
            self._db.execute("INSERT INTO today_tasks(id,day,payload,revision,updated_at,created_at) VALUES (?,?,?,?,?,?) "
                             "ON CONFLICT(id) DO UPDATE SET day=excluded.day,payload=excluded.payload,"
                             "revision=excluded.revision,updated_at=excluded.updated_at",
                             (task["id"], task["day"], json.dumps(task, allow_nan=False), task["revision"], saved, saved))
        return task

    def today_save_preferences(self, day: str, timezone: str, values: dict, revision: int) -> int:
        from service.assistant.today import RevisionConflict, validate_preferences
        preferences = validate_preferences(day, timezone, **values)
        with self._write_transaction():
            row = self._db.execute("SELECT revision FROM today_preferences WHERE day=? AND timezone=?",
                                   (day, timezone)).fetchone()
            current = row["revision"] if row else 0
            if type(revision) is not int or current != revision:
                raise RevisionConflict("Working hours changed. Refresh before replanning.")
            self._db.execute("INSERT INTO today_preferences(day,timezone,payload,revision) VALUES (?,?,?,?) "
                             "ON CONFLICT(day,timezone) DO UPDATE SET payload=excluded.payload,revision=excluded.revision",
                             (day, timezone, json.dumps(preferences, allow_nan=False), current + 1))
        return current + 1

    @staticmethod
    def calendar_payload(kind: str, payload: dict) -> dict:
        from service.assistant.verified_reminders import KINDS, validate_payload
        if isinstance(kind, str) and kind in KINDS:
            return validate_payload(kind, payload)
        if not isinstance(kind, str) or not isinstance(payload, dict) or kind not in CALENDAR_KINDS:
            raise ValueError("invalid Calendar payload")
        fields = {"create_calendar_event": {"type", "action_id", "title", "when_ts", "duration_min", "location"},
                  "delete_calendar_event": {"type", "action_id", "source_id", "when_ts"},
                  # A move names the exact occurrence (identifier + current start + title)
                  # and the new start; duration_min None keeps the event's own length.
                  # invitee_count: what the card told the user; the app refuses if it differs.
                  "move_calendar_event": {"type", "action_id", "source_id", "expected_title",
                                          "expected_start_ts", "when_ts", "duration_min",
                                          "invitee_count"},
                  # A one-tap cancel of one exact occurrence (the Day tab's Cancel).
                  "cancel_calendar_event": {"type", "action_id", "source_id", "expected_title",
                                            "expected_start_ts", "invitee_count"}}[kind]
        if set(payload) != fields or payload.get("type") != kind:
            raise ValueError("Calendar payload fields do not match the action")
        for field in fields - {"when_ts", "duration_min", "expected_start_ts", "invitee_count"}:
            if not isinstance(payload[field], str) or (field != "location" and not payload[field].strip()):
                raise ValueError("invalid Calendar payload text")
        times = {"move_calendar_event": ["when_ts", "expected_start_ts"],
                 "cancel_calendar_event": ["expected_start_ts"]}.get(kind, ["when_ts"])
        for field in times:
            when = payload[field]
            try:
                valid_time = type(when) in (int, float) and math.isfinite(when) and when > 0
            except OverflowError:
                valid_time = False
            if not valid_time:
                raise ValueError("invalid Calendar time")
        if kind == "cancel_calendar_event":
            invitees = payload["invitee_count"]
            if type(invitees) is not int or not 0 <= invitees <= 100000:
                raise ValueError("invalid Calendar invitee count")
            return {**payload, "expected_start_ts": float(payload["expected_start_ts"])}
        if kind == "move_calendar_event":
            duration = payload["duration_min"]
            if duration is not None and (type(duration) is not int or not 1 <= duration <= 10080):
                raise ValueError("invalid Calendar duration")
            invitees = payload["invitee_count"]
            if type(invitees) is not int or not 0 <= invitees <= 100000:
                raise ValueError("invalid Calendar invitee count")
            return {**payload, "when_ts": float(payload["when_ts"]),
                    "expected_start_ts": float(payload["expected_start_ts"])}
        when = payload["when_ts"]
        if kind == "create_calendar_event":
            duration = payload["duration_min"]
            if type(duration) is not int or not 1 <= duration <= 10080:
                raise ValueError("invalid Calendar duration")
        return {**payload, "when_ts": float(when)}

    @staticmethod
    def calendar_result(kind: str, result: dict, payload: dict | None = None) -> dict:
        from service.assistant.verified_reminders import KINDS, validate_result
        if isinstance(kind, str) and kind in KINDS:
            if payload is None:
                raise ValueError("reminder result requires exact action payload")
            return validate_result(kind, payload, result)
        if not isinstance(result, dict):
            raise ValueError("Calendar result must be an object")
        required = {"ok", "status", "error"}
        allowed = required | ({"source_id"} if kind == "create_calendar_event" else set())
        if kind == "move_calendar_event" and result.get("ok") is True:
            # A verified move reports the start and end it read back.
            required = allowed = required | {"when_ts", "end_ts"}
        if kind == "cancel_calendar_event" and result.get("ok") is True:
            # A verified cancel reports that the occurrence reads back as gone.
            required = allowed = required | {"is_absent"}
            if result.get("is_absent") is not True:
                raise ValueError("a verified cancel requires is_absent")
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
        if result["ok"] and kind == "move_calendar_event":
            start, end = result["when_ts"], result["end_ts"]
            if not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (start, end)) or end < start:
                raise ValueError("a verified move requires its read-back start and end")
            if payload is not None:
                if abs(start - payload["when_ts"]) >= 1:
                    raise ValueError("read-back start does not match the requested move")
                if payload["duration_min"] is not None and abs((end - start) - payload["duration_min"] * 60) >= 1:
                    raise ValueError("read-back length does not match the requested move")
            return {**result, "when_ts": float(start), "end_ts": float(end)}
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
                if member.get('all_day'):
                    return None  # A floating date never authorizes a clock alert.
                identity = _dedupe_key(member)
                if identity is None:
                    return None
                current = self._db.execute(
                    "SELECT id,title,when_ts,all_day FROM scheduled_commitments WHERE status='active' AND when_ts >= ? AND when_ts < ?"
                    + _seed_clause(), (identity[1] * 60, (identity[1] + 1) * 60)).fetchall()
                if {r["id"] for r in current if _dedupe_key(dict(r)) == identity} != set(reminder_snapshot):
                    return None
                for cid, expected in reminder_snapshot.items():
                    row = self._db.execute(
                        "SELECT title,kind,context,when_ts,status,all_day FROM scheduled_commitments WHERE id=?", (cid,)).fetchone()
                    if (row is None or {**dict(row), "generation": self.reminder_generation(cid)}
                            != {**expected, 'all_day': bool(expected.get('all_day'))}):
                        return None
            if target and target.get("type") == "reminder":
                for old in self._db.execute("SELECT * FROM assistant_events WHERE kind='reminder' AND state='pending'").fetchall():
                    previous = json.loads(old["target"])
                    if (previous.get("identity") == target.get("identity")
                            and set(previous.get("stages", [])) < set(target.get("stages", []))):
                        self._db.execute("UPDATE assistant_events SET state='superseded' WHERE id=?", (old["id"],))
            if (target and target.get("type") == "brief" and self._db.execute(
                    "SELECT 1 FROM assistant_events WHERE dedupe_key=?", (key,)).fetchone() is None):
                # A newer Daily Summary replaces any older one the app hasn't
                # acknowledged. Pending events replay oldest first, so an
                # unacknowledged morning brief would otherwise arrive after the
                # evening one and replace it on the Today tab.
                self._db.execute(
                    "UPDATE assistant_events SET state='superseded' "
                    "WHERE kind='daily_brief' AND state='pending' AND dedupe_key!=?", (key,))
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

    def reminder_actions_by_prefix(self, action_id_prefix: str) -> list[dict]:
        """Read exact semantic action generations for safe post-failure retry."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM assistant_events WHERE dedupe_key=? OR dedupe_key LIKE ? "
                "ORDER BY created_at,id",
                ("action:" + action_id_prefix, "action:" + action_id_prefix + ":%")
            ).fetchall()
            return [self._event(row) for row in rows]

    def reminder_update_state(self, payload: dict) -> str:
        """Classify the current local native row for a repeated exact update.

        A successful old A→B action can be requested again after B→A. Its
        semantic ID must then get a new generation, while an immediate retry
        of the still-current B result must reuse the recorded receipt.
        """
        from service.assistant.verified_reminders import due_timestamp, expected_matches
        with self._lock:
            rows = self._db.execute(
                "SELECT title,when_ts,all_day FROM scheduled_commitments WHERE source='reminders' "
                "AND source_id=? AND status='active'", (payload["source_id"],)
            ).fetchall()
        if len(rows) != 1:
            return "other"
        row = rows[0]
        if row["when_ts"] is None:
            return "other"
        if (row["title"] == payload["title"]
                and int(row["when_ts"] // 60) == int(due_timestamp(payload) // 60)
                and bool(row['all_day']) == bool(payload.get('all_day'))):
            return "desired"
        if (row["title"] == payload["expected_title"]
                and int(row["when_ts"] // 60) == int(payload["expected_due_ts"] // 60)
                and expected_matches(row, payload)):
            return "expected"
        return "other"

    def reminder_create_retry_state(self, previous: dict) -> str:
        """Classify a successful create before reusing its semantic receipt.

        A later verified delete or completion retires that native item, so an
        identical new create needs a fresh action generation. Missing local
        state without such a receipt is ambiguous and must not be retried.
        """
        from service.assistant.verified_reminders import due_timestamp, expected_matches
        source_id = previous["result"]["source_id"]
        with self._lock:
            rows = self._db.execute(
                "SELECT title,when_ts,status,all_day FROM scheduled_commitments WHERE source='reminders' AND source_id=?",
                (source_id,)).fetchall()
            if any(row["status"] == "active" and row["title"] == previous["payload"]["title"]
                   and row["when_ts"] is not None
                   and int(row["when_ts"] // 60) == int(due_timestamp(previous['payload']) // 60)
                   and bool(row['all_day']) == bool(previous['payload'].get('all_day'))
                   for row in rows):
                return "present"
            if any(row["status"] == "active" for row in rows):
                return "uncertain"
            later = self._db.execute(
                "SELECT kind,payload,result FROM assistant_events "
                "WHERE kind IN ('complete_reminder','delete_reminder') "
                "AND created_at>=? AND result IS NOT NULL ORDER BY created_at DESC",
                (previous["created_at"],)).fetchall()
        for action in later:
            payload = json.loads(action["payload"])
            result = json.loads(action["result"])
            if (payload.get("source_id") == source_id
                    and result.get("ok") is True
                    and result.get("status") == "succeeded"
                    and result.get("source_id") == source_id):
                return "retired"
        return "uncertain"

    def reminder_terminal_retry_state(self, kind: str, payload: dict) -> str:
        """Classify the current occurrence, ignoring historical completed rows."""
        from service.assistant.verified_reminders import expected_matches
        if kind == "delete_reminder" and payload.get("expected_due_ts") is None:
            # An undated reminder lives only in the undated list, never in commitments.
            with self._lock:
                terminal = self._db.execute(
                    "SELECT status FROM assistant_reminder_terminals WHERE source_id=?",
                    (payload["source_id"],)).fetchone()
                row = self._db.execute(
                    "SELECT title FROM assistant_undated_reminders WHERE source_id=?",
                    (payload["source_id"],)).fetchone()
            if row is not None:
                return "expected" if row["title"] == payload["expected_title"] else "other"
            return "desired" if terminal and terminal["status"] == "dismissed" else "other"
        with self._lock:
            rows = self._db.execute(
                "SELECT title,when_ts,status,all_day FROM scheduled_commitments WHERE source='reminders' "
                "AND source_id=?", (payload["source_id"],)).fetchall()
            terminal = self._db.execute(
                "SELECT status FROM assistant_reminder_terminals WHERE source_id=?",
                (payload["source_id"],)).fetchone()
        def matches(row) -> bool:
            return (row["title"] == payload["expected_title"]
                    and row["when_ts"] is not None
                    and int(row["when_ts"] // 60) == int(payload["expected_due_ts"] // 60)
                and expected_matches(row, payload))
        active = [row for row in rows if row["status"] == "active"]
        if active:
            return "expected" if len(active) == 1 and matches(active[0]) else "other"
        desired_status = "done" if kind == "complete_reminder" else "dismissed"
        if terminal and terminal["status"] == desired_status:
            return "desired"
        if any(row["status"] == desired_status and matches(row) for row in rows):
            return "desired"
        return "desired" if kind == "delete_reminder" and not rows else "other"

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
            row = self._db.execute("SELECT title,when_ts,status,all_day FROM scheduled_commitments WHERE id=?", (cid,)).fetchone()
            if (row and not row['all_day'] and row["status"] == "active" and row["when_ts"] == expected
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
            if target.get("type") in {"calendar", "verified_reminder"}:
                if row["result"] is None:
                    raise ValueError("native result required before acknowledgement")
                self.calendar_result(kind, json.loads(row["result"]), json.loads(row["payload"]))
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
                # Each of the day's runs completes on its own key; a brief queued
                # by an older once-a-day version (no "part") keeps the legacy key.
                part = target.get("part")
                self._db.execute(
                    "INSERT INTO assistant_completion VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value), "
                    "completed_at=excluded.completed_at",
                    ("daily_brief:" + part if part in {"morning", "evening"} else "daily_brief",
                     target["date"], now))
            elif target.get("type") == "scheduled_unknown":
                changed = self._db.execute(
                    "UPDATE scheduled_sends SET notified_at=COALESCE(notified_at,?) "
                    "WHERE id=? AND status='unknown'", (now, target["id"])).rowcount
                if not changed:
                    raise ValueError("scheduled notice is not in unknown state")
            self._db.execute("UPDATE assistant_events SET state='ack',acknowledged_at=? WHERE id=?",
                             (now, event_id))
            return True

    def _record_calendar_move(self, payload: dict, result: dict) -> None:
        """Apply a verified native move to the synced row (inside the receipt transaction):
        the occurrence at its old start now sits at the read-back start and end. The next
        Calendar sync replaces the row wholesale anyway; this keeps the gap honest."""
        now = time.time()
        old = self._db.execute(
            "SELECT * FROM scheduled_commitments WHERE source='calendar' AND source_id=? AND when_ts=?",
            (payload["source_id"], payload["expected_start_ts"])).fetchone()
        if old is None:
            return
        taken = self._db.execute(
            "SELECT id FROM scheduled_commitments WHERE source='calendar' AND source_id=? AND when_ts=? AND id<>?",
            (payload["source_id"], result["when_ts"], old["id"])).fetchone()
        if taken is not None:
            # A sync already recorded the new start; retire the old occurrence's row.
            self._db.execute("UPDATE commitments SET status='dismissed',updated_at=? WHERE id=?",
                             (now, old["id"]))
            return
        self._db.execute("UPDATE commitments SET when_ts=?,updated_at=? WHERE id=?",
                         (result["when_ts"], now, old["id"]))
        self._db.execute(
            "INSERT INTO calendar_event_ends(commitment_id,end_ts) VALUES (?,?) "
            "ON CONFLICT(commitment_id) DO UPDATE SET end_ts=excluded.end_ts",
            (old["id"], result["end_ts"]))
        # A new time is a new due stage: earlier notices must not suppress the next one.
        self._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (old["id"],))
        self._db.execute(
            "INSERT INTO assistant_schedule_versions VALUES (?,?) "
            "ON CONFLICT(commitment_id) DO UPDATE SET version=excluded.version",
            (old["id"], uuid.uuid4().hex))

    def calendar_occurrence(self, source_id: str, start_ts: float) -> dict | None:
        """The active synced Calendar occurrence (identifier + exact start) with its invitee
        metadata, or None. invitee_count None = not synced; organizer_is_user None = unknown;
        calendar_writable False = a read-only (e.g. subscribed) calendar, None = not said."""
        with self._lock:
            row = self._db.execute(
                "SELECT c.id,c.source_id,c.title,c.when_ts,i.invitee_count,i.organizer_is_user,"
                "w.writable AS calendar_writable "
                "FROM scheduled_commitments c LEFT JOIN calendar_event_invitees i ON i.commitment_id=c.id "
                "LEFT JOIN calendar_event_writable w ON w.commitment_id=c.id "
                "WHERE c.source='calendar' AND c.status='active' AND c.source_id=? AND c.when_ts=?",
                (source_id, start_ts)).fetchone()
        if row is None:
            return None
        row = dict(row)
        for flag in ("organizer_is_user", "calendar_writable"):
            if row[flag] is not None:
                row[flag] = bool(row[flag])
        return row

    def calendar_occurrence_current(self, source_id: str, start_ts: float, title: str) -> bool:
        """True while the synced Calendar occurrence (identifier + start) is active with
        this title: the check a confirmed move makes before it runs."""
        with self._lock:
            row = self._db.execute(
                "SELECT title FROM scheduled_commitments WHERE source='calendar' AND status='active' "
                "AND source_id=? AND when_ts=?", (source_id, start_ts)).fetchone()
        return row is not None and " ".join(row["title"].split()) == " ".join(str(title).split())

    def calendar_move_target(self, query: str, *, now: float | None = None,
                             current_start: float | None = None) -> dict:
        """The synced Calendar occurrence a move refers to, by title and (optionally) its
        current start. Returns {"row": {...}} or {"error": text, "needs_input": bool}.

        Only real Calendar rows (source 'calendar'), never a reminder or a Wisp item, and
        only occurrences not yet over. An exact title wins over a partial one. Several
        occurrences of one recurring event resolve to the next one; several different
        events need the user to say which."""
        from datetime import datetime
        now = time.time() if now is None else now
        needle = " ".join(str(query or "").split()).casefold()
        if not needle:
            return {"error": "Which event should move?", "needs_input": True}
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT c.id,c.source_id,c.title,c.when_ts,c.location,e.end_ts,"
                "i.invitee_count,i.organizer_is_user,w.writable AS calendar_writable FROM scheduled_commitments c "
                "LEFT JOIN calendar_event_ends e ON e.commitment_id=c.id "
                "LEFT JOIN calendar_event_invitees i ON i.commitment_id=c.id "
                "LEFT JOIN calendar_event_writable w ON w.commitment_id=c.id "
                "WHERE c.source='calendar' AND c.status='active' AND c.source_id IS NOT NULL "
                "AND c.source_id<>'' AND COALESCE(e.end_ts, c.when_ts) > ? "
                "ORDER BY c.when_ts, c.source_id", (now,)).fetchall()]
        key = lambda r: " ".join(r["title"].split()).casefold()  # noqa: E731
        matches = [r for r in rows if key(r) == needle] or [r for r in rows if needle in key(r)]
        if current_start is not None:
            matches = [r for r in matches if abs(r["when_ts"] - current_start) < 60]
        if not matches:
            return {"error": f"No upcoming Calendar event matches “{query}”.", "needs_input": True}
        if len({r["source_id"] for r in matches}) > 1:
            listed = "; ".join(f"{r['title']} ({datetime.fromtimestamp(r['when_ts']):%a %b %-d %-I:%M %p})"
                               for r in matches[:5])
            return {"error": f"Several events match “{query}”: {listed}. Which one?", "needs_input": True}
        row = matches[0]
        return {"row": {**row, "recurring": len(matches) > 1}}

    def claim_calendar_action(self, event_id: str, kind: str, action_id: str, payload: dict) -> dict:
        proposed = self.calendar_payload(kind, payload)
        with self._write_transaction():
            row = self._db.execute("SELECT * FROM assistant_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise KeyError("unknown action")
            stored = self.calendar_payload(row["kind"], json.loads(row["payload"]))
            if (row["kind"] != kind or json.loads(row["target"]).get("type") not in {"calendar", "verified_reminder"}
                    or not isinstance(action_id, str) or stored["action_id"] != action_id
                    or row["dedupe_key"] != "action:" + action_id or stored != proposed):
                raise ValueError("action identity or payload does not match")
            identity = {"event_id": event_id, "action_id": action_id, "kind": kind, "payload": stored}
            if row["result"]:
                return {**identity, "execute": False, "recorded": True,
                        **({"claim_token": row["claim_token"]} if row["kind"] in {
                            "create_reminder", "update_reminder", "complete_reminder", "delete_reminder"} else {}),
                        "result": self.calendar_result(kind, json.loads(row["result"]), stored)}
            if row["claim_token"]:
                return {**identity, "execute": False, "recorded": False,
                        **({"claim_token": row["claim_token"]} if row["kind"] in {
                            "create_reminder", "update_reminder", "complete_reminder", "delete_reminder"} else {}),
                        "error": "native outcome unknown; do not retry the action"}
            if row["state"] != "pending":
                raise ValueError("action is not pending")
            if row["expires_at"] is None or row["expires_at"] < time.time():
                result = {"ok": False, "status": "failed", "error": "action expired before native execution"}
                self._db.execute("UPDATE assistant_events SET result=?,state='ack',acknowledged_at=? WHERE id=?",
                                 (json.dumps(result, sort_keys=True), time.time(), event_id))
                return {**identity, "execute": False, "recorded": True, "result": result}
            token = uuid.uuid4().hex
            claim_order = self._db.execute(
                "SELECT COALESCE(MAX(claim_order),0)+1 FROM assistant_events").fetchone()[0]
            self._db.execute(
                "UPDATE assistant_events SET claim_token=?,claimed_at=?,claim_order=? WHERE id=?",
                (token, time.time(), claim_order, event_id))
            return {**identity, "execute": True, "recorded": False, "claim_token": token}

    def complete_calendar_action(self, event_id: str, kind: str, token: str, result: dict,
                                 *, reconcile: bool = False) -> bool:
        """Reconcile the successful native receipt and action in one transaction.
        Late receipts are valid; a timeout cannot prove native failure."""
        with self._write_transaction():
            row = self._db.execute("SELECT * FROM assistant_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise KeyError("unknown action")
            target = json.loads(row["target"])
            if (row["kind"] != kind or target.get("type") not in {"calendar", "verified_reminder"}
                    or not token or row["claim_token"] != token):
                raise ValueError("action identity or claim does not match")
            payload = self.calendar_payload(kind, json.loads(row["payload"]))
            result = self.calendar_result(kind, result, payload)
            if reconcile and result.get("ok") is not True:
                raise ValueError("reminder reconciliation requires positive native readback")
            encoded = json.dumps(result, sort_keys=True, allow_nan=False)
            if row["result"]:
                previous = json.loads(row["result"])
                if row["result"] != encoded and not (
                    reconcile and target.get("type") == "verified_reminder"
                    and previous.get("status") == "unknown" and result.get("ok") is True):
                    raise ValueError("action already has a different result")
                if row["result"] == encoded:
                    return True
            if result.get("ok") is True and kind in {
                "create_reminder", "update_reminder", "complete_reminder", "delete_reminder"}:
                source_id = result["source_id"] if kind == "create_reminder" else payload["source_id"]
                latest = self._db.execute(
                    "SELECT claim_order FROM assistant_reminder_verified WHERE source_id=?",
                    (source_id,)).fetchone()
                if latest and latest["claim_order"] > (row["claim_order"] or 0):
                    # A delayed older receipt still resolves its own action,
                    # but cannot undo a newer verified native action's state.
                    self._db.execute("UPDATE assistant_events SET result=? WHERE id=?",
                                     (encoded, event_id))
                    return True
            if result.get("ok") is True:
                reminder_receipt_at = (time.time() if kind in {
                    "create_reminder", "update_reminder", "complete_reminder", "delete_reminder"} else None)
                if kind == "create_calendar_event":
                    source_id = result.get("source_id")
                    if not isinstance(source_id, str) or not source_id:
                        raise ValueError("successful creation requires native source_id")
                    now = time.time()
                    inserted = self._db.execute(
                        "INSERT INTO commitments (id,source,source_id,kind,title,when_ts,location,"
                        "status,confidence,created_at,updated_at) VALUES (?,?,?,'event',?,?,?,'active',1,?,?) "
                        "ON CONFLICT(source,source_id,when_ts) DO NOTHING",
                        (uuid.uuid4().hex, "calendar", source_id, payload["title"], payload["when_ts"],
                         payload.get("location", ""), now, now))
                    if inserted.rowcount:
                        # A later native sync may already own this identity and duration.
                        # The creation receipt only supplies an end for the row it inserted.
                        self._db.execute(
                            "INSERT INTO calendar_event_ends(commitment_id,end_ts) "
                            "SELECT id,? FROM scheduled_commitments WHERE source='calendar' AND source_id=? AND when_ts=?",
                            (payload["when_ts"] + payload["duration_min"] * 60, source_id, payload["when_ts"]))
                elif kind == "delete_calendar_event":
                    self._db.execute(
                        "UPDATE commitments SET status='dismissed',updated_at=? "
                        "WHERE source='calendar' AND source_id=? AND when_ts=?",
                        (time.time(), payload["source_id"], payload["when_ts"]))
                elif kind == "move_calendar_event":
                    self._record_calendar_move(payload, result)
                elif kind == "cancel_calendar_event":
                    self._db.execute(
                        "UPDATE commitments SET status='dismissed',updated_at=? "
                        "WHERE source='calendar' AND source_id=? AND when_ts=?",
                        (time.time(), payload["source_id"], payload["expected_start_ts"]))
                elif kind == "create_reminder":
                    from service.assistant.verified_reminders import due_timestamp, expected_matches
                    receipt_at = reminder_receipt_at
                    self._db.execute(
                        "INSERT INTO commitments (id,source,source_id,kind,title,when_ts,all_day,due_date,status,"
                        "confidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'active',1,?,?) "
                        "ON CONFLICT(source,source_id,when_ts) DO UPDATE SET "
                        "kind=excluded.kind,title=excluded.title,all_day=excluded.all_day,due_date=excluded.due_date,status='active',"
                        "updated_at=excluded.updated_at",
                        (uuid.uuid4().hex, "reminders", result["source_id"],
                         payload["commitment_kind"], payload["title"],
                         due_timestamp(result), int(result.get('all_day') is True), result.get('due_date'), receipt_at, receipt_at))
                    self._db.execute(
                        "DELETE FROM assistant_reminder_terminals WHERE source_id=?",
                        (result["source_id"],))
                    self._db.execute(
                        "INSERT INTO assistant_reminder_writes VALUES (?,?,?) "
                        "ON CONFLICT(source_id) DO UPDATE SET recorded_at=excluded.recorded_at,"
                        "action_id=excluded.action_id",
                        (result["source_id"], receipt_at, payload["action_id"]))
                elif kind == "update_reminder":
                    from service.assistant.verified_reminders import due_timestamp, expected_matches
                    receipt_at = reminder_receipt_at
                    minute_start = int(payload["expected_due_ts"] // 60) * 60
                    minute_end = minute_start + 60
                    native_ids = [item["id"] for item in self._db.execute(
                        "SELECT id FROM scheduled_commitments WHERE source='reminders' AND source_id=? "
                        "AND title=? AND when_ts>=? AND when_ts<? AND all_day=? AND (due_date IS ? OR due_date=?)",
                        (payload["source_id"], payload["expected_title"], minute_start, minute_end,
                         int(bool(payload.get('expected_due_date'))), payload.get('expected_due_date'), payload.get('expected_due_date'))).fetchall()]
                    self._db.execute(
                        "UPDATE commitments SET title=?,when_ts=?,all_day=?,due_date=?,updated_at=? "
                        "WHERE source='reminders' AND source_id=? AND title=? AND id IN (" + ','.join('?' for _ in native_ids) + ")",
                        (payload["title"], due_timestamp(result), int(result.get('all_day') is True), result.get('due_date'), receipt_at,
                         payload["source_id"], payload["expected_title"], *native_ids))
                    for native_id in native_ids:
                        # A verified reschedule gives this item a new due stage.
                        # Keep the reset in the receipt transaction so an
                        # uncertain write cannot erase an old notification.
                        self._db.execute("DELETE FROM notify_log WHERE commitment_id=?",
                                         (native_id,))
                        self._db.execute(
                            "INSERT INTO assistant_schedule_versions VALUES (?,?) "
                            "ON CONFLICT(commitment_id) DO UPDATE SET version=excluded.version",
                            (native_id, uuid.uuid4().hex))
                    if native_ids:
                        for pending in self._db.execute(
                            "SELECT id,target FROM assistant_events WHERE kind='reminder' "
                            "AND state='pending'").fetchall():
                            target = json.loads(pending["target"])
                            if set(target.get("schedules", {})) & set(native_ids):
                                self._db.execute(
                                    "UPDATE assistant_events SET state='superseded' WHERE id=?",
                                    (pending["id"],))
                    self._db.execute(
                        "DELETE FROM assistant_reminder_terminals WHERE source_id=?",
                        (payload["source_id"],))
                    self._db.execute(
                        "INSERT INTO assistant_reminder_writes VALUES (?,?,?) "
                        "ON CONFLICT(source_id) DO UPDATE SET recorded_at=excluded.recorded_at,"
                        "action_id=excluded.action_id",
                        (payload["source_id"], receipt_at, payload["action_id"]))
                elif kind in {"complete_reminder", "delete_reminder"}:
                    terminal_at = reminder_receipt_at
                    terminal_status = "done" if kind == "complete_reminder" else "dismissed"
                    # The native writer checked the claimed title/due before
                    # acting and read back this exact native ID. An older
                    # verified update may since have moved its local row.
                    self._db.execute(
                        "UPDATE commitments SET status=?,updated_at=? WHERE source='reminders' "
                        "AND source_id=? AND status='active'",
                        (terminal_status, terminal_at, payload["source_id"]))
                    self._db.execute(
                        "INSERT INTO assistant_reminder_terminals VALUES (?,?,?,?) "
                        "ON CONFLICT(source_id) DO UPDATE SET status=excluded.status,"
                        "recorded_at=excluded.recorded_at,action_id=excluded.action_id",
                        (payload["source_id"], terminal_status, terminal_at, payload["action_id"]))
                    self._db.execute(
                        "DELETE FROM assistant_reminder_writes WHERE source_id=?",
                        (payload["source_id"],))
                    if kind == "delete_reminder" and payload["expected_due_ts"] is None:
                        removed = self._db.execute(
                            "DELETE FROM assistant_undated_reminders WHERE source_id=?",
                            (payload["source_id"],)).rowcount
                        self._db.execute("DELETE FROM assistant_stale_reminder_decisions WHERE source_id=?",
                                         (payload["source_id"],))
                        if removed:
                            self._db.execute(
                                "UPDATE assistant_undated_state SET stored=MAX(stored-1,0),"
                                "total=MAX(total-1,0) WHERE id=1")
                if reminder_receipt_at is not None:
                    source_id = (result["source_id"] if kind == "create_reminder"
                                 else payload["source_id"])
                    self._db.execute(
                        "INSERT INTO assistant_reminder_verified VALUES (?,?,?) "
                        "ON CONFLICT(source_id) DO UPDATE SET "
                        "recorded_at=CASE WHEN excluded.claim_order > "
                        "assistant_reminder_verified.claim_order THEN excluded.recorded_at "
                        "ELSE assistant_reminder_verified.recorded_at END,"
                        "claim_order=MAX(assistant_reminder_verified.claim_order,excluded.claim_order)",
                        (source_id, reminder_receipt_at, row["claim_order"] or 0))
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
            "SELECT id, title, when_ts, all_day FROM scheduled_commitments WHERE id=?", (cid,)).fetchone()
        if row is None:
            return []
        key = _dedupe_key(dict(row))
        if key is None:
            return []
        lo = key[1] * 60.0
        cands = self._db.execute(
            "SELECT id, title, when_ts, all_day FROM scheduled_commitments "
            "WHERE id <> ? AND when_ts >= ? AND when_ts < ?", (cid, lo, lo + 60.0)).fetchall()
        return [r["id"] for r in cands if _dedupe_key(dict(r)) == key]

    def _today_sync_receipt(self, source: str, diagnostics: dict, now: float, *, available: bool) -> None:
        """Called inside the same write transaction as source replacement."""
        from service.assistant.today import RevisionConflict, number
        if source not in {"calendar", "reminders"}:
            return
        if source == "reminders":
            self._behavior_receipt_head.authority.withdraw()
            self._behavior_receipt_head.metadata = None
            for frame in self._behavior_receipt_stages:
                frame["receipt"] = None
        started = diagnostics.get("snapshot_started_at")
        if started is not None:
            started = number(started, "snapshot_started_at")
            if not 0 <= started <= now + 300:
                raise ValueError("Invalid native snapshot time")
        previous = self._db.execute("SELECT payload FROM today_source_sync WHERE source=?", (source,)).fetchone()
        prior = json.loads(previous["payload"]) if previous else {}
        prior_started = prior.get("snapshot_started_at")
        if prior_started is not None and (started is None or started <= prior_started):
            raise RevisionConflict("An equal or newer native snapshot is already stored")
        # Persist only planning metadata; never copy diagnostic event-title lists.
        coverage = {key: diagnostics.get(key) for key in ("coverage_start", "coverage_end")}
        for key, value in coverage.items():
            if value is not None:
                coverage[key] = number(value, key)
        complete, partial = diagnostics.get("complete", True), diagnostics.get("partial", False)
        current = available and complete is True and partial is False
        if "complete" in diagnostics or "partial" in diagnostics:
            # Invalid latest metadata must not leave an older successful receipt
            # eligible. Keep only typed completeness proof, never diagnostic rows.
            coverage.update(complete=complete if type(complete) is bool else False,
                            partial=partial if type(partial) is bool else True)
        receipt = dict(available=current, syncing=bool(diagnostics.get("syncing")),
                       reason="Up to date" if current else (
                           "Source snapshot is incomplete" if available else "Source is unavailable or syncing"),
                       last_sync=min(now, started) if started is not None else now, snapshot_started_at=started, diagnostics=coverage)
        payload = json.dumps(receipt, allow_nan=False)
        self._db.execute("INSERT INTO today_source_sync(source,payload) VALUES (?,?) "
                         "ON CONFLICT(source) DO UPDATE SET payload=excluded.payload",
                         (source, payload))
        if (source == "reminders" and diagnostics.get("authorized") is True
                and diagnostics.get("available") is not False):
            metadata = _behavior_reminders_receipt(receipt)
            if metadata is not None and self._behavior_receipt_stages:
                deadline = time.monotonic()+min(metadata[:2])+1200-now
                self._behavior_receipt_stages[-1]["receipt"] = (payload, metadata, deadline)

    def today_source_unavailable(self, source: str, diagnostics: dict) -> None:
        with self._write_transaction():
            self._today_sync_receipt(source, diagnostics, time.time(), available=False)

    def _reopen_native_reminder(self, source_id: str) -> None:
        """Preserve completed history while starting a fresh active incarnation.

        Called only inside the authoritative sync transaction after its
        snapshot timestamp proves it began after the terminal receipt.
        Historical rows receive private IDs so an update back to their old
        due time cannot collide with the current native ID's unique key.
        """
        rows = self._db.execute(
            "SELECT id,status FROM scheduled_commitments WHERE source='reminders' AND source_id=?",
            (source_id,)).fetchall()
        for row in rows:
            cid = row["id"]
            if row["status"] == "done":
                self._db.execute(
                    "UPDATE commitments SET source_id=? WHERE id=?",
                    ("wisp-history:" + cid, cid))
            else:
                self._db.execute("DELETE FROM commitments WHERE id=?", (cid,))
                self._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
                self._db.execute("DELETE FROM assistant_schedule_versions WHERE commitment_id=?",
                                 (cid,))
        self._db.execute("DELETE FROM assistant_reminder_terminals WHERE source_id=?",
                         (source_id,))

    # --- writes -----------------------------------------------------------
    def sync_source(self, source: str, items: list[dict], *, diagnostics: dict | None = None,
                    undated: UndatedSnapshot | None = None) -> int:
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

        Claimed native writes keep their local rows until their outcome is
        known or a newer verified write supersedes them. Successful receipts
        fence snapshots begun before the write, even if those snapshots contain
        no items. An empty feed may remove a dismissed commitment without erasing
        its terminal watermark;
        a fresh native reopen archives completed history and gives the new
        active incarnation its own notification generation.
        """
        if source == MANUAL_SOURCE:
            # sync_source deletes every active row of `source` that is absent from
            # `items`. For Calendar/Reminders that mirrors upstream; for the user's own
            # reminders there is no upstream, so an empty or partial batch would erase
            # them. Local rows change only through add_manual and the per-ID mutators.
            raise ValueError("manual reminders are not a sync source")
        now = time.time()
        with self._write_transaction():
            self._today_sync_receipt(source, diagnostics or {}, now, available=True)
            if source == "reminders" and undated is not None:
                # Same unit as the dated rows and the receipt: a failure anywhere rolls all
                # three back together, and the equal-timestamp retry is then valid again.
                self._write_undated(undated, (diagnostics or {}).get("snapshot_started_at"), now)
            existing = self._db.execute(
                "SELECT id, source_id, when_ts, status, updated_at, all_day, due_date FROM commitments WHERE source=?",
                (source,)).fetchall()
            existing_by_key = {(r["source_id"], r["when_ts"]): r["id"] for r in existing}
            existing_status = {r["id"]: r["status"] for r in existing}
            existing_source_id = {r["id"]: r["source_id"] for r in existing}
            unresolved_native_ids: set[str] = set()
            if source == "reminders":
                # Claim order is durable across restarts. Receipt arrival time
                # cannot supersede a later claim: an older native write may
                # report success while the newer write is still in flight.
                verified_native_order = {r["source_id"]: r["claim_order"] for r in
                                         self._db.execute(
                                             "SELECT source_id,claim_order FROM assistant_reminder_verified"
                                         ).fetchall()}
                for action in self._db.execute(
                    "SELECT payload,result,claim_order FROM assistant_events "
                    "WHERE claim_token IS NOT NULL "
                    "AND kind IN ('update_reminder','complete_reminder','delete_reminder')"
                ).fetchall():
                    try:
                        result = json.loads(action["result"]) if action["result"] is not None else None
                        payload = json.loads(action["payload"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(result, dict) and result.get("status") in {"succeeded", "failed"}:
                        continue
                    source_id = payload.get("source_id") if isinstance(payload, dict) else None
                    if (isinstance(source_id, str) and source_id and
                            verified_native_order.get(source_id, 0) <=
                            (action["claim_order"] or 0)):
                        unresolved_native_ids.add(source_id)
            terminal_native_at = ({r["source_id"]: r["recorded_at"] for r in
                                   self._db.execute(
                                       "SELECT source_id,recorded_at FROM assistant_reminder_terminals"
                                   ).fetchall()} if source == "reminders" else {})
            snapshot_started_at = (diagnostics or {}).get("snapshot_started_at")
            write_native_at = ({r["source_id"]: r["recorded_at"] for r in
                                self._db.execute(
                                    "SELECT source_id,recorded_at FROM assistant_reminder_writes"
                                ).fetchall()} if source == "reminders" else {})
            # A snapshot begun after a verified write can supersede its fence,
            # including when it is empty (a later external deletion). Earlier
            # snapshots cannot erase or revert the receipt-backed local row.
            if snapshot_started_at is not None:
                for sid, recorded_at in list(write_native_at.items()):
                    if snapshot_started_at > recorded_at and sid not in unresolved_native_ids:
                        self._db.execute(
                            "DELETE FROM assistant_reminder_writes WHERE source_id=?", (sid,))
                        write_native_at.pop(sid)
            reopened_native_ids: set[str] = set()

            seen_keys: set[tuple] = set()
            for it in items:
                sid = str(it.get("source_id") or uuid.uuid4().hex)
                if (sid in unresolved_native_ids or
                        sid in write_native_at):
                    continue
                # Keep receipt protection independently of commitment rows:
                # an empty feed may prune a deleted row before an older fetch
                # returns it. A genuinely newer incomplete snapshot proves
                # the native item was reopened or restored.
                if (sid in terminal_native_at and
                        (snapshot_started_at is None or
                         snapshot_started_at <= terminal_native_at[sid])):
                    continue
                if sid in terminal_native_at:
                    self._reopen_native_reminder(sid)
                    for old_key in [key for key in existing_by_key if key[0] == sid]:
                        existing_by_key.pop(old_key)
                    terminal_native_at.pop(sid)
                    reopened_native_ids.add(sid)
                reopened = sid in reopened_native_ids
                when_ts = it.get("when_ts")
                due_date = it.get('due_date') if source == 'reminders' and it.get('all_day') else None
                from service.assistant.verified_reminders import valid_due_date
                if due_date is not None and not valid_due_date(due_date):
                    raise ValueError('invalid native floating date')
                if due_date:
                    same = [row for row in existing if row['source_id'] == sid and row['due_date'] == due_date and row['all_day']]
                    if len(same) == 1:
                        when_ts = same[0]['when_ts']
                key = (sid, when_ts)
                seen_keys.add(key)
                cid = existing_by_key.get(key) or uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO commitments "
                    "(id, source, source_id, kind, title, context, organizer, account, when_ts, "
                    " all_day, due_date, location, url, status, confidence, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source, source_id, when_ts) DO UPDATE SET "
                    "  kind=excluded.kind, title=excluded.title, context=excluded.context, "
                    "  organizer=excluded.organizer, account=excluded.account, "
                    "  all_day=excluded.all_day, due_date=excluded.due_date, location=excluded.location, "
                    "  url=excluded.url, updated_at=excluded.updated_at, "
                    "  status=CASE WHEN commitments.status='dismissed' THEN 'dismissed' ELSE 'active' END",
                    (cid, source, sid, it.get("kind", "event"), it.get("title", "(untitled)"),
                     it.get("context"), it.get("organizer"), it.get("account"), when_ts,
                     int(bool(it.get("all_day"))), due_date,
                     it.get("location"), it.get("url"), "active",
                     float(it.get("confidence", 1.0)), now, now))
                existing_by_key[key] = cid
                if reopened:
                    self._db.execute(
                        "INSERT INTO assistant_schedule_versions VALUES (?,?) "
                        "ON CONFLICT(commitment_id) DO UPDATE SET version=excluded.version",
                        (cid, uuid.uuid4().hex))
                if source == "calendar":
                    self._db.execute("DELETE FROM calendar_event_invitees WHERE commitment_id=?", (cid,))
                    count = it.get("invitee_count")
                    if type(count) is int and count >= 0:
                        flag = it.get("organizer_is_user")
                        self._db.execute("INSERT INTO calendar_event_invitees VALUES (?,?,?)",
                                         (cid, count, int(flag) if type(flag) is bool else None))
                    self._db.execute("DELETE FROM calendar_event_writable WHERE commitment_id=?", (cid,))
                    writable = it.get("calendar_writable")
                    if type(writable) is bool:
                        self._db.execute("INSERT INTO calendar_event_writable VALUES (?,?)", (cid, int(writable)))
                    self._db.execute("DELETE FROM calendar_event_ends WHERE commitment_id=?", (cid,))
                    end_ts = it.get("end_ts")
                    if end_ts is not None:
                        from service.assistant.today import number
                        end_ts = number(end_ts, "end_ts")
                        if end_ts < number(when_ts, "when_ts"):
                            raise ValueError("Calendar end must be at or after start")
                        self._db.execute("INSERT INTO calendar_event_ends VALUES (?,?)", (cid, end_ts))

            # drop rows for this source whose (source_id, when_ts) vanished
            # upstream (event deleted, or an occurrence's time changed — the old
            # occurrence row is pruned once the new one is inserted above).
            stale_ids = [rid for key, rid in existing_by_key.items() if key not in seen_keys]
            for rid in stale_ids:
                sid = existing_source_id.get(rid)
                if source == "reminders" and (sid in unresolved_native_ids or sid in write_native_at):
                    continue
                # The native feed contains only incomplete reminders. A
                # verified completion must remain in Wisp history after it
                # disappears from that feed; absence alone is not deletion.
                if source == "reminders" and existing_status.get(rid) == "done":
                    continue
                self._db.execute("DELETE FROM commitments WHERE id=?", (rid,))
                self._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (rid,))
            self._db.execute("DELETE FROM calendar_event_ends WHERE NOT EXISTS "
                             "(SELECT 1 FROM commitments WHERE id=commitment_id)")
            self._db.execute("DELETE FROM calendar_event_invitees WHERE NOT EXISTS "
                             "(SELECT 1 FROM commitments WHERE id=commitment_id)")
            self._db.execute("DELETE FROM calendar_event_writable WHERE NOT EXISTS "
                             "(SELECT 1 FROM commitments WHERE id=commitment_id)")
        return len(items)

    def _write_undated(self, snapshot: UndatedSnapshot, snapshot_started_at, now: float) -> None:
        """Replace the undated rows and their state. Call only inside a write transaction."""
        from service.assistant.today import number
        started = number(snapshot_started_at, "snapshot_started_at") if snapshot_started_at is not None else None
        # A native read that started before a verified delete still lists the reminder it
        # deleted. Re-inserting it would bring the question back for an item that is gone,
        # so rows whose verified delete was recorded after this read began are skipped. A
        # later read (started after the receipt) is believed, including a Recently Deleted
        # restore that brings the same identifier back.
        deleted_after = set()
        if started is not None:
            deleted_after = {row["source_id"] for row in self._db.execute(
                "SELECT source_id FROM assistant_reminder_terminals "
                "WHERE status='dismissed' AND recorded_at>=?", (started,)).fetchall()}
        rows = [row for row in snapshot.rows if row[0] not in deleted_after]
        skipped = len(snapshot.rows) - len(rows)
        self._db.execute("DELETE FROM assistant_undated_reminders")
        self._db.executemany(
            "INSERT INTO assistant_undated_reminders(source_id,title,context,created_ts,synced_at) "
            "VALUES (?,?,?,?,?)",
            [row + (now,) for row in rows])
        if not snapshot.truncated:
            # A complete read is authoritative: a Keep for a reminder that is gone from both
            # the undated list and the active dated rows (completed or deleted elsewhere) has
            # nothing left to protect. A Keep follows its reminder through a reschedule.
            self._db.execute(
                "DELETE FROM assistant_stale_reminder_decisions WHERE source_id NOT IN "
                "(SELECT source_id FROM assistant_undated_reminders) AND source_id NOT IN "
                "(SELECT source_id FROM scheduled_commitments WHERE source='reminders' AND status='active')")
        self._db.execute(
            "INSERT INTO assistant_undated_state(id,snapshot_started_at,status,total,stored,recorded_at) "
            "VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET snapshot_started_at=excluded.snapshot_started_at, "
            "status=excluded.status, total=excluded.total, stored=excluded.stored, recorded_at=excluded.recorded_at",
            (started, "truncated" if snapshot.truncated else "complete", snapshot.total - skipped,
             len(rows), now))

    def replace_undated_reminders(self, items: list[dict], *, total: int | None = None,
                                  snapshot_started_at: float | None = None) -> int:
        """Replace the undated list on its own (tests and tools). The sync endpoint does not
        use this: it passes the snapshot to ``sync_source`` so the list shares the dated
        update's transaction. With no ``snapshot_started_at`` the list can never read as
        current, which is the safe default."""
        snapshot = parse_undated_snapshot(items, len(items) if total is None else total)
        if snapshot is None:
            raise ValueError("invalid undated reminder snapshot")
        with self._write_transaction():
            self._write_undated(snapshot, snapshot_started_at, time.time())
        return len(snapshot.rows)

    def _undated_rows(self, now: float | None = None) -> list[dict]:
        """Every stored undated reminder with its stale-policy state, by title then id."""
        from service.assistant.stale_reminders import classify
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT u.source_id,u.title,u.context,u.created_ts,d.decision "
                "FROM assistant_undated_reminders u LEFT JOIN assistant_stale_reminder_decisions d "
                "ON d.source_id=u.source_id ORDER BY u.title COLLATE NOCASE, u.source_id").fetchall()
        return [{**dict(r), "stale_state": classify(r["created_ts"], r["decision"], now)} for r in rows]

    def undated_reminders(self, query: str = "") -> list[dict]:
        """Incomplete Apple reminders with no due date, by title then id.

        Reminders the stale policy excludes (about a week old or more, not kept by the user)
        are left out here, which is how every reader (Day plans, tools, task evidence, model
        context) stops using them. ``undated_view`` reports how many were hidden.
        """
        from service.assistant.stale_reminders import EXCLUDED_STATES
        needle = (query or "").strip().lower()
        return [{"source_id": r["source_id"], "title": r["title"], "context": r["context"]}
                for r in self._undated_rows()
                if r["stale_state"] not in EXCLUDED_STATES
                and (not needle or needle in r["title"].lower())]

    def stale_reminder_asks(self, now: float | None = None) -> list[dict]:
        """Undated reminders in the ask window, oldest first, but only from a current list:
        a stale list could offer to delete something that already changed."""
        if self.undated_view()["freshness"] != "current":
            return []
        now = time.time() if now is None else now
        rows = [r for r in self._undated_rows(now) if r["stale_state"] == "ask"]
        rows.sort(key=lambda r: (r["created_ts"], r["source_id"]))
        asks = [{"kind": "stale_reminder", "reason": "undated", "source_id": r["source_id"],
                 "title": r["title"], "context": r["context"], "created_ts": r["created_ts"],
                 "due_ts": None, "age_days": int((now - r["created_ts"]) // 86400),
                 "actions": ["keep", "delete"]} for r in rows]
        overdue = self._overdue_rows(now)
        once = {r["source_id"] for r in overdue}
        counts = {sid: sum(r["source_id"] == sid for r in overdue) for sid in once}
        # ``age_days`` is days overdue for these. A native ID with two active rows is
        # ambiguous for an exact delete, so it is hidden but never offered.
        asks += [{"kind": "stale_reminder", "reason": "overdue", "source_id": r["source_id"],
                  "title": r["title"], "context": r["context"], "created_ts": None,
                  "due_ts": r["when_ts"], "age_days": int((now - r["when_ts"]) // 86400),
                  "actions": ["keep", "delete"]}
                 for r in overdue if r["stale_state"] == "ask" and counts[r["source_id"]] == 1]
        asks.sort(key=lambda a: (a["created_ts"] if a["reason"] == "undated" else a["due_ts"],
                                 a["source_id"]))
        return asks

    def stale_reminder_target(self, source_id: str, now: float | None = None) -> dict | None:
        """The stored row for a Keep/Delete answer, with its policy state and ``reason``,
        or None. An overdue native ID with more than one active row is ambiguous: None."""
        row = next((r for r in self._undated_rows(now) if r["source_id"] == source_id), None)
        if row is not None:
            return {**row, "reason": "undated"}
        from service.assistant.stale_reminders import classify_overdue
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT c.id,c.source_id,c.title,c.context,c.when_ts,c.all_day,c.due_date,d.decision FROM scheduled_commitments c "
                "LEFT JOIN assistant_stale_reminder_decisions d ON d.source_id=c.source_id "
                "WHERE c.source='reminders' AND c.status='active' AND c.when_ts IS NOT NULL "
                "AND c.source_id=?", (source_id,)).fetchall()
        if len(rows) != 1:
            return None
        row = dict(rows[0])
        return {**row, "reason": "overdue",
                "stale_state": classify_overdue(row["when_ts"], row["decision"], now)}

    def _overdue_rows(self, now: float | None = None) -> list[dict]:
        """Active DATED Apple reminders at least ASK_AFTER_S overdue, with their policy
        state. Only Reminders.app items (source 'reminders'); a Wisp-only reminder has its
        own 14-day rule in the reminder tools."""
        from service.assistant.stale_reminders import ASK_AFTER_S, classify_overdue
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT c.id,c.source_id,c.title,c.context,c.when_ts,c.all_day,d.decision FROM scheduled_commitments c "
                "LEFT JOIN assistant_stale_reminder_decisions d ON d.source_id=c.source_id "
                "WHERE c.source='reminders' AND c.status='active' AND c.when_ts IS NOT NULL "
                "AND c.when_ts<=? ORDER BY c.when_ts, c.source_id", (now - ASK_AFTER_S,)).fetchall()
        return [{**dict(r), "stale_state": classify_overdue(r["when_ts"], r["decision"], now)}
                for r in rows]

    def hidden_commitment_ids(self, now: float | None = None) -> set[str]:
        """Commitment rows no reader may use: overdue Apple reminders in the ask or stale
        state, plus any row describing the same item (a Wisp mirror of it collapses with it
        in every list, so leaving the twin would bring the reminder straight back)."""
        from service.assistant.stale_reminders import EXCLUDED_STATES
        with self._lock:
            rows = [r for r in self._overdue_rows(now) if r["stale_state"] in EXCLUDED_STATES]
            hidden = {r["id"] for r in rows}
            keys = {_dedupe_key(r) for r in rows} - {None}
            if not keys:
                return hidden
            # One query for every twin, not one per hidden row. A twin is a non-native row
            # (a Wisp mirror, a same-minute event) that collapses with a hidden reminder.
            # Another Apple reminder is never a twin: it has its own question and its own
            # Keep, and must not disappear because a same-titled one was left unanswered.
            lo = min(k[1] for k in keys) * 60.0
            hi = (max(k[1] for k in keys) + 1) * 60.0
            for twin in self._db.execute(
                    "SELECT id,title,when_ts,all_day FROM scheduled_commitments WHERE status='active' "
                    "AND source<>'reminders' AND when_ts>=? AND when_ts<?", (lo, hi)).fetchall():
                if _dedupe_key(dict(twin)) in keys:
                    hidden.add(twin["id"])
        return hidden

    def _hide_stale(self, items: list[dict], now: float | None) -> list[dict]:
        """Drop collapsed items the stale policy hides (the item or any of its twins)."""
        hidden = self.hidden_commitment_ids(now)
        if not hidden:
            return items

        def shown(c: dict) -> bool:
            members = [(c["id"], c.get("source"))] + list(zip(
                c.get("duplicate_ids") or (), c.get("duplicate_sources") or ()))
            # A group stays while any Apple reminder in it is still shown (one the user
            # kept, say), so it never vanishes because a same-titled one went unanswered.
            if any(src == "reminders" and mid not in hidden for mid, src in members):
                return True
            return not any(mid in hidden for mid, _ in members)
        return [c for c in items if shown(c)]

    def hidden_reminders(self, now: float | None = None) -> list[dict]:
        """Every Apple reminder the stale policy currently hides, undated and overdue alike:
        [{source_id, title, reason: "undated"|"overdue", stale_state: "ask"|"stale",
        hidden_since}].
        For features that carry reminders forward on their own (the evening check-in), so
        they can drop the same items every other reader already leaves out.
        ``hidden_since`` is when the policy began hiding it: a week after it was added, or a
        week after it fell due."""
        from service.assistant.stale_reminders import ASK_AFTER_S, EXCLUDED_STATES
        out = [{"source_id": r["source_id"], "title": r["title"], "reason": "undated",
                "stale_state": r["stale_state"], "hidden_since": r["created_ts"] + ASK_AFTER_S}
               for r in self._undated_rows(now) if r["stale_state"] in EXCLUDED_STATES]
        out += [{"source_id": r["source_id"], "title": r["title"], "reason": "overdue",
                 "stale_state": r["stale_state"], "hidden_since": r["when_ts"] + ASK_AFTER_S}
                for r in self._overdue_rows(now) if r["stale_state"] in EXCLUDED_STATES]
        return out

    def hidden_reminder_titles(self, now: float | None = None) -> set[str]:
        """``hidden_reminders`` titles, whitespace-collapsed and casefolded: the form to
        compare a carried-forward task title against."""
        return {" ".join(r["title"].split()).casefold() for r in self.hidden_reminders(now)}

    def hide_stale_tasks(self, tasks: list[dict], now: float | None = None) -> list[dict]:
        """``tasks`` without the Wisp Day tasks that duplicate a hidden reminder: same title,
        whitespace-collapsed and casefolded, or "Review: " plus it. Display only; nothing is
        deleted, and a Keep on the reminder brings the task back with it.

        A task planned after the reminder was hidden is the user's own choice and stays.
        "Planned" is its ``origin_ts`` when it has one (Plan time records the reminder's
        due time there), else when the task was first saved. A task with neither, made
        before created_at existed, is treated as made from the reminder. created_at is the
        first save, so renaming an older task to a hidden reminder's title hides it too."""
        from service.assistant.stale_reminders import valid_created_ts
        since: dict[str, float] = {}
        for r in self.hidden_reminders(now):
            key = " ".join(r["title"].split()).casefold()
            since[key] = max(since.get(key, 0.0), r["hidden_since"])
        if not since:
            return tasks

        def matched(task: dict) -> float | None:
            title = " ".join(str(task.get("title") or "").split()).casefold()
            if title.startswith("review:"):
                title = title[len("review:"):].strip()
            return since.get(title)
        candidates = [t for t in tasks if matched(t) is not None]
        if not candidates:
            return tasks
        ids = [t["id"] for t in candidates]
        with self._lock:
            created = {r["id"]: r["created_at"] for r in self._db.execute(
                f"SELECT id,created_at FROM today_tasks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        hidden = set()
        for task in candidates:
            planned = valid_created_ts(task.get("origin_ts"))
            if planned is None:
                planned = valid_created_ts(created.get(task["id"]))
            if planned is None or planned < matched(task):
                hidden.add(task["id"])
        return [t for t in tasks if t.get("id") not in hidden]

    def keep_stale_reminder(self, source_id: str, title: str) -> None:
        with self._write_transaction():
            self._db.execute(
                "INSERT INTO assistant_stale_reminder_decisions(source_id,decision,title,decided_at) "
                "VALUES (?,'keep',?,?) ON CONFLICT(source_id) DO UPDATE SET "
                "decision=excluded.decision,title=excluded.title,decided_at=excluded.decided_at",
                (source_id, title, time.time()))

    def undated_listing(self, query: str = "") -> tuple[dict, list[dict]]:
        """The view and the matching rows from ONE locked read, so they always describe the
        same snapshot even if a sync lands between two separate calls."""
        with self._lock:
            return self.undated_view(), self.undated_reminders(query)

    def raw_reminder_list_snapshot(self, *, list_label: str = "", query: str = "",
                                   limit: int = 500) -> dict:
        """Native incomplete rows and receipts in one read transaction, without collapse.

        Context is only a list label: this carrier supplies no stable list or
        account identity. Distinct native IDs with equal titles remain distinct.
        """
        if (not isinstance(list_label, str) or not isinstance(query, str)
                or type(limit) is not int or not 1 <= limit <= 500):
            raise ValueError("invalid Reminder list filters or bounds")
        with self.transaction(write=False) as db:
            receipt_row = db.execute("SELECT payload FROM today_source_sync WHERE source='reminders'").fetchone()
            receipt = json.loads(receipt_row["payload"]) if receipt_row else {}
            dated_total = db.execute("SELECT COUNT(*) FROM scheduled_commitments WHERE source='reminders' AND status='active'").fetchone()[0]
            dated = [dict(row) for row in db.execute(
                "SELECT id,source_id,title,context,when_ts,status FROM scheduled_commitments "
                "WHERE source='reminders' AND status='active' ORDER BY when_ts,source_id LIMIT 5000").fetchall()]
            undated = self.undated_reminders()
            view = self.undated_view()
        label, needle = list_label.strip().casefold(), query.strip().casefold()
        def matches(row):
            return ((not label or str(row.get("context") or "").strip().casefold() == label)
                    and (not needle or needle in row["title"].casefold()))
        rows = [{**row, "due_state": "dated"} for row in dated if matches(row)]
        rows += [{**row, "due_state": "undated", "when_ts": None, "status": "active"}
                 for row in undated if matches(row)]
        return {"receipt": receipt, "undated_view": view, "dated_cached": dated_total,
                "dated_inspected": len(dated), "scan_omitted": dated_total - len(dated),
                "undated_cached": len(undated), "matching": len(rows), "shown": min(limit, len(rows)),
                "omitted": max(0, len(rows) - limit), "records": rows[:limit],
                "list_label": list_label, "stable_list_id": None, "account_id": None}

    def undated_view(self) -> dict:
        """How trustworthy the stored undated list is, read as one coherent unit.

        ``freshness`` is "current" only when the list was written from the very native
        snapshot that the Reminders source receipt records as the latest accepted one.
        A later dated-only snapshot, an invalid or missing undated payload, or an
        unavailable read all leave the receipt ahead of the list, so the list is "stale".
        "never" means no list has ever been received. ``status`` is "complete" or
        "truncated" (``total`` is the app's true count, ``stored`` how many it kept).
        """
        with self._lock:
            state = self._db.execute(
                "SELECT snapshot_started_at,status,total,stored FROM assistant_undated_state WHERE id=1"
            ).fetchone()
            receipt = self._db.execute(
                "SELECT payload FROM today_source_sync WHERE source='reminders'").fetchone()
        if state is None:
            return {"freshness": "never", "status": "unknown", "total": 0, "stored": 0}
        latest = None
        if receipt is not None:
            payload = json.loads(receipt["payload"])
            if payload.get("available") is True:
                latest = payload.get("snapshot_started_at")
        current = (state["snapshot_started_at"] is not None and latest is not None
                   and state["snapshot_started_at"] == latest)
        # Excluded stale reminders are not part of what any reader can see, so they come
        # out of both counts; ``stale_hidden`` says how many, for an honest caveat.
        from service.assistant.stale_reminders import EXCLUDED_STATES
        hidden = sum(r["stale_state"] in EXCLUDED_STATES for r in self._undated_rows())
        return {"freshness": "current" if current else "stale", "status": state["status"],
                "total": max(0, state["total"] - hidden), "stored": max(0, state["stored"] - hidden),
                "stale_hidden": hidden}

    def add_manual(self, title: str, when_ts: float, kind: str = "reminder",
                   context: str | None = None, *, source: str = MANUAL_SOURCE, all_day: bool = False, due_date: str | None = None) -> dict:
        assert_mutation_allowed()
        from service.assistant.verified_reminders import valid_due_date
        if all_day and not valid_due_date(due_date):
            raise ValueError('all-day reminders require a floating date')
        cid = uuid.uuid4().hex
        now = time.time()
        with self._write_transaction():
            self._db.execute(
                "INSERT INTO commitments (id, source, source_id, kind, title, context, "
                "when_ts, all_day, due_date, status, confidence, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, source, cid, kind, title, context, when_ts, int(all_day), due_date if all_day else None, "active", 1.0, now, now))
        return self.get(cid)

    def delete(self, cid: str) -> bool:
        with self._write_transaction():
            cur = self._db.execute("DELETE FROM commitments WHERE id=?", (cid,))
            self._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
            self._db.execute("DELETE FROM calendar_event_ends WHERE commitment_id=?", (cid,))
            self._db.execute("DELETE FROM calendar_event_invitees WHERE commitment_id=?", (cid,))
            self._db.execute("DELETE FROM calendar_event_writable WHERE commitment_id=?", (cid,))
        return cur.rowcount > 0

    def update_schedule(self, ids: list[str], when_ts: float,
                        title: str | None = None, *, all_day: bool | None = None, due_date: str | None = None) -> int:
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
                old = self._db.execute('SELECT all_day FROM commitments WHERE id=?', (cid,)).fetchone()
                floating = bool(old and old['all_day']) if all_day is None else all_day
                from service.assistant.verified_reminders import valid_due_date
                if floating and not valid_due_date(due_date):
                    raise ValueError('all-day updates require a floating date')
                self._db.execute('UPDATE commitments SET due_date=? WHERE id=?', (due_date if floating else None, cid))
                if title is None:
                    cur = self._db.execute(
                        "UPDATE commitments SET when_ts=?, all_day=COALESCE(?,all_day), updated_at=? WHERE id=?",
                        (float(when_ts), int(all_day) if all_day is not None else None, now, cid))
                else:
                    cur = self._db.execute(
                        "UPDATE commitments SET title=?, when_ts=?, all_day=COALESCE(?,all_day), updated_at=? WHERE id=?",
                        (title, float(when_ts), int(all_day) if all_day is not None else None, now, cid))
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
        assert_mutation_allowed()
        with self._write_transaction():
            cur = self._db.execute(
                "UPDATE commitments SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), cid))
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
        assert_mutation_allowed()
        with self._write_transaction():
            now = time.time()
            ids = [cid] + self.duplicate_ids(cid)
            self._db.executemany(
                "INSERT OR IGNORE INTO notify_log (commitment_id, stage, sent_at) "
                "VALUES (?,?,?)", [(i, stage, now) for i in ids])

    def already_notified(self, cid: str, stage: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM notify_log WHERE commitment_id=? AND stage=?",
            (cid, stage)).fetchone()
        return row is not None

    # --- reads ------------------------------------------------------------
    def get(self, cid: str) -> dict | None:
        row = self._db.execute("SELECT * FROM scheduled_commitments WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None

    def next_active(self, now: float | None = None) -> dict | None:
        now = now if now is not None else time.time()
        # LIMIT 1 would return an arbitrary member of a duplicate group (they
        # share when_ts, so SQLite's tie-break decides) — take a small window
        # and collapse it so the chip shows the same row the lists do.
        rows = self._db.execute(
            "SELECT * FROM scheduled_commitments WHERE status='active' AND when_ts >= ?"
            + _seed_clause() +
            " ORDER BY when_ts ASC LIMIT 50", (now - 300,)).fetchall()  # 5-min grace
        items = self._collapse([dict(r) for r in rows])
        return items[0] if items else None

    def upcoming(self, now: float | None = None, days: int = 7) -> list[dict]:
        now = now if now is not None else time.time()
        rows = self._db.execute(
            "SELECT * FROM scheduled_commitments WHERE status='active' AND when_ts >= ? AND when_ts <= ?"
            + _seed_clause() +
            " ORDER BY when_ts ASC", (now - 300, now + days * 86400)).fetchall()
        return self._collapse([dict(r) for r in rows])

    def calendar_events_overlapping(self, start_ts: float, end_ts: float) -> list[dict]:
        """Active Calendar events that overlap a half-open local-day interval.

        A start-time-only upcoming query loses all-day events after midnight
        and timed events once they have started. For a day view, include events
        that start inside the day plus earlier starts whose recorded end crosses
        into it. ``end_ts == start_ts`` events inside the day are kept by the
        start-in-range arm; an event ending exactly at day start is excluded.
        """
        start, end = float(start_ts), float(end_ts)
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("Calendar day bounds must be finite and increasing")
        with self._lock:
            rows = self._db.execute(
                "SELECT c.*, e.end_ts FROM scheduled_commitments c "
                "LEFT JOIN calendar_event_ends e ON e.commitment_id=c.id "
                "WHERE c.status='active' AND c.source='calendar' AND c.when_ts < ? "
                "AND (c.when_ts >= ? OR e.end_ts > ?)" + _seed_clause() +
                " ORDER BY c.when_ts ASC", (end, start, start)).fetchall()
        return [dict(row) for row in rows]

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
            "SELECT * FROM scheduled_commitments WHERE status='active' AND created_at >= ?"
            + _seed_clause() +
            " ORDER BY created_at DESC LIMIT ?", (since_ts, limit)).fetchall()
        return self._hide_stale(self._collapse([dict(r) for r in rows]), None)

    def active_between(self, start_ts: float | None = None,
                       end_ts: float | None = None, *, include_stale: bool = False) -> list[dict]:
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
            "SELECT * FROM scheduled_commitments WHERE " + " AND ".join(where)
            + _seed_clause() + " ORDER BY when_ts ASC", params).fetchall()
        items = self._collapse([dict(r) for r in rows])
        # ``include_stale`` is only for an explicit, confirmed removal the user asked for
        # (clearing or cancelling reminders), or for read-only aging
        # (checkin.reminder_index): hiding an item must not stop deleting it, or stop a
        # task made from it from aging.
        return items if include_stale else self._hide_stale(items, None)

    def history(self, now: float | None = None, days: int = 365, *,
                include_stale: bool = False) -> list[dict]:
        """Past active commitments — the mirror of `upcoming`, most-recent
        first. Safe to widen: `due_reminders` only ever reads `active_future`
        (when_ts >= now), so past rows synced in here can never trigger a
        stale reminder no matter how far back `days` reaches."""
        now = now if now is not None else time.time()
        rows = self._db.execute(
            "SELECT * FROM scheduled_commitments WHERE status='active' AND when_ts < ? AND when_ts >= ?"
            + _seed_clause() +
            " ORDER BY when_ts DESC", (now - 300, now - days * 86400)).fetchall()
        items = self._collapse([dict(r) for r in rows])
        return items if include_stale else self._hide_stale(items, now)

    def reminder_snapshot(self, now: float | None = None, horizon_days: int = 30) -> list[dict]:
        """Capture payload inputs, twin schedules and revisions in one SELECT.

        Each twin retains its own timestamp (native sync can truncate seconds).
        The private snapshot is checked again atomically when enqueueing.
        """
        now = now if now is not None else time.time()
        with self._lock:
            rows = self._db.execute(
                "SELECT commitments.*, COALESCE(v.version, '') AS generation FROM scheduled_commitments commitments "
                "LEFT JOIN assistant_schedule_versions v ON v.commitment_id=commitments.id "
                "WHERE status='active' AND when_ts >= ? AND when_ts <= ?"
                + _seed_clause() + " ORDER BY when_ts ASC",
                (now - 300, now + horizon_days * 86400)).fetchall()
        members = {r["id"]: {k: r[k] for k in ("title", "kind", "context", "when_ts", "status", "generation", "all_day")}
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
            "SELECT * FROM scheduled_commitments WHERE status='active' AND when_ts >= ? AND when_ts <= ?"
            + _seed_clause() +
            " ORDER BY when_ts ASC", (now - 300, now + horizon_days * 86400)).fetchall()
        return self._collapse([dict(r) for r in rows])


# module-level singleton
assistant_store = AssistantStore()

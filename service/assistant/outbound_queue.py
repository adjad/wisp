"""Queue for sends the user scheduled for later ("text mom at 6pm").

Why this is its own store rather than a commitment with a reminder on it: a
commitment NOTIFIES the user and they act. A scheduled send ACTS on its own,
unattended, and speaks in the user's name to someone else. That difference
drives every design decision below.

APPROVAL HAPPENS AT SCHEDULE TIME, NOT AT FIRE TIME. The confirmation card
shows the recipient, the full body, and the delivery time, and approving it
authorizes the eventual send. Confirming at fire time would defeat the entire
feature — the user schedules something for 6pm precisely because they don't
expect to be sitting there at 6pm — and a card nobody answers would either
block forever or, worse, time out into sending anyway. So the send tools stay
always-confirm (see policy._ALWAYS_CONFIRM_OUTBOUND, which `schedule_send`
joins), and firing is unattended by design.

DURABILITY: SQLite in ~/.moe/assistant.db, alongside commitments. A queue held
in memory would silently drop everything on a backend restart, and the user
would have no way to know the 6pm text was never going to happen.

MISSED WINDOWS ARE NOT SILENTLY SENT LATE. Wisp stops when the lid closes, so a
send scheduled for 18:00 can easily come due while the machine is asleep and
only be noticed at 23:30. Firing it then is usually wrong — "running late, be
there in 10" delivered five hours afterwards is worse than not sending it. Past
_STALE_AFTER_S the row is marked `missed` and surfaced to the user instead, who
can resend if it still makes sense.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

from service.paths import MOE_DIR

DB_PATH = MOE_DIR / "assistant.db"

# How late a due send may fire. Beyond this it's marked `missed` rather than
# delivered — see the module docstring. 30 minutes is deliberately short: the
# common case this protects against is an overnight sleep, and almost anything
# time-sensitive enough to schedule is wrong to deliver hours late.
_STALE_AFTER_S = 30 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_sends (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,      -- 'email' | 'message'
    recipient   TEXT NOT NULL,      -- address / phone / handle, already resolved
    display     TEXT,               -- what to call them in UI ("Mom (+1…)")
    subject     TEXT,               -- email only
    body        TEXT NOT NULL,
    when_ts     REAL NOT NULL,      -- epoch seconds, when to send
    status      TEXT DEFAULT 'pending',  -- pending | sending | sent | missed | cancelled | failed | unknown
    error       TEXT,
    created_at  REAL,
    fired_at    REAL,
    notified_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_sends(status, when_ts);
"""


class OutboundQueue:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS will not add a column to a database made
        # by an earlier build, so bring `notified_at` forward explicitly.
        if "notified_at" not in {
                row["name"] for row in
                self._db.execute("PRAGMA table_info(scheduled_sends)")}:
            self._db.execute(
                "ALTER TABLE scheduled_sends ADD COLUMN notified_at REAL")
        self._db.commit()
        self._lock = threading.Lock()

    def add(self, *, channel: str, recipient: str, body: str, when_ts: float,
            subject: str = "", display: str = "") -> str:
        sid = uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute(
                "INSERT INTO scheduled_sends (id, channel, recipient, display, "
                "subject, body, when_ts, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,'pending',?)",
                (sid, channel, recipient, display or recipient, subject, body,
                 float(when_ts), time.time()))
            self._db.commit()
        return sid

    def due(self, now: float | None = None) -> list[sqlite3.Row]:
        """Pending sends whose time has arrived and that are still fresh enough
        to deliver. Stale ones are NOT returned — sweep_stale() retires them."""
        now = time.time() if now is None else now
        with self._lock:
            return self._db.execute(
                "SELECT * FROM scheduled_sends WHERE status='pending' "
                "AND when_ts <= ? AND when_ts >= ? ORDER BY when_ts",
                (now, now - _STALE_AFTER_S)).fetchall()

    def claim(self, sid: str) -> bool:
        """Take a pending row for delivery. False if someone already has it.

        The claim is committed BEFORE the send leaves, so a crash mid-flight
        leaves the row in `sending` rather than `pending` — an outcome we do
        not know, instead of an invitation to deliver the same message twice.
        """
        with self._lock:
            changed = self._db.execute(
                "UPDATE scheduled_sends SET status='sending', fired_at=? "
                "WHERE id=? AND status='pending'", (time.time(), sid)).rowcount
            self._db.commit()
        return bool(changed)

    def recover_in_flight(self) -> list[sqlite3.Row]:
        """Rows claimed but never resolved — a crash happened mid-send.

        They become `unknown` and are reported, never retried: the message may
        well have gone out, and a silent resend is worse than saying so.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM scheduled_sends WHERE status='sending'").fetchall()
            if rows:
                self._db.execute(
                    "UPDATE scheduled_sends SET status='unknown', "
                    "error='interrupted before the outcome was recorded' "
                    "WHERE status='sending'")
                self._db.commit()
        return rows

    def unannounced_unknown(self) -> list[sqlite3.Row]:
        """Interrupted sends the user has not actually been told about yet.

        recover_in_flight() moves a row out of `sending` exactly once, so if the
        notification is published to an in-memory hub with nobody connected it
        is gone for good. The row stays unannounced until a delivery is
        acknowledged, and is re-offered on every sweep until then.
        """
        with self._lock:
            return self._db.execute(
                "SELECT * FROM scheduled_sends WHERE status='unknown' "
                "AND notified_at IS NULL ORDER BY when_ts").fetchall()

    def mark_announced(self, sid: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE scheduled_sends SET notified_at=? WHERE id=?",
                (time.time(), sid))
            self._db.commit()

    def sweep_stale(self, now: float | None = None) -> list[sqlite3.Row]:
        """Retire pending sends that came due while Wisp wasn't running and are
        now too late to deliver. Returns them so the caller can tell the user."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM scheduled_sends WHERE status='pending' AND when_ts < ?",
                (now - _STALE_AFTER_S,)).fetchall()
            if rows:
                self._db.execute(
                    "UPDATE scheduled_sends SET status='missed' WHERE status='pending' "
                    "AND when_ts < ?", (now - _STALE_AFTER_S,))
                self._db.commit()
        return rows

    def mark(self, sid: str, status: str, error: str = "") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE scheduled_sends SET status=?, error=?, fired_at=? WHERE id=?",
                (status, error, time.time(), sid))
            self._db.commit()

    def pending(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM scheduled_sends WHERE status='pending' "
                "ORDER BY when_ts").fetchall()

    def cancel(self, sid: str) -> sqlite3.Row | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM scheduled_sends WHERE id=? AND status='pending'",
                (sid,)).fetchone()
            if row:
                self._db.execute(
                    "UPDATE scheduled_sends SET status='cancelled' WHERE id=?", (sid,))
                self._db.commit()
        return row


outbound_queue = OutboundQueue()

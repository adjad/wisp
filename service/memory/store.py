"""SQLite-backed session store.

One file at ~/.moe/sessions.db, two tables. Stdlib only, safe for the local
single-user case (one connection, a lock around EVERY access — see below).
Transcripts are bytes on disk — nothing stays resident — so this costs no
model memory.

A session carries a rolling `summary` of everything older than the live window
and `summarized_idx` = how many turns have been folded into it. `pinned_role` /
`pinned_model` make a conversation sticky to the best expert it has reached, so
follow-ups don't get downgraded to a small model.

THE LOCK COVERS READS TOO, NOT JUST WRITES (fixed 2026-08-23). It didn't
always: only add_turn/set_summary/set_pinned/delete_session/create_session
acquired `_lock`; get_session/turns_from/turn_count/last_assistant_turn/
last_assistant_tools/turns_range read `self._db` bare. That looked safe for a
single-user local app, but `check_same_thread=False` on the connection only
disables sqlite3's safety CHECK for cross-thread use — it does not make one
Connection object safe for UNSYNCHRONIZED concurrent access from multiple
threads, and this process genuinely has more than one: `service.tools.
registry.run_tool` dispatches every plain-`def` tool (18 of them) through
`asyncio.to_thread`, a real OS thread from the default pool, running
concurrently with the event-loop thread that's mid-`add_turn` for the SAME
session.

MOTIVATING INCIDENT (2026-08-23, user's debug export): mid-conversation, one
turn's request went out with history trimmed to a single prior message —
just the last assistant turn plus the new user turn — dropping five real,
already-saved turns (the original task and a clarifying follow-up) that the
model then had no way to act on correctly. Direct DB inspection afterward
showed the full, correctly-ordered 8-turn history was there all along
(nothing was lost), and replaying `build_messages` against that same session
returned all 8 turns correctly — ruling out both a storage bug and a token-
budget trim (the whole conversation was under 200 tokens total, and history
has a 1,500-token floor regardless of the model's context window; see
memory/context.py). That combination — correct data, correct trimming logic,
wrong one-off result, unreproducible after the fact — is the signature of a
reader racing a writer on a connection that only half-synchronized itself.
Locking every method closes that gap outright rather than chasing the exact
interleaving that tripped it, since the interleaving depends on the OS
thread scheduler and may never reproduce identically twice.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

from service.paths import MOE_DIR

DB_PATH = MOE_DIR / "sessions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    created_at     REAL,
    last_used      REAL,
    summary        TEXT DEFAULT '',
    summarized_idx INTEGER DEFAULT 0,
    pinned_role    TEXT,
    pinned_model   TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    session_id   TEXT,
    idx          INTEGER,
    role         TEXT,
    content      TEXT,
    tool_digest  TEXT,
    created_at   REAL,
    PRIMARY KEY (session_id, idx)
);
"""


class SessionStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()

    # --- sessions ---------------------------------------------------------
    def create_session(self) -> str:
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (id, created_at, last_used) VALUES (?,?,?)",
                (sid, now, now))
            self._db.commit()
        return sid

    def get_session(self, sid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def set_summary(self, sid: str, summary: str, summarized_idx: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET summary=?, summarized_idx=? WHERE id=?",
                (summary, summarized_idx, sid))
            self._db.commit()

    def set_pinned(self, sid: str, role: str, model: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET pinned_role=?, pinned_model=? WHERE id=?",
                (role, model, sid))
            self._db.commit()

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, created_at, last_used, summary FROM sessions "
                "ORDER BY last_used DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, sid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM turns WHERE session_id=?", (sid,))
            self._db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self._db.commit()

    # --- turns ------------------------------------------------------------
    def turn_count(self, sid: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM turns WHERE session_id=?", (sid,)).fetchone()
        return int(row["n"])

    def add_turn(self, sid: str, role: str, content: str,
                 tool_digest: str | None = None) -> int:
        with self._lock:
            # Compute the next idx INSIDE the lock (via SQL) so two concurrent
            # turns on one session can't read the same count and collide on the
            # (session_id, idx) primary key.
            row = self._db.execute(
                "SELECT COALESCE(MAX(idx) + 1, 0) AS n FROM turns WHERE session_id=?",
                (sid,)).fetchone()
            idx = int(row["n"])
            self._db.execute(
                "INSERT INTO turns (session_id, idx, role, content, tool_digest, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (sid, idx, role, content, tool_digest, time.time()))
            self._db.execute("UPDATE sessions SET last_used=? WHERE id=?",
                             (time.time(), sid))
            self._db.commit()
        return idx

    def turns_from(self, sid: str, start_idx: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM turns WHERE session_id=? AND idx>=? ORDER BY idx",
                (sid, start_idx)).fetchall()
        return [dict(r) for r in rows]

    def last_assistant_turn(self, sid: str) -> str | None:
        """The most recent assistant reply's text, or None. Used to give the
        router a sliver of context — e.g. so "go ahead" can be recognized as
        confirming an action the assistant just offered, instead of classified
        in a vacuum."""
        with self._lock:
            row = self._db.execute(
                "SELECT content FROM turns WHERE session_id=? AND role='assistant' "
                "ORDER BY idx DESC LIMIT 1", (sid,)).fetchone()
        return row["content"] if row else None

    def last_assistant_tools(self, sid: str) -> str | None:
        """The tool_digest of the most recent assistant turn (a comma-joined
        list of the tools it called, e.g. "summarize_emails" or
        "summarize_messages, get_upcoming"), or None. Lets the router continue
        a bare follow-up fragment ("from yesterday") on the SAME domain the
        previous turn used, instead of classifying the fragment in a vacuum
        and defaulting it to the agent model."""
        with self._lock:
            row = self._db.execute(
                "SELECT tool_digest FROM turns WHERE session_id=? AND role='assistant' "
                "ORDER BY idx DESC LIMIT 1", (sid,)).fetchone()
        return row["tool_digest"] if row else None

    def turns_range(self, sid: str, start_idx: int, end_idx: int) -> list[dict]:
        """Turns with start_idx <= idx < end_idx, in order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM turns WHERE session_id=? AND idx>=? AND idx<? ORDER BY idx",
                (sid, start_idx, end_idx)).fetchall()
        return [dict(r) for r in rows]


# module-level singleton
store = SessionStore()

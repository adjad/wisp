"""SQLite-backed session store.

One file at ~/.moe/sessions.db, two tables. Stdlib only, safe for the local
single-user case (one connection, a lock around writes). Transcripts are bytes
on disk — nothing stays resident — so this costs no model memory.

A session carries a rolling `summary` of everything older than the live window
and `summarized_idx` = how many turns have been folded into it. `pinned_role` /
`pinned_model` make a conversation sticky to the best expert it has reached, so
follow-ups don't get downgraded to a small model.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

from service.paths import STATE_DIR

DB_PATH = STATE_DIR / "sessions.db"

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
        rows = self._db.execute(
            "SELECT * FROM turns WHERE session_id=? AND idx>=? ORDER BY idx",
            (sid, start_idx)).fetchall()
        return [dict(r) for r in rows]

    def last_assistant_turn(self, sid: str) -> str | None:
        """The most recent assistant reply's text, or None. Used to give the
        router a sliver of context — e.g. so "go ahead" can be recognized as
        confirming an action the assistant just offered, instead of classified
        in a vacuum."""
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
        and defaulting it to gpt-oss."""
        row = self._db.execute(
            "SELECT tool_digest FROM turns WHERE session_id=? AND role='assistant' "
            "ORDER BY idx DESC LIMIT 1", (sid,)).fetchone()
        return row["tool_digest"] if row else None

    def turns_range(self, sid: str, start_idx: int, end_idx: int) -> list[dict]:
        """Turns with start_idx <= idx < end_idx, in order."""
        rows = self._db.execute(
            "SELECT * FROM turns WHERE session_id=? AND idx>=? AND idx<? ORDER BY idx",
            (sid, start_idx, end_idx)).fetchall()
        return [dict(r) for r in rows]


# module-level singleton
store = SessionStore()

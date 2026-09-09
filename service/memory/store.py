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
import json
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
    pinned_model   TEXT,
    active_skill   TEXT DEFAULT ''
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
CREATE TABLE IF NOT EXISTS workflows (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workflows_session_status
    ON workflows(session_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS task_effect_claims (
    call_id     TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_events (
    workflow_id TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event       TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    PRIMARY KEY (workflow_id, seq)
);
"""


class SessionStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        from service.memory.queue import install as install_memory_queue
        install_memory_queue(self._db)
        # CREATE TABLE IF NOT EXISTS does not add columns to a database made by
        # an older Wisp. Keep the migration inline and idempotent so existing
        # conversations gain workflow state without a separate upgrade step.
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(sessions)")}
        if "active_skill" not in columns:
            try:
                self._db.execute(
                    "ALTER TABLE sessions ADD COLUMN active_skill TEXT DEFAULT ''")
            except sqlite3.OperationalError as e:
                # Read-only inspection processes (including path diagnostics)
                # may open an existing database without permission to migrate
                # it. Preserve the old read-only behavior; the real Wisp
                # process, which owns this file, applies the migration.
                if "readonly" not in str(e).lower():
                    raise
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

    def set_active_skill(self, sid: str, name: str | None) -> None:
        """Persist the conversational workflow currently guiding a session."""
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET active_skill=? WHERE id=?", (name or "", sid))
            self._db.commit()

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, created_at, last_used, summary FROM sessions "
                "ORDER BY last_used DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, sid: str) -> None:
        with self._lock:
            workflow_ids = [row["id"] for row in self._db.execute(
                "SELECT id FROM workflows WHERE session_id=?", (sid,)).fetchall()]
            for workflow_id in workflow_ids:
                self._db.execute(
                    "DELETE FROM workflow_events WHERE workflow_id=?", (workflow_id,))
            self._db.execute("DELETE FROM workflows WHERE session_id=?", (sid,))
            self._db.execute("DELETE FROM turns WHERE session_id=?", (sid,))
            self._db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self._db.commit()

    # --- persistent task workflows --------------------------------------
    def save_workflow(self, sid: str, workflow: dict) -> None:
        """Insert or update one typed workflow and its current status."""
        workflow_id = str(workflow["id"])
        status = str(workflow["status"])
        now = time.time()
        payload = json.dumps(workflow, sort_keys=True, separators=(",", ":"))
        with self._lock:
            existing = self._db.execute(
                "SELECT created_at FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            created = float(existing["created_at"]) if existing else now
            self._db.execute(
                "INSERT OR REPLACE INTO workflows "
                "(id, session_id, kind, status, state_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (workflow_id, sid, str(workflow.get("kind") or ""), status,
                 payload, created, now))
            self._db.commit()

    def active_workflow(self, sid: str, max_age_seconds: float = 21600) -> dict | None:
        """Newest unfinished workflow for the session, bounded to six hours."""
        active = ("waiting_for_channel", "waiting_for_recipient", "waiting_for_time",
                  "waiting_for_location", "waiting_for_symbols",
                  "ready", "running", "failed")
        placeholders = ",".join("?" for _ in active)
        with self._lock:
            row = self._db.execute(
                f"SELECT state_json, updated_at FROM workflows WHERE session_id=? "
                f"AND kind='deliver_summary' "
                f"AND status IN ({placeholders}) ORDER BY updated_at DESC LIMIT 1",
                (sid, *active)).fetchone()
        if not row or time.time() - float(row["updated_at"]) > max_age_seconds:
            return None
        try:
            return json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def transition_task(self, sid: str, task: dict, *, from_status: str) -> bool:
        """Do not let a late async result overwrite a correction or winner.

        The revision and effect-claim list identify the state this caller owns.
        A losing execution has no matching claim list; a corrected task has a
        newer revision. Neither can be overwritten by the old completion.
        """
        with self._lock:
            changed = self._db.execute(
                "UPDATE workflows SET status=?, state_json=?, updated_at=? "
                "WHERE id=? AND session_id=? AND status=? "
                "AND json_extract(state_json, '$.revision')=? "
                "AND coalesce(json_extract(state_json, '$.claimed_calls'), '[]')=json(?)",
                (task["status"], json.dumps(task), time.time(), task["id"], sid,
                 from_status, task["revision"], json.dumps(task.get("claimed_calls", [])))).rowcount
            self._db.commit()
        return bool(changed)

    def latest_workflow(self, sid: str, max_age_seconds: float = 1800) -> dict | None:
        """Recent source plan, including denied deliveries, for explicit retargeting only."""
        with self._lock:
            row = self._db.execute(
                "SELECT state_json, updated_at FROM workflows WHERE session_id=? "
                "AND kind='deliver_summary' ORDER BY updated_at DESC LIMIT 1", (sid,)
            ).fetchone()
        if not row or time.time() - float(row["updated_at"]) > max_age_seconds:
            return None
        try:
            return json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def active_task(self, sid: str, max_age_seconds: float = 21600) -> dict | None:
        """Newest unfinished versioned typed task, separate from old workflows."""
        active = ("waiting_for_input", "ready", "running", "failed")
        placeholders = ",".join("?" for _ in active)
        with self._lock:
            row = self._db.execute(
                f"SELECT state_json, updated_at FROM workflows WHERE session_id=? "
                f"AND kind LIKE 'task.%' AND status IN ({placeholders}) "
                f"ORDER BY updated_at DESC LIMIT 1",
                (sid, *active)).fetchone()
        if not row or time.time() - float(row["updated_at"]) > max_age_seconds:
            return None
        try:
            return json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def latest_task(self, sid: str, max_age_seconds: float = 1800) -> dict | None:
        """Newest typed task, including terminal states, for local corrections.

        A phrase such as ``I mean today`` only has a target when it immediately
        follows a completed typed reminder operation. Keeping this lookup
        separate from ``active_task`` prevents old completed work from becoming
        general conversation state.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT state_json, updated_at FROM workflows WHERE session_id=? "
                "AND kind LIKE 'task.%' ORDER BY updated_at DESC LIMIT 1", (sid,)
            ).fetchone()
        if not row or time.time() - float(row["updated_at"]) > max_age_seconds:
            return None
        try:
            return json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def claim_effect_call(self, plan_id: str, call_id: str, *, revision: int | None = None) -> bool:
        """Win the right to run one effect exactly once. True = you won it.

        The decision has to be a single atomic write, not a read followed by a
        write: two executions of the same plan revision can both pass an
        in-memory check and both send. INSERT OR IGNORE against a primary key
        makes the database the arbiter, and it survives a restart, so a second
        process loading the same persisted plan loses too.
        """
        with self._lock:
            if revision is None:
                changed = self._db.execute(
                    "INSERT OR IGNORE INTO task_effect_claims "
                    "(call_id, plan_id, created_at) VALUES (?,?,?)",
                    (call_id, plan_id, time.time())).rowcount
            else:
                # A cancellation/supersession while approval is open invalidates
                # the effect. Check state and take the claim in ONE SQL write.
                changed = self._db.execute(
                    "INSERT OR IGNORE INTO task_effect_claims (call_id, plan_id, created_at) "
                    "SELECT ?,?,? WHERE EXISTS (SELECT 1 FROM workflows WHERE id=? "
                    "AND status='running' AND json_extract(state_json, '$.revision')=?)",
                    (call_id, plan_id, time.time(), plan_id, revision)).rowcount
            self._db.commit()
        return bool(changed)

    def add_workflow_event(self, workflow_id: str, event: str,
                           payload: dict | None = None) -> int:
        """Append an auditable state transition or execution observation."""
        encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS n FROM workflow_events "
                "WHERE workflow_id=?", (workflow_id,)).fetchone()
            seq = int(row["n"])
            self._db.execute(
                "INSERT INTO workflow_events "
                "(workflow_id, seq, event, payload_json, created_at) VALUES (?,?,?,?,?)",
                (workflow_id, seq, event, encoded, time.time()))
            self._db.commit()
        return seq

    def workflow_events(self, workflow_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, event, payload_json, created_at FROM workflow_events "
                "WHERE workflow_id=? ORDER BY seq", (workflow_id,)).fetchall()
        events: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except (TypeError, json.JSONDecodeError):
                item["payload"] = {}
            events.append(item)
        return events

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

    def last_user_turn(self, sid: str) -> str | None:
        """The user request immediately preceding the current turn.

        Routing a follow-up from only the assistant's prose and a list of tool
        names loses the actual task. In particular, ``yes`` after "send Mom
        my calendar via Messages" used to inherit the calendar domain and
        expose calendar mutations, while forgetting both recipient and channel.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT content FROM turns WHERE session_id=? AND role='user' "
                "ORDER BY idx DESC LIMIT 1", (sid,)).fetchone()
        return row["content"] if row else None

    def recent_user_turns(self, sid: str, limit: int = 4) -> list[str]:
        """Recent user requests in chronological order for workflow routing.

        A clarification can take more than one exchange ("send it" -> "which
        address?" -> an address), so the immediately previous user turn alone
        is insufficient to recover the payload. The window is deliberately
        small to avoid reviving an unrelated old task.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT content FROM turns WHERE session_id=? AND role='user' "
                "ORDER BY idx DESC LIMIT ?", (sid, max(1, int(limit)))).fetchall()
        return [row["content"] for row in reversed(rows)]

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

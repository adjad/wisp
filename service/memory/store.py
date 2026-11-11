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

import logging
import sqlite3
import threading
import time
import uuid
import json
import hashlib
from pathlib import Path

from service.paths import MOE_DIR, private_sqlite_file

DB_PATH = MOE_DIR / "sessions.db"
_log = logging.getLogger(__name__)

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
    display_content TEXT,
    display_kind TEXT,
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


# The corpus runner's chats are titled "Corpus · <case> · <request>".
CORPUS_TITLE = "Corpus · "
_CORPUS_TITLE_SQL = f"title LIKE '{CORPUS_TITLE}%'"


# session_store_meta row: this file has been rebuilt once with secure_delete on,
# so chats deleted by earlier builds are gone from its free pages and its index.
OLD_TEXT_CLEARED = "old_text_cleared"
_FTS = "memory_turn_fts"


class SessionStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        private_sqlite_file(path)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # A deleted chat's text is overwritten in the file, not only unlinked
        # from its rows. Per connection; this store does the deletes.
        self._db.execute("PRAGMA secure_delete=ON")
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
        if "title" not in columns:
            # Additive and nullable, no backfill: NULL keeps the old title (the
            # first user turn), and an older Wisp reading this file ignores it.
            try:
                self._db.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
            except sqlite3.OperationalError as e:
                # Read-only, or another opener added it first.
                if not any(k in str(e).lower() for k in ("readonly", "duplicate column")):
                    raise
        if "subject" not in columns:
            # The conversation's current subject (service/memory/subject.py):
            # additive and nullable, an older Wisp ignores it.
            try:
                self._db.execute("ALTER TABLE sessions ADD COLUMN subject TEXT")
            except sqlite3.OperationalError as e:
                if not any(k in str(e).lower() for k in ("readonly", "duplicate column")):
                    raise
        # A read-only opener of an older file may not have the column.
        session_columns = {row[1] for row in self._db.execute("PRAGMA table_info(sessions)")}
        self._has_title = "title" in session_columns
        self._has_subject = "subject" in session_columns
        turn_columns = {row[1] for row in self._db.execute("PRAGMA table_info(turns)")}
        if "display_content" not in turn_columns:
            # Nullable/additive: old turns and their indices remain untouched.
            # Fail closed if a writable service cannot establish the boundary.
            self._db.execute("ALTER TABLE turns ADD COLUMN display_content TEXT")
        if "display_kind" not in turn_columns:
            self._db.execute("ALTER TABLE turns ADD COLUMN display_kind TEXT")
        try:
            self._db.execute("CREATE TABLE IF NOT EXISTS session_store_meta "
                             "(name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            if new_file:
                # Made by this build: nothing an earlier build deleted is in it.
                self._db.execute("INSERT OR IGNORE INTO session_store_meta VALUES (?, '1')",
                                 (OLD_TEXT_CLEARED,))
        except sqlite3.OperationalError as e:
            if "readonly" not in str(e).lower():
                raise
        self._db.commit()
        self._lock = threading.Lock()
        self._clear_guard = threading.Lock()   # one clear_old_text at a time

    # --- one-time rebuild ---------------------------------------------------
    def old_text_cleared(self) -> bool:
        with self._lock:
            try:
                return self._db.execute("SELECT 1 FROM session_store_meta WHERE name=?",
                                        (OLD_TEXT_CLEARED,)).fetchone() is not None
            except sqlite3.Error:
                return False

    def clear_old_text(self) -> bool:
        """Once per database, rebuild the chat search index and the file.

        secure_delete only covers what this store removes from now on. Chats an
        earlier build deleted still sit in free pages, in the unused space of
        live pages and in old search-index segments (FTS5 without its
        secure-delete option only records that a row is gone). This drops the
        index, removes turns whose chat is gone, rewrites the index from the
        live turns, then VACUUMs. Mirrors the assistant.db and facts.db
        rebuilds (service/assistant/store.py, service/memory/facts.py).

        Blocking: startup runs it in a worker thread, never awaiting it inline.
        The store lock is held one step at a time (check, orphans + index,
        VACUUM, index check, checkpoint + marker), so a chat saved between
        steps goes through instead of waiting for the whole rebuild; the
        triggers are back in place whenever the lock is released, so that
        turn is indexed normally. Each step is one SQLite transaction: an
        interruption rolls back to the intact file and the next start tries
        again. A file that is locked by another process, read-only, damaged,
        short of disk space or whose WAL could not be fully checkpointed is
        left alone, unmarked, and the reason logged. Never raises. True once
        the file has been cleared.
        """
        if not self._clear_guard.acquire(blocking=False):
            return False          # another call is already rebuilding it
        try:
            if self.old_text_cleared():
                return True
            return self._clear_old_text_steps()
        except Exception as exc:  # noqa: BLE001 — never blocks or breaks startup
            _log.warning("session store: deleted chats not cleared yet: %s", type(exc).__name__)
            return False
        finally:
            self._clear_guard.release()

    def _clear_old_text_steps(self) -> bool:
        db = self._db
        try:
            with self._lock:
                db.execute("PRAGMA busy_timeout=250")
                if not self._enough_disk_space():
                    return False
                check = db.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    _log.warning("session store: deleted chats not cleared: the file failed its check")
                    return False
            with self._lock:
                db.execute("PRAGMA busy_timeout=250")
                self._rebuild_index_dropping_orphans()
            with self._lock:
                db.execute("PRAGMA busy_timeout=250")
                db.execute("VACUUM")
                # Same hold as the VACUUM: a turn saved in between could meet an index row
                # whose rowid the VACUUM had just renumbered.
                if self._has_index() and not self._index_matches_turns():
                    # VACUUM may renumber the implicit rowids the index is keyed by.
                    self._rebuild_index()
                if not self._checkpointed():
                    return False
                with db:
                    db.execute("INSERT OR IGNORE INTO session_store_meta VALUES (?, '1')",
                               (OLD_TEXT_CLEARED,))
            return True
        except sqlite3.Error as exc:
            # No rollback here. Every step above ends its own transaction, and the lock is
            # already released: a transaction open on this shared connection now belongs to
            # another thread's save, and rolling it back would drop that turn without an error.
            _log.warning("session store: deleted chats not cleared yet: %s", exc)
            return False
        finally:
            with self._lock:
                db.execute("PRAGMA busy_timeout=5000")   # sqlite3.connect's own default

    def _enough_disk_space(self) -> bool:
        """VACUUM writes a full copy, plus journal or log: ask for twice the
        file and a margin before doing any of the costly steps, so a full disk
        is refused up front instead of after every start's index rebuild."""
        import os
        import shutil
        path = next((row[2] for row in self._db.execute("PRAGMA database_list")
                     if row[1] == "main"), "")
        if not path:
            return True               # in-memory: nothing to copy
        need = 2 * os.path.getsize(path) + (32 << 20)
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free
        if free < need:
            _log.warning("session store: deleted chats not cleared yet: %d MB free, "
                         "%d MB needed to rebuild the file", free >> 20, need >> 20)
            return False
        return True

    def _checkpointed(self) -> bool:
        """VACUUM's copy of the old pages sits in the WAL until checkpointed;
        the marker is only safe once the log is empty and in the main file."""
        db = self._db
        if str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
            return True
        row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy, log, done = (row[0], row[1], row[2]) if row else (1, 0, -1)
        if busy or log != done:
            _log.warning("session store: deleted chats not cleared yet: the log could not "
                         "be fully checkpointed (busy=%s, %s of %s frames)", busy, done, log)
            return False
        return True

    def _has_index(self) -> bool:
        return self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (_FTS,)).fetchone() is not None

    def _rebuild_index_dropping_orphans(self) -> None:
        """One transaction: drop the index, remove what is left of chats whose
        session row is gone, rebuild the index from the live turns.

        Every chat starts with create_session (an unknown id gets a new
        session), and delete_session removes its rows with it, so a turn,
        workflow or memory job without a session is a deleted chat's remnant
        (a turn saved while its chat was being deleted, or an older build's
        partial delete). It is not listed or openable, so no live chat is lost.

        The turns delete trigger would remove each orphan's index row (and
        secure-delete its words) one by one just before the index is dropped
        anyway, so the index and the triggers that name it are dropped first.
        All of it commits or none of it does (DDL is transactional): a crash
        leaves the old index and triggers, and the lock is not released
        until they are back, so no write can reach the turns without them.
        """
        from service.memory.facts import secure_delete_fts
        db = self._db
        create = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                            (_FTS,)).fetchone()
        triggers = db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                              "AND lower(tbl_name)='turns' AND sql LIKE ?", (f"%{_FTS}%",)).fetchall()
        with db:
            # Explicit: sqlite3 opens no transaction before DDL on its own.
            db.execute("BEGIN IMMEDIATE")
            if create is not None:
                for name, _sql in triggers:
                    db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
                db.execute(f"DROP TABLE {_FTS}")
            db.execute("DELETE FROM workflow_events WHERE workflow_id IN (SELECT id FROM workflows "
                       "WHERE session_id NOT IN (SELECT id FROM sessions))")
            db.execute("DELETE FROM workflows WHERE session_id NOT IN (SELECT id FROM sessions)")
            db.execute("DELETE FROM turns WHERE session_id NOT IN (SELECT id FROM sessions)")
            db.execute("DELETE FROM memory_jobs WHERE session_id NOT IN (SELECT id FROM sessions)")
            if create is not None:
                db.execute(create[0])
                db.execute(f"INSERT INTO {_FTS}(rowid,text,session_id,turn_idx,created_at,role) "
                           "SELECT rowid,content,session_id,idx,created_at,role FROM turns")
                secure_delete_fts(db, _FTS)
                for _name, sql in triggers:
                    db.execute(sql)

    def _rebuild_index(self) -> None:
        """Replace the chat search index with one built from the live turns.

        Dropping it frees every old segment, including the words of chats
        deleted before this build (zeroed: secure_delete is on). The
        memory_turn_* triggers refer to the index by name and keep working.
        """
        from service.memory.facts import secure_delete_fts
        db = self._db
        create_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                                (_FTS,)).fetchone()[0]
        with db:
            # Explicit: the drop and the refill must commit, or roll back, together.
            db.execute("BEGIN IMMEDIATE")
            db.execute(f"DROP TABLE {_FTS}")
            db.execute(create_sql)
            db.execute(f"INSERT INTO {_FTS}(rowid,text,session_id,turn_idx,created_at,role) "
                       "SELECT rowid,content,session_id,idx,created_at,role FROM turns")
            secure_delete_fts(db, _FTS)

    def _index_matches_turns(self) -> bool:
        row = self._db.execute(
            f"SELECT (SELECT COUNT(*) FROM {_FTS} f LEFT JOIN turns t ON t.rowid=f.rowid "
            " WHERE t.rowid IS NULL OR t.session_id IS NOT f.session_id OR t.idx IS NOT f.turn_idx), "
            f"(SELECT COUNT(*) FROM turns) - (SELECT COUNT(*) FROM {_FTS})").fetchone()
        return row[0] == 0 and row[1] == 0

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

    def session_subject(self, sid: str) -> dict | None:
        """The session's current-subject record (service/memory/subject.py)."""
        if not self._has_subject:
            return None
        with self._lock:
            row = self._db.execute("SELECT subject FROM sessions WHERE id=?", (sid,)).fetchone()
        try:
            value = json.loads(row["subject"]) if row and row["subject"] else None
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def set_session_subject(self, sid: str, record: dict | None) -> None:
        if not self._has_subject:
            return
        with self._lock:
            self._db.execute("UPDATE sessions SET subject=? WHERE id=?",
                             (json.dumps(record, ensure_ascii=False) if record else None, sid))
            self._db.commit()

    def set_active_skill(self, sid: str, name: str | None) -> None:
        """Persist the conversational workflow currently guiding a session."""
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET active_skill=? WHERE id=?", (name or "", sid))
            self._db.commit()

    def set_title(self, sid: str, title: str) -> None:
        """Name a conversation in the chat list (the corpus runner's cases).
        An empty title clears it back to the first user turn."""
        if not self._has_title:
            return
        with self._lock:
            self._db.execute("UPDATE sessions SET title=? WHERE id=?",
                             (" ".join(str(title).split()) or None, sid))
            self._db.commit()

    def is_corpus_session(self, sid: str) -> bool:
        """A /corpus test case saved as a chat (set_title with CORPUS_TITLE).
        Its prompts are test input, not something the user said, so memory
        extraction, behaviour observations and conversation search skip it."""
        if not self._has_title:
            return False
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM sessions WHERE id=? AND " + _CORPUS_TITLE_SQL,
                (sid,)).fetchone() is not None

    def not_corpus_sql(self, column: str = "session_id") -> str:
        """A WHERE condition on `column` that leaves corpus chats out."""
        if not self._has_title:
            return "1"
        return f"{column} NOT IN (SELECT id FROM sessions WHERE {_CORPUS_TITLE_SQL})"

    def drop_memory_jobs(self, sid: str) -> None:
        """Remove a corpus chat's queued memory extraction jobs."""
        with self._lock:
            self._db.execute("DELETE FROM memory_jobs WHERE session_id=?", (sid,))
            self._db.commit()

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, created_at, last_used, summary FROM sessions "
                "ORDER BY last_used DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def list_chats(self, limit: int = 100) -> list[dict]:
        """Sessions with at least one user turn, newest first, for a chat sidebar.

        title is the session's own title when one was set (set_title), else
        the first user turn (whitespace-collapsed, at most 60 chars
        including a trailing ellipsis when cut); preview is the latest
        assistant turn (collapsed, at most 120 chars, empty when none). Turn
        content is read from `content`, which add_turn has already credential-
        scrubbed, never from display_content.
        """
        def tidy(text, cap: int, ellipsis: bool) -> str:
            text = " ".join(str(text or "").split())
            if len(text) <= cap:
                return text
            return text[:cap - 1].rstrip() + "\u2026" if ellipsis else text[:cap]

        first_user = ("(SELECT t.content FROM turns t WHERE t.session_id=s.id "
                      "AND t.role='user' ORDER BY t.idx ASC LIMIT 1)")
        title = f"COALESCE(s.title, {first_user})" if self._has_title else first_user
        with self._lock:
            rows = self._db.execute(
                "SELECT s.id AS id, s.created_at AS created_at, s.last_used AS last_used, "
                f"{title} AS title, "
                "(SELECT t.content FROM turns t WHERE t.session_id=s.id AND t.role='assistant' "
                " ORDER BY t.idx DESC LIMIT 1) AS preview, "
                "(SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id) AS turn_count "
                "FROM sessions s WHERE EXISTS "
                "(SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.role='user') "
                "ORDER BY s.last_used DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"id": r["id"], "title": tidy(r["title"], 60, True),
                 "preview": tidy(r["preview"], 120, False),
                 "created_at": r["created_at"], "last_used": r["last_used"],
                 "turn_count": int(r["turn_count"])} for r in rows]

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

    def save_workflow_revision(self, sid: str, workflow: dict,
                               *, expected_revision: int) -> bool:
        """Advance one delivery workflow only from its persisted revision."""
        workflow_id = str(workflow["id"])
        incoming_revision = int(workflow.get("revision", 0))
        if incoming_revision != expected_revision + 1:
            return False
        now = time.time()
        payload = json.dumps(workflow, sort_keys=True, separators=(",", ":"))
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT session_id, state_json FROM workflows WHERE id=?",
                    (workflow_id,)).fetchone()
                if row is None:
                    if expected_revision != 0:
                        changed = 0
                    else:
                        changed = self._db.execute(
                            "INSERT INTO workflows "
                            "(id, session_id, kind, status, state_json, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (workflow_id, sid, str(workflow.get("kind") or ""),
                             str(workflow["status"]), payload, now, now)).rowcount
                else:
                    try:
                        current = json.loads(row["state_json"])
                    except (TypeError, json.JSONDecodeError):
                        current = {}
                    if (row["session_id"] != sid
                            or int(current.get("revision", 0)) != expected_revision):
                        changed = 0
                    else:
                        changed = self._db.execute(
                            "UPDATE workflows SET kind=?, status=?, state_json=?, updated_at=? "
                            "WHERE id=? AND session_id=?",
                            (str(workflow.get("kind") or ""), str(workflow["status"]),
                             payload, now, workflow_id, sid)).rowcount
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return bool(changed)

    def workflow_state(self, sid: str, plan_id: str) -> dict | None:
        """Return one exact workflow row, including terminal states."""
        with self._lock:
            row = self._db.execute(
                "SELECT state_json FROM workflows WHERE id=? AND session_id=?",
                (plan_id, sid)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def workflow_is_current(self, sid: str, plan_id: str, revision: int) -> bool:
        """True only while this exact workflow revision still owns execution."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM workflows WHERE id=? AND session_id=? AND status='running' "
                "AND coalesce(json_extract(state_json, '$.revision'), 0)=?",
                (plan_id, sid, revision)).fetchone()
        return row is not None

    def transition_workflow(self, sid: str, workflow: dict, *, from_status: str,
                            expected_revision: int) -> bool:
        """Conditionally persist a terminal state for the owned revision."""
        if int(workflow.get("revision", 0)) != expected_revision + 1:
            return False
        now = time.time()
        payload = json.dumps(workflow, sort_keys=True, separators=(",", ":"))
        with self._lock:
            changed = self._db.execute(
                "UPDATE workflows SET status=?, state_json=?, updated_at=? "
                "WHERE id=? AND session_id=? AND status=? "
                "AND coalesce(json_extract(state_json, '$.revision'), 0)=?",
                (str(workflow["status"]), payload, now, str(workflow["id"]), sid,
                 from_status, expected_revision)).rowcount
            self._db.commit()
        return bool(changed)

    def active_workflow(self, sid: str, max_age_seconds: float = 21600) -> dict | None:
        """Newest unfinished workflow for the session, bounded to six hours."""
        active = ("waiting_for_channel", "waiting_for_recipient", "waiting_for_time",
                  "waiting_for_location", "waiting_for_symbols", "waiting_for_content",
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

    def transition_task(self, sid: str, task: dict, *, from_status: str,
                        expected_revision: int | None = None,
                        expected_claimed_calls: list[str] | None = None) -> bool:
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
                 from_status, task["revision"] if expected_revision is None else expected_revision,
                 json.dumps(task.get("claimed_calls", []) if expected_claimed_calls is None
                            else expected_claimed_calls))).rowcount
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

    def workflow_effect_claimed(self, plan_id: str) -> bool:
        """A delivery attempt remains consumed across restarts and timeouts."""
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM task_effect_claims WHERE plan_id=? AND call_id=?",
                (plan_id, f"workflow_effect:{plan_id}")).fetchone() is not None

    def claim_effect_call(self, plan_id: str, call_id: str, *, revision: int | None = None) -> bool:
        """Win the right to run one effect exactly once. True = you won it.

        The decision has to be a single atomic write, not a read followed by a
        write: two executions of the same plan revision can both pass an
        in-memory check and both send. INSERT OR IGNORE against a primary key
        makes the database the arbiter, and it survives a restart, so a second
        process loading the same persisted plan loses too.
        """
        with self._lock:
            try:
                # Claim and record the attempt in one transaction. A separate
                # save of the caller's snapshot can resurrect a cancelled task
                # or overwrite a newer revision in another process.
                self._db.execute("BEGIN IMMEDIATE")
                owner = self._db.execute("SELECT session_id FROM workflows WHERE id=?", (plan_id,)).fetchone()
                if owner and self._unresolved_action_locked(
                        owner["session_id"], exclude=plan_id, model_effects_only=True):
                    self._db.commit()
                    return False
                if revision is None:
                    changed = self._db.execute(
                        "INSERT OR IGNORE INTO task_effect_claims "
                        "(call_id, plan_id, created_at) VALUES (?,?,?)",
                        (call_id, plan_id, time.time())).rowcount
                else:
                    changed = self._db.execute(
                        "INSERT OR IGNORE INTO task_effect_claims (call_id, plan_id, created_at) "
                        "SELECT ?,?,? WHERE EXISTS (SELECT 1 FROM workflows WHERE id=? "
                        "AND status='running' AND json_extract(state_json, '$.revision')=?)",
                        (call_id, plan_id, time.time(), plan_id, revision)).rowcount
                    if changed:
                        self._db.execute(
                            "UPDATE workflows SET state_json=json_set(state_json, '$.claimed_calls', "
                            "json_insert(coalesce(json_extract(state_json, '$.claimed_calls'), '[]'), "
                            "'$[#]', ?)), updated_at=? WHERE id=?",
                            (call_id, time.time(), plan_id))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return bool(changed)

    def claim_workflow_effect(self, sid: str, plan_id: str, call_id: str,
                              *, revision: int) -> bool:
        """Atomically claim an effect for the exact running delivery revision."""
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if self._unresolved_action_locked(sid, exclude=plan_id, model_effects_only=True):
                    self._db.commit()
                    return False
                changed = self._db.execute(
                    "INSERT OR IGNORE INTO task_effect_claims (call_id, plan_id, created_at) "
                    "SELECT ?,?,? WHERE EXISTS (SELECT 1 FROM workflows WHERE id=? "
                    "AND session_id=? AND status='running' "
                    "AND coalesce(json_extract(state_json, '$.revision'), 0)=?)",
                    (call_id, plan_id, time.time(), plan_id, sid, revision)).rowcount
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return bool(changed)

    def claim_model_effect(self, sid: str, request_id: str, tool: str,
                           args: dict, *, outbound: bool = False) -> dict:
        """Claim an approved model-proposed effect before calling its function.

        Uses the existing workflow/claim tables. The exact argument fingerprint
        survives restarts without storing another copy of private arguments.
        A concurrent or uncertain outbound attempt blocks retargeting too.
        Successful actions may be explicitly requested again in a later turn;
        a repeated call inside the same request never gets a second claim.
        """
        encoded = json.dumps({"tool": tool, "args": args}, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        effect_id = "model_effect:" + hashlib.sha256(
            (sid + ":" + fingerprint).encode("utf-8")).hexdigest()
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if not self._db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                    raise ValueError("Effect owner requires an existing session")
                pending = self._unresolved_action_locked(sid, tool=tool, outbound=outbound)
                if pending:
                    self._db.commit()
                    return {"admitted": False, "effect_id": pending["id"],
                            "status": pending["status"]}
                row = self._db.execute(
                    "SELECT state_json, status, created_at FROM workflows WHERE id=?",
                    (effect_id,)).fetchone()
                prior = json.loads(row["state_json"]) if row else {}
                if row and (row["status"] in {"running", "uncertain"}
                            or prior.get("request_id") == request_id):
                    self._db.commit()
                    return {"admitted": False, "effect_id": effect_id, "status": row["status"]}
                revision = int(prior.get("revision", 0)) + 1
                now = time.time()
                state = {"id": effect_id, "kind": "model_led_effect", "status": "running",
                         "request_id": request_id, "revision": revision, "tool": tool,
                         "fingerprint": fingerprint, "outbound": bool(outbound)}
                call_id = f"{effect_id}:{revision}"
                self._db.execute(
                    "INSERT INTO task_effect_claims (call_id,plan_id,created_at) VALUES (?,?,?)",
                    (call_id, effect_id, now))
                self._db.execute(
                    "INSERT INTO workflows (id,session_id,kind,status,state_json,created_at,updated_at) "
                    "VALUES (?,?,'model_led_effect','running',?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                    "state_json=excluded.state_json, updated_at=excluded.updated_at",
                    (effect_id, sid, json.dumps(state), row["created_at"] if row else now, now))
                self._db.commit()
                return {"admitted": True, "effect_id": effect_id, "revision": revision,
                        "request_id": request_id, "status": "running"}
            except BaseException:
                self._db.rollback()
                raise

    def finish_model_effect(self, sid: str, claim: dict, *, verified: bool,
                            not_sent: bool = False, not_done: bool = False) -> bool:
        """Settle only the exact owner/revision; a crash leaves its claim consumed.

        not_sent is host proof that the attempt stopped before any dispatch
        (registry._NotSentResult), and not_done that another effect refused
        before any write (registry._NotDoneResult). Both are terminal, so they
        block nothing, and recorded apart from a completed action. Anything
        unproven stays "uncertain".
        """
        status = "completed" if verified or not_sent or not_done else "uncertain"
        state_status = ("not_sent" if not_sent else "not_done" if not_done else status
                        ) if not verified else status
        with self._lock:
            try:
                changed = self._db.execute(
                    "UPDATE workflows SET status=?, state_json=json_set(state_json,'$.status',?), "
                    "updated_at=? WHERE id=? AND session_id=? AND kind='model_led_effect' "
                    "AND status='running' AND json_extract(state_json,'$.request_id')=? "
                    "AND json_extract(state_json,'$.revision')=?",
                    (status, state_status, time.time(), claim["effect_id"], sid,
                     claim["request_id"], claim["revision"])).rowcount
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
        return bool(changed)

    def resolve_model_effects(self, sid: str, effect_id: str | None = None) -> int:
        """User attests they checked the destination of unverified model effects.

        Moves running/uncertain model effects to a terminal state so they stop
        blocking the session. The consumed claim stays in place: it is never
        a retry token, only the barrier is lifted. Returns the number resolved.
        """
        sql = ("UPDATE workflows SET status='completed', "
               "state_json=json_set(state_json,'$.status','resolved','$.resolved_by_user',json('true')), "
               "updated_at=? WHERE session_id=? AND kind='model_led_effect' "
               "AND status IN ('running','uncertain')")
        params: list = [time.time(), sid]
        # A legacy typed task (rc2 and earlier) that consumed its effect claim
        # and never reached a verified result blocks every model-proposed action
        # in the session (_unresolved_action_locked), with no age limit. The
        # user's attestation lifts it the same way. Only its status column
        # changes, so a legacy state_json, however old, is never rewritten,
        # and the consumed claim stays (0.5.0-rc3 audit M2).
        legacy_sql = ("UPDATE workflows SET status='completed', updated_at=? "
                      "WHERE session_id=? AND kind != 'model_led_effect' AND status != 'completed' "
                      "AND EXISTS (SELECT 1 FROM task_effect_claims c WHERE c.plan_id=workflows.id)")
        legacy_params: list = [time.time(), sid]
        if effect_id is not None:
            sql += " AND id=?"
            params.append(effect_id)
            legacy_sql += " AND id=?"
            legacy_params.append(effect_id)
        with self._lock:
            try:
                changed = self._db.execute(sql, params).rowcount
                changed += self._db.execute(legacy_sql, legacy_params).rowcount
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
        return int(changed)

    def pending_model_effects(self, sid: str) -> list[dict]:
        """Bounded factual recovery context, never replay instructions/arguments."""
        with self._lock:
            rows = self._db.execute(
                "SELECT state_json FROM workflows WHERE session_id=? AND kind='model_led_effect' "
                "AND status IN ('running','uncertain') ORDER BY updated_at DESC LIMIT 16",
                (sid,)).fetchall()
        return [{"tool": (state := json.loads(row["state_json"]))["tool"],
                 "status": state["status"], "outbound": state["outbound"]} for row in rows]

    def _unresolved_action_locked(self, sid: str, *, tool: str | None = None,
                                  outbound: bool = True, exclude: str | None = None,
                                  model_effects_only: bool = False) -> dict | None:
        """Unresolved-action barrier for model-proposed effects.

        Caller holds the connection lock/transaction. Unknown external effects
        block retargeting; uncertain same-tool mutations block argument changes.
        Legacy owners with consumed claims block a NEW model-proposed effect
        until a verified terminal result, independent of age. Legacy claim
        paths pass model_effects_only=True: they are blocked only by an
        unresolved model effect, never by an unrelated earlier legacy plan in
        the same session (that restores the 1.2.5 per-plan claim semantics).
        """
        rows = self._db.execute(
            "SELECT id,kind,status,state_json FROM workflows WHERE session_id=? "
            "AND status != 'completed'", (sid,)).fetchall()
        for row in rows:
            if row["id"] == exclude:
                continue
            if row["kind"] != "model_led_effect" and model_effects_only:
                # Legacy rows are skipped unparsed on the legacy claim paths.
                continue
            if row["kind"] == "model_led_effect":
                # Only model-led rows have state worth parsing; a malformed
                # legacy state_json is never touched.
                state = json.loads(row["state_json"])
                if (self._db.execute("SELECT 1 FROM task_effect_claims WHERE plan_id=? LIMIT 1", (row["id"],)).fetchone() and
                        (tool is None or state.get("tool") == tool or
                         (outbound and state.get("outbound")))):
                    return dict(row)
            elif self._db.execute(
                    "SELECT 1 FROM task_effect_claims WHERE plan_id=? LIMIT 1", (row["id"],)).fetchone():
                return dict(row)
        return None

    def unresolved_action(self, sid: str) -> dict | None:
        with self._lock:
            row = self._unresolved_action_locked(sid)
        return {"id": row["id"], "status": row["status"]} if row else None

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
                 tool_digest: str | None = None, *, display_content=None) -> int:
        from service.tools.registry import DisplayOnlyToolResult
        if isinstance(content, DisplayOnlyToolResult):
            display_content, content = content, content.model_text
        display_kind = None
        if display_content is not None:
            if (role != "assistant" or not isinstance(display_content, DisplayOnlyToolResult)
                    or content.strip() != display_content.model_text.strip()):
                raise ValueError("Display content requires a matching trusted display-only result")
            display_kind = display_content.artifact_kind
            display_content = str(display_content)
        elif isinstance(content, str):
            # Last line of defence before persistence and the full-text index:
            # a credential that reached here (typed by the user, echoed by a
            # model, or returned by a tool) is never stored.
            from service.safety.redaction import scrub
            content = scrub(content)[0]
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
                "created_at, display_content, display_kind) VALUES (?,?,?,?,?,?,?,?)",
                (sid, idx, role, content, tool_digest, time.time(), display_content, display_kind))
            self._db.execute("UPDATE sessions SET last_used=? WHERE id=?",
                             (time.time(), sid))
            self._db.commit()
        return idx

    def turns_from(self, sid: str, start_idx: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id, idx, role, content, tool_digest, created_at "
                "FROM turns WHERE session_id=? AND idx>=? ORDER BY idx",
                (sid, start_idx)).fetchall()
        return [dict(r) for r in rows]

    def display_turns_from(self, sid: str, start_idx: int) -> list[dict]:
        """UI reload only. Inference callers must use turns_from/turns_range."""
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id, idx, role, content, tool_digest, created_at, "
                "display_content FROM turns WHERE session_id=? AND idx>=? ORDER BY idx",
                (sid, start_idx)).fetchall()
        turns = []
        for row in rows:
            turn = dict(row)
            display = turn.pop("display_content")
            if display is not None:
                turn["content"] = display
                turn["content_provenance"] = "display_only_publisher_metadata"
            turns.append(turn)
        return turns

    def display_artifact(self, sid: str, idx: int | None = None, *, immediate: bool = False):
        """Explicit deterministic delivery only; never inference context.

        ``immediate`` permits a new conversational reference only when the
        latest assistant turn itself is a display artifact. Existing workflows
        instead resolve their already-bound artifact by its exact turn index.
        """
        from service.tools.registry import StoredDisplayArtifact
        with self._lock:
            row = self._db.execute(
                "SELECT idx, display_content, display_kind FROM turns "
                "WHERE session_id=? AND role='assistant' "
                + ("AND idx=? " if idx is not None else
                   "" if immediate else "AND display_content IS NOT NULL ")
                + "ORDER BY idx DESC LIMIT 1", (sid, idx) if idx is not None else (sid,)).fetchone()
        if row is None or row["display_content"] is None:
            return None
        return StoredDisplayArtifact(sid, row["idx"], row["display_content"],
                                     row["display_kind"] or "unknown")

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
                "SELECT session_id, idx, role, content, tool_digest, created_at "
                "FROM turns WHERE session_id=? AND idx>=? AND idx<? ORDER BY idx",
                (sid, start_idx, end_idx)).fetchall()
        return [dict(r) for r in rows]


# module-level singleton
store = SessionStore()

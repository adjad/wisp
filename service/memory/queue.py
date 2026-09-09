"""Transactional capture queue and full-history index, owned by sessions.db.

Triggers cover every persistence path, including direct responses. They enqueue
only new user turns; old history is extracted only by the explicit pilot command.
"""
from __future__ import annotations
import time


def install(db) -> None:
    db.executescript('''
        CREATE TABLE IF NOT EXISTS memory_control (id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL);
        INSERT OR IGNORE INTO memory_control VALUES(1,1);
        CREATE TABLE IF NOT EXISTS memory_jobs (
            session_id TEXT NOT NULL, turn_idx INTEGER NOT NULL, origin TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(session_id,turn_idx));
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_transcripts USING fts5(
            text,session_id UNINDEXED,turn_idx UNINDEXED,created_at UNINDEXED,role UNINDEXED,
            tokenize='porter unicode61');
        CREATE TRIGGER IF NOT EXISTS memory_turn_insert AFTER INSERT ON turns BEGIN
            INSERT INTO memory_transcripts(rowid,text,session_id,turn_idx,created_at,role)
                VALUES(new.rowid,new.content,new.session_id,new.idx,new.created_at,new.role);
            INSERT OR IGNORE INTO memory_jobs(session_id,turn_idx,origin)
                SELECT new.session_id,new.idx,'automatic'
                WHERE new.role='user' AND (SELECT enabled FROM memory_control WHERE id=1)=1;
        END;
        CREATE TRIGGER IF NOT EXISTS memory_turn_delete AFTER DELETE ON turns BEGIN
            DELETE FROM memory_transcripts WHERE rowid=old.rowid;
            DELETE FROM memory_jobs WHERE session_id=old.session_id AND turn_idx=old.idx;
        END;
        CREATE TRIGGER IF NOT EXISTS memory_turn_update AFTER UPDATE OF content ON turns BEGIN
            DELETE FROM memory_transcripts WHERE rowid=old.rowid;
            INSERT INTO memory_transcripts(rowid,text,session_id,turn_idx,created_at,role)
                VALUES(new.rowid,new.content,new.session_id,new.idx,new.created_at,new.role);
        END;
        INSERT INTO memory_transcripts(rowid,text,session_id,turn_idx,created_at,role)
            SELECT rowid,content,session_id,idx,created_at,role FROM turns
            WHERE rowid NOT IN (SELECT rowid FROM memory_transcripts);
    ''')


class MemoryQueue:
    def __init__(self, sessions):
        self.sessions = sessions

    def enabled(self) -> bool:
        with self.sessions._lock:
            return bool(self.sessions._db.execute('SELECT enabled FROM memory_control WHERE id=1').fetchone()[0])

    def enable(self, enabled: bool) -> None:
        with self.sessions._lock, self.sessions._db:
            self.sessions._db.execute('UPDATE memory_control SET enabled=? WHERE id=1', (int(enabled),))

    def recover(self) -> None:
        with self.sessions._lock, self.sessions._db:
            self.sessions._db.execute("UPDATE memory_jobs SET status='queued' WHERE status='running'")

    def pilot(self, session_limit: int = 20) -> int:
        with self.sessions._lock, self.sessions._db:
            return self.sessions._db.execute('''INSERT OR IGNORE INTO memory_jobs(session_id,turn_idx,origin)
                SELECT session_id,idx,'historical' FROM turns WHERE role='user' AND session_id IN
                (SELECT id FROM sessions ORDER BY last_used DESC LIMIT ?) ORDER BY created_at DESC LIMIT 2000''',
                (max(1, min(100, session_limit)),)).rowcount

    def next(self) -> dict | None:
        with self.sessions._lock, self.sessions._db:
            row = self.sessions._db.execute('''SELECT * FROM memory_jobs WHERE status='queued' AND available_at<=?
                ORDER BY origin='automatic' DESC,available_at,session_id,turn_idx LIMIT 1''', (time.time(),)).fetchone()
            if not row:
                return None
            self.sessions._db.execute("UPDATE memory_jobs SET status='running',attempts=attempts+1 WHERE session_id=? AND turn_idx=?",
                                     (row['session_id'], row['turn_idx']))
            return {**dict(row), 'attempts': row['attempts'] + 1}

    def finish(self, job: dict, status: str = 'done', error: str = '') -> None:
        with self.sessions._lock, self.sessions._db:
            self.sessions._db.execute('''UPDATE memory_jobs SET status=?,error=?,available_at=?
                WHERE session_id=? AND turn_idx=?''',
                (status, error[:300], time.time() + min(300, 15 * job['attempts']), job['session_id'], job['turn_idx']))

    def stats(self) -> dict:
        with self.sessions._lock:
            counts = dict(self.sessions._db.execute('SELECT status,count(*) FROM memory_jobs GROUP BY status').fetchall())
            errors = [dict(r) for r in self.sessions._db.execute("SELECT session_id,turn_idx,error FROM memory_jobs WHERE status='failed' LIMIT 5")]
        return {'capture_enabled': self.enabled(), 'jobs': counts, 'errors': errors}

    def retry(self) -> int:
        with self.sessions._lock, self.sessions._db:
            return self.sessions._db.execute("UPDATE memory_jobs SET status='queued',attempts=0,available_at=0,error='' WHERE status='failed'").rowcount

    def search(self, query: str, facts, limit: int = 15, *, user_only: bool = False, exclude_session: str = '') -> list[dict]:
        from service.memory.facts import fts_query
        expr = fts_query(query)
        if not expr:
            return []
        with self.sessions._lock:
            rows = self.sessions._db.execute('''SELECT *,bm25(memory_transcripts) AS rank FROM memory_transcripts
                WHERE memory_transcripts MATCH ? AND session_id != ? AND (?=0 OR role='user')
                ORDER BY rank,created_at DESC LIMIT 250''', (expr, exclude_session, int(user_only))).fetchall()
        # Validate against the durable suppression set on every read. Old index entries cannot resurrect forgotten facts.
        return [dict(r) for r in rows if not facts.suppressed_source('conversation', f"{r['session_id']}:{r['turn_idx']}") and not facts.suppressed_text(r['text'])][:max(1, min(limit, 50))]

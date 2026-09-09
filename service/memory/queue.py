"""Transactional turn indexing and a durable, versioned extraction queue."""
from __future__ import annotations

import sqlite3
import time
import uuid

from service.memory.facts import fts_query, terms


def install(db):
    db.execute('SAVEPOINT memory_queue_install')
    try:
        db.execute('CREATE TABLE IF NOT EXISTS memory_control(id INTEGER PRIMARY KEY,enabled INTEGER NOT NULL)')
        db.execute('INSERT OR IGNORE INTO memory_control VALUES(1,1)')
        db.execute('''CREATE TABLE IF NOT EXISTS memory_jobs(session_id TEXT NOT NULL,turn_idx INTEGER NOT NULL,
            origin TEXT NOT NULL DEFAULT 'automatic',status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,available_at REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(session_id,turn_idx))''')
        for table, name, spec in [('turns', 'memory_version', 'INTEGER NOT NULL DEFAULT 0'),
                                   ('memory_jobs', 'generation', 'INTEGER NOT NULL DEFAULT 0'),
                                   ('memory_jobs', 'claim', "TEXT NOT NULL DEFAULT ''")]:
            if name not in {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {spec}')
        # Replace this module's named triggers even when an older definition
        # does not mention the current index or queue tables.  The first
        # version of memory_turn_update only maintained memory_transcripts, so
        # content-based discovery alone left it behind and made startup fail
        # when the current trigger was created with the same name.
        owned_triggers = {'memory_turn_insert', 'memory_turn_delete', 'memory_turn_update'}
        triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='turns'").fetchall()
        for row in triggers:
            if row['name'] in owned_triggers or any(
                    name in (row['sql'] or '')
                    for name in ('memory_jobs', 'transcript_fts', 'memory_turn_fts')):
                db.execute('DROP TRIGGER "' + row['name'].replace('"', '""') + '"')
        db.execute('DROP TABLE IF EXISTS transcript_fts')
        has_fts = True
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_turn_fts USING fts5(text,session_id UNINDEXED,turn_idx UNINDEXED,created_at UNINDEXED,role UNINDEXED,tokenize='porter unicode61')")
            db.execute('''INSERT INTO memory_turn_fts(rowid,text,session_id,turn_idx,created_at,role)
                SELECT rowid,content,session_id,idx,created_at,role FROM turns
                WHERE rowid NOT IN (SELECT rowid FROM memory_turn_fts)''')
        except sqlite3.OperationalError as exc:
            if 'no such module' not in str(exc):
                raise
            has_fts = False
        insert_index = '''INSERT INTO memory_turn_fts(rowid,text,session_id,turn_idx,created_at,role)
            VALUES(new.rowid,new.content,new.session_id,new.idx,new.created_at,new.role);''' if has_fts else ''
        delete_index = 'DELETE FROM memory_turn_fts WHERE rowid=old.rowid;' if has_fts else ''
        db.execute(f'''CREATE TRIGGER memory_turn_insert AFTER INSERT ON turns BEGIN
            {insert_index}
            INSERT OR IGNORE INTO memory_jobs(session_id,turn_idx,origin,generation)
            SELECT new.session_id,new.idx,'automatic',new.memory_version
            WHERE new.role='user' AND (SELECT enabled FROM memory_control WHERE id=1)=1; END''')
        db.execute(f'''CREATE TRIGGER memory_turn_delete AFTER DELETE ON turns BEGIN
            {delete_index}
            DELETE FROM memory_jobs WHERE session_id=old.session_id AND turn_idx=old.idx; END''')
        db.execute(f'''CREATE TRIGGER memory_turn_update AFTER UPDATE OF content,role ON turns
            WHEN new.content IS NOT old.content OR new.role IS NOT old.role BEGIN
            {delete_index} {insert_index}
            UPDATE turns SET memory_version=old.memory_version+1 WHERE rowid=new.rowid;
            DELETE FROM memory_jobs WHERE session_id=old.session_id AND turn_idx=old.idx;
            INSERT INTO memory_jobs(session_id,turn_idx,origin,generation)
            SELECT new.session_id,new.idx,'automatic',old.memory_version+1
            WHERE new.role='user' AND (SELECT enabled FROM memory_control WHERE id=1)=1; END''')
        db.execute('RELEASE SAVEPOINT memory_queue_install')
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT memory_queue_install')
        db.execute('RELEASE SAVEPOINT memory_queue_install')
        raise


class MemoryQueue:
    def __init__(self, sessions):
        self.sessions = sessions

    def enabled(self):
        with self.sessions._lock:
            return bool(self.sessions._db.execute('SELECT enabled FROM memory_control WHERE id=1').fetchone()[0])

    def enable(self, enabled):
        with self.sessions._lock, self.sessions._db:
            self.sessions._db.execute('UPDATE memory_control SET enabled=? WHERE id=1', (int(enabled),))

    def recover(self):
        # Called once by the single service worker at startup. Rotating claims
        # ensures stale completions cannot overwrite a recovered job.
        with self.sessions._lock, self.sessions._db:
            self.sessions._db.execute("UPDATE memory_jobs SET status='queued',claim='',available_at=0 WHERE status='running'")

    def pilot(self, session_limit=20):
        with self.sessions._lock, self.sessions._db:
            before = self.sessions._db.total_changes
            self.sessions._db.execute('''INSERT OR IGNORE INTO memory_jobs(session_id,turn_idx,origin,generation)
                SELECT session_id,idx,'historical',memory_version FROM turns WHERE role='user'
                AND session_id IN (SELECT id FROM sessions ORDER BY last_used DESC LIMIT ?)
                ORDER BY created_at DESC LIMIT 2000''', (max(1, min(100, int(session_limit))),))
            return self.sessions._db.total_changes - before

    def next(self):
        claim = uuid.uuid4().hex
        with self.sessions._lock, self.sessions._db:
            # A single UPDATE arbitrates between connections/processes.
            self.sessions._db.execute('''UPDATE memory_jobs SET status='running',attempts=attempts+1,claim=?
                WHERE (session_id,turn_idx)=(SELECT session_id,turn_idx FROM memory_jobs
                WHERE status='queued' AND available_at<=? ORDER BY origin='automatic' DESC,available_at,session_id,turn_idx LIMIT 1)
                AND status='queued' ''', (claim, time.time()))
            row = self.sessions._db.execute('SELECT * FROM memory_jobs WHERE claim=?', (claim,)).fetchone()
        return dict(row) if row else None

    def finish(self, job, status='done', error=''):
        if status not in ('done', 'queued', 'failed', 'cancelled'):
            raise ValueError('Invalid job status')
        with self.sessions._lock, self.sessions._db:
            return bool(self.sessions._db.execute('''UPDATE memory_jobs SET status=?,error=?,available_at=?,claim=''
                WHERE session_id=? AND turn_idx=? AND generation=? AND claim=? AND status='running' ''',
                (status, error[:200], time.time() + (min(300, 15 * job['attempts']) if status == 'queued' else 0),
                 job['session_id'], job['turn_idx'], job['generation'], job['claim'])).rowcount)

    def current(self, job):
        with self.sessions._lock:
            return self._current_locked(job)

    def _current_locked(self, job):
        return self.sessions._db.execute('''SELECT 1 FROM memory_jobs j JOIN turns t
            ON t.session_id=j.session_id AND t.idx=j.turn_idx WHERE j.session_id=? AND j.turn_idx=?
            AND j.status='running' AND j.claim=? AND j.generation=? AND t.memory_version=j.generation
            AND t.role='user' ''', (job['session_id'], job['turn_idx'], job['claim'], job['generation'])).fetchone() is not None

    def stats(self):
        with self.sessions._lock:
            counts = {r['status']: r['n'] for r in self.sessions._db.execute('SELECT status,COUNT(*) n FROM memory_jobs GROUP BY status')}
            failed = [dict(r) for r in self.sessions._db.execute("SELECT session_id,turn_idx,attempts,error FROM memory_jobs WHERE status='failed' ORDER BY session_id,turn_idx LIMIT 20")]
        return {'capture_enabled': self.enabled(), 'jobs': counts, 'failed_jobs': failed}

    def retry(self):
        with self.sessions._lock, self.sessions._db:
            return self.sessions._db.execute("UPDATE memory_jobs SET status='queued',attempts=0,available_at=0,error='',claim='' WHERE status='failed'").rowcount

    def cancel_pilot(self):
        with self.sessions._lock, self.sessions._db:
            return self.sessions._db.execute("UPDATE memory_jobs SET status='cancelled',claim='' WHERE origin='historical' AND status IN ('queued','running')").rowcount

    def visible(self, row, facts):
        if facts.suppressed_source('conversation', f"{row['session_id']}:{row['turn_idx']}") or facts.suppressed_text(row['text'] or ''):
            return False
        if row['role'] != 'user':
            # Assistant paraphrases inherit the preceding user's suppression.
            with self.sessions._lock:
                prior = self.sessions._db.execute("SELECT idx,content FROM turns WHERE session_id=? AND idx<? AND role='user' ORDER BY idx DESC LIMIT 1", (row['session_id'], row['turn_idx'])).fetchone()
            if prior and (facts.suppressed_source('conversation', f"{row['session_id']}:{prior['idx']}") or facts.suppressed_text(prior['content'] or '')):
                return False
        return True

    def search(self, query, facts, limit=15, *, user_only=True, exclude_session=''):
        if not terms(query):
            return []
        limit = max(1, min(50, limit))
        result, offset = [], 0
        # Page through ranked matches until visible results fill the budget.
        # There is no newest-session cutoff, nor a fixed pre-filter hit cutoff.
        while len(result) < limit:
            with self.sessions._lock:
                try:
                    rows = self.sessions._db.execute('''SELECT text,session_id,turn_idx,created_at,role FROM memory_turn_fts
                        WHERE memory_turn_fts MATCH ? AND session_id!=? AND (?=0 OR role='user')
                        ORDER BY bm25(memory_turn_fts),created_at DESC LIMIT 100 OFFSET ?''',
                        (fts_query(query), exclude_session, int(user_only), offset)).fetchall()
                except sqlite3.OperationalError as exc:
                    if 'no such table' not in str(exc) and 'no such module' not in str(exc):
                        raise
                    predicates = ' OR '.join('LOWER(content) LIKE ?' for _ in terms(query))
                    rows = self.sessions._db.execute(f'''SELECT content text,session_id,idx turn_idx,created_at,role
                        FROM turns WHERE session_id!=? AND (?=0 OR role='user') AND ({predicates})
                        ORDER BY created_at DESC LIMIT 100 OFFSET ?''',
                        (exclude_session, int(user_only), *('%' + t + '%' for t in terms(query)), offset)).fetchall()
            if not rows:
                break
            for row in rows:
                item = dict(row)
                if self.visible(item, facts):
                    result.append(item)
                    if len(result) == limit:
                        break
            offset += len(rows)
        return result

    def source_context(self, sid, idx, facts):
        rows = self.sessions.turns_range(sid, max(0, idx - 2), idx + 3)
        target = next((r for r in rows if r['idx'] == idx), None)
        if not target or not self.visible({**target, 'text': target['content'], 'turn_idx': idx}, facts):
            return None
        return [dict(r) for r in rows if self.visible({**r, 'text': r['content'], 'turn_idx': r['idx']}, facts)]

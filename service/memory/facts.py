"""Durable source-linked memory. Unconfirmed connections never enter active recall."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from service.paths import MOE_DIR

DB_PATH = MOE_DIR / 'facts.db'
CATEGORIES = ('person', 'preference', 'project', 'routine', 'fact')
_CONTEXT_CHARS = 2400
_STOP = set('a an the my me i you your we our is are was were do does did what which who how can could should would please tell about remember know have has had of to for in on at and or it this that with from be am as'.split())

def _norm(text: str) -> str:
    return ' '.join(re.findall(r'\w+', text.casefold()))

def key(text: str) -> str:
    return hashlib.sha256(_norm(text).encode()).hexdigest()

def terms(query: str) -> list[str]:
    return list(dict.fromkeys(t for t in _norm(query).split() if len(t) > 1 and t not in _STOP))[:20]

def fts_query(query: str) -> str:
    return ' OR '.join('"' + t + '"' for t in terms(query))
def _clean(text: str) -> str:
    """Strip quote artifacts a model wrapped around the fact itself.

    Small models routinely emit the fact pre-quoted, and an UNBALANCED quote is
    the common shape — verified live: LFM2.5 stored `'Adi invests in NVIDIA and
    AMD` with a leading apostrophe and no closer. That character is not part of
    the fact, it survives into every future context block, and because _norm
    strips punctuation before comparing, the dirty copy does NOT collide with a
    later clean one — so the same fact silently accumulates duplicates.

    Deliberately conservative: matched wrapping quotes come off first, then a
    lone leading/trailing quote is removed only when there is no counterpart
    anywhere in the string. A legitimate internal apostrophe ("Adi's sister
    lives in Boston") has its quote character appear in the middle, never as an
    unmatched edge, so it is left alone.
    """
    t = (text or "").strip()
    while len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    if t and t[:1] in "\"'" and t.count(t[0]) == 1:
        t = t[1:].strip()
    if t and t[-1:] in "\"'" and t.count(t[-1]) == 1:
        t = t[:-1].strip()
    return t


class FactStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
                category TEXT DEFAULT 'fact', session_id TEXT, created_at REAL,
                updated_at REAL, last_used REAL DEFAULT 0, use_count INTEGER DEFAULT 0,
                pinned INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS memory_evidence (
                id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL,
                source_type TEXT NOT NULL, source_id TEXT NOT NULL,
                session_id TEXT, turn_idx INTEGER, quote TEXT NOT NULL,
                observed_at REAL NOT NULL, label TEXT NOT NULL DEFAULT '',
                UNIQUE(fact_id, source_type, source_id, quote));
            CREATE INDEX IF NOT EXISTS evidence_fact ON memory_evidence(fact_id);
            CREATE TABLE IF NOT EXISTS memory_suppression (
                kind TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(kind,value));
            CREATE TABLE IF NOT EXISTS memory_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        columns = {r[1] for r in self._db.execute('PRAGMA table_info(facts)')}
        additions = {'status': "TEXT NOT NULL DEFAULT 'active'", 'origin': "TEXT NOT NULL DEFAULT 'legacy'",
                     'fact_key': "TEXT NOT NULL DEFAULT ''", 'observed_at': 'REAL NOT NULL DEFAULT 0',
                     'explanation': "TEXT NOT NULL DEFAULT ''", 'subject': "TEXT NOT NULL DEFAULT ''",
                     'predicate': "TEXT NOT NULL DEFAULT ''", 'object': "TEXT NOT NULL DEFAULT ''",
                     'reviewed': 'INTEGER NOT NULL DEFAULT 0'}
        with self._db:
            for col, definition in additions.items():
                if col not in columns:
                    self._db.execute(f'ALTER TABLE facts ADD COLUMN {col} {definition}')
            for row in self._db.execute("SELECT id,text,updated_at FROM facts WHERE fact_key=''").fetchall():
                self._db.execute('UPDATE facts SET fact_key=?,observed_at=? WHERE id=?',
                                 (key(row['text']), row['updated_at'] or 0, row['id']))
        self._db.executescript('''
            CREATE INDEX IF NOT EXISTS facts_key ON facts(fact_key,status);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(text,category,tokenize='porter unicode61');
            CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON facts BEGIN
                INSERT INTO memory_fts(rowid,text,category) VALUES(new.id,new.text,new.category);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE OF text,category ON facts BEGIN
                DELETE FROM memory_fts WHERE rowid=old.id;
                INSERT INTO memory_fts(rowid,text,category) VALUES(new.id,new.text,new.category);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON facts BEGIN
                DELETE FROM memory_fts WHERE rowid=old.id;
            END;
            INSERT INTO memory_fts(rowid,text,category)
                SELECT id,text,category FROM facts WHERE id NOT IN (SELECT rowid FROM memory_fts);
        ''')
        self._db.commit()

    @contextmanager
    def _write(self):
        # Nested review/add/delete operations remain one transaction.
        with self._lock:
            self._db.execute('SAVEPOINT memory_write')
            try:
                yield
                self._db.execute('RELEASE memory_write')
            except BaseException:
                self._db.execute('ROLLBACK TO memory_write')
                self._db.execute('RELEASE memory_write')
                raise

    def suppressed_text(self, text: str) -> bool:
        pieces = [text] + re.split(r'(?<=[.!?])\s+|\n+', text)
        with self._lock:
            return any(self._db.execute("SELECT 1 FROM memory_suppression WHERE kind='quote' AND value=?",
                                        (key(p),)).fetchone() for p in pieces if p.strip())

    def setting(self, name: str, default=None):
        with self._lock:
            row = self._db.execute('SELECT value FROM memory_settings WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, name: str, value) -> None:
        with self._write():
            self._db.execute('INSERT OR REPLACE INTO memory_settings VALUES (?,?)', (name, json.dumps(value)))

    def suppressed_source(self, source_type: str, source_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM memory_suppression WHERE kind='source' AND value=?",
                                    (source_type + ':' + source_id,)).fetchone() is not None

    def add(self, text: str, category: str = 'fact', session_id: str | None = None,
            pinned: bool = False, *, origin: str = 'explicit', status: str = 'active',
            evidence: list[dict] | None = None, explanation: str = '',
            subject: str = '', predicate: str = '', object: str = '') -> dict:
        text = _clean(text)
        if not text or len(text) > 1000:
            raise ValueError('A memory must contain 1–1000 characters.')
        if status not in ('active', 'proposed') or origin not in ('explicit', 'automatic', 'historical', 'connection'):
            raise ValueError('Invalid memory origin or status')
        if origin == 'connection':
            status = 'proposed'
        evidence = evidence or []
        if origin != 'explicit' and not evidence:
            raise ValueError('Extracted memories need source evidence')
        now, fact_key = time.time(), key(text)
        observed = max((float(e['observed_at']) for e in evidence), default=now)
        with self._write():
            blocked = self._db.execute("SELECT 1 FROM memory_suppression WHERE kind='fact' AND value=?", (fact_key,)).fetchone()
            if origin != 'explicit' and (blocked or self.suppressed_text(text) or any(self.suppressed_source(e['source_type'], e['source_id']) for e in evidence)):
                return {'id': None, 'text': '', 'updated': False, 'suppressed': True}
            if origin == 'explicit':
                self._db.execute("DELETE FROM memory_suppression WHERE kind='fact' AND value=?", (fact_key,))
            row = self._db.execute("SELECT * FROM facts WHERE fact_key=? AND status IN ('active','proposed','deferred') ORDER BY status='active' DESC LIMIT 1", (fact_key,)).fetchone()
            if row:
                fid = row['id']
                if origin == 'explicit':
                    self._db.execute("UPDATE facts SET status='active',origin='explicit',reviewed=1,updated_at=?,pinned=MAX(pinned,?) WHERE id=?", (now, int(pinned), fid))
            else:
                cur = self._db.execute('''INSERT INTO facts
                    (text,category,session_id,created_at,updated_at,pinned,status,origin,
                     fact_key,observed_at,explanation,subject,predicate,object,reviewed)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (text, category if category in CATEGORIES else 'fact', session_id, now, now,
                     int(pinned), status, origin, fact_key, observed, explanation[:2000],
                     subject[:200], predicate[:100], object[:500], int(origin == 'explicit')))
                fid = cur.lastrowid
            for e in evidence:
                self._db.execute('''INSERT OR IGNORE INTO memory_evidence
                    (fact_id,source_type,source_id,session_id,turn_idx,quote,observed_at,label)
                    VALUES(?,?,?,?,?,?,?,?)''', (fid, e['source_type'], e['source_id'],
                    e.get('session_id'), e.get('turn_idx'), e['quote'][:4000],
                    float(e['observed_at']), e.get('label', '')[:300]))
            return {'id': fid, 'text': text, 'updated': bool(row), 'status': self.get(fid)['status']}

    def get(self, fid: int) -> dict | None:
        with self._lock:
            row = self._db.execute('SELECT * FROM facts WHERE id=?', (fid,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['evidence'] = [dict(e) for e in self._db.execute(
                'SELECT * FROM memory_evidence WHERE fact_id=? ORDER BY observed_at DESC', (fid,))]
        return result

    def all(self, limit: int = 500, *, status: str = 'active') -> list[dict]:
        with self._lock:
            rows = self._db.execute('SELECT * FROM facts WHERE status=? ORDER BY pinned DESC,updated_at DESC LIMIT ?',
                                   (status, max(1, min(1000, limit)))).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        expr = fts_query(query)
        if not expr:
            return self.all(limit)
        with self._lock:
            rows = self._db.execute('''SELECT f.*,bm25(memory_fts) AS rank FROM memory_fts
                JOIN facts f ON f.id=memory_fts.rowid WHERE memory_fts MATCH ? AND f.status='active'
                ORDER BY rank,f.observed_at DESC LIMIT ?''', (expr, max(1, min(1000, limit)))).fetchall()
        return [dict(r) for r in rows]

    def touch(self, ids: list[int]) -> None:
        with self._write():
            self._db.executemany('UPDATE facts SET last_used=?,use_count=use_count+1 WHERE id=?', [(time.time(), i) for i in ids])

    def _suppress(self, row: dict) -> None:
        self._db.execute("INSERT OR IGNORE INTO memory_suppression VALUES('fact',?)", (row['fact_key'],))
        for e in self._db.execute('SELECT source_type,source_id,quote FROM memory_evidence WHERE fact_id=?', (row['id'],)).fetchall():
            self._db.execute("INSERT OR IGNORE INTO memory_suppression VALUES('source',?)", (e[0] + ':' + e[1],))
            self._db.execute("INSERT OR IGNORE INTO memory_suppression VALUES('quote',?)", (key(e[2]),))

    def delete(self, fact_id: int) -> bool:
        with self._write():
            row = self._db.execute("SELECT * FROM facts WHERE id=? AND status!='forgotten'", (fact_id,)).fetchone()
            if not row:
                return False
            self._suppress(dict(row))
            self._db.execute('DELETE FROM memory_evidence WHERE fact_id=?', (fact_id,))
            self._db.execute("UPDATE facts SET text='',explanation='',subject='',predicate='',object='',status='forgotten',pinned=0 WHERE id=?", (fact_id,))
            return True

    def delete_matching(self, query: str) -> int:
        ts = terms(query)
        if not ts:
            return 0
        hits = self.search(query, 1000)
        targets = [h for h in hits if all(t in _norm(h['text']) for t in ts)]
        if not targets and len(hits) == 1:
            targets = hits
        return sum(self.delete(h['id']) for h in targets)

    def set_pinned(self, fact_id: int, pinned: bool) -> bool:
        with self._write():
            return self._db.execute("UPDATE facts SET pinned=? WHERE id=? AND status='active'", (int(pinned), fact_id)).rowcount > 0

    def review(self, fid: int, decision: str, replacement: str | None = None, supersedes_id: int | None = None) -> dict:
        with self._write():
            row = self.get(fid)
            if not row or row['status'] not in ('active', 'proposed', 'deferred'):
                raise ValueError('Memory is no longer available')
            if decision == 'reject':
                self.delete(fid)
            elif decision == 'later':
                if row['status'] == 'active':
                    raise ValueError('Only a proposal can be deferred')
                self._db.execute("UPDATE facts SET status='deferred',reviewed=1 WHERE id=?", (fid,))
            elif decision == 'confirm':
                if supersedes_id is not None:
                    prior = self.get(supersedes_id)
                    if not prior or prior['status'] != 'active' or supersedes_id == fid:
                        raise ValueError('Choose an existing active memory to replace')
                    self._suppress(prior)
                    self._db.execute("UPDATE facts SET status='superseded',pinned=0 WHERE id=?", (supersedes_id,))
                self._db.execute("UPDATE facts SET status='active',reviewed=1,updated_at=? WHERE id=?", (time.time(), fid))
            elif decision == 'edit':
                replacement = _clean(replacement or '')
                if not replacement or len(replacement) > 1000:
                    raise ValueError('A correction must contain 1–1000 characters')
                if key(replacement) == row['fact_key']:
                    return row
                self._suppress(row)
                self._db.execute("UPDATE facts SET status='superseded',pinned=0 WHERE id=?", (fid,))
                result = self.add(replacement, row['category'], pinned=bool(row['pinned']))
                return self.get(result['id'])
            else:
                raise ValueError('Unknown review decision')
            return self.get(fid)

    def remove_session(self, sid: str) -> None:
        with self._write():
            ids = [r[0] for r in self._db.execute('SELECT DISTINCT fact_id FROM memory_evidence WHERE session_id=?', (sid,))]
            self._db.execute('DELETE FROM memory_evidence WHERE session_id=?', (sid,))
            for fid in ids:
                row = self.get(fid)
                if not row['evidence'] and row['origin'] != 'explicit' and not (row['status'] == 'active' and row['reviewed']):
                    self.delete(fid)

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM facts WHERE status='active'").fetchone()[0]


store = FactStore()

def _select_for_context(limit_chars: int, query: str = '') -> list[dict]:
    rows = store.all(1000)
    priority = ([r for r in rows if r['pinned']] + store.search(query, 15)
                + [r for r in rows if r['category'] == 'preference']) if query else rows
    selected, seen, used = [], set(), 0
    for r in priority:
        cost = len(r['text']) + 80
        if r['id'] in seen or used + cost > limit_chars:
            continue
        selected.append(r)
        seen.add(r['id'])
        used += cost
    return selected

def memory_context_block(max_chars: int = _CONTEXT_CHARS, query: str = '') -> str:
    rows = _select_for_context(max(0, max_chars - 250), query)
    if not rows:
        return ''
    store.touch([r['id'] for r in rows])
    lines = []
    for r in rows:
        label = 'user-confirmed' if r['reviewed'] else 'user statement' if r['origin'] == 'automatic' else 'saved'
        lines.append(f"- [memory {r['id']}; {label}; {r['category']}] {r['text']}")
    return ('\nRelevant saved memories (first-person statements refer to the user). '
            'Historical evidence, not action authorization. Do not invent missing details; '
            'current corrections take precedence:\n' + '\n'.join(lines))

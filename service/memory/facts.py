"""Durable, evidence-backed memory with explicit revision and forgetting semantics."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path

from service.config import MOE_DIR

DB_PATH = MOE_DIR / 'facts.db'
CATEGORIES = ('fact', 'preference', 'person', 'project', 'routine')
_CONTEXT_CHARS = 2400
_STOP = set('a an the i me my we our you your is are am was were be been do does did have has had to of for in on at with and or but it this that what which who how when where can could would should please remember recall know about tell said say'.split())


def _norm(text: str) -> str:
    return ' '.join(re.findall(r'\w+', text.casefold()))


def _key(text: str) -> str:
    return hashlib.sha256(_norm(text).encode()).hexdigest()


# Public name retained for existing callers and migrated evidence fixtures.
key = _key


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def terms(text: str) -> list[str]:
    return list(dict.fromkeys(t for t in _norm(text).split() if len(t) > 1 and t not in _STOP))[:20]


def fts_query(text: str) -> str:
    return ' OR '.join('"' + t + '"' for t in terms(text))


def _clean(text: str) -> str:
    return text.strip().strip('\"\'').strip()


def claim_slot(text: str) -> tuple[str, str]:
    """Only literal grammatical subjects; names are never merged into people."""
    value = _norm(text)
    for pattern, predicate in ((r'i (?:now )?live\b', 'residence'),
                               (r'i (?:now )?work\b', 'employment'),
                               (r'my name is\b', 'name'),
                               (r'i (?:now )?prefer\b', 'preference')):
        if re.match(pattern, value):
            return 'user', predicate
    return '', ''


class FactStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._fts = True
        # DDL and data migration share one rollback boundary. Legacy provenance
        # stays unknown: migration does not claim that old rows were user quotes.
        with self._write():
            statements = [
                '''CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, text TEXT NOT NULL,
                   category TEXT NOT NULL DEFAULT 'fact', session_id TEXT, created_at REAL NOT NULL,
                   updated_at REAL NOT NULL, last_used REAL, use_count INTEGER NOT NULL DEFAULT 0,
                   pinned INTEGER NOT NULL DEFAULT 0)''',
                '''CREATE TABLE IF NOT EXISTS memory_evidence(id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL,
                   source_type TEXT NOT NULL, source_id TEXT NOT NULL, session_id TEXT, turn_idx INTEGER,
                   quote TEXT NOT NULL, observed_at REAL NOT NULL, label TEXT NOT NULL DEFAULT '',
                   UNIQUE(fact_id,source_type,source_id,quote))''',
                'CREATE INDEX IF NOT EXISTS evidence_fact ON memory_evidence(fact_id)',
                'CREATE TABLE IF NOT EXISTS memory_suppression(kind TEXT,value TEXT,PRIMARY KEY(kind,value))',
                'CREATE TABLE IF NOT EXISTS memory_settings(name TEXT PRIMARY KEY,value TEXT NOT NULL)',
                '''CREATE TABLE IF NOT EXISTS memory_revisions(old_id INTEGER NOT NULL,new_id INTEGER NOT NULL,
                   created_at REAL NOT NULL,PRIMARY KEY(old_id,new_id))''',
                'CREATE TABLE IF NOT EXISTS memory_schema(version INTEGER NOT NULL)',
            ]
            for statement in statements:
                self._db.execute(statement)
            version = self._db.execute('SELECT MAX(version) FROM memory_schema').fetchone()[0]
            if version is not None and version > 2:
                raise ValueError('Memory schema is newer than this service')
            columns = {r['name'] for r in self._db.execute('PRAGMA table_info(facts)')}
            for name, spec in {
                'status': "TEXT NOT NULL DEFAULT 'active'", 'origin': "TEXT NOT NULL DEFAULT 'legacy'",
                'fact_key': "TEXT NOT NULL DEFAULT ''", 'observed_at': 'REAL NOT NULL DEFAULT 0',
                'explanation': "TEXT NOT NULL DEFAULT ''", 'subject': "TEXT NOT NULL DEFAULT ''",
                'predicate': "TEXT NOT NULL DEFAULT ''", 'object': "TEXT NOT NULL DEFAULT ''",
                'reviewed': 'INTEGER NOT NULL DEFAULT 0',
            }.items():
                if name not in columns:
                    self._db.execute(f'ALTER TABLE facts ADD COLUMN {name} {spec}')
            evidence_columns = {r['name'] for r in self._db.execute('PRAGMA table_info(memory_evidence)')}
            for name in ('source_fingerprint', 'extractor_version'):
                if name not in evidence_columns:
                    self._db.execute(f"ALTER TABLE memory_evidence ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            for r in self._db.execute("SELECT id,text,updated_at FROM facts WHERE fact_key='' AND status!='forgotten'").fetchall():
                self._db.execute('UPDATE facts SET fact_key=?,observed_at=? WHERE id=?',
                                 (_key(r['text']), r['updated_at'], r['id']))
            # Separate table name allows upgrading previous FTS layouts safely.
            try:
                self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fact_fts USING fts5(text,category,tokenize='porter unicode61')")
                self._db.execute('''CREATE TRIGGER IF NOT EXISTS memory_fact_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO memory_fact_fts(rowid,text,category) VALUES(new.id,new.text,new.category); END''')
                self._db.execute('''CREATE TRIGGER IF NOT EXISTS memory_fact_au AFTER UPDATE ON facts BEGIN
                    DELETE FROM memory_fact_fts WHERE rowid=old.id;
                    INSERT INTO memory_fact_fts(rowid,text,category) VALUES(new.id,new.text,new.category); END''')
                self._db.execute('''CREATE TRIGGER IF NOT EXISTS memory_fact_ad AFTER DELETE ON facts BEGIN
                    DELETE FROM memory_fact_fts WHERE rowid=old.id; END''')
                self._db.execute('''INSERT INTO memory_fact_fts(rowid,text,category)
                    SELECT id,text,category FROM facts WHERE id NOT IN (SELECT rowid FROM memory_fact_fts)''')
            except sqlite3.OperationalError as exc:
                if 'no such module' not in str(exc):
                    raise
                self._fts = False
            self._db.execute('DELETE FROM memory_schema')
            self._db.execute('INSERT INTO memory_schema VALUES(2)')

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute('SAVEPOINT memory_write')
            try:
                yield
                self._db.execute('RELEASE SAVEPOINT memory_write')
            except BaseException:
                self._db.execute('ROLLBACK TO SAVEPOINT memory_write')
                self._db.execute('RELEASE SAVEPOINT memory_write')
                raise

    def setting(self, name, default=None):
        with self._lock:
            row = self._db.execute('SELECT value FROM memory_settings WHERE name=?', (name,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_setting(self, name, value):
        with self._write():
            self._db.execute('INSERT OR REPLACE INTO memory_settings VALUES(?,?)', (name, json.dumps(value)))

    def _blocked(self, kind, value):
        return self._db.execute('SELECT 1 FROM memory_suppression WHERE kind=? AND value=?', (kind, value)).fetchone() is not None

    def suppressed_source(self, source_type, source_id):
        with self._lock:
            return self._blocked('source', f'{source_type}:{source_id}')

    def suppressed_text(self, text):
        words = _norm(text).split()
        with self._lock:
            # Old suppression hashes remain effective after the migration.
            if any(self._blocked('quote', _key(q)) for q in [text, *re.split(r'[.!?\n]+', text)] if q.strip()):
                return True
            rows = self._db.execute("SELECT kind,value FROM memory_suppression WHERE kind LIKE 'fragment:%'").fetchall()
        by_length = {}
        for r in rows:
            by_length.setdefault(int(r['kind'].split(':')[1]), set()).add(r['value'])
        for n, hashes in by_length.items():
            for i in range(len(words) - n + 1):
                if hashlib.sha256(' '.join(words[i:i+n]).encode()).hexdigest() in hashes:
                    return True
        return False

    def add(self, text, category='fact', session_id=None, pinned=False, *, origin='explicit',
            status='active', evidence=None, explanation='', subject='', predicate='', object=''):
        text = _clean(text)
        if not text or len(text) > 1000 or category not in CATEGORIES:
            raise ValueError('A memory needs 1-1000 characters and a valid category')
        if origin not in ('explicit', 'automatic', 'historical', 'connection') or status not in ('active', 'proposed'):
            raise ValueError('Invalid memory origin or status')
        evidence = evidence or []
        if origin != 'explicit' and not evidence:
            raise ValueError('Extracted memory requires evidence')
        now = time.time()
        observed = max((float(e.get('observed_at', now)) for e in evidence), default=now)
        if not math.isfinite(observed):
            raise ValueError('Invalid observation time')
        for e in evidence:
            if not e.get('source_type') or not e.get('source_id') or not e.get('quote'):
                raise ValueError('Incomplete memory evidence')
        subject, predicate = (subject, predicate) if subject else claim_slot(text)
        key = _key(text)
        with self._write():
            if any(e.get('session_id') and self._blocked('session', e['session_id']) for e in evidence):
                return {'id': None, 'suppressed': True, 'updated': False}
            if origin != 'explicit' and (self._blocked('fact', key) or self.suppressed_text(text) or
                    any(self.suppressed_source(e['source_type'], e['source_id']) or self.suppressed_text(e['quote']) for e in evidence)):
                return {'id': None, 'suppressed': True, 'updated': False}
            if origin == 'explicit':
                # A new explicit save is permitted, but never unblocks old sources.
                self._db.execute("DELETE FROM memory_suppression WHERE kind='fact' AND value=?", (key,))
            if origin in ('connection', 'historical'):
                status = 'proposed'
            if origin == 'automatic' and subject and predicate and self._db.execute(
                    "SELECT 1 FROM facts WHERE status='active' AND subject=? AND predicate=? AND fact_key!=?",
                    (subject, predicate, key)).fetchone():
                status = 'proposed'
                explanation = explanation or 'May update an existing memory. Review which statement is current.'
            prior = self._db.execute("SELECT * FROM facts WHERE fact_key=? AND status IN ('active','proposed','deferred') ORDER BY id LIMIT 1", (key,)).fetchone()
            updated = prior is not None
            if prior:
                fid = prior['id']
                if origin == 'explicit':
                    self._db.execute("UPDATE facts SET status='active',origin='explicit',reviewed=1,updated_at=?,observed_at=?,pinned=MAX(pinned,?) WHERE id=?", (now, observed, int(pinned), fid))
            else:
                fid = self._db.execute('''INSERT INTO facts(text,category,session_id,created_at,updated_at,
                    pinned,status,origin,fact_key,observed_at,explanation,subject,predicate,object,reviewed)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (text, category, session_id, now, now, int(pinned), status, origin, key, observed,
                     explanation, subject, predicate, object, int(origin == 'explicit'))).lastrowid
            for e in evidence:
                self._db.execute('''INSERT OR IGNORE INTO memory_evidence(fact_id,source_type,source_id,
                    session_id,turn_idx,quote,observed_at,label,source_fingerprint,extractor_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (fid, e['source_type'], e['source_id'], e.get('session_id'), e.get('turn_idx'),
                     e['quote'][:6000], float(e.get('observed_at', now)), e.get('label', '')[:300],
                     e.get('source_fingerprint', ''), e.get('extractor_version', '')))
            return {'id': fid, 'text': text, 'updated': updated, 'status': self.get(fid)['status']}

    def get(self, fid):
        with self._lock:
            row = self._db.execute('SELECT * FROM facts WHERE id=?', (fid,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['evidence'] = [dict(r) for r in self._db.execute('SELECT * FROM memory_evidence WHERE fact_id=? ORDER BY observed_at,id', (fid,))]
            result['supersedes'] = [r[0] for r in self._db.execute('SELECT old_id FROM memory_revisions WHERE new_id=?', (fid,))]
            result['superseded_by'] = [r[0] for r in self._db.execute('SELECT new_id FROM memory_revisions WHERE old_id=?', (fid,))]
            return result

    def all(self, limit=500, *, status='active'):
        with self._lock:
            return [dict(r) for r in self._db.execute('SELECT * FROM facts WHERE status=? ORDER BY pinned DESC,updated_at DESC,id DESC LIMIT ?', (status, max(1, min(1000, limit))))]

    def search(self, query, limit=30, *, status='active'):
        tokens = terms(query)
        if not tokens:
            return []
        with self._lock:
            if self._fts:
                return [dict(r) for r in self._db.execute('''SELECT f.* FROM memory_fact_fts
                    JOIN facts f ON f.id=memory_fact_fts.rowid WHERE memory_fact_fts MATCH ? AND f.status=?
                    ORDER BY bm25(memory_fact_fts),f.pinned DESC,f.observed_at DESC,f.id DESC LIMIT ?''',
                    (fts_query(query), status, max(1, min(1000, limit))))]
            rows = [dict(r) for r in self._db.execute('SELECT * FROM facts WHERE status=?', (status,))]
        ranked = [(sum(t in _norm(r['text']).split() for t in tokens), r) for r in rows]
        return [r for score, r in sorted(ranked, key=lambda item: (item[0], item[1]['pinned'], item[1]['observed_at']), reverse=True) if score][:limit]

    def touch(self, ids):
        with self._write():
            self._db.executemany("UPDATE facts SET last_used=?,use_count=use_count+1 WHERE id=? AND status='active'", [(time.time(), i) for i in ids])

    def _suppress(self, row):
        self._db.execute('INSERT OR IGNORE INTO memory_suppression VALUES(?,?)', ('fact', row['fact_key']))
        quotes = [row['text']]
        for e in self._db.execute('SELECT * FROM memory_evidence WHERE fact_id=?', (row['id'],)).fetchall():
            self._db.execute('INSERT OR IGNORE INTO memory_suppression VALUES(?,?)', ('source', f"{e['source_type']}:{e['source_id']}"))
            quotes.append(e['quote'])
        for quote in quotes:
            count = len(_norm(quote).split())
            if count:
                self._db.execute('INSERT OR IGNORE INTO memory_suppression VALUES(?,?)', (f'fragment:{count}', _key(quote)))
                self._db.execute('INSERT OR IGNORE INTO memory_suppression VALUES(?,?)', ('quote', _key(quote)))

    def delete(self, fid):
        with self._write():
            row = self.get(fid)
            if not row or row['status'] == 'forgotten':
                return False
            # Forget the entire correction family, including historical wording.
            pending, visited = [fid], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                r = self.get(current)
                if not r:
                    continue
                pending += r['supersedes'] + r['superseded_by']
                self._suppress(r)
                self._db.execute('DELETE FROM memory_evidence WHERE fact_id=?', (current,))
                self._db.execute("UPDATE facts SET text='',explanation='',subject='',predicate='',object='',status='forgotten',pinned=0 WHERE id=?", (current,))
            return True

    def delete_matching(self, query):
        tokens = terms(query)
        if not tokens:
            return 0
        targets = [r for r in self.search(query, 1000) if all(t in _norm(r['text']).split() for t in tokens)]
        with self._write():
            return sum(self.delete(r['id']) for r in targets)

    def set_pinned(self, fid, pinned):
        with self._write():
            return bool(self._db.execute("UPDATE facts SET pinned=? WHERE id=? AND status='active'", (int(pinned), fid)).rowcount)

    def _supersede(self, old, new_id):
        if old['id'] == new_id:
            raise ValueError('A memory cannot replace itself')
        self._suppress(old)
        self._db.execute("UPDATE facts SET status='superseded',pinned=0 WHERE id=?", (old['id'],))
        self._db.execute('INSERT OR IGNORE INTO memory_revisions VALUES(?,?,?)', (old['id'], new_id, time.time()))

    def review(self, fid, decision, text=None, *, supersedes_id=None):
        with self._write():
            row = self.get(fid)
            if not row or row['status'] not in ('active', 'proposed', 'deferred'):
                raise ValueError('Memory is unavailable for review')
            if decision == 'reject':
                self.delete(fid)
                return {'id': fid, 'status': 'forgotten'}
            if decision == 'later' and row['status'] in ('proposed', 'deferred'):
                self._db.execute("UPDATE facts SET status='deferred',reviewed=1 WHERE id=?", (fid,))
                return self.get(fid)
            if decision == 'edit':
                replacement = _clean(text or '')
                if not replacement or len(replacement) > 1000:
                    raise ValueError('A correction needs 1-1000 characters')
                other = None
                if supersedes_id is not None and supersedes_id != fid:
                    other = self.get(supersedes_id)
                    if not other or other['status'] != 'active':
                        raise ValueError('Choose an active memory to replace')
                if _key(replacement) == row['fact_key']:
                    return self.review(fid, 'confirm', supersedes_id=supersedes_id) if other else self.get(fid)
                # Hide the old version before add() so a duplicate cannot bind
                # the replacement to the record being superseded.
                self._db.execute("UPDATE facts SET status='superseded' WHERE id=?", (fid,))
                result = self.add(replacement, row['category'], pinned=bool(row['pinned'] or (other and other['pinned'])))
                self._supersede(row, result['id'])
                if other and other['id'] != result['id']:
                    self._supersede(other, result['id'])
                return self.get(result['id'])
            if decision == 'confirm':
                pinned = row['pinned']
                if supersedes_id is not None:
                    old = self.get(supersedes_id)
                    if not old or old['status'] != 'active' or supersedes_id == fid:
                        raise ValueError('Choose an active memory to replace')
                    pinned = max(pinned, old['pinned'])
                    self._supersede(old, fid)
                self._db.execute("UPDATE facts SET status='active',reviewed=1,pinned=?,updated_at=? WHERE id=?", (pinned, time.time(), fid))
                return self.get(fid)
            raise ValueError('Invalid review decision')

    def bind_source(self, request_id, session_id, turn_idx):
        """Attach synchronous saves to their eventual persisted user turn."""
        with self._write():
            source_id = f'{session_id}:{turn_idx}'
            if self.suppressed_source('user_request', request_id):
                self._db.execute('INSERT OR IGNORE INTO memory_suppression VALUES(?,?)', ('source', 'conversation:' + source_id))
            self._db.execute("UPDATE OR IGNORE memory_evidence SET source_type='conversation',source_id=?,session_id=?,turn_idx=? WHERE source_type='user_request' AND source_id=?", (source_id, session_id, turn_idx, request_id))

    def remove_session(self, sid):
        with self._write():
            self._db.execute('INSERT OR IGNORE INTO memory_suppression VALUES(?,?)', ('session', sid))
            ids = [r[0] for r in self._db.execute('SELECT DISTINCT fact_id FROM memory_evidence WHERE session_id=?', (sid,))]
            self._db.execute('DELETE FROM memory_evidence WHERE session_id=?', (sid,))
            for fid in ids:
                row = self.get(fid)
                if not row['evidence'] and row['origin'] != 'explicit' and not row['pinned'] and not (row['status'] == 'active' and row['reviewed']):
                    # Session deletion is source retention, not a request to
                    # forget explicit corrections elsewhere in the family.
                    self._suppress(row)
                    self._db.execute("UPDATE facts SET text='',explanation='',subject='',predicate='',object='',status='forgotten',pinned=0 WHERE id=?", (fid,))

    def count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM facts WHERE status='active'").fetchone()[0]


store = FactStore()


def _select_for_context(max_chars=_CONTEXT_CHARS, query=''):
    rows = store.search(query, 30) if query.strip() else store.all(30)
    result, used = [], 0
    for row in rows:
        cost = len(row['text']) + 100
        if used + cost <= max_chars:
            result.append(row)
            used += cost
    return result


def memory_context_block(max_chars=_CONTEXT_CHARS, query=''):
    if not query.strip():
        return ''
    from service.memory.retrieval import retrieve, render_context
    return render_context(retrieve(query, facts=store, include_passages=bool(query.strip())), max_chars=max_chars)

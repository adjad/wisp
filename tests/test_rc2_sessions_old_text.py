"""The one-time rebuild of a sessions.db an earlier build wrote.

Chats that build deleted stayed readable in the file's free pages and in the
search index's old segments. All state is in tmp_path; no user data.
"""
import importlib
import sqlite3

from service.memory.facts import FactStore
from service.memory.queue import MemoryQueue
from service.memory.store import OLD_TEXT_CLEARED, SessionStore

_MARKERS = (b'zorvaquill', b'plimbrusk')


def _file_bytes(path):
    """The database file and its -wal/-shm/-journal sidecars, lower-cased (the
    search index stores lower-case words)."""
    return b''.join(p.read_bytes() for p in sorted(path.parent.glob(path.name + '*'))).lower()


def _fill_chat(sessions, first='Zorvaquillen', second='plimbruskar'):
    sid = sessions.create_session()
    for i in range(20):
        sessions.add_turn(sid, 'user', f'The gate phrase is {first} {second}, try {i}. ' * 4)
        sessions.add_turn(sid, 'assistant', f'Noted: {first.lower()} {second} {i}.')
    sessions.set_summary(sid, f'The user gave the gate phrase {first.lower()} {second}.', 2)
    return sid


def _old_build_file(path, monkeypatch, *, wal=False):
    """A sessions.db as builds before this fix left it: a deleted chat, a live
    chat, no secure_delete on the file or its index, and no rebuild marker."""
    import service.memory.queue as queue_module
    with monkeypatch.context() as earlier:
        earlier.setattr(queue_module, 'secure_delete_fts', lambda db, table: None)
        sessions = SessionStore(path)
    db = sessions._db
    db.execute('PRAGMA secure_delete=OFF')
    db.execute("INSERT INTO memory_turn_fts(memory_turn_fts,rank) VALUES('secure-delete',0)")
    db.execute('DROP TABLE session_store_meta')          # that build had no marker
    db.commit()
    if wal:
        db.execute('PRAGMA journal_mode=WAL')
    gone = _fill_chat(sessions)
    live = _fill_chat(sessions, 'Quentaborix', 'velmuthar')
    sessions.add_turn(live, 'user', 'I prefer oolong tea.')
    # The old way: plain DELETEs, no overwrite.
    sessions.delete_session(gone)
    # A turn saved while its chat was being deleted: no session row any more.
    db.execute("INSERT INTO turns (session_id, idx, role, content, created_at) "
               "VALUES ('vanished', 0, 'user', 'zorvaquillen orphan', 0)")
    db.commit()
    db.close()
    return live


def _open(path):
    facts = FactStore(path.parent / 'facts.db')
    sessions = SessionStore(path)
    return facts, sessions, MemoryQueue(sessions)


def test_first_start_clears_chats_an_earlier_build_deleted(tmp_path, monkeypatch):
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)
    assert all(marker in _file_bytes(path) for marker in _MARKERS)
    facts, sessions, q = _open(path)
    try:
        assert not sessions.old_text_cleared()
        assert sessions.clear_old_text() is True
        assert sessions.old_text_cleared()
        # The live chat is whole and still findable; the deleted one is not.
        assert sessions.turn_count(live) == 41
        assert sessions.get_session(live)['summary'].startswith('The user gave')
        assert {r['session_id'] for r in q.search('quentaborix', facts)} == {live}
        assert [r['session_id'] for r in q.search('oolong', facts)] == [live]
        assert q.search('zorvaquillen', facts) == []
        assert sessions._db.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id='vanished'").fetchone()[0] == 0
        # Future deletes keep overwriting, in the file and in the index.
        assert sessions._db.execute('PRAGMA secure_delete').fetchone()[0] == 1
        assert sessions._db.execute(
            "SELECT v FROM memory_turn_fts_config WHERE k='secure-delete'").fetchone()[0] == 1
        assert sessions._db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    finally:
        sessions._db.close()
        facts._db.close()
    left = _file_bytes(path)
    assert not any(marker in left for marker in _MARKERS)
    assert b'quentaborix' in left and b'oolong' in left


def test_a_wal_file_is_cleared_including_its_log(tmp_path, monkeypatch):
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch, wal=True)
    assert all(marker in _file_bytes(path) for marker in _MARKERS)
    sessions = SessionStore(path)
    try:
        assert sessions.clear_old_text() is True
        # Checked while the connection is open, so -wal and -shm are present.
        assert not any(marker in _file_bytes(path) for marker in _MARKERS)
        assert sessions.turn_count(live) == 41
    finally:
        sessions._db.close()
    assert not any(marker in _file_bytes(path) for marker in _MARKERS)


def test_the_rebuild_runs_once(tmp_path, monkeypatch):
    path = tmp_path / 'sessions.db'
    _old_build_file(path, monkeypatch)
    sessions = SessionStore(path)
    assert sessions.clear_old_text() is True
    sessions._db.close()
    seen = []

    class Spy(sqlite3.Connection):
        def execute(self, sql, *args):
            seen.append(sql)
            return super().execute(sql, *args)

    store_module = importlib.import_module('service.memory.store')   # the package exports a `store` too
    connect = sqlite3.connect
    monkeypatch.setattr(store_module.sqlite3, 'connect',
                        lambda *a, **k: connect(*a, factory=Spy, **k))
    sessions = SessionStore(path)
    try:
        assert sessions.clear_old_text() is True
        assert 'VACUUM' not in seen and not any('DROP TABLE memory_turn_fts' in s for s in seen)
    finally:
        sessions._db.close()


def test_a_file_made_by_this_build_needs_no_rebuild(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    try:
        assert sessions.old_text_cleared()
        assert sessions._db.execute('SELECT value FROM session_store_meta WHERE name=?',
                                    (OLD_TEXT_CLEARED,)).fetchone()[0] == '1'
    finally:
        sessions._db.close()


def test_a_locked_file_is_left_alone_and_retried_next_start(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)
    sessions = SessionStore(path)
    other = sqlite3.connect(str(path))
    other.execute('BEGIN EXCLUSIVE')           # another process holds the file
    try:
        assert sessions.clear_old_text() is False
        assert 'deleted chats not cleared yet' in caplog.text
    finally:
        other.rollback()
        other.close()
    try:
        assert not sessions.old_text_cleared()
        assert sessions._db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        assert not sessions._db.in_transaction
        assert sessions.turn_count(live) == 41
        sessions.add_turn(live, 'user', 'Still works.')
        assert sessions.clear_old_text() is True
    finally:
        sessions._db.close()
    assert not any(marker in _file_bytes(path) for marker in _MARKERS)


def test_an_interrupted_rebuild_keeps_every_chat_and_is_retried(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)

    class Interrupted(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == 'VACUUM':
                raise sqlite3.OperationalError('disk I/O error')
            return super().execute(sql, *args)

    store_module = importlib.import_module('service.memory.store')   # the package exports a `store` too
    connect = sqlite3.connect
    with monkeypatch.context() as broken:
        broken.setattr(store_module.sqlite3, 'connect',
                       lambda *a, **k: connect(*a, factory=Interrupted, **k))
        sessions = SessionStore(path)
        assert sessions.clear_old_text() is False
    assert not sessions.old_text_cleared()
    sessions._db.close()
    facts, sessions, q = _open(path)
    try:
        assert sessions.turn_count(live) == 41
        assert {r['session_id'] for r in q.search('quentaborix', facts)} == {live}
        assert sessions.clear_old_text() is True
    finally:
        sessions._db.close()
        facts._db.close()
    assert not any(marker in _file_bytes(path) for marker in _MARKERS)


def test_a_damaged_file_is_left_alone(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'sessions.db'
    _old_build_file(path, monkeypatch)
    sessions = SessionStore(path)
    original = sessions._db

    class Damaged:
        """The connection, except that its integrity check reports damage."""
        def __getattr__(self, name):
            return getattr(original, name)

        def execute(self, sql, *args):
            if sql == 'PRAGMA quick_check':
                return original.execute("SELECT 'row 3 missing from index'")
            return original.execute(sql, *args)

    sessions._db = Damaged()
    try:
        assert sessions.clear_old_text() is False
        assert 'failed its check' in caplog.text
        assert not sessions.old_text_cleared()
    finally:
        original.close()


def test_a_file_missing_its_tables_never_raises(tmp_path):
    path = tmp_path / 'sessions.db'
    sessions = SessionStore(path)
    sessions._db.execute('DELETE FROM session_store_meta')
    sessions._db.commit()
    sessions._db.close()
    sessions._db = sqlite3.connect(':memory:')   # the tables are gone from under it
    try:
        assert sessions.clear_old_text() is False
    finally:
        sessions._db.close()


def test_startup_runs_the_rebuild_off_the_event_loop():
    from pathlib import Path
    import service
    source = (Path(service.__file__).parent / 'main.py').read_text()
    start = source.index('async def lifespan(')
    body = source[start:source.index('\napp = FastAPI(', start)]
    # In the background task beside the attachment prune, never awaited inline.
    task = body[body.index('async def _prune_attachment_store'):]
    assert 'await asyncio.to_thread(store.clear_old_text)' in task.split('asyncio.create_task(_prune_attachment_store())')[0]


def test_a_rebuild_that_fails_midway_keeps_the_old_search_index(tmp_path, monkeypatch):
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)
    facts, sessions, q = _open(path)
    import service.memory.facts as facts_module

    def interrupted(db, table):
        raise sqlite3.OperationalError('disk I/O error')

    try:
        with monkeypatch.context() as broken:
            broken.setattr(facts_module, 'secure_delete_fts', interrupted)
            assert sessions.clear_old_text() is False
        # The drop and the refill rolled back together: the index is still there.
        assert {r['session_id'] for r in q.search('quentaborix', facts)} == {live}
        assert not sessions.old_text_cleared()
        assert sessions.clear_old_text() is True
        assert {r['session_id'] for r in q.search('quentaborix', facts)} == {live}
    finally:
        sessions._db.close()
        facts._db.close()
    assert not any(marker in _file_bytes(path) for marker in _MARKERS)


def _spy_connection(monkeypatch, spy_class):
    store_module = importlib.import_module('service.memory.store')
    connect = sqlite3.connect
    monkeypatch.setattr(store_module.sqlite3, 'connect',
                        lambda *a, **k: connect(*a, factory=spy_class, **k))


def test_orphans_are_deleted_while_the_index_and_its_triggers_are_absent(tmp_path, monkeypatch):
    """Structural: no per-row index work for rows about to disappear."""
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)
    seen = {}

    class Watching(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith('DELETE FROM turns WHERE session_id NOT IN'):
                seen['index'] = super().execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'memory_turn_fts%'").fetchone()[0]
                seen['triggers'] = super().execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                    "AND sql LIKE '%memory_turn_fts%'").fetchone()[0]
            return super().execute(sql, *args)

    with monkeypatch.context() as watch:
        _spy_connection(watch, Watching)
        sessions = SessionStore(path)
    facts = FactStore(tmp_path / 'facts.db')
    q = MemoryQueue(sessions)
    try:
        assert sessions.clear_old_text() is True
        assert seen == {'index': 0, 'triggers': 0}
        # Back afterwards: index, triggers, and a new turn is indexed by them.
        assert sessions._db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'memory_turn_%'"
        ).fetchone()[0] == 3
        sessions.add_turn(live, 'user', 'A fresh kumquatzor note.')
        assert [r['session_id'] for r in q.search('kumquatzor', facts)] == [live]
        assert q.search('zorvaquillen', facts) == []
    finally:
        sessions._db.close()
        facts._db.close()


def test_a_crash_during_the_orphan_step_rolls_back_the_index_and_triggers(tmp_path, monkeypatch):
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)

    class Crashing(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith('DELETE FROM memory_jobs WHERE session_id NOT IN'):
                raise sqlite3.OperationalError('disk I/O error')
            return super().execute(sql, *args)

    with monkeypatch.context() as broken:
        _spy_connection(broken, Crashing)
        sessions = SessionStore(path)
        assert sessions.clear_old_text() is False
    names = {r[0] for r in sessions._db.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'memory_turn_%'")}
    assert {'memory_turn_fts', 'memory_turn_insert', 'memory_turn_delete',
            'memory_turn_update'} <= names
    assert not sessions.old_text_cleared() and not sessions._db.in_transaction
    sessions._db.close()
    facts, sessions, q = _open(path)
    try:
        assert sessions.turn_count(live) == 41
        assert {r['session_id'] for r in q.search('quentaborix', facts)} == {live}
        assert sessions.clear_old_text() is True
    finally:
        sessions._db.close()
        facts._db.close()


def test_a_file_without_its_index_is_repaired_on_open_and_cleared(tmp_path, monkeypatch):
    """What a hard kill between dropping the index and re-creating it would
    leave, were the steps not one transaction: opening repairs it anyway."""
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)
    raw = sqlite3.connect(str(path))
    for trig in ('memory_turn_insert', 'memory_turn_delete', 'memory_turn_update'):
        raw.execute(f'DROP TRIGGER {trig}')
    raw.execute('DROP TABLE memory_turn_fts')
    raw.commit()
    raw.close()
    facts, sessions, q = _open(path)
    try:
        assert {r['session_id'] for r in q.search('quentaborix', facts)} == {live}
        assert sessions.clear_old_text() is True
        assert {r['session_id'] for r in q.search('quentaborix', facts)} == {live}
    finally:
        sessions._db.close()
        facts._db.close()
    assert not any(marker in _file_bytes(path) for marker in _MARKERS)


def test_a_chat_saved_between_steps_is_indexed_and_survives_vacuum(tmp_path, monkeypatch):
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)
    facts, sessions, q = _open(path)
    real = sessions._lock
    steps = []

    class Releasing:
        """The store lock; after each release a chat is saved, as a request
        waiting for the lock would."""
        busy = False

        def __enter__(self):
            real.acquire()
            return self

        def __exit__(self, *exc):
            real.release()
            if not Releasing.busy:
                Releasing.busy = True
                try:
                    n = len(steps)
                    steps.append(n)
                    sessions.add_turn(live, 'user', f'Between steps {n}: marzipanquor{n}.')
                finally:
                    Releasing.busy = False

        def acquire(self, *a, **k):
            return real.acquire(*a, **k)

        def release(self):
            real.release()

    sessions._lock = Releasing()
    try:
        assert sessions.clear_old_text() is True
        done = len(steps)
        assert done >= 4                # the lock really was released between steps
        sessions._lock = real           # stop adding turns
        for n in range(done):
            assert [r['session_id'] for r in q.search(f'marzipanquor{n}', facts)] == [live], n
        assert sessions._index_matches_turns()
        assert sessions.turn_count(live) == 41 + done
    finally:
        sessions._db.close()
        facts._db.close()


def test_a_wal_that_cannot_be_checkpointed_leaves_no_marker(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch, wal=True)
    sessions = SessionStore(path)
    reader = sqlite3.connect(str(path))
    reader.execute('BEGIN')
    reader.execute('SELECT COUNT(*) FROM turns').fetchone()      # a read snapshot
    try:
        assert sessions.clear_old_text() is False
        assert not sessions.old_text_cleared()
        assert 'checkpointed' in caplog.text
    finally:
        reader.rollback()
        reader.close()
    try:
        assert sessions.clear_old_text() is True
        assert sessions.old_text_cleared()
        assert sessions.turn_count(live) == 41
        assert not any(marker in _file_bytes(path) for marker in _MARKERS)
    finally:
        sessions._db.close()
    assert not any(marker in _file_bytes(path) for marker in _MARKERS)


def test_too_little_disk_skips_the_start_before_any_costly_step(tmp_path, monkeypatch, caplog):
    import shutil
    path = tmp_path / 'sessions.db'
    live = _old_build_file(path, monkeypatch)
    seen = []

    class Spy(sqlite3.Connection):
        def execute(self, sql, *args):
            seen.append(sql)
            return super().execute(sql, *args)

    with monkeypatch.context() as spy:
        _spy_connection(spy, Spy)
        sessions = SessionStore(path)
    size = path.stat().st_size
    seen.clear()
    with monkeypatch.context() as full:
        full.setattr(shutil, 'disk_usage',
                     lambda p: shutil._ntuple_diskusage(10 * size, 9 * size, 2 * size))
        assert sessions.clear_old_text() is False
    assert 'MB free' in caplog.text
    assert not sessions.old_text_cleared()
    assert not any(s.startswith(('DELETE', 'DROP', 'VACUUM', 'PRAGMA quick_check')) for s in seen)
    assert sessions._db.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id='vanished'").fetchone()[0] == 1
    assert sessions.turn_count(live) == 41
    # Space comes back: the next start does the work.
    try:
        assert sessions.clear_old_text() is True
    finally:
        sessions._db.close()
    assert not any(marker in _file_bytes(path) for marker in _MARKERS)

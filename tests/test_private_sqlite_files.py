"""Wisp's SQLite stores are owner-only (0600) on disk.

They hold chat history (sessions.db), remembered facts (facts.db), synced mail
and message caches and the outbound queue (assistant.db), and learned behavior
(behavior.db, behavior_runtime.db). Before this, they were created with the
process umask (typically 0644 in a 0755 ~/.moe), so any other account on the
Mac could read them, unlike mail_memory.db and profile.json which were 0600.

No model, no server, no user data: every store opens under tmp_path.
"""
import os
import sqlite3
import stat

import pytest

from service.paths import private_sqlite_file


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)          # what a normal login shell hands the app
    try:
        yield
    finally:
        os.umask(old)


def test_helper_creates_a_missing_file_private(tmp_path, loose_umask):
    db = tmp_path / "new.db"
    private_sqlite_file(db)
    assert db.exists() and mode(db) == 0o600


def test_helper_tightens_an_existing_file_and_its_sidecars(tmp_path, loose_umask):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    for name in ("old.db", "old.db-wal", "old.db-shm"):
        os.chmod(tmp_path / name, 0o644)
    private_sqlite_file(db)
    for name in ("old.db", "old.db-wal", "old.db-shm"):
        assert mode(tmp_path / name) == 0o600, name
    assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]   # contents untouched
    conn.close()


def test_helper_ignores_memory_and_never_raises(tmp_path):
    private_sqlite_file(":memory:")
    private_sqlite_file(tmp_path / "missing-dir" / "x.db")       # logged, not raised


def test_new_sidecars_inherit_the_private_mode(tmp_path, loose_umask):
    db = tmp_path / "wal.db"
    private_sqlite_file(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    for name in ("wal.db", "wal.db-wal", "wal.db-shm"):
        if (tmp_path / name).exists():
            assert mode(tmp_path / name) == 0o600, name
    conn.close()


def _open_session_store(path):
    from service.memory.store import SessionStore
    return SessionStore(path)._db


def _open_fact_store(path):
    from service.memory.facts import FactStore
    return FactStore(path)._db


def _open_behavior_store(path):
    from service.behavior.store import Store
    return Store(path)._conn


def _open_outbound_queue(path):
    from service.assistant.outbound_queue import OutboundQueue
    return OutboundQueue(path)._db


@pytest.mark.parametrize("opener", [
    _open_session_store, _open_fact_store, _open_behavior_store, _open_outbound_queue])
def test_each_store_is_private_whether_new_or_upgraded(opener, tmp_path, loose_umask):
    fresh = tmp_path / "fresh.db"
    conn = opener(fresh)
    conn.close()
    assert mode(fresh) == 0o600
    # A file an older build left world-readable is tightened when it is opened.
    os.chmod(fresh, 0o644)
    conn = opener(fresh)
    conn.close()
    assert mode(fresh) == 0o600

"""Durability tests use only private synthetic SQLite state."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from tests.test_mini_resources import synthetic_capacity, synthetic_process

from mini.http import encode
from mini.protocol import KINDS, identity
from mini.store import CapacityError, Collision, CursorError, Store, StoreUnavailable


def store(tmp_path, **limits):
    return Store(tmp_path.resolve() / "private", "fixture-mini", **limits)


def staged(db, *, job="study", due=0, kind="study.generate", body="synthetic", proposal=None):
    db.register_job(job, kind)
    oid = db.stage_occurrence(job, due)
    return db.result(job, oid, kind, "Fixture title", body, proposal)


def test_restart_preserves_pending_complete_and_cursor(tmp_path):
    db = store(tmp_path)
    first = staged(db)
    pending = staged(db, due=3600)
    db.complete(first)
    page = db.page(limit=1)
    restarted = store(tmp_path)
    assert restarted.stage_occurrence("study", 0) == first["occurrence_id"]
    assert restarted.status()["pending_occurrences"] == 1
    assert restarted.page(page["next_cursor"])["next_cursor"] == page["next_cursor"]
    restarted.complete(pending)
    next_page = restarted.page(page["next_cursor"])
    assert next_page["results"] == [pending]
    assert next_page["next_cursor"] != page["next_cursor"]
    with restarted.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert restarted.path.stat().st_mode & 0o777 == 0o600


def test_retry_and_collision_at_full_capacity(tmp_path):
    db = store(tmp_path, max_results=1)
    first, second = staged(db), staged(db, due=3600)
    assert db.complete(first) == db.complete(first)
    with pytest.raises(Collision):
        db.complete({**first, "text": "conflicting"})
    with pytest.raises(Collision):
        db.complete({**first, "result_id": "alternate"})
    with pytest.raises(CapacityError):
        db.complete(second)
    assert db.status()["pending_occurrences"] == 1
    assert db.page()["results"] == [first]


def test_concurrent_same_result_and_last_capacity_slot(tmp_path):
    db = store(tmp_path, max_results=1)
    first, second = staged(db), staged(db, due=3600)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(db.complete, [first] * 4)) == [first["result_id"]] * 4
    assert db.status()["results"] == 1
    other = Store(tmp_path.resolve() / "other", "fixture-mini", max_results=1)
    a, b = staged(other), staged(other, due=3600)
    def attempt(row):
        try:
            other.complete(row)
            return "complete"
        except CapacityError:
            return "full"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, [a, b])) == ["complete", "full"]
    assert other.status()["results"] == 1 and other.status()["pending_occurrences"] == 1


def test_completion_publication_rollback_on_injected_failure(tmp_path):
    db = store(tmp_path)
    row = staged(db)
    with db.connect(write=True) as connection:
        connection.execute("""CREATE TRIGGER fail_completion BEFORE UPDATE ON occurrences
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
    with pytest.raises(StoreUnavailable, match="^Database unavailable$"):
        db.complete(row)
    assert db.page()["results"] == [] and db.status()["pending_occurrences"] == 1
    with db.connect(write=True) as connection:
        connection.execute("DROP TRIGGER fail_completion")
    assert store(tmp_path).complete(row) == row["result_id"]


def test_sqlite_full_rolls_back_and_preserves_pending(tmp_path):
    db = store(tmp_path)
    row = staged(db, body="x" * 100_000)
    # Keep this connection open: max_page_count is a connection setting.
    with db.connect() as control:
        pages = control.execute("PRAGMA page_count").fetchone()[0]
    original = db.connect
    from contextlib import contextmanager
    @contextmanager
    def capped(**kwargs):
        with original(**kwargs) as connection:
            connection.execute(f"PRAGMA max_page_count={pages}")
            yield connection
    db.connect = capped
    with pytest.raises(StoreUnavailable):
        db.complete(row)
    db.connect = original
    assert db.page()["results"] == [] and db.status()["pending_occurrences"] == 1


def test_capacity_bytes_and_occurrences_refuse_without_eviction(tmp_path):
    db = store(tmp_path, max_bytes=10, max_occurrences=1)
    row = staged(db)
    with pytest.raises(CapacityError):
        db.complete(row)
    assert db.stage_occurrence("study", 0) == row["occurrence_id"]
    with pytest.raises(CapacityError):
        db.stage_occurrence("study", 3600)
    assert db.status()["pending_occurrences"] == 1 and db.page()["results"] == []


@pytest.mark.parametrize("corruption", [b"", b"this is corrupt synthetic state"])
def test_corruption_never_reinitializes(tmp_path, corruption):
    db = store(tmp_path)
    db.complete(staged(db))
    db.path.write_bytes(corruption)
    with pytest.raises(StoreUnavailable):
        store(tmp_path)
    assert db.path.read_bytes() == corruption


def test_unknown_schema_identity_and_deleted_state_fail_closed(tmp_path):
    db = store(tmp_path)
    with pytest.raises(StoreUnavailable, match="identity"):
        Store(db.path.parent, "different-node")
    with db.connect(write=True) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(StoreUnavailable):
        store(tmp_path)
    db.path.unlink()
    with pytest.raises(StoreUnavailable):
        db.page()
    assert not db.path.exists()


def test_cursor_tampering_foreign_instance_and_rollback(tmp_path):
    db = store(tmp_path)
    first = staged(db)
    db.complete(first)
    cursor = db.page()["next_cursor"]
    with pytest.raises(CursorError):
        db.page(cursor[:-5] + "xxxxx")
    with pytest.raises(CursorError):
        db.page(cursor + "=")
    other = Store(tmp_path.resolve() / "other", "fixture-mini")
    with pytest.raises(CursorError):
        other.page(cursor)
    backup = tmp_path.resolve() / "backup.sqlite3"
    with db.connect() as source, sqlite3.connect(backup) as dest:
        source.backup(dest)
    db.complete(staged(db, due=3600))
    later = db.page()["next_cursor"]
    with sqlite3.connect(backup) as source, sqlite3.connect(db.path) as dest:
        source.backup(dest)
    with pytest.raises(CursorError):
        store(tmp_path).page(later)
    assert store(tmp_path).page(cursor)["results"] == []


def test_cursor_anchor_mismatch_and_bad_stored_size(tmp_path):
    db = store(tmp_path)
    db.complete(staged(db))
    cursor = db.page()["next_cursor"]
    with db.connect(write=True) as connection:
        connection.execute("DROP TRIGGER immutable_results_update")
        connection.execute("UPDATE results SET digest='tampered'")
    with pytest.raises(CursorError):
        db.page(cursor)
    with pytest.raises(StoreUnavailable):
        db.page()
    with db.connect(write=True) as connection:
        connection.execute("UPDATE results SET bytes=2000001")
    with pytest.raises(StoreUnavailable):
        db.page()


def test_immutable_schedule_and_effect_id_collisions(tmp_path):
    db = store(tmp_path)
    proposal = {"effect_id": "fixture-effect", "kind": "email.send", "arguments": {"to": "fixture@example.invalid"}}
    a = staged(db, job="effects", kind="effect.proposal", proposal=proposal)
    b = staged(db, job="effects", due=3600, kind="effect.proposal", proposal=proposal)
    db.complete(a)
    with pytest.raises(Collision):
        db.complete(b)
    with pytest.raises(Collision):
        db.register_job("effects", "study.generate")
    with pytest.raises(ValueError):
        db.stage_occurrence("effects", 1)
    assert identity("r_", "a:b", "c") != identity("r_", "a", "b:c")


def test_large_pages_match_actual_pro_protocol(tmp_path, monkeypatch):
    # Configure synthetic paths before importing the Pro inbox.
    monkeypatch.setenv("WISP_HOME", str(tmp_path.resolve() / "pro"))
    from service.nodes import NodeInbox, validate_result
    db = store(tmp_path)
    for i in range(30):
        kind = sorted(KINDS)[i % len(KINDS)]
        proposal = {"effect_id": f"effect-{i}", "kind": "calendar.write", "arguments": {"synthetic": True}} if kind == "effect.proposal" else None
        row = staged(db, job=kind, due=i * 3600, kind=kind, body="\u20ac" * 30_000, proposal=proposal)
        validate_result(row, db.node_id)
        db.complete(row)
    inbox = NodeInbox(tmp_path.resolve() / "pro" / "inbox.sqlite3")
    cursor, seen, pages = "", [], 0
    while True:
        page = store(tmp_path).page(cursor)
        assert len(encode(page)) < 2_000_000
        if not page["results"]:
            assert page["next_cursor"] == cursor
            break
        assert page["next_cursor"] != cursor
        inbox.ingest(db.node_id, page, expected_cursor=cursor)
        seen.extend(r["result_id"] for r in page["results"])
        cursor, pages = page["next_cursor"], pages + 1
    assert len(seen) == len(set(seen)) == 30 and pages > 1
    assert len(inbox.pending(db.node_id)) == 30


def test_abrupt_process_exit_recovers_committed_wal(tmp_path):
    root = tmp_path.resolve() / "crash-state"
    code = """
import os, sqlite3, sys
from mini.store import Store
s=Store(sys.argv[1], 'fixture-mini')
keeper=sqlite3.connect(s.path)
keeper.execute('PRAGMA wal_autocheckpoint=0')
keeper.execute('BEGIN')
keeper.execute('SELECT * FROM metadata').fetchall()
s.register_job('study','study.generate')
o=s.stage_occurrence('study',0)
s.complete(s.result('study',o,'study.generate','Fixture','Synthetic'))
os._exit(0)
"""
    completed = subprocess.run([sys.executable, "-B", "-c", synthetic_process(code, tmp_path), str(root)], capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr.decode()
    assert Path(str(root / "node.sqlite3") + "-wal").exists()
    db = Store(root, "fixture-mini")
    assert db.status()["results"] == 1 and db.status()["pending_occurrences"] == 0


def test_private_state_permissions_and_symlinks(tmp_path):
    shared = tmp_path.resolve() / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(StoreUnavailable):
        Store(shared, "fixture-mini")
    target = tmp_path.resolve() / "private"
    target.mkdir(mode=0o700)
    link = tmp_path.resolve() / "link"
    link.symlink_to(target)
    with pytest.raises(StoreUnavailable):
        Store(link, "fixture-mini")


@pytest.mark.parametrize('change', [
    'DROP TRIGGER immutable_results_update',
    'DROP TRIGGER immutable_results_delete',
    'CREATE INDEX unowned ON jobs(kind)',
    'CREATE TABLE unowned (secret TEXT)',
])
def test_startup_rejects_changed_owned_schema(tmp_path, change):
    store = Store(tmp_path.resolve()/'private', 'fixture')
    with store.connect(write=True) as db:
        db.execute(change)
    with pytest.raises(StoreUnavailable, match='schema'):
        Store(store.path.parent, 'fixture')


def test_occurrence_capacity_is_advertised(tmp_path):
    store = Store(tmp_path.resolve()/'private', 'fixture', max_occurrences=1)
    store.register_job('fixture', next(iter(KINDS)))
    store.stage_occurrence('fixture', 0)
    status = store.status()
    assert status['max_occurrences'] == 1 and status['capacity_reached'] is True

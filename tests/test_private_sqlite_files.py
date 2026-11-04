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


# ── assistant.db recovery backup (service/assistant/recovery.py) ──────────────
# The backup is a full copy of assistant.db. It used to be created by
# sqlite3.connect with the process umask, so it sat 0644 next to a 0600 store.

def _recovery():
    from service.assistant import recovery
    return recovery


def _conflicted_assistant_db(path, journal_mode="delete"):
    """A live and a legacy commitments table, as an interrupted migration leaves them."""
    recovery = _recovery()
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA journal_mode={journal_mode}")
    for table in (recovery.LIVE_TABLE, recovery.LEGACY_TABLE):
        recovery._create_merged_table(db)
        db.execute(f"ALTER TABLE {recovery.MERGED_TABLE} RENAME TO {table}")
    values = ("c1", "calendar", "s1", "event", "Dentist", None, None, None,
              1000.0, 0, None, None, None, "active", 1.0, 1.0, 1.0)
    db.execute(recovery._insert_sql(recovery.LEGACY_TABLE), values)
    db.commit()
    db.close()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_recovery_backup_is_private_from_the_moment_sqlite_opens_it(
        tmp_path, loose_umask, monkeypatch, journal_mode):
    recovery = _recovery()
    db = tmp_path / "assistant.db"
    _conflicted_assistant_db(db, journal_mode)
    backup = recovery._backup_path(db)
    seen = []
    real_connect = recovery.sqlite3.connect

    def watching_connect(target, *args, **kwargs):
        if str(backup) in str(target):
            seen.append(mode(backup))         # the mode SQLite finds when it opens the copy
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(recovery.sqlite3, "connect", watching_connect)
    report = recovery.repair(db, apply=True)
    assert report.state == "recovered" and report.backup == backup.resolve()
    assert seen and set(seen) == {0o600}
    for leftover in tmp_path.glob("assistant.db.recovery-backup*"):
        assert mode(leftover) == 0o600, leftover.name


def test_an_earlier_loose_recovery_backup_is_tightened_when_reused(tmp_path, loose_umask):
    recovery = _recovery()
    db = tmp_path / "assistant.db"
    _conflicted_assistant_db(db)
    backup = recovery._backup_path(db)
    source = sqlite3.connect(db)
    copy = sqlite3.connect(backup)
    source.backup(copy)
    copy.close()
    source.close()
    stale_journal = backup.with_name(backup.name + "-journal")
    stale_journal.write_bytes(b"")             # an empty journal is not hot
    for path in (backup, stale_journal):
        os.chmod(path, 0o644)                 # what a run before this fix left
    report = recovery.repair(db, apply=True)
    assert report.state == "recovered"
    assert mode(backup) == 0o600
    if stale_journal.exists():
        assert mode(stale_journal) == 0o600


def test_a_recovery_backup_sidecar_link_is_refused_not_followed(tmp_path, loose_umask):
    recovery = _recovery()
    db = tmp_path / "assistant.db"
    _conflicted_assistant_db(db)
    backup = recovery._backup_path(db)
    source = sqlite3.connect(db)
    copy = sqlite3.connect(backup)
    source.backup(copy)
    copy.close()
    source.close()
    outside = tmp_path / "someone-elses-file"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    backup.with_name(backup.name + "-wal").symlink_to(outside)
    with pytest.raises(recovery.RecoveryRefused, match="owner-only"):
        recovery.repair(db, apply=True)
    assert mode(outside) == 0o644             # the link target was not chmod-ed


def test_stale_loose_sidecars_are_tightened_when_a_new_backup_is_created(tmp_path, loose_umask):
    # A crashed pre-fix run, or a backup the user moved aside, can leave a 0644
    # -wal (row data) next to where the new backup is created.
    recovery = _recovery()
    db = tmp_path / "assistant.db"
    _conflicted_assistant_db(db, "wal")
    backup = recovery._backup_path(db)
    stale = backup.with_name(backup.name + "-shm")
    stale.write_bytes(b"\0" * 32768)
    os.chmod(stale, 0o644)
    assert recovery.repair(db, apply=True).state == "recovered"
    for leftover in tmp_path.glob("assistant.db.recovery-backup*"):
        assert mode(leftover) == 0o600, leftover.name


def test_a_failed_new_backup_keeps_sidecars_it_did_not_create(tmp_path, loose_umask, monkeypatch):
    recovery = _recovery()
    db = tmp_path / "assistant.db"
    _conflicted_assistant_db(db)
    backup = recovery._backup_path(db)
    kept = backup.with_name(backup.name + "-wal")
    kept.write_bytes(b"frames the user may still want")
    real_connect = recovery.sqlite3.connect

    def failing_connect(target, *args, **kwargs):
        if "recovery-backup" in str(target):
            raise sqlite3.OperationalError("injected")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(recovery.sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        recovery.repair(db, apply=True)
    assert not backup.exists()
    assert kept.read_bytes() == b"frames the user may still want" and mode(kept) == 0o600


def test_a_failed_recovery_backup_leaves_no_placeholder(tmp_path, loose_umask, monkeypatch):
    recovery = _recovery()
    db = tmp_path / "assistant.db"
    _conflicted_assistant_db(db)
    real_connect = recovery.sqlite3.connect

    def failing_connect(target, *args, **kwargs):
        if "recovery-backup" in str(target):
            raise sqlite3.OperationalError("injected")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(recovery.sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        recovery.repair(db, apply=True)
    assert not list(tmp_path.glob("assistant.db.recovery-backup*"))


def test_recovery_repair_tightens_a_loose_live_database(tmp_path, loose_umask):
    recovery = _recovery()
    db = tmp_path / "assistant.db"
    _conflicted_assistant_db(db)
    os.chmod(db, 0o644)
    assert recovery.repair(db, apply=True).state == "recovered"
    assert mode(db) == 0o600
    # inspect() stays read-only, metadata included
    os.chmod(db, 0o644)
    recovery.inspect(db)
    assert mode(db) == 0o644


# ── LoRA dataset exports of sessions.db (developer scripts) ──────────────────
# build_lora_dataset.py writes the user's chat turns (with real tool arguments)
# out of sessions.db; merge_lora_data.py copies those rows again. Both wrote
# 0644 files into a 0755 directory under ~.

def _load_script(name):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_wisp_script_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_merged_lora_export_is_owner_only_even_over_an_older_loose_run(
        tmp_path, loose_umask, monkeypatch):
    merge = _load_script("merge_lora_data")
    real = tmp_path / "real"
    real.mkdir()
    (real / "train.jsonl").write_text('{"messages": [{"role": "user", "content": "hi"}]}\n')
    out = tmp_path / "merged"
    out.mkdir(mode=0o755)
    os.chmod(out, 0o755)
    (out / "train.jsonl").write_text("old\n")
    os.chmod(out / "train.jsonl", 0o644)
    monkeypatch.setattr("sys.argv", ["merge_lora_data.py", "--harvest", str(tmp_path / "none"),
                                     "--real-usage", str(real), "--out", str(out)])
    assert merge.main() == 0
    assert mode(out) == 0o755                 # an existing --out (even ~) is not re-moded
    for name in ("train.jsonl", "valid.jsonl", "test.jsonl"):
        assert mode(out / name) == 0o600, name
    assert "old" not in (out / "train.jsonl").read_text()
    fresh = tmp_path / "fresh" / "merged"
    monkeypatch.setattr("sys.argv", ["merge_lora_data.py", "--harvest", str(tmp_path / "none"),
                                     "--real-usage", str(real), "--out", str(fresh)])
    assert merge.main() == 0
    assert mode(fresh) == 0o700               # one it creates is owner-only


def test_lora_dataset_export_writes_private_files_and_never_creates_the_store(
        tmp_path, loose_umask, monkeypatch):
    import asyncio
    build = _load_script("build_lora_dataset")
    out = build._private_dir(tmp_path / "lora")
    build._write_private(out / "train.jsonl", "x\n")
    assert mode(out) == 0o700 and mode(out / "train.jsonl") == 0o600
    missing = tmp_path / "no-moe" / "sessions.db"
    missing.parent.mkdir()
    monkeypatch.setattr(build, "SESSIONS_DB", missing)
    monkeypatch.setattr(build, "AUDIT_LOG", tmp_path / "no-moe" / "audit.jsonl")
    monkeypatch.setattr("sys.argv", ["build_lora_dataset.py", "--out", str(tmp_path / "lora2")])
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(build.main())
    assert not missing.exists()               # read-only: no empty 0644 sessions.db

"""SQLite-safe snapshots; restore publishes a NEW private state directory only.

Restore never replaces a running/existing store. Atomic directory rename makes
publication all-or-nothing; interrupted staging remains private and retryable.
Default identity rotation invalidates every old cursor while retaining immutable
result IDs for consumer deduplication. Preserve is explicit and only for resuming
this snapshot with consumers whose cursors are not ahead of it.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import tempfile

from mini.http import encode
from mini.protocol import identity, validate
from mini.resources import GB, integer, volume_lease
from mini.store import SCHEMA, StoreUnavailable, CapacityError, private_directory


@contextmanager
def operation_lock(parent):
    fd = os.open(parent / "snapshot.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise StoreUnavailable("Unsafe snapshot lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StoreUnavailable("Snapshot operation busy") from None
        yield
    finally:
        os.close(fd)


def capacity(parent, growth, *, preflight=False):
    integer(growth)
    fs = os.statvfs(parent)
    free = fs.f_bavail * fs.f_frsize
    if free < max(150*GB if preflight else 50*GB, 50*GB + growth):
        raise CapacityError("Snapshot storage capacity refused")


def fsync(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_database(db, node_id, *, max_results=10000, max_bytes=100000000, max_occurrences=20000):
    if db.execute("PRAGMA user_version").fetchone()[0] != 2 or db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise StoreUnavailable("Invalid snapshot integrity or version")
    with sqlite3.connect(":memory:") as expected:
        for sql in SCHEMA:
            expected.execute(sql)
        catalog = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        if db.execute(catalog).fetchall() != expected.execute(catalog).fetchall():
            raise StoreUnavailable("Invalid snapshot schema")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise StoreUnavailable("Invalid snapshot relationships")
    if (db.execute("SELECT count(*) FROM occurrences").fetchone()[0] > max_occurrences
            or db.execute("SELECT count(*) FROM results").fetchone()[0] > max_results
            or db.execute("SELECT count(*) FROM jobs").fetchone()[0] > 100):
        raise CapacityError("Snapshot record capacity refused")
    # Check SQL storage classes and actual byte lengths BEFORE fetching/parsing
    # payloads. Declared bytes are untrusted until canonical validation below.
    if (db.execute("""SELECT 1 FROM results WHERE typeof(payload)!='text'
            OR length(CAST(payload AS BLOB))>200000 OR typeof(bytes)!='integer'
            OR bytes!=length(CAST(payload AS BLOB)) LIMIT 1""").fetchone()
            or db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM results").fetchone()[0] > max_bytes
            or db.execute("""SELECT 1 FROM runtime_inputs WHERE typeof(payload)!='text'
                OR length(CAST(payload AS BLOB))>100000 LIMIT 1""").fetchone()):
        raise CapacityError("Snapshot payload capacity refused")
    meta = dict(db.execute("SELECT key,value FROM metadata"))
    if set(meta) != {"node_id", "instance", "cursor_key"} or meta["node_id"] != node_id:
        raise StoreUnavailable("Snapshot identity mismatch")
    try:
        if len(bytes.fromhex(meta["instance"])) != 16 or len(bytes.fromhex(meta["cursor_key"])) != 32:
            raise ValueError
        jobs = {row[0]: row[1:] for row in db.execute("SELECT job_id,kind,interval_s,first_due,enabled FROM jobs")}
        if len(jobs) > 100:
            raise CapacityError("Snapshot job capacity refused")
        from mini.protocol import KINDS, text
        for jid, (kind, interval, first, enabled) in jobs.items():
            text(jid)
            if kind not in KINDS or enabled != 0:
                raise ValueError
            integer(interval, 1, 2**53)
            integer(first, 0, 2**53)
        occurrences = {}
        for oid, jid, due, state in db.execute("SELECT * FROM occurrences"):
            integer(due, 0, 2**53)
            kind, interval, first, _ = jobs[jid]
            if (oid != identity("o_", node_id, jid, due) or due < first or (due-first) % interval
                    or state not in ("pending", "complete")):
                raise ValueError
            occurrences[oid] = (jid, kind, state)
        if len(occurrences) > max_occurrences:
            raise CapacityError("Snapshot occurrence capacity refused")
        total = 0
        complete = set()
        for seq, rid, oid, payload, digest, effect_id, size in db.execute("SELECT * FROM results"):
            integer(seq, 1)
            result = json.loads(payload)
            canonical, expected_digest = validate(result, node_id)
            jid, kind, state = occurrences[oid]
            if (payload != canonical or digest != expected_digest or size != len(payload.encode())
                    or rid != identity("r_", node_id, jid, oid) or result["result_id"] != rid
                    or result["occurrence_id"] != oid or result["job_id"] != jid or result["kind"] != kind
                    or state != "complete" or effect_id != (result["proposal"]["effect_id"] if result["proposal"] else None)):
                raise ValueError
            total += size
            complete.add(oid)
        if len(complete) > max_results or total > max_bytes:
            raise CapacityError("Snapshot result capacity refused")
        if complete != {oid for oid, (_, _, state) in occurrences.items() if state == "complete"}:
            raise ValueError
        from mini.adapters import SnapshotAdapter, REVISION
        for oid, payload in db.execute("SELECT * FROM runtime_inputs"):
            kind = occurrences[oid][1]
            adapter = SnapshotAdapter(kind, enabled=True, qualification={"kind": kind, "revision": REVISION, "scope": "portable-snapshot-only"})
            stored = json.loads(payload)
            if set(stored) not in ({"adapter_revision", "snapshot"}, {"adapter_revision", "snapshot", "acquisition"}) or stored["adapter_revision"] != REVISION:
                raise ValueError
            if "acquisition" in stored:
                from mini.runtime import Runtime
                Runtime.validate_acquisition(stored["acquisition"], stored["snapshot"],
                                             node_id=node_id, job_id=occurrences[oid][0], kind=kind)
            adapter.validate(stored["snapshot"])
            if encode(stored).decode() != payload or len(payload.encode()) > 100000:
                raise ValueError
        # Ensure the autoincrement sequence cannot reuse published sequence IDs.
        sequence = dict(db.execute("SELECT name,seq FROM sqlite_sequence"))
        maximum = db.execute("SELECT coalesce(max(seq),0) FROM results").fetchone()[0]
        if set(sequence) - {"results"} or sequence.get("results", 0) != maximum:
            raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise StoreUnavailable("Invalid snapshot content") from None
    return meta


def publish(source, destination):
    """One atomic no-replace rename. Unsupported filesystems/platforms refuse."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        raise StoreUnavailable("Atomic snapshot publication unavailable")
    function.restype = ctypes.c_int
    if function(*args) != 0:
        raise StoreUnavailable("Atomic snapshot publication refused")


def backup(store, destination):
    destination = Path(destination)
    parent = private_directory(destination.parent)
    if destination.name in ("node.sqlite3", "snapshot.lock") or destination.exists() or destination.is_symlink():
        raise StoreUnavailable("Snapshot destination already exists or is reserved")
    temp = None
    try:
        with volume_lease(parent), operation_lock(parent), store.connect() as source:
            pages = source.execute("PRAGMA page_count").fetchone()[0]
            size = pages * source.execute("PRAGMA page_size").fetchone()[0]
            capacity(parent, size * 2 + 1048576, preflight=True)
            fd, name = tempfile.mkstemp(prefix=".backup-", dir=parent)
            os.close(fd)
            temp = Path(name)
            with sqlite3.connect(temp) as target:
                source.backup(target, pages=100, progress=lambda *_: capacity(parent, 1048576))
                validate_database(target, store.node_id, max_results=store.max_results,
                                  max_bytes=store.max_bytes, max_occurrences=store.max_occurrences)
                target.execute("PRAGMA journal_mode=DELETE")
            fsync(temp)
            capacity(parent, 0)
            publish(temp, destination)
            temp = None
            fsync(parent)
        return destination
    except sqlite3.Error:
        raise StoreUnavailable("Snapshot database unavailable") from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def restore(snapshot, destination, node_id, *, identity_mode="rotate"):
    if identity_mode not in ("rotate", "preserve"):
        raise ValueError("Invalid restore identity mode")
    snapshot, destination = Path(snapshot), Path(destination)
    parent = private_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise StoreUnavailable("Restore requires a new destination")
    # Use a pinned no-follow file descriptor. SQLite opens that same inode via
    # /dev/fd on macOS/POSIX; immutable is safe only for the published standalone
    # backup, never for a live WAL database.
    fd = os.open(snapshot, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    staging = None
    try:
        info = os.fstat(fd)
        if (not snapshot.is_absolute() or any(p.is_symlink() for p in snapshot.parents)
                or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid() or info.st_nlink != 1):
            raise StoreUnavailable("Unsafe snapshot file")
        if any(Path(str(snapshot) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise StoreUnavailable("Restore requires a standalone backup")
        with volume_lease(parent), operation_lock(parent):
            capacity(parent, info.st_size * 3 + 1048576, preflight=True)
            staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=parent))
            target_path = staging / "node.sqlite3"
            # Copy the opened immutable backup into staging using the SQLite
            # backup API after pinning and validating a read transaction.
            with sqlite3.connect(f"file:/dev/fd/{fd}?mode=ro&immutable=1", uri=True) as source:
                source.execute("BEGIN")
                validate_database(source, node_id)
                with sqlite3.connect(target_path) as target:
                    target_path.chmod(0o600)
                    source.backup(target, pages=100, progress=lambda *_: capacity(parent, 1048576))
                    if identity_mode == "rotate":
                        target.execute("UPDATE metadata SET value=? WHERE key='instance'", (secrets.token_hex(16),))
                        target.execute("UPDATE metadata SET value=? WHERE key='cursor_key'", (secrets.token_hex(32),))
                        target.commit()
                    validate_database(target, node_id)
                    target.execute("PRAGMA journal_mode=DELETE")
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise StoreUnavailable("Snapshot changed during restore")
            fsync(target_path)
            fsync(staging)
            capacity(parent, 0)
            publish(staging, destination)
            staging = None
            fsync(parent)
        return destination
    except sqlite3.Error:
        raise StoreUnavailable("Restore database unavailable") from None
    finally:
        os.close(fd)
        if staging is not None:
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--identity", choices=("rotate", "preserve"), default="rotate")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.operation == "backup":
            from mini.store import Store
            if not (Path(args.state_dir) / "node.sqlite3").is_file():
                raise StoreUnavailable("Backup requires existing state")
            backup(Store(args.state_dir, args.node_id), args.snapshot)
        else:
            restore(args.snapshot, args.state_dir, args.node_id, identity_mode=args.identity)
    except Exception:
        print("mini-backup: operation refused")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

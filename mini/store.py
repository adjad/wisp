"""Private append-only WAL store. Completion and publication are one commit.

Runtime scheduling is disabled by default. Staging/completion are private
owned interfaces, never HTTP APIs.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from mini.http import encode
from mini.protocol import KINDS, identity, text, validate


class StoreUnavailable(RuntimeError):
    pass


class Collision(ValueError):
    pass


class CapacityError(RuntimeError):
    pass


class CursorError(ValueError):
    pass


SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE jobs (job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
        interval_s INTEGER NOT NULL CHECK(interval_s>0), first_due INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled=0))""",
    """CREATE TABLE occurrences (occurrence_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id), due INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','complete')),
        UNIQUE(job_id,due))""",
    """CREATE TABLE results (seq INTEGER PRIMARY KEY AUTOINCREMENT,
        result_id TEXT NOT NULL UNIQUE, occurrence_id TEXT NOT NULL UNIQUE REFERENCES occurrences(occurrence_id),
        payload TEXT NOT NULL, digest TEXT NOT NULL, effect_id TEXT UNIQUE,
        bytes INTEGER NOT NULL CHECK(bytes>0))""",
    "CREATE TRIGGER immutable_results_update BEFORE UPDATE ON results BEGIN SELECT RAISE(ABORT,'immutable result'); END",
    "CREATE TRIGGER immutable_results_delete BEFORE DELETE ON results BEGIN SELECT RAISE(ABORT,'immutable result'); END",
)


LEGACY_SCHEMA = SCHEMA
SCHEMA = SCHEMA + (
    """CREATE TABLE runtime_inputs (occurrence_id TEXT PRIMARY KEY REFERENCES occurrences(occurrence_id),
        payload TEXT NOT NULL)""",
    "CREATE TRIGGER immutable_inputs_update BEFORE UPDATE ON runtime_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END",
    "CREATE TRIGGER immutable_inputs_delete BEFORE DELETE ON runtime_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END",
)


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise StoreUnavailable("Invalid state directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.stat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise StoreUnavailable("State directory must be private")
    return path


class Store:
    def __init__(self, state_dir, node_id, *, max_results=10_000, max_bytes=100_000_000, max_occurrences=20_000):
        self.node_id = text(node_id)
        if any(type(n) is not int or n < 1 for n in (max_results, max_bytes, max_occurrences)):
            raise ValueError("Invalid store capacity")
        self.max_results, self.max_bytes, self.max_occurrences = max_results, max_bytes, max_occurrences
        self.path = private_directory(state_dir) / "node.sqlite3"
        # Refuse non-regular, shared, or symlink state; never overwrite corruption.
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if path.is_symlink():
                raise StoreUnavailable("Invalid state file")
            if path.exists():
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                    raise StoreUnavailable("State file must be private")
                if path == self.path and info.st_size < 100:
                    raise StoreUnavailable("Invalid existing database")
        self.require_capacity(preflight=True)
        created = False
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
            created = True
        with self.connect(write=True, initializing=True) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreUnavailable("Invalid database")
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not tables and version == 0 and created:
                for sql in SCHEMA:
                    db.execute(sql)
                db.executemany("INSERT INTO metadata VALUES (?,?)", [
                    ("node_id", self.node_id), ("instance", secrets.token_hex(16)),
                    ("cursor_key", secrets.token_hex(32)),
                ])
                db.execute("PRAGMA user_version=2")
            elif version == 1 and tables:
                with sqlite3.connect(":memory:") as expected:
                    for sql in LEGACY_SCHEMA:
                        expected.execute(sql)
                    catalog = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                    if [tuple(r) for r in db.execute(catalog)] != list(expected.execute(catalog)):
                        raise StoreUnavailable("Unexpected legacy database schema")
                for sql in SCHEMA[len(LEGACY_SCHEMA):]:
                    db.execute(sql)
                db.execute("PRAGMA user_version=2")
            elif version != 2 or not tables:
                raise StoreUnavailable("Unsupported database version")
            # Compare SQLite's own canonical catalog from the exact owned DDL.
            # This includes autoindexes, constraints, and immutable triggers.
            with sqlite3.connect(":memory:") as expected:
                for sql in SCHEMA:
                    expected.execute(sql)
                catalog = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                if [tuple(r) for r in db.execute(catalog)] != list(expected.execute(catalog)):
                    raise StoreUnavailable("Unexpected database schema")
            meta = dict(db.execute("SELECT key,value FROM metadata"))
            if set(meta) != {"node_id", "instance", "cursor_key"} or meta["node_id"] != self.node_id:
                raise StoreUnavailable("Database identity mismatch")
            try:
                self.key = bytes.fromhex(meta["cursor_key"])
                self.instance = meta["instance"]
                if len(self.key) != 32 or len(bytes.fromhex(self.instance)) != 16:
                    raise ValueError
            except ValueError:
                raise StoreUnavailable("Invalid database identity") from None
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise StoreUnavailable("Invalid database relationships")

    def require_capacity(self, growth=0, *, preflight=False):
        from mini.resources import GB, integer
        integer(growth)
        try:
            fs = os.statvfs(self.path.parent)
            if fs.f_bavail * fs.f_frsize < max(150*GB if preflight else 50*GB, 50*GB + growth):
                raise CapacityError("Permanent storage reserve refused")
        except OSError:
            raise StoreUnavailable("Storage capacity unavailable") from None

    @contextmanager
    def connect(self, *, write=False, initializing=False):
        db = None
        volume = None
        try:
            if write:
                from mini.resources import volume_lease
                lease = volume_lease(self.path.parent, timeout=0.25)
                lease.__enter__()
                volume = lease
            # mode=rw prevents accidental recreation if state disappears at runtime.
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.25, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if initializing:
                if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise StoreUnavailable("WAL unavailable")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                self.require_capacity(1048576)
            yield db
            if write:
                self.require_capacity()
            db.commit()
        except sqlite3.Error:
            if db is not None:
                db.rollback()
            raise StoreUnavailable("Database unavailable") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()
            if volume is not None:
                volume.__exit__(None, None, None)

    def register_job(self, job_id, kind, *, interval_s=3600, first_due=0):
        text(job_id)
        if kind not in KINDS or type(interval_s) is not int or not 1 <= interval_s <= 2**53 or type(first_due) is not int or not 0 <= first_due <= 2**53:
            raise ValueError("Invalid schedule")
        with self.connect(write=True) as db:
            row = db.execute("SELECT kind,interval_s,first_due FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row and tuple(row) != (kind, interval_s, first_due):
                raise Collision("Immutable job identity conflict")
            if not row and db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 100:
                raise CapacityError("Job capacity reached")
            db.execute("INSERT OR IGNORE INTO jobs(job_id,kind,interval_s,first_due) VALUES(?,?,?,?)",
                       (job_id, kind, interval_s, first_due))

    def stage_occurrence(self, job_id, due):
        """Persist a scheduled UTC second; retries use the same immutable ID.

        Only explicit internal staging is provided. The running node never calls
        this method, and disabled jobs cannot become enabled through configuration.
        """
        if type(due) is not int or not 0 <= due <= 2**53:
            raise ValueError("Invalid schedule time")
        occurrence_id = identity("o_", self.node_id, job_id, due)
        with self.connect(write=True) as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job or due < job["first_due"] or (due - job["first_due"]) % job["interval_s"]:
                raise ValueError("Occurrence is outside schedule")
            row = db.execute("SELECT job_id,due FROM occurrences WHERE occurrence_id=?", (occurrence_id,)).fetchone()
            if row and tuple(row) != (job_id, due):
                raise Collision("Occurrence identity conflict")
            if not row and db.execute("SELECT count(*) FROM occurrences").fetchone()[0] >= self.max_occurrences:
                raise CapacityError("Occurrence capacity reached")
            db.execute("INSERT OR IGNORE INTO occurrences VALUES(?,?,?,'pending')", (occurrence_id, job_id, due))
        return occurrence_id

    def result(self, job_id, occurrence_id, kind, title, body, proposal=None):
        return {"schema_version": 1, "node_id": self.node_id,
                "result_id": identity("r_", self.node_id, job_id, occurrence_id),
                "job_id": job_id, "occurrence_id": occurrence_id, "kind": kind,
                "title": title, "text": body, "proposal": proposal}

    def complete(self, result):
        payload, digest = validate(result, self.node_id)
        rid, oid, jid = result["result_id"], result["occurrence_id"], result["job_id"]
        if rid != identity("r_", self.node_id, jid, oid):
            raise Collision("Result identity conflict")
        with self.connect(write=True) as db:
            job = db.execute("""SELECT o.state,j.kind FROM occurrences o JOIN jobs j USING(job_id)
                WHERE o.occurrence_id=? AND o.job_id=?""", (oid, jid)).fetchone()
            if not job or job["kind"] != result["kind"]:
                raise Collision("Result occurrence mismatch")
            old = db.execute("SELECT digest,payload FROM results WHERE result_id=? OR occurrence_id=?", (rid, oid)).fetchone()
            if old:
                if old["digest"] != digest or old["payload"] != payload or job["state"] != "complete":
                    raise Collision("Immutable result conflict")
                return rid
            if job["state"] != "pending":
                raise Collision("Occurrence already completed")
            effect_id = result["proposal"]["effect_id"] if result["proposal"] else None
            if effect_id and db.execute("SELECT 1 FROM results WHERE effect_id=?", (effect_id,)).fetchone():
                raise Collision("Effect identity conflict")
            count, size = db.execute("SELECT count(*),coalesce(sum(bytes),0) FROM results").fetchone()
            if count >= self.max_results or size + len(payload.encode()) > self.max_bytes:
                raise CapacityError("Result capacity reached")
            self.require_capacity(len(payload.encode()) * 4 + 1048576)
            db.execute("INSERT INTO results(result_id,occurrence_id,payload,digest,effect_id,bytes) VALUES(?,?,?,?,?,?)",
                       (rid, oid, payload, digest, effect_id, len(payload.encode())))
            db.execute("UPDATE occurrences SET state='complete' WHERE occurrence_id=?", (oid,))
        return rid

    def _cursor(self, seq, digest):
        payload = encode([1, self.instance, seq, digest])
        signature = hmac.digest(self.key, payload, "sha256")
        return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode()

    def _position(self, db, cursor):
        if cursor == "":
            return 0
        try:
            if not isinstance(cursor, str) or len(cursor) > 1024 or "=" in cursor:
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, payload, "sha256")):
                raise ValueError
            version, instance, seq, digest = json.loads(payload)
            if type(version) is not int or version != 1 or instance != self.instance or type(seq) is not int or seq < 1:
                raise ValueError
            row = db.execute("SELECT digest FROM results WHERE seq=?", (seq,)).fetchone()
            if row is None or row[0] != digest or self._cursor(seq, digest) != cursor:
                raise ValueError
            return seq
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise CursorError("Invalid or rolled-back cursor") from None

    def page(self, cursor="", limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid page limit")
        with self.connect() as db:
            position = self._position(db, cursor)
            page = {"schema_version": 1, "results": [], "next_cursor": cursor}
            size = 2048  # envelope + maximum cursor + commas; Pro cap is 2MB
            for row in db.execute("SELECT * FROM results WHERE seq>? ORDER BY seq LIMIT ?", (position, limit)):
                try:
                    result = json.loads(row["payload"])
                    payload, digest = validate(result, self.node_id)
                    if payload != row["payload"] or digest != row["digest"] or len(payload.encode()) != row["bytes"]:
                        raise ValueError
                except (ValueError, TypeError, RecursionError):
                    raise StoreUnavailable("Invalid stored result") from None
                if size + row["bytes"] > 1_900_000:
                    break
                page["results"].append(result)
                page["next_cursor"] = self._cursor(row["seq"], row["digest"])
                size += row["bytes"] + 1
            return page

    def status(self):
        with self.connect() as db:
            results, size = db.execute("SELECT count(*),coalesce(sum(bytes),0) FROM results").fetchone()
            occurrences = db.execute("SELECT count(*) FROM occurrences").fetchone()[0]
            pending = db.execute("SELECT count(*) FROM occurrences WHERE state='pending'").fetchone()[0]
            return {"schema_version": 1, "status": "ok", "jobs_enabled": False,
                    "connectors_enabled": False, "effects_enabled": False,
                    "results": results, "pending_occurrences": pending,
                    "max_occurrences": self.max_occurrences,
                    "capacity_reached": occurrences >= self.max_occurrences or results >= self.max_results or size >= self.max_bytes}

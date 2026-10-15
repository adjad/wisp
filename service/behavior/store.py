"""SQLite persistence for the behavior engine.

One file per engine (``db_path``), WAL so readers on other threads can read while
a mine/snapshot writes. Everything the engine knows lives here: the observation
timeline, the last snapshot per source (for cancel/move diffing), mined patterns
with the user's verdict on each, nudges with a monotonically increasing ``seq``
cursor for the app, and nudge feedback.

Rows hold JSON blobs for the dataclasses in ``types`` so the contract can grow
additively without migrations; the few columns next to them exist only for
filtering and ordering.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from service.behavior.types import Nudge, Observation, Pattern

SCHEMA_VERSION = 2
log = logging.getLogger(__name__)
# Retention (applied by prune(), which the engine runs during mine()). Mining
# looks back ~16 weeks; overlapping Calendar occupancy remains report evidence.
OBSERVATION_RETENTION_S = 180 * 86400
NUDGE_RETENTION_S = 30 * 86400
# A scheduled interval remains relevant until its end ages out. Other kinds
# retain their original timestamp limit; invalid/unbounded ends grant no lease.
OBSERVATION_EXPIRY_SQL = """DELETE FROM observations WHERE
 CASE WHEN kind='event' AND typeof(end_ts) IN ('integer','real')
 AND end_ts > ts AND end_ts <= 1.7976931348623157e308
 THEN end_ts ELSE ts END < ?"""
PATTERN_STATUSES = ("active", "confirmed", "rejected")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, ts REAL NOT NULL, end_ts REAL,
    subject TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL DEFAULT '',
    attrs TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS obs_kind_ts ON observations(kind, ts);
CREATE INDEX IF NOT EXISTS obs_ts ON observations(ts);
CREATE TABLE IF NOT EXISTS snapshots (
    source TEXT PRIMARY KEY, taken_at REAL NOT NULL, window_start REAL NOT NULL,
    window_end REAL NOT NULL, items TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patterns (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', confidence REAL NOT NULL DEFAULT 0,
    json TEXT NOT NULL, updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nudges (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS nudges_created ON nudges(created_at);
CREATE TABLE IF NOT EXISTS feedback (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT, nudge_id TEXT NOT NULL, verdict TEXT NOT NULL,
    ts REAL NOT NULL, kind TEXT NOT NULL DEFAULT '', pattern_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feedback_ts ON feedback(ts);
CREATE TABLE IF NOT EXISTS source_coverage (
    source TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL,
    state TEXT NOT NULL, observed_at REAL NOT NULL,
    PRIMARY KEY(source, start_ts, end_ts)
);
"""


def _obs_row(o: Observation) -> tuple:
    return (o.id, o.kind, float(o.ts), None if o.end_ts is None else float(o.end_ts),
            o.subject or "", o.source or "", o.category or "", o.source_id or "",
            json.dumps(o.attrs or {}, sort_keys=True, default=str))


def _row_obs(r: sqlite3.Row) -> Observation:
    return Observation(id=r["id"], kind=r["kind"], ts=r["ts"], subject=r["subject"],
                       source=r["source"], end_ts=r["end_ts"], category=r["category"],
                       source_id=r["source_id"], attrs=json.loads(r["attrs"] or "{}"))


class Store:
    """Thread-safe: a single connection guarded by an RLock (the runtime calls it
    from worker threads; SQLite connections are not safe to share without it)."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        if str(db_path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock:
            self._open(str(db_path))
            found = self._schema_version()
            if found is not None and found != SCHEMA_VERSION and not self._migrate(found):
                # A db written by newer code (or with an unreadable version) is
                # moved aside, never half-read or overwritten: the engine
                # restarts empty and re-learns from the next ingest.
                self._conn.close()
                moved = self._move_aside(found)
                log.warning("behavior db schema %r != %d; moved to %s and starting fresh",
                            found, SCHEMA_VERSION, moved)
                self._open(str(db_path))
                found = None
            if found is None:
                self.meta_set("schema_version", SCHEMA_VERSION)

    def _open(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass  # e.g. :memory: or a filesystem without shared memory
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def _schema_version(self) -> int | str | None:
        """Stored version as int; the raw value when it is not an int; None if absent."""
        try:
            found = self.meta_get("schema_version")
        except ValueError:
            return "unreadable"
        if found is None:
            return None
        try:
            return int(found)
        except (TypeError, ValueError):
            return str(found)

    def _migrate(self, found: int | str) -> bool:
        """Upgrade an older schema in place. Returns False when it cannot
        (newer/unknown version). Version 1 is the first schema, so the only
        "migration" today is none; add steps here as SCHEMA_VERSION grows."""
        if isinstance(found, int) and found < SCHEMA_VERSION:
            # (no older schemas exist yet; the CREATE IF NOT EXISTS above is additive)
            self.meta_set("schema_version", SCHEMA_VERSION)
            return True
        return False

    def _move_aside(self, found: int | str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.schema-{found}-{stamp}.bak")
        if str(self.path) == ":memory:":
            return target
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                src.rename(Path(str(target) + suffix))
        return target

    def prune(self, now: float, *, observation_retention_s: float = OBSERVATION_RETENTION_S,
              nudge_retention_s: float = NUDGE_RETENTION_S) -> dict[str, int]:
        """Age points by timestamp and valid scheduled intervals by their end.

        Apply the 180-day observation and 30-day nudge limits
        (seq never goes backwards: AUTOINCREMENT keeps the counter)."""
        with self._lock, self._conn:
            obs = self._conn.execute(OBSERVATION_EXPIRY_SQL,
                                     (now - observation_retention_s,)).rowcount
            nud = self._conn.execute("DELETE FROM nudges WHERE created_at < ?",
                                     (now - nudge_retention_s,)).rowcount
        return {"observations": int(obs), "nudges": int(nud)}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_coverage(self, source: str, start: float, end: float, state: str, now: float) -> None:
        import math
        if (state not in {"complete", "partial", "unavailable"} or not source
                or not all(math.isfinite(v) for v in (start, end, now)) or end <= start):
            raise ValueError("Invalid source coverage")
        with self._lock, self._conn:
            self._write_coverage(source,start,end,state,now)

    def _write_coverage(self, source, start, end, state, now):
        """Caller holds the transaction. Merge complete intervals, keeping gaps."""
        if state == "complete":
            rows=self._conn.execute("SELECT start_ts,end_ts FROM source_coverage WHERE source=? "
                                    "AND state='complete' AND start_ts<=? AND end_ts>=?",(source,end,start)).fetchall()
            if rows:
                start=min(start,*(r[0] for r in rows)); end=max(end,*(r[1] for r in rows))
                self._conn.execute("DELETE FROM source_coverage WHERE source=? AND state='complete' "
                                   "AND start_ts<=? AND end_ts>=?",(source,end,start))
        self._conn.execute("INSERT INTO source_coverage VALUES (?,?,?,?,?) ON CONFLICT(source,start_ts,end_ts) "
                               "DO UPDATE SET state=excluded.state,observed_at=excluded.observed_at "
                               "WHERE excluded.observed_at >= observed_at", (source,start,end,state,now))

    def coverage(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM source_coverage ORDER BY source,start_ts")]

    # ------------------------------------------------------------------ meta
    def meta_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def meta_set(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) "
                               "DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # ----------------------------------------------------------- observations
    def upsert_observations(self, observations: Iterable[Observation]) -> int:
        """Insert or update by id; returns rows actually inserted or changed
        (re-sending an identical observation is a no-op, so the count is honest)."""
        rows = [_obs_row(o) for o in observations]
        if not rows:
            return 0
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT INTO observations(id, kind, ts, end_ts, subject, source, category, source_id, attrs) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, ts=excluded.ts, "
                "end_ts=excluded.end_ts, subject=excluded.subject, source=excluded.source, "
                "category=excluded.category, source_id=excluded.source_id, attrs=excluded.attrs "
                "WHERE kind IS NOT excluded.kind OR ts IS NOT excluded.ts OR end_ts IS NOT excluded.end_ts "
                "OR subject IS NOT excluded.subject OR source IS NOT excluded.source "
                "OR category IS NOT excluded.category OR source_id IS NOT excluded.source_id "
                "OR attrs IS NOT excluded.attrs", rows)
            return self._conn.total_changes - before

    def delete_observations(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.executemany("DELETE FROM observations WHERE id=?", [(i,) for i in ids])
            return self._conn.total_changes - before

    def get_observation(self, obs_id: str) -> Observation | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        return None if row is None else _row_obs(row)

    def observations(self, *, kinds: Iterable[str] | None = None, since: float | None = None,
                     until: float | None = None, subject: str | None = None) -> list[Observation]:
        """Timeline slice ordered by ts. ``until`` is exclusive."""
        sql, args = "SELECT * FROM observations WHERE 1=1", []
        if kinds is not None:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        if until is not None:
            sql += " AND ts < ?"
            args.append(until)
        if subject is not None:
            sql += " AND subject = ?"
            args.append(subject)
        sql += " ORDER BY ts, id"
        with self._lock:
            return [_row_obs(r) for r in self._conn.execute(sql, args)]

    def last_observation(self, kind: str, subject: str, *, before: float | None = None) -> Observation | None:
        sql, args = "SELECT * FROM observations WHERE kind=? AND subject=?", [kind, subject]
        if before is not None:
            sql += " AND ts <= ?"
            args.append(before)
        with self._lock:
            row = self._conn.execute(sql + " ORDER BY ts DESC LIMIT 1", args).fetchone()
        return None if row is None else _row_obs(row)

    def count_by_kind(self) -> dict[str, int]:
        with self._lock:
            return {r[0]: r[1] for r in self._conn.execute(
                "SELECT kind, COUNT(*) FROM observations GROUP BY kind ORDER BY kind")}

    # -------------------------------------------------------------- snapshots
    def get_snapshot(self, source: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM snapshots WHERE source=?", (source,)).fetchone()
        if row is None:
            return None
        return {"taken_at": row["taken_at"], "window_start": row["window_start"],
                "window_end": row["window_end"], "items": json.loads(row["items"])}

    def put_snapshot(self, source: str, *, taken_at: float, window_start: float,
                     window_end: float, items: list[dict]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO snapshots(source, taken_at, window_start, window_end, items) VALUES(?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET taken_at=excluded.taken_at, "
                "window_start=excluded.window_start, window_end=excluded.window_end, items=excluded.items",
                (source, taken_at, window_start, window_end, json.dumps(items, default=str)))

    # --------------------------------------------------------------- patterns
    def patterns(self, *, status: str | None = None, kind: str | None = None) -> list[Pattern]:
        sql, args = "SELECT json, status FROM patterns WHERE 1=1", []
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY confidence DESC, id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        from service.behavior.evidence import pattern_evidence
        return [pattern_evidence(replace(Pattern.from_dict(json.loads(r[0])), status=r[1])) for r in rows]

    def get_pattern(self, pattern_id: str) -> Pattern | None:
        with self._lock:
            row = self._conn.execute("SELECT json, status FROM patterns WHERE id=?", (pattern_id,)).fetchone()
        from service.behavior.evidence import pattern_evidence
        return None if row is None else pattern_evidence(replace(Pattern.from_dict(json.loads(row[0])), status=row[1]))

    def replace_patterns(self, mined: list[Pattern], now: float) -> list[Pattern]:
        """Persist a fresh mining run. The user's verdict survives because pattern
        ids are deterministic: a re-mined pattern keeps its stored status. Active
        patterns that no longer hold are dropped; confirmed/rejected ones are kept
        (a verdict is the user's, not ours to forget) unless they have exactly
        one successor with the same kind+subject, which inherits the verdict."""
        out: list[Pattern] = []
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, status, kind, subject, json FROM patterns").fetchall()
            existing = {r[0]: r[1] for r in rows}
            from service.behavior.evidence import pattern_evidence
            old_basis = {r[0]: pattern_evidence(Pattern.from_dict(json.loads(r[4]))).stats.get("basis") for r in rows}
            mined_ids = {p.id for p in mined}
            # A verdict whose pattern drifted to a new id (e.g. a weekday joined
            # the window) follows it when the kind+subject is unambiguous.
            orphans: dict[tuple[str, str], list[str]] = {}
            for pid, st, kind, subject, _raw in rows:
                if st != "active" and pid not in mined_ids:
                    orphans.setdefault((kind, subject), []).append(pid)
            successors: dict[tuple[str, str], list[Pattern]] = {}
            for p in mined:
                if p.id not in existing:
                    successors.setdefault((p.kind, p.subject), []).append(p)
            inherited: dict[str, str] = {}
            for key, olds in orphans.items():
                news = successors.get(key, [])
                if (len(olds) == 1 and len(news) == 1
                        and old_basis[olds[0]] == pattern_evidence(news[0]).stats.get("basis")):
                    inherited[news[0].id] = existing[olds[0]]
                    self._conn.execute("DELETE FROM patterns WHERE id=?", (olds[0],))
            seen = set()
            for p in mined:
                status = existing.get(p.id, inherited.get(p.id, "active"))
                p = replace(p, status=status, updated_at=now)
                seen.add(p.id)
                self._conn.execute(
                    "INSERT INTO patterns(id, kind, subject, status, confidence, json, updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, "
                    "subject=excluded.subject, confidence=excluded.confidence, json=excluded.json, "
                    "updated_at=excluded.updated_at",
                    (p.id, p.kind, p.subject, status, p.confidence,
                     json.dumps(p.to_dict(), default=str), now))
                out.append(p)
            stale = [pid for pid, st in existing.items() if pid not in seen and st == "active"]
            self._conn.executemany("DELETE FROM patterns WHERE id=?", [(s,) for s in stale])
        return out

    def set_pattern_status(self, pattern_id: str, status: str) -> Pattern:
        if status not in PATTERN_STATUSES:
            raise ValueError(f"unknown pattern status {status!r}")
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE patterns SET status=? WHERE id=?", (status, pattern_id))
            if cur.rowcount == 0:
                raise KeyError(pattern_id)
        pattern = self.get_pattern(pattern_id)
        assert pattern is not None
        return pattern

    # ----------------------------------------------------------------- nudges
    def has_nudge(self, nudge_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM nudges WHERE id=?", (nudge_id,)).fetchone() is not None

    def insert_nudge(self, nudge: Nudge) -> Nudge | None:
        """Assigns the next seq. Returns None when the id already exists (dedupe)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO nudges(id, kind, json, status, created_at) VALUES(?,?,?,?,?)",
                (nudge.id, nudge.kind, json.dumps(nudge.to_dict(), default=str), "new", nudge.created_at))
            if cur.rowcount == 0:
                return None
            seq = int(cur.lastrowid)
            stored = replace(nudge, seq=seq)
            self._conn.execute("UPDATE nudges SET json=? WHERE seq=?",
                               (json.dumps(stored.to_dict(), default=str), seq))
        return stored

    def get_nudge(self, nudge_id: str) -> Nudge | None:
        with self._lock:
            row = self._conn.execute("SELECT json FROM nudges WHERE id=?", (nudge_id,)).fetchone()
        return None if row is None else Nudge.from_dict(json.loads(row[0]))

    def nudges_since(self, cursor: int = 0, limit: int = 50) -> list[Nudge]:
        with self._lock:
            rows = self._conn.execute("SELECT json FROM nudges WHERE seq > ? ORDER BY seq LIMIT ?",
                                      (int(cursor), int(limit))).fetchall()
        return [Nudge.from_dict(json.loads(r[0])) for r in rows]

    def nudges_created_since(self, ts: float) -> list[Nudge]:
        with self._lock:
            rows = self._conn.execute("SELECT json FROM nudges WHERE created_at >= ? ORDER BY seq",
                                      (ts,)).fetchall()
        return [Nudge.from_dict(json.loads(r[0])) for r in rows]

    def set_nudge_status(self, nudge_id: str, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE nudges SET status=? WHERE id=?", (status, nudge_id))

    def max_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(seq) FROM nudges").fetchone()
            # After prune() the newest rows may be gone; the AUTOINCREMENT
            # counter still knows the highest seq ever handed out.
            seq = self._conn.execute("SELECT seq FROM sqlite_sequence WHERE name='nudges'").fetchone()
        return max(int(row[0] or 0), int(seq[0] or 0) if seq else 0)

    def nudge_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM nudges").fetchone()[0])

    # --------------------------------------------------------------- feedback
    def add_feedback(self, nudge_id: str, verdict: str, ts: float, kind: str, pattern_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO feedback(nudge_id, verdict, ts, kind, pattern_key) "
                               "VALUES(?,?,?,?,?)", (nudge_id, verdict, ts, kind, pattern_key))

    def feedback_since(self, ts: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT nudge_id, verdict, ts, kind, pattern_key FROM feedback "
                                      "WHERE ts >= ? ORDER BY ts, rowid", (ts,)).fetchall()
        return [dict(r) for r in rows]

    def pattern_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0])

"""Restart-safe disabled scheduler for presentation-only snapshot jobs.

A tick atomically stages every missed UTC occurrence up to now (bounded by the
store's capacity). No missed occurrence is silently skipped or coalesced. The
first accepted snapshot for each occurrence is persisted before computation.
"""
from __future__ import annotations

import asyncio
import json
import time

from mini.adapters import REVISION, SnapshotAdapter, disabled_adapters
from mini.http import encode
from mini.protocol import identity, text
from mini.store import CapacityError, Collision, StoreUnavailable

MAX_TIME = 2**53


class Runtime:
    def __init__(self, store, *, enabled_jobs=(), adapters=None):
        if not isinstance(enabled_jobs, (tuple, frozenset)) or any(not isinstance(j, str) for j in enabled_jobs):
            raise ValueError("Invalid runtime enablement")
        self.store = store
        self.enabled_jobs = frozenset(enabled_jobs)
        self.adapters = disabled_adapters() if adapters is None else dict(adapters)
        if any(type(a) is not SnapshotAdapter or k != a.kind for k, a in self.adapters.items()):
            raise ValueError("Only owned snapshot adapters are allowed")

    def stage(self, now, snapshots):
        if type(now) is not int or not 0 <= now <= MAX_TIME or not isinstance(snapshots, dict):
            raise ValueError("Invalid scheduler tick")
        if not self.enabled_jobs:
            return []
        # BEGIN IMMEDIATE serializes concurrent ticks and capacity checks. A long
        # outage exceeding capacity refuses the entire batch before any insert.
        with self.store.connect(write=True) as db:
            jobs = db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
            if self.enabled_jobs - {j["job_id"] for j in jobs}:
                raise ValueError("Unknown enabled job")
            plan = []
            available = self.store.max_occurrences - db.execute("SELECT count(*) FROM occurrences").fetchone()[0]
            for job in jobs:
                jid = job["job_id"]
                if jid not in self.enabled_jobs:
                    continue
                adapter = self.adapters.get(job["kind"])
                if adapter is None:
                    raise ValueError("No portable adapter")
                snapshot = snapshots.get(jid)
                adapter.validate(snapshot)
                payload = encode({"adapter_revision": REVISION, "snapshot": snapshot}).decode()
                if len(payload.encode()) > 100_000:
                    raise CapacityError("Snapshot capacity reached")
                if job["first_due"] > now:
                    continue
                count = (now - job["first_due"]) // job["interval_s"] + 1
                existing = {r["due"]: r["occurrence_id"] for r in db.execute(
                    "SELECT due,occurrence_id FROM occurrences WHERE job_id=? AND due<=?", (jid, now))}
                if count - len(existing) > available:
                    raise CapacityError("Catch-up capacity reached")
                for index in range(count):
                    due = job["first_due"] + index * job["interval_s"]
                    oid = identity("o_", self.store.node_id, jid, due)
                    if due in existing:
                        if existing[due] != oid:
                            raise Collision("Occurrence identity conflict")
                        continue  # durable first snapshot wins, never replaced
                    plan.append((oid, jid, due, payload))
                available -= count - len(existing)
            self.store.require_capacity(sum(len(p[3].encode()) for p in plan) * 4 + 1048576)
            for oid, jid, due, payload in plan:
                if db.execute("SELECT 1 FROM occurrences WHERE occurrence_id=?", (oid,)).fetchone():
                    raise Collision("Occurrence identity conflict")
                db.execute("INSERT INTO occurrences VALUES(?,?,?,'pending')", (oid, jid, due))
                db.execute("INSERT INTO runtime_inputs VALUES(?,?)", (oid, payload))
            return [p[0] for p in plan]

    def complete_pending(self):
        if not self.enabled_jobs:
            return []
        completed = []
        position = ""
        while True:
            with self.store.connect() as db:
                pending = db.execute("""SELECT o.*,j.kind,
                    CASE WHEN typeof(i.payload)='text' AND length(CAST(i.payload AS BLOB))<=100000
                         THEN i.payload ELSE NULL END AS payload
                    FROM occurrences o JOIN jobs j USING(job_id)
                    JOIN runtime_inputs i USING(occurrence_id)
                    WHERE o.state='pending' AND o.occurrence_id>?
                    ORDER BY o.occurrence_id LIMIT 64""", (position,)).fetchall()
            if not pending:
                break
            position = pending[-1]["occurrence_id"]
            for row in pending:
                if row["job_id"] not in self.enabled_jobs:
                    continue
                adapter = self.adapters.get(row["kind"])
                if adapter is None:
                    raise ValueError("No portable adapter")
                try:
                    stored = json.loads(row["payload"])
                    if (set(stored) != {"adapter_revision", "snapshot"} or stored["adapter_revision"] != REVISION
                            or encode(stored).decode() != row["payload"]):
                        raise ValueError
                    snapshot = stored["snapshot"]
                except (ValueError, TypeError, RecursionError, KeyError):
                    raise StoreUnavailable("Invalid staged snapshot") from None
                title, body = adapter.render(snapshot)
                result = self.store.result(row["job_id"], row["occurrence_id"], row["kind"], title, body)
                completed.append(self.store.complete(result))
        return completed

    def tick(self, now, snapshots):
        if type(now) is not int or not 0 <= now <= MAX_TIME or not isinstance(snapshots, dict):
            raise ValueError("Invalid scheduler tick")
        completed = self.complete_pending()
        self.stage(now, snapshots)
        return completed + self.complete_pending()

    async def run(self, snapshots, *, stop, interval=1):
        """Optional owned loop; Node never starts it and defaults do no work."""
        if type(interval) is not int or not 1 <= interval <= 60:
            raise ValueError("Invalid tick interval")
        while not stop.is_set():
            self.tick(int(time.time()), snapshots)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

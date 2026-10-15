"""Bounded metadata-only seed progress in the existing behavior database.

No source bodies, quotations, titles or model prose are persisted. The supplied
behavior Store owns locking, transactions and the existing Forget lifecycle.
Publication receipts are not durable permission: the host must supply fresh
eligible records again after restart and at every materialization.
"""
from __future__ import annotations

import json
import math

SOURCES = ("email", "messages", "notes", "calendar", "reminders")
MAX_RECORDS = 10000
MAX_EDGES = 1000
PASSAGE_CHARS = 2400
PASSAGE_BYTES = 1400
OVERLAP = 160
VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS behavior_seed_sources (
 source TEXT PRIMARY KEY, manifest TEXT NOT NULL, observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS behavior_seed_records (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, revision TEXT NOT NULL,
 chars INTEGER NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
 covered INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, attempted_at REAL NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS behavior_seed_links (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS behavior_seed_work (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL
);
"""


class SourceIndex:
    def __init__(self, store):
        self.store = store
        with store._lock, store._conn:
            store._conn.executescript(SCHEMA)

    def _epoch(self):
        return self.store.meta_get("behavior_seed_epoch")

    def reset(self, epoch=None):
        """A new consent epoch or Forget never inherits prior analysis."""
        with self.store._lock, self.store._conn:
            self._reset(epoch)

    def _reset(self, epoch):
        for table in ("behavior_seed_links", "behavior_seed_work", "behavior_seed_records", "behavior_seed_sources"):
            self.store._conn.execute("DELETE FROM " + table)
        for key, value in (("behavior_seed_epoch", epoch), ("behavior_seed_turn", 0), ("behavior_seed_version", VERSION)):
            self.store._conn.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                     (key, json.dumps(value)))

    def revoke(self, source):
        if source not in SOURCES:
            raise ValueError("Unsupported seed source")
        with self.store._lock, self.store._conn:
            self._revoke(source)

    def _revoke(self, source):
        self.store._conn.execute("DELETE FROM behavior_seed_records WHERE source=?", (source,))
        self.store._conn.execute("DELETE FROM behavior_seed_sources WHERE source=?", (source,))
        self._remove_links(lambda e: source in (e["left"]["source"], e["right"]["source"]))
        self._remove_work(lambda w: w["endpoint"]["source"] == source)

    def _remove_links(self, predicate):
        for row in self.store._conn.execute("SELECT id,payload FROM behavior_seed_links").fetchall():
            if predicate(json.loads(row["payload"])):
                self.store._conn.execute("DELETE FROM behavior_seed_links WHERE id=?", (row["id"],))

    def _remove_work(self, predicate):
        for row in self.store._conn.execute("SELECT id,payload FROM behavior_seed_work").fetchall():
            if predicate(json.loads(row["payload"])):
                self.store._conn.execute("DELETE FROM behavior_seed_work WHERE id=?", (row["id"],))

    def sync(self, records, manifests, *, epoch, now):
        """Index only already-normalized, eligible records supplied by the host.

        A missing record in a capped publication is not a source deletion. Keep
        its checkpoint, but it cannot be analyzed or used without current bytes.
        """
        if not isinstance(epoch, str) or not epoch or len(epoch) > 200 or type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("Valid epoch and clock required")
        with self.store._lock, self.store._conn:
            if self._epoch() != epoch or self.store.meta_get("behavior_seed_version") != VERSION:
                self._reset(epoch)
            for source in SOURCES:
                manifest = manifests.get(source)
                if manifest is None or manifest["state"] == "off":
                    self._revoke(source)
                    continue
                self.store._conn.execute("INSERT INTO behavior_seed_sources VALUES (?,?,?) ON CONFLICT(source) "
                                         "DO UPDATE SET manifest=excluded.manifest,observed_at=excluded.observed_at",
                                         (source, json.dumps(manifest, allow_nan=False), now))
            existing = {r["id"]: r for r in self.store._conn.execute("SELECT * FROM behavior_seed_records")}
            for r in records:
                old = existing.get(r["id"])
                if old is not None and old["revision"] != r["revision"]:
                    self.store._conn.execute("DELETE FROM behavior_seed_records WHERE id=?", (r["id"],))
                    self._remove_links(lambda e: r["id"] in (e["left"]["record_id"], e["right"]["record_id"]))
                    self._remove_work(lambda w: w["endpoint"]["record_id"] == r["id"])
                self.store._conn.execute("INSERT INTO behavior_seed_records(id,source,revision,chars,seen_at) VALUES (?,?,?,?,?) "
                                         "ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at",
                                         (r["id"], r["source"], r["revision"], len(r["text"]), now))
            # Never grow indefinitely as old capped publications rotate.
            stale = [r[0] for r in self.store._conn.execute(
                "SELECT id FROM behavior_seed_records ORDER BY seen_at DESC,id LIMIT -1 OFFSET ?", (MAX_RECORDS,))]
            for ident in stale:
                self.store._conn.execute("DELETE FROM behavior_seed_records WHERE id=?", (ident,))
            if stale:
                self._remove_links(lambda e: e["left"]["record_id"] in stale or e["right"]["record_id"] in stale)
                self._remove_work(lambda w: w["endpoint"]["record_id"] in stale)

    def select(self, records, *, now, limit=4):
        """Round-robin sources and least-recently attempted record revisions."""
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("Invalid seed batch size")
        current = {r["id"]: r for r in records}
        queues = {s: [] for s in SOURCES}
        with self.store._lock, self.store._conn:
            rows = self.store._conn.execute("SELECT * FROM behavior_seed_records WHERE state!='analyzed' AND retry_at<=? "
                                            "ORDER BY attempted_at,id", (now,)).fetchall()
            turn = self.store.meta_get("behavior_seed_turn", 0)
            turn = turn if type(turn) is int else 0
            self.store._conn.execute("INSERT INTO meta VALUES ('behavior_seed_turn',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                     (json.dumps((turn+1) % len(SOURCES)),))
        for row in rows:
            r = current.get(row["id"])
            if r is not None and r["revision"] == row["revision"]:
                text = r["text"][row["cursor"]:row["cursor"] + PASSAGE_CHARS].encode()[:PASSAGE_BYTES].decode("utf-8", errors="ignore")
                queues[r["source"]].append(dict(r, text=text,
                                                text_start=row["cursor"]))
        selected = []
        while len(selected) < limit and any(queues.values()):
            for source in SOURCES[turn:] + SOURCES[:turn]:
                if queues[source] and len(selected) < limit:
                    selected.append(queues[source].pop(0))
        return selected

    def finish(self, anchors, context, proposals, *, epoch, now, admit, work=()):
        """CAS progress and validated links together, or publish neither.

        The host's admission guard includes live source receipts, permissions,
        cancellation and its publication gate; no callback here grants consent.
        """
        from service.behavior.associations import validate
        edges = validate(proposals, context)
        # Never acquire the host's consent gate while holding the Store lock.
        # Seed holds that publication gate before entering this method.
        if not admit():
            return False
        with self.store._lock, self.store._conn:
            if self._epoch() != epoch:
                return False
            for r in anchors:
                old = self.store._conn.execute("SELECT revision,cursor FROM behavior_seed_records WHERE id=?", (r["id"],)).fetchone()
                if old is None or old["revision"] != r["revision"] or old["cursor"] != r["text_start"]:
                    return False
            for e in edges:
                for end in (e["left"], e["right"]):
                    old = self.store._conn.execute("SELECT revision FROM behavior_seed_records WHERE id=?", (end["record_id"],)).fetchone()
                    if old is None or old["revision"] != end["revision"]:
                        return False
            for w in work:
                end = w["endpoint"]
                old = self.store._conn.execute("SELECT revision FROM behavior_seed_records WHERE id=?", (end["record_id"],)).fetchone()
                if old is None or old["revision"] != end["revision"]:
                    return False
            for r in anchors:
                end = r["text_start"] + len(r["text"])
                total = self.store._conn.execute("SELECT chars FROM behavior_seed_records WHERE id=?", (r["id"],)).fetchone()[0]
                done = end >= total
                self.store._conn.execute("UPDATE behavior_seed_records SET cursor=?,covered=?,state=?,attempts=attempts+1,"
                                         "attempted_at=?,retry_at=0 WHERE id=?",
                                         (total if done else max(r["text_start"]+1, end-OVERLAP), end,
                                          "analyzed" if done else "pending", now, r["id"]))
            for e in edges:
                e = dict(e, analyzed_at=now)
                self.store._conn.execute("INSERT INTO behavior_seed_links VALUES (?,?,?) ON CONFLICT(id) DO NOTHING",
                                         (e["id"], json.dumps(e, sort_keys=True, allow_nan=False), now))
            for w in work:
                self.store._conn.execute("INSERT INTO behavior_seed_work VALUES (?,?,?) ON CONFLICT(id) "
                                         "DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",
                                         (w["id"], json.dumps(w, sort_keys=True, allow_nan=False), now))
            self.store._conn.execute("DELETE FROM behavior_seed_work WHERE id IN (SELECT id FROM behavior_seed_work "
                                     "ORDER BY created_at DESC,id LIMIT -1 OFFSET ?)", (MAX_EDGES,))
            self.store._conn.execute("DELETE FROM behavior_seed_links WHERE id IN (SELECT id FROM behavior_seed_links "
                                     "ORDER BY created_at DESC,id LIMIT -1 OFFSET ?)", (MAX_EDGES,))
            return True

    def defer(self, anchors, *, epoch, now):
        with self.store._lock, self.store._conn:
            if self._epoch() != epoch:
                return
            for r in anchors:
                self.store._conn.execute("UPDATE behavior_seed_records SET state='deferred',attempts=attempts+1,attempted_at=?,"
                                         "retry_at=? WHERE id=? AND revision=? AND cursor=?",
                                         (now, now+300, r["id"], r["revision"], r["text_start"]))

    def edges(self):
        with self.store._lock:
            return [json.loads(r[0]) for r in self.store._conn.execute("SELECT payload FROM behavior_seed_links ORDER BY created_at DESC,id")]

    def work(self):
        with self.store._lock:
            return [json.loads(r[0]) for r in self.store._conn.execute("SELECT payload FROM behavior_seed_work ORDER BY created_at DESC,id")]

    def status(self, records):
        current = {r["id"]: r for r in records}
        with self.store._lock:
            manifests = {r["source"]: json.loads(r["manifest"]) for r in self.store._conn.execute("SELECT * FROM behavior_seed_sources")}
            rows = list(self.store._conn.execute("SELECT * FROM behavior_seed_records"))
        result = {}
        for source in SOURCES:
            manifest = manifests.get(source, dict(state="off", eligible=0, omitted=0))
            eligible = [r for r in rows if r["source"] == source and r["id"] in current and current[r["id"]]["revision"] == r["revision"]]
            counts = {state: sum(r["state"] == state for r in eligible) for state in ("pending", "deferred", "analyzed")}
            not_indexed = max(0, manifest["eligible"]-len(eligible))
            result[source] = dict(manifest, **counts, indexed=len(eligible), unresolved=sum(r["source"] == source for r in rows)-len(eligible),
                                  not_indexed=not_indexed,
                                  coverage="partial" if (not manifest.get("publication_complete") or manifest.get("omitted")
                                                          or manifest.get("skipped") or not_indexed) else "bounded_publication",
                                  analyzed_characters=sum(r["covered"] for r in eligible),
                                  state=("seeded" if counts["analyzed"] == len(eligible) else "scanning")
                                        if manifest["state"] == "available" else manifest["state"],
                                  completion_scope="bounded current publication; never an exhaustive source history")
        return result

    def prune(self, *, now, retention=180*86400):
        with self.store._lock, self.store._conn:
            return prune_connection(self.store._conn, now-retention)


def prune_connection(conn, cutoff):
    """Retention without constructing an engine, migrating, or creating tables."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "behavior_seed_records" not in tables:
        return 0
    removed = conn.execute("DELETE FROM behavior_seed_records WHERE seen_at<?", (cutoff,)).rowcount
    known = {r[0] for r in conn.execute("SELECT id FROM behavior_seed_records")}
    for table in ("behavior_seed_links", "behavior_seed_work"):
        if table not in tables:
            continue
        for ident, payload, created in conn.execute("SELECT id,payload,created_at FROM "+table).fetchall():
            try:
                item = json.loads(payload)
                refs = [item["left"]["record_id"], item["right"]["record_id"]] if table.endswith("links") else [item["endpoint"]["record_id"]]
                keep = created >= cutoff and all(ref in known for ref in refs)
            except (ValueError, TypeError, KeyError):
                keep = False
            if not keep:
                removed += conn.execute("DELETE FROM "+table+" WHERE id=?", (ident,)).rowcount
    if "behavior_seed_sources" in tables:
        removed += conn.execute("DELETE FROM behavior_seed_sources WHERE observed_at<?", (cutoff,)).rowcount
    return removed

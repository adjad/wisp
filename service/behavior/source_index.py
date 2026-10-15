"""Bounded metadata-only seed progress in the existing behavior database.

No source bodies, quotations, titles or model prose are persisted. The supplied
behavior Store owns locking, transactions and the existing Forget lifecycle.
Publication receipts are not durable permission: the host must supply fresh
eligible records again after restart and at every materialization.
"""
from __future__ import annotations

import json
import math
import hashlib
import weakref
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from functools import wraps

from service.publication_authority import Authority, PublicationLease, PublicationUnavailable, mutating, revocation


class CallerTransactionActive(PublicationUnavailable):
    """Publication was refused without changing a caller-owned transaction."""


class _PublicationDeclined(Exception):
    pass


class _IndexHead:
    def __init__(self):
        self.authority = Authority("behavior-seed-progress-epoch",owner=self)


_heads = weakref.WeakValueDictionary()


@contextmanager
def database_mutation(path):
    """Order external retention of existing metadata, without opening an index."""
    from service.behavior.activity import database_identity
    with revocation():
        head = _heads.get(database_identity(Path(path)))
        if head is not None:
            head.authority.withdraw()
        yield
        if head is not None:
            head.authority.publish()


def _ordered_mutation(function):
    @wraps(function)
    def call(self,*args,**kwargs):
        lease = kwargs.get("lease")
        if lease is not None:
            if type(lease) is not PublicationLease or not lease.covers(self._head.authority):
                raise PublicationUnavailable("An active prepared metadata publication lease is required")
            return function(self,*args,**kwargs)
        with revocation():
            self._head.authority.withdraw()
            result = function(self,*args,**kwargs)
            self._head.authority.publish()
            return result
    return call

SOURCES = ("email", "messages", "notes", "calendar", "reminders")
MAX_RECORDS = 10000
MAX_EDGES = 1000
PASSAGE_CHARS = 2400
PASSAGE_BYTES = 1400
OVERLAP = 160
VERSION = 2
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
CREATE TABLE IF NOT EXISTS behavior_seed_capacity (
 record_id TEXT NOT NULL, revision TEXT NOT NULL, kind TEXT NOT NULL,
 deferred_at REAL NOT NULL, retrying INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(record_id,kind)
);
"""


def _endpoints(item, kind):
    return (item["left"], item["right"]) if kind == "links" else (item["endpoint"],)


def _loss_counts(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='behavior_seed_loss_counts'").fetchone()
    try:
        value = json.loads(row[0]) if row else {}
        return {source: {key: count for key, count in entry.items()
                         if key in ("expired_derivations", "capacity_evictions")
                         and type(count) is int and count >= 0}
                for source, entry in value.items() if source in SOURCES and isinstance(entry, dict)} if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _count_loss(conn, endpoint, key):
    source = endpoint.get("source")
    if source not in SOURCES:
        return
    counts = _loss_counts(conn)
    entry = counts.setdefault(source, {})
    old = entry.get(key, 0)
    entry[key] = (old if type(old) is int and old >= 0 else 0) + 1
    conn.execute("INSERT INTO meta VALUES ('behavior_seed_loss_counts',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps(counts),))


def _future_range(item, now):
    value = item.get("due_range")
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(v) in (int, float) and math.isfinite(v) for v in value)
            and value[0] <= value[1]):
        return value if value[1] > now else False
    return None


def _work_rank(row, now):
    item = json.loads(row["payload"])
    window = _future_range(item, now)
    if window:
        return (0 if item.get("ownership") == "user" else 1, window[1], window[0], row["id"])
    return (3 if window is False else 2, 0, 0, row["id"])


class SourceIndex:
    def __init__(self, store):
        from service.behavior.activity import database_identity
        self.store = store
        self._account_picks = {}
        with revocation():
            with store._lock:
                if store._conn.in_transaction:
                    raise CallerTransactionActive("Seed initialization was not applied inside a caller-owned transaction")
                with store._conn:
                    store._conn.executescript(SCHEMA)
            identity = database_identity(store.path,store._conn)
            head = _heads.get(identity)
            if head is None:
                head = _IndexHead()
                head.authority.publish()
                _heads[identity] = head
            self._head = head

    def publication_token(self):
        return self._head.authority.token()

    def _epoch(self):
        return self.store.meta_get("behavior_seed_epoch")

    @mutating
    def reset(self, epoch=None):
        """A new consent epoch or Forget never inherits prior analysis."""
        self._head.authority.withdraw()
        with self.store._lock, self.store._conn:
            self._reset(epoch)
        self._head.authority.publish()

    def _reset(self, epoch):
        self._account_picks.clear()
        for table in ("behavior_seed_capacity", "behavior_seed_links", "behavior_seed_work", "behavior_seed_records", "behavior_seed_sources"):
            self.store._conn.execute("DELETE FROM " + table)
        for key, value in (("behavior_seed_epoch", epoch), ("behavior_seed_turn", 0), ("behavior_seed_version", VERSION),
                           ("behavior_seed_loss_counts", {})):
            self.store._conn.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                     (key, json.dumps(value)))

    @mutating
    def revoke(self, source):
        if source not in SOURCES:
            raise ValueError("Unsupported seed source")
        self._head.authority.withdraw()
        with self.store._lock, self.store._conn:
            self._revoke(source)
        self._head.authority.publish()

    def _revoke(self, source):
        self.store._conn.execute("DELETE FROM behavior_seed_capacity WHERE record_id IN "
                                 "(SELECT id FROM behavior_seed_records WHERE source=?)", (source,))
        self.store._conn.execute("DELETE FROM behavior_seed_records WHERE source=?", (source,))
        self.store._conn.execute("DELETE FROM behavior_seed_sources WHERE source=?", (source,))
        self._remove_links(lambda e: source in (e["left"]["source"], e["right"]["source"]))
        self._remove_work(lambda w: w["endpoint"]["source"] == source)
        counts = _loss_counts(self.store._conn)
        if source in counts:
            counts.pop(source)
            self.store._conn.execute("UPDATE meta SET value=? WHERE key='behavior_seed_loss_counts'", (json.dumps(counts),))

    def _remove_links(self, predicate):
        for row in self.store._conn.execute("SELECT id,payload FROM behavior_seed_links").fetchall():
            if predicate(json.loads(row["payload"])):
                self.store._conn.execute("DELETE FROM behavior_seed_links WHERE id=?", (row["id"],))

    def _remove_work(self, predicate):
        for row in self.store._conn.execute("SELECT id,payload FROM behavior_seed_work").fetchall():
            if predicate(json.loads(row["payload"])):
                self.store._conn.execute("DELETE FROM behavior_seed_work WHERE id=?", (row["id"],))

    @_ordered_mutation
    def sync(self, records, manifests, *, epoch, now, lease=None):
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
            self._clean_capacity()

    def _clean_capacity(self):
        self.store._conn.execute("DELETE FROM behavior_seed_capacity WHERE NOT EXISTS "
                                 "(SELECT 1 FROM behavior_seed_records r WHERE r.id=record_id AND r.revision=behavior_seed_capacity.revision)")

    def _trim(self, kind, now):
        table = "behavior_seed_" + kind
        rows = list(self.store._conn.execute("SELECT id,payload,created_at FROM " + table))
        rows.sort(key=(lambda r: _work_rank(r, now)) if kind == "work" else lambda r: (-r["created_at"], r["id"]))
        for row in rows[MAX_EDGES:]:
            self.store._conn.execute("DELETE FROM " + table + " WHERE id=?", (row["id"],))
            for end in _endpoints(json.loads(row["payload"]), kind):
                current = self.store._conn.execute("SELECT 1 FROM behavior_seed_records WHERE id=? AND revision=?",
                                                   (end["record_id"], end["revision"])).fetchone()
                if current:
                    self.store._conn.execute("INSERT INTO behavior_seed_capacity VALUES (?,?,?,?,0) ON CONFLICT(record_id,kind) "
                                             "DO UPDATE SET retrying=CASE WHEN retrying>0 THEN 2 ELSE 0 END",
                                             (end["record_id"], end["revision"], kind, now))
                    _count_loss(self.store._conn, end, "capacity_evictions")

    def _expire_work(self, now):
        # Passed deadline ranges are already withheld by materialize_work. Their
        # metadata can release space; this asserts neither completion nor attendance.
        for row in self.store._conn.execute("SELECT id,payload FROM behavior_seed_work").fetchall():
            item = json.loads(row["payload"])
            if _future_range(item, now) is False:
                self.store._conn.execute("DELETE FROM behavior_seed_work WHERE id=?", (row["id"],))
                _count_loss(self.store._conn, item["endpoint"], "expired_derivations")

    def _release_capacity(self, current, now, limit):
        self._expire_work(now)
        # A missing publication must not reserve space indefinitely. Preserve
        # its retry cursor, but require new capacity credit when it returns.
        for row in self.store._conn.execute("SELECT * FROM behavior_seed_capacity WHERE retrying>0").fetchall():
            record = current.get(row["record_id"])
            if record is None or record["revision"] != row["revision"]:
                self.store._conn.execute("UPDATE behavior_seed_capacity SET retrying=0 WHERE record_id=? AND kind=?",
                                         (row["record_id"], row["kind"]))
                self.store._conn.execute("UPDATE behavior_seed_records SET state='capacity_deferred' WHERE id=? AND state!='analyzed'",
                                         (row["record_id"],))
        free = {kind: max(0, MAX_EDGES-self.store._conn.execute("SELECT count(*) FROM behavior_seed_"+kind).fetchone()[0]
                           -self.store._conn.execute("SELECT count(*) FROM behavior_seed_capacity WHERE kind=? AND retrying>0", (kind,)).fetchone()[0])
                for kind in ("work", "links")}
        grouped = {}
        for row in self.store._conn.execute("SELECT c.*,r.state FROM behavior_seed_capacity c JOIN behavior_seed_records r "
                                            "ON r.id=c.record_id AND r.revision=c.revision ORDER BY c.deferred_at,c.record_id"):
            record = current.get(row["record_id"])
            if row["retrying"] == 0 and row["state"] in ("analyzed", "capacity_deferred") and record and record["revision"] == row["revision"]:
                entry = grouped.setdefault(row["record_id"], dict(kinds=set(), state=row["state"]))
                entry["kinds"].add(row["kind"])
        released = 0
        for ident, entry in grouped.items():
            if released >= limit:
                break
            kinds = {kind for kind in entry["kinds"] if free.get(kind, 0) > 0}
            if not kinds:
                continue
            # Reserve at most one record per free slot. Insert/evict activity at
            # a full cap never creates retry credit, avoiding a rescan loop.
            for kind in kinds:
                free[kind] -= 1
                self.store._conn.execute("UPDATE behavior_seed_capacity SET retrying=1 WHERE record_id=? AND kind=?", (ident, kind))
            if entry["state"] == "analyzed":
                self.store._conn.execute("UPDATE behavior_seed_records SET cursor=0,covered=0 WHERE id=?", (ident,))
            self.store._conn.execute("UPDATE behavior_seed_records SET state='pending',retry_at=0 WHERE id=?", (ident,))
            released += 1

    @staticmethod
    def _account(record):
        reference = record.get("reference")
        account = reference.get("account_id") if isinstance(reference,Mapping) else None
        return account if isinstance(account,str) else ""

    @staticmethod
    def _ordering(record):
        value = record.get("ordering_at",record.get("source_time"))
        return value if type(value) in (int,float) and math.isfinite(value) else float("-inf")

    def prioritize(self, source, records):
        """Fair finite input order before the unchanged source byte budget.

        Only ordering metadata is used; it never becomes an utterance timestamp.
        These process-local choices restore neither permission nor source proof.
        """
        if source not in SOURCES:
            return records
        with self.store._lock:
            known = {row["id"]:dict(row) for row in self.store._conn.execute(
                "SELECT id,state,cursor,attempted_at FROM behavior_seed_records WHERE source=?",(source,))}
        groups, service = {}, {}
        for pos, raw in enumerate(records):
            if not isinstance(raw,Mapping) or not isinstance(raw.get("id"),str):
                account, prior = "", None
            else:
                account = self._account(raw)
                try:
                    ident = source+":"+hashlib.sha256(raw["id"].encode()).hexdigest()[:32]
                    prior = known.get(ident)
                except UnicodeError:
                    prior = None
            service[account] = max(service.get(account,0),prior["attempted_at"] if prior else 0)
            key = (2 if prior and prior["state"] == "analyzed" else
                   0 if prior and prior["cursor"] > 0 else 1,
                   prior["attempted_at"] if prior and prior["cursor"] > 0 else 0,
                   -self._ordering(raw) if isinstance(raw,Mapping) else float("inf"),pos)
            groups.setdefault(account,[]).append((key,raw))
        for values in groups.values():
            values.sort(key=lambda item:item[0])
        order = sorted(groups,key=lambda account:(service[account],account))
        result = []
        while any(groups.values()):
            for account in order:
                if groups[account]:
                    result.append(groups[account].pop(0)[1])
        return tuple(result)

    @_ordered_mutation
    def select(self, records, *, now, limit=4, lease=None):
        """Rotate sources/accounts; alternate recent work and retained backlog."""
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("Invalid seed batch size")
        current = {r["id"]: r for r in records}
        queues = {s: {} for s in SOURCES}
        service = {}
        with self.store._lock, self.store._conn:
            self._release_capacity(current, now, limit)
            all_rows = self.store._conn.execute("SELECT * FROM behavior_seed_records ORDER BY attempted_at,id").fetchall()
            turn = self.store.meta_get("behavior_seed_turn", 0)
            turn = turn if type(turn) is int else 0
            self.store._conn.execute("INSERT INTO meta VALUES ('behavior_seed_turn',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                     (json.dumps((turn+1) % len(SOURCES)),))
        for row in all_rows:
            r = current.get(row["id"])
            if r is not None and r["revision"] == row["revision"]:
                account = self._account(r)
                key = (r["source"],account)
                service[key] = max(service.get(key,0),row["attempted_at"])
                if row["state"] not in {"pending","deferred"} or row["retry_at"] > now:
                    continue
                text = r["text"][row["cursor"]:row["cursor"] + PASSAGE_CHARS].encode()[:PASSAGE_BYTES].decode("utf-8", errors="ignore")
                queues[r["source"]].setdefault(account,[]).append((row,dict(r,text=text,text_start=row["cursor"])))
        active = set(service)
        self._account_picks = {key:value for key,value in self._account_picks.items() if key in active}
        def pick(source):
            accounts = [account for account,values in queues[source].items() if values]
            if not accounts:
                return None
            account = min(accounts,key=lambda account:(service[(source,account)],local_picks.get((source,account),0),account))
            key = (source,account)
            values = queues[source][account]
            recent = self._account_picks.get(key,0) % 2 == 0
            chosen = (max(range(len(values)),key=lambda i:(self._ordering(values[i][1]),values[i][1]["id"])) if recent else
                min(range(len(values)),key=lambda i:(0 if values[i][0]["cursor"] > 0 else 1,
                    values[i][0]["attempted_at"],values[i][0]["id"])))
            _, record = values.pop(chosen)
            self._account_picks[key] = self._account_picks.get(key,0)+1
            service[key] = now
            local_picks[key] = local_picks.get(key,0)+1
            return record
        selected = []
        local_picks = {}
        while len(selected) < limit and any(values for groups in queues.values() for values in groups.values()):
            for source in SOURCES[turn:] + SOURCES[:turn]:
                if len(selected) < limit and (record := pick(source)) is not None:
                    selected.append(record)
        return selected

    def finish(self, anchors, context, proposals, *, epoch, now, admit, work=()):
        """Compatibility entry while callers acquire prepared source witnesses.

        Full admission is performed before the Store lock or transaction. It
        does not provide the ordered source-witness contract of finish_prepared.
        """
        if not admit():
            return False
        with revocation():
            self._head.authority.withdraw()
            result = self._finish_atomic(anchors, context, proposals, epoch=epoch, now=now, work=work)
            self._head.authority.publish()
            return result

    def finish_prepared(self, anchors, context, proposals, *, epoch, now, lease, work=()):
        """Publish under an already-held outer application publication lease.

        The caller prepares full source/config admission before taking that
        lease, then takes its Runtime gate and finally this Store lock. Only
        typed memory checks run here; no source or admission callback runs in
        the transaction. The caller retains the lease through our commit.
        """
        if type(lease) is not PublicationLease or not lease.covers(self._head.authority):
            return False
        return self._finish_atomic(anchors, context, proposals, epoch=epoch, now=now, work=work, lease=lease)

    def _finish_atomic(self, anchors, context, proposals, *, epoch, now, work, lease=None):
        from service.behavior.associations import validate
        edges = validate(proposals, context)
        with self.store._lock:
            conn = self.store._conn
            # A connection context would commit unrelated outer work. Refuse
            # before entering one, issuing SQL, or changing any progress state.
            if conn.in_transaction:
                raise CallerTransactionActive("Seed publication was not applied inside a caller-owned transaction")
            if lease is not None and not lease.current():
                return False
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not self._write_finish(anchors, edges, epoch=epoch, now=now, work=work, lease=lease):
                    raise _PublicationDeclined()
                # Logical publication decision: expiry after this check and
                # during disk commit is not a physical commit-before-deadline
                # promise. Cooperating revokers remain outside the outer lease.
                if lease is not None and not lease.current():
                    raise _PublicationDeclined()
                conn.commit()
            except BaseException as error:
                if conn.in_transaction:
                    try:
                        conn.rollback()
                    except BaseException as cleanup:
                        error.add_note("Seed transaction rollback failed: " + type(cleanup).__name__)
                        raise error from cleanup
                if isinstance(error, _PublicationDeclined):
                    return False
                raise
            return True

    def _write_finish(self, anchors, edges, *, epoch, now, work, lease):
        if lease is not None and not lease.current():
            return False
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
        completed = []
        for r in anchors:
            end = r["text_start"] + len(r["text"])
            total = self.store._conn.execute("SELECT chars FROM behavior_seed_records WHERE id=?", (r["id"],)).fetchone()[0]
            done = end >= total
            if done:
                completed.append(r["id"])
            self.store._conn.execute("UPDATE behavior_seed_records SET cursor=?,covered=?,state=?,attempts=attempts+1,"
                                     "attempted_at=?,retry_at=0 WHERE id=?",
                                     (total if done else max(r["text_start"]+1, end-OVERLAP), end,
                                      "analyzed" if done else "pending", now, r["id"]))
        for e in edges:
            e = dict(e, analyzed_at=now)
            self.store._conn.execute("INSERT INTO behavior_seed_links VALUES (?,?,?) ON CONFLICT(id) DO NOTHING",
                                     (e["id"], json.dumps(e, sort_keys=True, allow_nan=False), now))
        self._expire_work(now)
        for w in work:
            if _future_range(w, now) is False:
                _count_loss(self.store._conn, w["endpoint"], "expired_derivations")
                continue
            self.store._conn.execute("INSERT INTO behavior_seed_work VALUES (?,?,?) ON CONFLICT(id) "
                                     "DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",
                                     (w["id"], json.dumps(w, sort_keys=True, allow_nan=False), now))
        self._trim("work", now)
        self._trim("links", now)
        for ident in completed:
            # A reservation lasts through every passage. A fresh eviction
            # during this retry stays deferred instead of being forgiven.
            self.store._conn.execute("DELETE FROM behavior_seed_capacity WHERE record_id=? AND retrying=1", (ident,))
            self.store._conn.execute("UPDATE behavior_seed_capacity SET retrying=0 WHERE record_id=? AND retrying=2", (ident,))
        return True

    @_ordered_mutation
    def defer(self, anchors, *, epoch, now, lease=None):
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
            capacity_rows = list(self.store._conn.execute("SELECT * FROM behavior_seed_capacity"))
            capacity = {(r["record_id"], r["revision"]) for r in capacity_rows}
            waiting = {(r["record_id"], r["revision"]) for r in capacity_rows if r["retrying"] == 0}
            retrying = {(r["record_id"], r["revision"]) for r in capacity_rows if r["retrying"] > 0}
            losses = _loss_counts(self.store._conn)
        result = {}
        for source in SOURCES:
            manifest = manifests.get(source, dict(state="off", eligible=0, omitted=0))
            eligible = [r for r in rows if r["source"] == source and r["id"] in current and current[r["id"]]["revision"] == r["revision"]]
            counts = {state: sum(r["state"] == state for r in eligible) for state in ("pending", "deferred", "analyzed")}
            limited = sum((r["id"], r["revision"]) in capacity for r in eligible)
            not_indexed = max(0, manifest["eligible"]-len(eligible))
            result[source] = dict(manifest, **counts, indexed=len(eligible), unresolved=sum(r["source"] == source for r in rows)-len(eligible),
                                  capacity_deferred=sum((r["id"], r["revision"]) in waiting for r in eligible),
                                  capacity_retrying=sum((r["id"], r["revision"]) in retrying for r in eligible),
                                  derived_omitted_records=limited,
                                  expired_derivations=losses.get(source, {}).get("expired_derivations", 0),
                                  capacity_evictions=losses.get(source, {}).get("capacity_evictions", 0),
                                  not_indexed=not_indexed,
                                  coverage="partial" if (not manifest.get("publication_complete") or manifest.get("omitted")
                                                          or manifest.get("skipped") or not_indexed or limited) else "bounded_publication",
                                  analyzed_characters=sum(r["covered"] for r in eligible),
                                  state=("capacity_limited" if limited else "seeded" if counts["analyzed"] == len(eligible) else "scanning")
                                        if manifest["state"] == "available" else manifest["state"],
                                  completion_scope="bounded current publication; never an exhaustive source history")
        return result

    def analyzed_ids(self, records):
        """Current exact-revision progress for host-side acquisition backpressure."""
        current = {r["id"]:r["revision"] for r in records}
        with self.store._lock:
            return {row["id"] for row in self.store._conn.execute(
                "SELECT id,revision FROM behavior_seed_records WHERE state='analyzed'")
                if current.get(row["id"]) == row["revision"]}

    @mutating
    def prune(self, *, now, retention=180*86400):
        self._head.authority.withdraw()
        with self.store._lock, self.store._conn:
            removed = prune_connection(self.store._conn, now-retention)
        self._head.authority.publish()
        return removed


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
            ends = ()
            try:
                item = json.loads(payload)
                ends = _endpoints(item, "links" if table.endswith("links") else "work")
                if any(not isinstance(e, dict) or any(not isinstance(e.get(key), str)
                       for key in ("record_id", "revision", "source")) for e in ends):
                    raise ValueError("Invalid derivation endpoint")
                refs = [e["record_id"] for e in ends]
                keep = created >= cutoff and all(ref in known for ref in refs)
            except (ValueError, TypeError, KeyError):
                keep = False
                ends = ()
            if not keep:
                removed += conn.execute("DELETE FROM "+table+" WHERE id=?", (ident,)).rowcount
                if type(created) in (int, float) and math.isfinite(created) and created < cutoff:
                    for end in ends:
                        # Only an exact surviving revision can be reopened. It
                        # remains unusable without fresh host-supplied bytes.
                        changed = conn.execute("UPDATE behavior_seed_records SET cursor=0,covered=0,state='pending',retry_at=0 "
                                               "WHERE id=? AND revision=?", (end["record_id"], end["revision"])).rowcount
                        if changed:
                            _count_loss(conn, end, "expired_derivations")
    if "behavior_seed_capacity" in tables:
        conn.execute("DELETE FROM behavior_seed_capacity WHERE NOT EXISTS "
                     "(SELECT 1 FROM behavior_seed_records r WHERE r.id=record_id AND r.revision=behavior_seed_capacity.revision)")
    if "behavior_seed_sources" in tables:
        removed += conn.execute("DELETE FROM behavior_seed_sources WHERE observed_at<?", (cutoff,)).rowcount
    return removed

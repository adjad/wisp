"""Pure initial-seed orchestration over supplied, permitted cached snapshots.

No getters, providers, native readers, model transport or scheduler activation.
The host supplies an analyzer and fresh receipt checks. Default is OFF. Saved
progress is not saved permission: materialization always needs current evidence.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack, contextmanager, nullcontext
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
import math
import re

from service.behavior.associations import materialize
from service.behavior.source_index import CallerTransactionActive, PASSAGE_BYTES, PASSAGE_CHARS, SOURCES
from service.publication_authority import PublicationUnavailable, Witness, publication as ordered_publication, revocation

MAX_SOURCE_RECORDS = 2000
MAX_RECORD_CHARS = 64000
MAX_BYTES = 4_000_000
MAX_SOURCE_BYTES = MAX_BYTES // len(SOURCES)
MAX_INPUT_BYTES = 16000


@dataclass(frozen=True)
class Snapshot:
    source: str
    records: tuple = ()
    enabled: bool = False
    available: bool = False
    current: object = lambda: False
    publication_complete: bool = False
    omitted: int = 0
    requested_range: tuple | None = None
    declared_range: tuple | None = None
    scope: str = "cached_publication"
    published_at: float | None = None
    publication_witness: Witness | None = None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _range(value):
    return list(value) if (isinstance(value, (list, tuple)) and len(value) == 2
                           and all(_number(v) for v in value) and value[0] < value[1]) else None


def _choice(value, allowed, default="unknown"):
    return value if isinstance(value, str) and value in allowed else default


def record_metadata(source, raw):
    """Exact normalized identity/revision without retaining source text.

    Used for progress/backpressure across a byte-capped interpretation batch.
    It grants no eligibility/currentness; the caller still needs fresh receipts.
    """
    if not isinstance(raw,Mapping) or source not in SOURCES:
        return None
    identity,text = raw.get("id"),raw.get("text")
    if (not isinstance(identity,str) or not 1 <= len(identity) <= 1000
            or not isinstance(text,str) or not text.strip() or len(text) > MAX_RECORD_CHARS
            or _choice(raw.get("status"),{"done","completed","cancelled","dismissed"}) != "unknown"
                and not (source == "reminders" and raw.get("evidence_basis") == "recorded_completion")):
        return None
    basis = _choice(raw.get("evidence_basis"),{"scheduled","recorded_completion","observed","source_statement"})
    if source == "calendar":
        basis = "scheduled"
    elif source in {"email","messages","notes"}:
        basis = "source_statement"
    time_basis,stamp = _choice(raw.get("time_basis"),{"source_timestamp","scheduled_time","completion_time"}),raw.get("source_time")
    if source == "notes" or time_basis == "unknown" or not _number(stamp):
        time_basis,stamp = "unknown",None
    direction = _choice(raw.get("direction"),{"incoming","outgoing"})
    status = _choice(raw.get("status"),{"active","pending"})
    try:
        ident = source+":"+hashlib.sha256(identity.encode()).hexdigest()[:32]
        revision = hashlib.sha256(json.dumps([text,stamp,time_basis,basis,direction,status],ensure_ascii=False).encode()).hexdigest()
    except UnicodeError:
        return None
    reference = raw.get("reference")
    reference = {str(k):v for k,v in reference.items() if isinstance(k,str) and len(k) <= 80
                 and isinstance(v,str) and len(v) <= 1000} if isinstance(reference,Mapping) and len(reference) <= 6 else {}
    return dict(id=ident,source=source,revision=revision,source_time=stamp,time_basis=time_basis,
        evidence_basis=basis,direction=direction,reference=reference or {"source":source,"id":ident},
        ordering_at=raw.get("ordering_at") if _number(raw.get("ordering_at")) else stamp)


def prepare(snapshots):
    """Freeze bounded copies. A note modification time never dates its prose."""
    records, manifests, receipts, seen, used = [], {}, [], set(), 0
    by_source = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, Snapshot) or snapshot.source not in SOURCES or snapshot.source in by_source:
            raise ValueError("One known snapshot per source is required")
        if not isinstance(snapshot.records, tuple):
            raise ValueError("Snapshot records must be a frozen bounded tuple")
        by_source[snapshot.source] = snapshot
    for source in SOURCES:
        snap = by_source.get(source, Snapshot(source))
        info = dict(state="off", eligible=0, seen=0, skipped=0, omitted=0,
                    requested_range=_range(snap.requested_range), declared_range=_range(snap.declared_range),
                    observed_range=None, publication_complete=snap.publication_complete is True,
                    scope=_choice(snap.scope, {"cached_publication", "retained_history"}, "cached_publication"),
                    record_cap=MAX_SOURCE_RECORDS)
        manifests[source] = info
        if snap.enabled is not True:
            continue
        try:
            current = snap.available is True and snap.current() is True
        except Exception:
            current = False
        if not current:
            info["state"] = "unavailable"
            continue
        receipts.append(snap.current)
        info.update(state="available", omitted=snap.omitted if type(snap.omitted) is int and snap.omitted >= 0 else 0)
        source_bytes = 0
        info["seen"] = len(snap.records)
        info["omitted"] += max(0, len(snap.records)-MAX_SOURCE_RECORDS)
        for raw in snap.records[:MAX_SOURCE_RECORDS]:
            if not isinstance(raw, Mapping):
                info["skipped"] += 1
                continue
            identity, text = raw.get("id"), raw.get("text")
            if (_choice(raw.get("status"), {"done", "completed", "cancelled", "dismissed"}) != "unknown"
                    and not (source == "reminders" and raw.get("evidence_basis") == "recorded_completion")):
                info["skipped"] += 1
                continue
            if not isinstance(identity, str) or not 1 <= len(identity) <= 1000 or not isinstance(text, str) or not text.strip():
                info["skipped"] += 1
                continue
            if len(text) > MAX_RECORD_CHARS:
                info["omitted"] += 1
                continue
            try:
                size, identity_bytes = len(text.encode("utf-8")), identity.encode("utf-8")
            except UnicodeError:
                info["skipped"] += 1
                continue
            if used+size > MAX_BYTES or source_bytes+size > MAX_SOURCE_BYTES:
                info["omitted"] += 1
                continue
            ident = source + ":" + hashlib.sha256(identity_bytes).hexdigest()[:32]
            if ident in seen:
                # Conflicting duplicate identities cannot choose their own winner.
                prior = next((r for r in records if r["id"] == ident), None)
                if prior is not None:
                    records.remove(prior)
                    prior_size = len(prior["text"].encode("utf-8"))
                    source_bytes -= prior_size
                    used -= prior_size
                    info["eligible"] -= 1
                    info["skipped"] += 1
                info["skipped"] += 1
                continue
            seen.add(ident)
            metadata = record_metadata(source,raw)
            if metadata is None:
                info["skipped"] += 1
                continue
            records.append(dict(metadata,text=text,
                publication_at=snap.published_at if _number(snap.published_at) else None))
            used += size
            source_bytes += size
            info["eligible"] += 1
        stamps = [r["source_time"] for r in records if r["source"] == source and r["source_time"] is not None]
        if stamps:
            info["observed_range"] = [min(stamps), max(stamps)]
    return records, manifests, receipts


def _current(receipts, admit):
    try:
        return admit() is True and all(check() is True for check in receipts)
    except Exception:
        return False


@contextmanager
def _publication_scope(guard, gate, prepare_publication):
    if prepare_publication is None:
        # Transitional callers retain their existing full-admission contract.
        # They cannot be described as using the ordered prepared witness path.
        with gate():
            yield guard(),None
        return
    # Full source/config/SQL checks and source-issued stamp preparation occur
    # before the outer application lease and before the Runtime gate.
    witness = prepare_publication() if guard() else None
    with ExitStack() as stack:
        try:
            lease = stack.enter_context(ordered_publication(witness))
        except PublicationUnavailable:
            yield False,None
            return
        stack.enter_context(gate())
        yield lease.current(),lease


def context_for(anchors, records):
    """Transient lexical retrieval proposes candidates, never assigns meaning."""
    def words(text):
        return set(re.findall(r"[^\W_]{3,}", text.casefold(), re.UNICODE))
    selected = list(anchors)
    ids = {r["id"] for r in selected}
    for anchor in anchors:
        terms = words(anchor["text"])
        ranked = []
        for r in records:
            if r["source"] == anchor["source"] or r["id"] in ids:
                continue
            # Pick a bounded passage around shared text, without retaining a
            # persistent body index or introducing topic/app/site categories.
            matches = [m.start() for m in re.finditer(r"[^\W_]{3,}", r["text"], re.UNICODE) if m.group().casefold() in terms]
            if not matches:
                continue
            start = max(0, matches[0]-160)
            text = r["text"][start:start+PASSAGE_CHARS].encode()[:PASSAGE_BYTES].decode("utf-8", errors="ignore")
            other = words(text)
            score = len(terms & other) / max(1, len(terms | other))
            ranked.append((score, r["id"], dict(r, text=text, text_start=start)))
        for _, ident, r in sorted(ranked, key=lambda v: (-v[0], v[1]))[:2]:
            if len(selected) >= 8:
                break
            if ident not in ids:
                selected.append(r)
                ids.add(ident)
    result, size = [], 0
    for r in selected:
        # Opaque native references are unnecessary in a semantic prompt. Keep
        # them only in the current host snapshot for later source resolution.
        r = {k:v for k,v in r.items() if k not in {"reference", "publication_at", "ordering_at"}}
        n = len(json.dumps(r, ensure_ascii=False).encode())
        if size+n <= MAX_INPUT_BYTES:
            result.append(r)
            size += n
    return result


class Seed:
    def __init__(self, index):
        self.index = index

    def run(self, snapshots, *, epoch, now, analysis_enabled=False, analyze=None, admit=lambda: False,
            publication=nullcontext, timezone="UTC", prepare_publication=None):
        if prepare_publication is not None:
            with self.index.store._lock:
                if self.index.store._conn.in_transaction:
                    raise CallerTransactionActive("Seed preparation was not applied inside a caller-owned transaction")
        if analysis_enabled is not True:
            with revocation(),publication():
                self.index.reset()
            return dict(state="off", sources=self.index.status([]), analyzed=0)
        # Rotate finite byte-capped input before normalization so completed
        # prefix records cannot keep an unread tail out of every later pass.
        snapshots = tuple(replace(s,records=self.index.prioritize(s.source,s.records))
                          if isinstance(s,Snapshot) and isinstance(s.records,tuple)
                             and s.enabled is True and s.available is True else s for s in snapshots)
        records, manifests, receipts = prepare(snapshots)
        guard = lambda: _current(receipts, admit)
        with _publication_scope(guard,publication,prepare_publication) as (current,lease):
            if not current:
                return dict(state="unavailable", sources={}, analyzed=0)
            self.index.sync(records, manifests, epoch=epoch, now=now)
            anchors = self.index.select(records, now=now)
        context = context_for(anchors, records)
        included = {r["id"] for r in context}
        anchors = [r for r in anchors if r["id"] in included]
        state = "waiting" if anchors else "idle"
        analyzed = 0
        if anchors and analyze is not None and guard():
            try:
                # An analyzer cannot mutate the authoritative passages it cites.
                response = analyze(deepcopy(context), admit=guard)
                if (not isinstance(response, dict) or not {"links"} <= set(response) <= {"links", "demands", "processed_ids"}
                        or not isinstance(response["links"], list) or not isinstance(response.get("demands", []), list)):
                    raise ValueError("Unusable seed interpretation")
                processed = response.get("processed_ids", [r["id"] for r in context])
                if not isinstance(processed, list) or any(not isinstance(i, str) or i not in included for i in processed):
                    raise ValueError("Unknown analyzed record")
                anchors = [r for r in anchors if r["id"] in processed]
                from service.behavior.associations import MAX_LINKS, validate
                if (len(json.dumps(response, ensure_ascii=False).encode()) > 16000
                        or len(response["links"]) > MAX_LINKS
                        or any(not validate([p], context) for p in response["links"])):
                    raise ValueError("Ungrounded seed interpretation")
                from service.behavior.associations import work_metadata
                from service.assistant.behavior_interpreter import temporal_choices
                grounded = [dict(r, temporal=temporal_choices(r["text"],
                                 r["source_time"] if r["time_basis"] == "source_timestamp" else None,
                                 timezone)) for r in context]
                work = work_metadata(response.get("demands", []), grounded, now=now, timezone=timezone)
                with _publication_scope(guard,publication,prepare_publication) as (current,lease):
                    if current and anchors and (self.index.finish_prepared(anchors,context,response["links"],
                            epoch=epoch,now=now,lease=lease,work=work) if lease is not None else
                            self.index.finish(anchors,context,response["links"],epoch=epoch,now=now,admit=guard,work=work)):
                        state, analyzed = "advanced", len(anchors)
                    else:
                        state = "cancelled"
            except CallerTransactionActive:
                # Deferring under a connection context would commit the very
                # outer transaction finish_prepared explicitly refused to own.
                raise
            except Exception:
                with _publication_scope(guard,publication,prepare_publication) as (current,lease):
                    if current:
                        self.index.defer(anchors, epoch=epoch, now=now)
                        state = "deferred"
                    else:
                        state = "cancelled"
        return dict(state=state, sources=self.index.status(records) if guard() else {}, analyzed=analyzed,
                    detail="Progress covers supplied bounded records, not an exhaustive account or device history.")

    def materialize(self, snapshots, *, epoch, admit=lambda: False, limit=20):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Invalid relation view limit")
        records, _, receipts = prepare(snapshots)
        if self.index._epoch() != epoch or not _current(receipts, admit):
            return []
        result = []
        for edge in self.index.edges():
            view = materialize(edge, records)
            if view is not None:
                result.append(view)
            if len(result) >= limit:
                break
        return result if self.index._epoch() == epoch and _current(receipts, admit) else []

    def demands(self, snapshots, *, epoch, now, default_minutes=30, admit=lambda: False):
        from service.behavior.associations import materialize_work
        records, _, receipts = prepare(snapshots)
        if self.index._epoch() != epoch or not _current(receipts, admit):
            return []
        values = [v for w in self.index.work()
                  if (v := materialize_work(w, records, now=now, default_minutes=default_minutes)) is not None]
        values.sort(key=lambda d: (d["due_ts"] if d["due_ts"] is not None else float("inf"), d["id"]))
        return values[:100] if self.index._epoch() == epoch and _current(receipts, admit) else []

"""Pure initial-seed orchestration over supplied, permitted cached snapshots.

No getters, providers, native readers, model transport or scheduler activation.
The host supplies an analyzer and fresh receipt checks. Default is OFF. Saved
progress is not saved permission: materialization always needs current evidence.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re

from service.behavior.associations import materialize
from service.behavior.source_index import PASSAGE_CHARS, SOURCES

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


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _range(value):
    return list(value) if (isinstance(value, (list, tuple)) and len(value) == 2
                           and all(_number(v) for v in value) and value[0] < value[1]) else None


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
                    scope=snap.scope if snap.scope in {"cached_publication", "retained_history"} else "cached_publication",
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
            if not isinstance(raw, dict):
                info["skipped"] += 1
                continue
            identity, text = raw.get("id"), raw.get("text")
            if not isinstance(identity, str) or not 1 <= len(identity) <= 1000 or not isinstance(text, str) or not text.strip():
                info["skipped"] += 1
                continue
            size = len(text.encode("utf-8"))
            if len(text) > MAX_RECORD_CHARS or used+size > MAX_BYTES or source_bytes+size > MAX_SOURCE_BYTES:
                info["omitted"] += 1
                continue
            ident = source + ":" + hashlib.sha256(identity.encode()).hexdigest()[:32]
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
            basis = raw.get("evidence_basis", "unknown")
            if basis not in {"scheduled", "recorded_completion", "observed", "source_statement", "unknown"}:
                basis = "unknown"
            if source == "calendar":
                basis = "scheduled"
            elif source in {"email", "messages", "notes"}:
                basis = "source_statement"
            time_basis, stamp = raw.get("time_basis", "unknown"), raw.get("source_time")
            if time_basis not in {"source_timestamp", "scheduled_time", "completion_time"} or not _number(stamp):
                time_basis, stamp = "unknown", None
            # Notes lack an original utterance date. Even a caller-supplied
            # modified_at/source_time must not anchor relative words in prose.
            if source == "notes":
                time_basis, stamp = "unknown", None
            revision = hashlib.sha256(json.dumps([text, stamp, time_basis, basis], ensure_ascii=False).encode()).hexdigest()
            records.append(dict(id=ident, source=source, revision=revision, text=text,
                                source_time=stamp, time_basis=time_basis, evidence_basis=basis))
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
            text = r["text"][start:start+PASSAGE_CHARS]
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
        n = len(json.dumps(r, ensure_ascii=False).encode())
        if size+n <= MAX_INPUT_BYTES:
            result.append(r)
            size += n
    return result


class Seed:
    def __init__(self, index):
        self.index = index

    def run(self, snapshots, *, epoch, now, analysis_enabled=False, analyze=None, admit=lambda: False):
        if analysis_enabled is not True:
            self.index.reset()
            return dict(state="off", sources=self.index.status([]), analyzed=0)
        records, manifests, receipts = prepare(snapshots)
        guard = lambda: _current(receipts, admit)
        if not guard():
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
                if not isinstance(response, dict) or set(response) != {"links"} or not isinstance(response["links"], list):
                    raise ValueError("Unusable seed interpretation")
                from service.behavior.associations import MAX_LINKS, validate
                if (len(json.dumps(response, ensure_ascii=False).encode()) > 16000
                        or len(response["links"]) > MAX_LINKS
                        or any(not validate([p], context) for p in response["links"])):
                    raise ValueError("Ungrounded seed interpretation")
                if guard() and self.index.finish(anchors, context, response["links"], epoch=epoch, now=now, admit=guard):
                    state, analyzed = "advanced", len(anchors)
                else:
                    state = "cancelled"
            except Exception:
                if guard():
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

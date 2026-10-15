"""Read-only evidence adapters over public, current cached publications.

No refresh, provider access or scan. Raw text lives only in this bounded batch.
The receipt must still be current at publication and at every planning read.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
import math
import time

from service.assistant.behavior_seed import Snapshot


@dataclass
class Batch:
    records: list
    visits: list
    receipts: list
    detail: dict
    snapshots: tuple = ()

    def current(self):
        try:
            return all(check() for check in self.receipts)
        except Exception:
            return False


def _notes_eligible(snapshot, now):
    receipt = snapshot.latest_receipt
    return (snapshot.origin == "live" and receipt.get("available") is True and snapshot.malformed == 0
            and all(type(v) in (int, float) and math.isfinite(v) and 0 < v <= now
                    for v in (snapshot.body_started_at, snapshot.body_received_at))
            and now-snapshot.body_started_at <= 86400
            and receipt.get("started_at") == snapshot.body_started_at
            and receipt.get("received_at") == snapshot.body_received_at)


def _gmail_records(publications):
    """Adapt already permitted source-owned caches, never acquire history here.

    Each callable must be a public source snapshot getter. Captured receipts
    recheck only the exact complete records used, so another page publication
    cannot invalidate unchanged evidence or admit a partial body.
    """
    from service.gmail_behavior_history import HistorySnapshot
    rows, checks, stamps, omitted = [], [], [], 0
    try:
        getters = publications() if callable(publications) else ()
        if not isinstance(getters, (tuple, list)):
            return rows, checks, stamps, omitted
        for getter in getters:
            if not callable(getter):
                continue
            try:
                snapshot = getter()
                if not isinstance(snapshot, HistorySnapshot):
                    continue
                selected = tuple(r for r in snapshot.records if r.body.eligible)
                omitted += len(snapshot.records)-len(selected)
                if not selected:
                    continue
                refs = tuple(r.ref for r in selected)
                def current(g=getter, receipt=snapshot.receipt, refs=refs):
                    value = g(expected_receipt=receipt, required_refs=refs)
                    return (isinstance(value, HistorySnapshot) and value.complete is True
                            and not value.missing_refs and tuple(r.ref for r in value.records) == refs)
                if not current():
                    continue
                checks.append(current)
                stamps.append(snapshot.receipt.read_started_at)
                for r in selected:
                    identity = json.dumps(["gmail", r.account.account_id, r.resource_id], separators=(",", ":"))
                    rows.append(dict(id="email:"+hashlib.sha256(identity.encode()).hexdigest()[:24],
                        source="email", text=r.body.text, ts=None, source_time=None, time_basis="unknown",
                        ordering_at=r.provider_internal_at, direction="unknown", evidence_basis="source_statement",
                        reference=dict(source="email", provider="gmail", account_id=r.account.account_id,
                            resource_id=r.resource_id, connection_revision=r.account.connection_revision,
                            account_set_revision=r.account.account_set_revision)))
            except Exception:
                # An invalid Gmail publication cannot suppress valid native Mail.
                continue
    except Exception:
        pass
    return rows, checks, stamps, omitted


def capture(settings, getters=None, *, retained=(), now=None, gmail_history_getters=None):
    # Inject getters in tests: importing an adapter never reads a user's cache.
    if getters is None:
        getters = {}
        if settings.get("email"):
            from service.tools.email_tools import raw_email_snapshot

            getters["email"] = raw_email_snapshot
        if settings.get("messages"):
            from service.tools.imessage_tools import (
                behavior_messages_snapshot_with_receipt,
            )

            getters["messages"] = behavior_messages_snapshot_with_receipt
        if settings.get("websites"):
            from service.tools.browser_history_tools import current_browser_snapshot

            getters["websites"] = current_browser_snapshot
        if settings.get("notes"):
            from service.tools.notes_tools import raw_notes_snapshot
            getters["notes"] = raw_notes_snapshot
    if gmail_history_getters is not None:
        getters = dict(getters, gmail_history=gmail_history_getters)
    now = time.time() if now is None else now
    records, visits, receipts, detail, seeds = [], [], [], {}, []
    for source in ("email", "messages", "notes", "websites"):
        if not settings.get(source):
            detail[source] = "off"
            if source != "websites":
                seeds.append(Snapshot(source))
            continue
        getter = getters.get(source)
        try:
            snapshot = getter() if getter else None
        except Exception:
            snapshot = None
        if snapshot is None:
            detail[source] = "current complete evidence unavailable"
            if source != "websites":
                seeds.append(Snapshot(source, enabled=True))
            continue
        detail[source] = "bounded current publication; not full history"
        if source == "websites":
            receipt = snapshot["receipt"]
            receipts.append(
                lambda g=getter, token=receipt: g(expected_receipt=token) is not None
            )
            visits = [dict(v) for v in snapshot["rows"]]
            continue
        if source == "notes":
            if not _notes_eligible(snapshot, now):
                detail[source] = "Current complete live Notes publication unavailable; modification time is not prose time."
                seeds.append(Snapshot(source, enabled=True))
                continue
            token = (snapshot.generation, snapshot.raw_digest, snapshot.body_started_at, snapshot.body_received_at)
            def note_current(g=getter, token=token):
                s = g()
                return (_notes_eligible(s, time.time())
                        and (s.generation, s.raw_digest, s.body_started_at, s.body_received_at) == token)
            receipts.append(note_current)
            rows = tuple(dict(id=r["reference"], reference={"source": "notes", "reference": r["reference"]},
                              text=f"Title: {r['title']}\n{r['body']}", source_time=None,
                              time_basis="unknown", evidence_basis="source_statement") for r in snapshot.records)
            seeds.append(Snapshot(source, rows, enabled=True, available=True, current=note_current,
                                  publication_complete=True, omitted=snapshot.omitted, published_at=snapshot.body_started_at))
            detail[source] = "Current Notes contents only (at most 500); no revisions or dated prose inferred."
            continue
        generation = (
            snapshot.generation if source == "email" else snapshot["generation"]
        )
        record_scope = snapshot.get("record_scope") if source == "messages" else None
        if source == "messages" and record_scope is not None:
            proof = record_scope["proof"]
            check = lambda g=getter, token=generation, proof=proof: g(expected_generation=token, expected_proof=proof) is not None
            detail[source] = (f"Complete-record subset: {record_scope['selected']} eligible, "
                f"{record_scope['excluded_partial']} individually partial excluded; aggregate "
                f"{snapshot['coverage']['status']} coverage. Full history and absence remain unknown.")
        else:
            check = lambda g=getter, token=generation: g(expected_generation=token) is not None
        receipts.append(check)
        rows = snapshot.rows if source == "email" else snapshot["records"]
        source_records = []
        for r in rows:
            if source == "email":
                identity = f"{r['account_id']}:{r['message_id']}"
                text = f"Subject: {r['subject']}\n{r['body']}"
                ts, direction = r["ts"], "unknown"
                reference = dict(
                    source="email",
                    account_id=r["account_id"],
                    message_id=r["message_id"],
                )
            else:
                identity = str(r["identity"])
                text = r["text"]
                try:
                    ts = datetime.fromisoformat(
                        r["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, TypeError):
                    continue
                direction = r.get("direction", "unknown")
                reference = dict(source="messages", identity=identity)
            ident = source + ":" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            if text.strip():
                record = dict(
                        id=ident,
                        source=source,
                        reference=reference,
                        text=text,
                        ts=ts,
                        direction=direction,
                        source_time=ts, time_basis="source_timestamp", evidence_basis="source_statement",
                    )
                source_records.append(record)
                records.append(dict(record, text=text[:2400]))
        seeds.append(Snapshot(source, tuple(source_records), enabled=True, available=True, current=check,
                              publication_complete=(record_scope["publication_complete"] if record_scope is not None else True),
                              omitted=(record_scope["known_omitted"] if record_scope is not None else 0),
                              published_at=snapshot.synced_at if source == "email" else snapshot.get("captured_at")))
    if settings.get("email") and getters.get("gmail_history") is not None:
        history_rows, history_checks, stamps, omitted = _gmail_records(getters["gmail_history"])
        if history_rows:
            original = next(s for s in seeds if s.source == "email")
            checks = ([original.current] if original.available else []) + history_checks
            combined_current = lambda checks=tuple(checks): all(check() is True for check in checks)
            if original.available and type(original.published_at) in (int, float):
                stamps.append(original.published_at)
            combined = Snapshot("email", original.records+tuple(history_rows), enabled=True, available=True,
                current=combined_current, publication_complete=False, omitted=original.omitted+omitted,
                published_at=min(stamps), scope="cached_publication")
            seeds = [combined if s.source == "email" else s for s in seeds]
            receipts.extend(history_checks)
            records.extend(dict(r, text=r["text"][:2400]) for r in history_rows)
            detail["email"] = (f"{len(history_rows)} current complete Gmail history bodies; bounded cached subset, "
                "original event time and exhaustive mailbox/semantic coverage unknown. "
                + ("Native Mail evidence also available." if original.available else "Native Mail unavailable."))
    for snapshot in retained:
        seeds.append(snapshot)
        if snapshot.available:
            receipts.append(snapshot.current)
        detail[snapshot.source] = "Retained 180-day evidence, not attendance or exhaustive history." if snapshot.available else "Current permitted retained evidence unavailable."
    records.sort(key=lambda r: r.get("ts") if type(r.get("ts")) in (int, float)
                 else r.get("ordering_at") or 0, reverse=True)
    return Batch(records[:8], visits[:10000], receipts, detail, tuple(seeds))

"""Read-only evidence adapters over public, current cached publications.

No refresh, provider access or scan. Raw text lives only in this bounded batch.
The receipt must still be current at publication and at every planning read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Batch:
    records: list
    visits: list
    receipts: list
    detail: dict

    def current(self):
        try:
            return all(check() for check in self.receipts)
        except Exception:
            return False


def capture(settings, getters=None):
    # Inject getters in tests: importing an adapter never reads a user's cache.
    if getters is None:
        getters = {}
        if settings.get("email"):
            from service.tools.email_tools import raw_email_snapshot

            getters["email"] = raw_email_snapshot
        if settings.get("messages"):
            from service.tools.imessage_tools import (
                structured_messages_snapshot_with_receipt,
            )

            getters["messages"] = structured_messages_snapshot_with_receipt
        if settings.get("websites"):
            from service.tools.browser_history_tools import current_browser_snapshot

            getters["websites"] = current_browser_snapshot
    records, visits, receipts, detail = [], [], [], {}
    for source in ("email", "messages", "websites"):
        if not settings.get(source):
            detail[source] = "off"
            continue
        getter = getters.get(source)
        try:
            snapshot = getter() if getter else None
        except Exception:
            snapshot = None
        if snapshot is None:
            detail[source] = "current complete evidence unavailable"
            continue
        detail[source] = "bounded current publication; not full history"
        if source == "websites":
            receipt = snapshot["receipt"]
            receipts.append(
                lambda g=getter, token=receipt: g(expected_receipt=token) is not None
            )
            visits = [dict(v) for v in snapshot["rows"]]
            continue
        generation = (
            snapshot.generation if source == "email" else snapshot["generation"]
        )
        receipts.append(
            lambda g=getter, token=generation: g(expected_generation=token) is not None
        )
        rows = snapshot.rows if source == "email" else snapshot["records"]
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
                records.append(
                    dict(
                        id=ident,
                        source=source,
                        reference=reference,
                        text=text[:2400],
                        ts=ts,
                        direction=direction,
                    )
                )
    records.sort(key=lambda r: r["ts"], reverse=True)
    return Batch(records[:8], visits[:10000], receipts, detail)

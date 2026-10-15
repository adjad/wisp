"""Validate generic source-backed proposals. No topic or phrase classifiers.

The host supplies evidence and mechanically grounded temporal choices. The
interpreter can select a choice, never invent an authoritative time or action.
Results remain tentative and have no execution path here.
"""

from __future__ import annotations

import hashlib


def validate(proposals, records, *, now, default_minutes=30):
    sources = {r["id"]: r for r in records}
    out, seen = [], set()
    if not isinstance(proposals, list):
        return out
    for p in proposals[:20]:
        if not isinstance(p, dict) or set(p) - {
            "source_id",
            "quote",
            "kind",
            "title",
            "temporal_id",
            "ownership",
            "importance",
        }:
            continue
        record = sources.get(p.get("source_id"))
        quote, title = p.get("quote"), p.get("title")
        if (
            record is None
            or not isinstance(quote, str)
            or not 8 <= len(quote) <= 800
            or quote not in record["text"]
            or not isinstance(title, str)
            or not 1 <= len(title) <= 160
            or p.get("kind") not in {"preparation", "task", "meeting_request"}
            or p.get("ownership") not in {"user", "unknown"}
        ):
            continue
        # No sender-direction shortcut: a received document may describe somebody
        # else's task. Unknown ownership is review context, not a schedulable demand.
        temporal = next(
            (
                t
                for t in record.get("temporal", [])
                if t["id"] == p.get("temporal_id") and t["quote"] in quote
            ),
            None,
        )
        ident = hashlib.sha256(
            f"{record['id']}:{p['kind']}:{quote}".encode()
        ).hexdigest()[:24]
        if ident in seen:
            continue
        seen.add(ident)
        schedulable = (
            p["ownership"] == "user" and temporal is not None and temporal["end"] > now
        )
        out.append(
            dict(
                id="demand:" + ident,
                source=record["source"],
                reference=record["reference"],
                source_id=record["id"],
                kind=p["kind"],
                title=title,
                quote=quote,
                ownership=p["ownership"],
                status="tentative" if schedulable else "unresolved",
                due_ts=(
                    temporal["start"]
                    if temporal and temporal["precision"] == "instant"
                    else None
                ),
                due_range=[temporal["start"], temporal["end"]] if temporal else None,
                deadline_precision=temporal["precision"] if temporal else "unknown",
                earliest_start=now,
                suggested_minutes=default_minutes,
                duration_basis="adjustable default; not observed effort",
                importance="unknown",
                importance_hypothesis=(
                    p.get("importance")
                    if p.get("importance") in {"high", "normal", "low"}
                    else "unknown"
                ),
                urgency_basis="source time constraint" if temporal else "unknown",
                basis="inferred",
                observed_at=record["ts"],
                evidence=[record["id"]],
                because="Interpreted from a source passage; relevance and required effort remain uncertain.",
            )
        )
    from service.behavior.priorities import dimensions

    return [{**d, "priority_dimensions": dimensions(d, now=now)} for d in out]

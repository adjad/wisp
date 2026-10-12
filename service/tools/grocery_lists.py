"""Bounded shopping evidence. No completion, quantity merging or writes."""
from __future__ import annotations

import json
import math
import re
import time

from service.tools.registry import register

_SCHEMA = {"type": "object", "properties": {
    "list_label": {"type": "string", "description": "Exact list label (Notes title/folder or Reminder list context), not an item query."},
    "query": {"type": "string", "description": "Literal item text filter, e.g. milk. Never use a list label as an item query."},
    "label_kind": {"type": "string", "enum": ["list", "note", "folder"], "description": "Notes selector kind. Reminder evidence supports list only."},
}}
_CHECKBOX = re.compile(r"^\s*(?:[-*+]\s+)?\[([ xX])\]\s*(.*)$")
_OUTPUT_BUDGET = 9_000


def _valid(list_label, query, label_kind="list"):
    return (isinstance(list_label, str) and isinstance(query, str)
            and len(list_label) <= 200 and len(query) <= 200
            and (not list_label or bool(list_label.strip())) and (not query or bool(query.strip()))
            and isinstance(label_kind, str) and label_kind in {"list", "note", "folder"})


def _bounded(records: list[dict], budget: int = _OUTPUT_BUDGET) -> tuple[list[dict], int]:
    kept, used = [], 0
    for record in records:
        size = len(json.dumps(record, ensure_ascii=True))
        if len(kept) >= 100 or used + size > budget:
            continue
        kept.append(record)
        used += size
    return kept, len(records) - len(kept)


def _render(payload: dict) -> str:
    """Keep metadata and whole records below the agent's 12k result cap."""
    counts = payload if "output_omitted" in payload else payload["counts"]
    while True:
        text = json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False)
        if len(text) <= 11_000:
            return text
        if not payload["records"]:
            return "(error: shopping evidence metadata exceeds the bounded response; narrow scope; no action occurred.)"
        payload["records"].pop()
        counts["output_omitted"] += 1
        counts["shown"] = len(payload["records"])


@register("grocery_reminder_evidence",
    "Read bounded incomplete shopping candidates from Apple Reminders, with exact list-label/item filters, native IDs and source freshness. No quantities merged; no writes.",
    _SCHEMA, category="assistant_read")
async def grocery_reminder_evidence(list_label: str = "", query: str = "", label_kind: str = "list") -> str:
    if not _valid(list_label, query, label_kind) or label_kind != "list":
        return "(error: shopping filters must be strings of at most 200 characters; no source was read.)"
    from service.tools.assistant_tools import assistant_store
    from service.tools.everyday import _native_receipt_current
    snapshot = assistant_store.raw_reminder_list_snapshot(list_label=list_label, query=query)
    current = _native_receipt_current(snapshot["receipt"], time.time())
    undated_current = current and snapshot["undated_view"].get("freshness") == "current"
    records, withheld = [], 0
    for row in snapshot["records"]:
        if not current or row["due_state"] == "undated" and not undated_current:
            withheld += 1
            continue
        records.append({"source": "reminders", "native_id": row["source_id"],
            "wisp_id": row.get("id"), "text": row["title"], "list_label": row.get("context"),
            "list_id": None, "account_id": None, "checked_state": "incomplete_native_snapshot",
            "due_state": row["due_state"], "due_at": row["when_ts"]})
    kept, output_omitted = _bounded(records)
    return _render({"source": "reminders", "list_label_filter": list_label, "item_filter": query,
        "source_state": "current_cached_read" if current else "unknown_stale_or_unavailable",
        "undated_state": snapshot["undated_view"],
        "counts": {key: snapshot[key] for key in ("dated_cached", "dated_inspected", "scan_omitted", "undated_cached", "matching", "omitted")},
        "withheld_unverified": withheld, "shown": len(kept), "output_omitted": output_omitted,
        "limitations": ["Native feed contains incomplete items only; absence does not prove completion.",
            "Matching counts cover inspected rows only; scan, snapshot and output omissions are not empty lists.",
            "List/account IDs are unavailable; an exact label does not uniquely identify a native list.",
            "Keep each native ID and complete quantity/unit/variant wording separate; never sum or mark items automatically."],
        "records": kept})


@register("grocery_note_evidence",
    "Read bounded plaintext shopping-list evidence from Notes with original line wording and explicit [ ]/[x] state only. Unmarked state and native IDs are unknown; no writes or quantity merging.",
    _SCHEMA, category="assistant_read")
async def grocery_note_evidence(list_label: str = "", query: str = "", label_kind: str = "list") -> str:
    if not _valid(list_label, query, label_kind):
        return "(error: shopping filters must be strings of at most 200 characters; no source was read.)"
    from service.tools.notes_tools import raw_notes_snapshot
    snapshot = raw_notes_snapshot()
    label, needle = list_label.strip().casefold(), query.strip().casefold()
    records, matching_notes = [], 0
    for note in snapshot.records:
        values = ({note["title"].strip().casefold()} if label_kind == "note" else
                  {note["folder"].strip().casefold()} if label_kind == "folder" else
                  {note["title"].strip().casefold(), note["folder"].strip().casefold()})
        if label and label not in values:
            continue
        matching_notes += 1
        for number, line in enumerate(note["body"].splitlines(), 1):
            if not line.strip() or needle and needle not in line.casefold():
                continue
            match = _CHECKBOX.fullmatch(line)
            state = "unknown"
            if match:
                state = "checked_plaintext" if match[1].casefold() == "x" else "unchecked_plaintext"
            records.append({"source": "notes", "reference": note["reference"], "native_id": None,
                "title": note["title"], "folder": note["folder"], "line": number,
                "text": line, "checked_state": state, "modified_at": note["modified_at"]})
    kept, output_omitted = _bounded(records)
    now = time.time()
    stamp = snapshot.body_started_at or snapshot.body_received_at
    age = now - stamp if type(stamp) in (int, float) and math.isfinite(stamp) and stamp > 0 else None
    if age is not None and (not math.isfinite(age) or age < 0):
        age = None
    receipt = dict(snapshot.latest_receipt)
    if len(str(receipt["reason"])) > 500:
        receipt["reason"] = str(receipt["reason"])[:500]
        receipt["reason_truncated"] = True
    current_body = (snapshot.origin == "live" and receipt["available"] is True
                    and snapshot.body_received_at == receipt["received_at"])
    return _render({"source": "notes", "list_label_filter": list_label, "label_kind": label_kind, "item_filter": query,
        "generation": snapshot.generation, "origin": snapshot.origin,
        "body_received_at": snapshot.body_received_at, "body_started_at": snapshot.body_started_at,
        "latest_receipt": receipt, "held_body_is_latest_available_publish": current_body,
        "body_age_seconds": age, "raw_digest": snapshot.raw_digest,
        "counts": {"raw_records": snapshot.raw_records, "malformed": snapshot.malformed,
                   "snapshot_omitted": snapshot.omitted, "matching_notes": matching_notes,
                   "matching_lines": len(records), "shown": len(kept), "output_omitted": output_omitted},
        "limitations": ["Bounded cached plaintext; native reader caps at 500 notes and exhaustive coverage is unknown.",
            "A restored or failed-read body is historical evidence, not verified current shopping state.",
            "Only literal [ ]/[x] marks plaintext state; native checkbox state and stable Notes/list/account IDs are unavailable.",
            "Unmarked lines may be context rather than items; preserve quantities/variants and conflicts across sources without summing."],
        "records": kept})

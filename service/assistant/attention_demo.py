"""Runtime for ``uncaptured_commitment``: read messages, detect, add, alert.

Lives beside the store it writes; the pure detector is ``service.attention.detectors``
(the Slice 0 package may not import ``service.assistant``).

Off unless ``WISP_ATTENTION_DEMO=1``. ``WISP_ATTENTION_SYNTHETIC_ONLY=1`` bypasses
the live Messages reader entirely. Rules run first; one eligible synthetic miss
per tick may use the idle, resident local model. For every validated hit it:

1. adds a commitment to Wisp's own store (never Apple Reminders or Calendar), and
2. publishes a ``reminder``-shaped hub event, so the existing app notification
   path shows the source quote and "Added to Wisp's schedule."

Overlay messages live in ``<WISP_HOME>/demo/messages.json``. They are never written
into the Messages cache (the Swift reader replaces that wholesale), and the
commitments they produce use the QA seed source, so they are hidden unless
``WISP_QA_SEED=1`` and ``wisp_demo.py clear`` removes them.

Quiet by default: at most ``DAILY_CAP`` alerts per local day, one per message.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from service.attention.corpus import Item
from service.attention.detectors import HORIZON_S, REASON, Candidate, already_on_file, detect
from service.paths import MOE_DIR

DAILY_CAP = 3
LIVE_WINDOW_S = 12 * 3600
KEY_PREFIX = "attention:"
OVERLAY_ID_PREFIX = "demo:"
_last_model_item: str | None = None


def enabled() -> bool:
    return os.environ.get("WISP_ATTENTION_DEMO") == "1"


def synthetic_only() -> bool:
    return os.environ.get("WISP_ATTENTION_SYNTHETIC_ONLY") == "1"


def overlay_path() -> Path:
    return MOE_DIR / "demo" / "messages.json"


# --- overlay (synthetic messages) -------------------------------------------

def load_overlay(path: Path | None = None) -> list[Item]:
    path = path or overlay_path()
    try:
        rows = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    items: list[Item] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            items.append(Item(id=OVERLAY_ID_PREFIX + str(row["id"]), source="messages",
                              ts=float(row["ts"]), direction=str(row.get("direction", "incoming")),
                              sender=str(row["sender"]), conversation=str(row.get("conversation", row["sender"])),
                              text=str(row["text"])))
        except (KeyError, TypeError, ValueError):
            continue
    return items


def add_overlay_message(sender: str, text: str, *, ts: float | None = None,
                        direction: str = "incoming", path: Path | None = None) -> dict:
    path = path or overlay_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        rows = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        rows = []
    row = {"id": f"{int(time.time() * 1000)}-{len(rows)}", "sender": sender, "conversation": sender,
           "direction": direction, "text": text, "ts": ts if ts is not None else time.time()}
    rows.append(row)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return row


def clear_overlay(path: Path | None = None) -> bool:
    path = path or overlay_path()
    try:
        path.unlink()
        return True
    except OSError:
        return False


# --- live feed --------------------------------------------------------------

def live_items(now: float) -> list[Item]:
    """Recent incoming Messages from the current launch. A restored cache never counts."""
    try:
        from service.tools.imessage_tools import structured_messages_snapshot
        snap = structured_messages_snapshot()
    except Exception:  # noqa: BLE001 — the feed is optional
        return []
    if snap.get("state") != "ready":
        return []
    items: list[Item] = []
    for r in snap.get("records", []):
        try:
            ts = float(r["timestamp"])
            if now - ts > LIVE_WINDOW_S or r.get("direction") != "incoming":
                continue
            items.append(Item(id="msg:" + str(r["identity"]), source="messages", ts=ts,
                              direction="incoming", sender=str(r["sender"]),
                              conversation=str(r.get("conversation") or r["sender"]), text=str(r["text"])))
        except (KeyError, TypeError, ValueError):
            continue
    return items


# --- tick -------------------------------------------------------------------

def _key(item_id: str) -> str:
    namespace = "demo:" if item_id.startswith(OVERLAY_ID_PREFIX) else ""
    return KEY_PREFIX + namespace + hashlib.sha256(item_id.encode()).hexdigest()[:24]


def _sent_today(store, now: float) -> int:
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with store._lock:
        return store._db.execute(
            "SELECT COUNT(*) FROM assistant_events WHERE dedupe_key LIKE ? AND created_at >= ?",
            (KEY_PREFIX + "%", midnight)).fetchone()[0]


def _persist(store, cand: Candidate, now: float, *, evidence: Item | None = None) -> dict | None:
    """Save the commitment and its replayable alert together, with fresh checks."""
    from service.assistant.reminders import _humanize
    from service.assistant.store import MANUAL_SOURCE, QA_SEED_SOURCE

    key = _key(cand.item_id)
    with store.transaction() as db:
        if evidence is not None and evidence not in load_overlay():
            return None
        # Include hidden seed rows too: changing UI visibility must not re-arm a plan.
        known = [dict(r) for r in db.execute(
            "SELECT title,when_ts,status FROM commitments WHERE status='active' "
            "AND when_ts BETWEEN ? AND ?", (now - 3600, now + HORIZON_S + 3600))]
        if (store.event_by_key(key) or _sent_today(store, now) >= DAILY_CAP
                or not now <= cand.when_ts <= now + HORIZON_S
                or already_on_file(cand.when_ts, cand.text, known)):
            return None
        cid = uuid.uuid4().hex
        source = QA_SEED_SOURCE if cand.item_id.startswith(OVERLAY_ID_PREFIX) else MANUAL_SOURCE
        context = "No matching item in Wisp's synced schedule. Added to Wisp's schedule."
        # Legacy add_manual commits independently; use the existing transaction
        # API so an event persistence failure rolls back the commitment as well.
        db.execute(
            "INSERT INTO commitments (id,source,source_id,kind,title,context,when_ts,"
            "status,confidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, source, cid, "event", cand.quote, context, cand.when_ts, "active", 1.0, now, now))
        event = store.enqueue_event({
            "type": "reminder", "commitment_id": cid, "kind": "event",
            "title": f"{cand.sender}: {cand.quote}", "context": context, "when_ts": cand.when_ts,
            "stage": "caught", "when_label": _humanize(cand.when_ts - now),
            "reason": REASON, "quote": cand.quote, "sender": cand.sender, "why": cand.rule,
        }, dedupe_key=key, expires_at=cand.when_ts, target={
            "type": "reminder", "identity": [" ".join(cand.quote.split()).casefold(), int(cand.when_ts // 60)],
            "schedules": {cid: cand.when_ts}, "generations": {cid: store.reminder_generation(cid)},
            "when_ts": cand.when_ts, "stages": ["caught"],
        })
        if event is None:
            raise RuntimeError("Attention event was not persisted")
        return {**event["payload"], "event_id": event["id"]}


async def tick(store, hub, *, now: float | None = None, items: list[Item] | None = None) -> list[dict]:
    global _last_model_item
    from service.attention.extract import eligible_when, local_extractor

    fixed_now = now
    now = now if now is not None else time.time()
    pool = items if items is not None else (load_overlay() if synthetic_only()
                                          else live_items(now) + load_overlay())
    if synthetic_only():
        pool = [i for i in pool if i.id.startswith(OVERLAY_ID_PREFIX)]
    if not pool:
        return []
    published: list[dict] = []
    fast = detect(pool, now=now, commitments=store.active_future(now, horizon_days=3))
    fast_ids = {c.item_id for c in fast}

    async def publish(cand):
        evidence = next((i for i in pool if i.id == cand.item_id), None)
        if items is not None or not cand.item_id.startswith(OVERLAY_ID_PREFIX):
            evidence = None
        event = _persist(store, cand, fixed_now if fixed_now is not None else time.time(), evidence=evidence)
        if event is not None:
            # Failure leaves a durable pending event for the hub's normal replay.
            published.append(await hub.publish(event, dedupe_key=_key(cand.item_id)))

    for cand in fast:
        await publish(cand)
    # Local-model behavior is deliberately limited to synthetic Messages.
    # One attempt per tick caps scheduler latency even with many eligible misses.
    ordered = sorted(pool, key=lambda i: i.ts)
    previous = next((n for n, i in enumerate(ordered) if i.id == _last_model_item), None)
    if previous is not None:
        ordered = ordered[previous + 1:] + ordered[:previous + 1]
    for item in ordered:
        if _sent_today(store, now) >= DAILY_CAP:
            break
        known = store.active_future(now, horizon_days=3)
        if (not item.id.startswith(OVERLAY_ID_PREFIX) or item.id in fast_ids
                or store.event_by_key(_key(item.id))
                or local_extractor.abstained(item)
                or eligible_when(item, now=now, commitments=known) is None):
            continue
        _last_model_item = item.id
        cand = await local_extractor.extract(item, now=now, commitments=known)
        # A clear or replacement while inference ran invalidates its evidence.
        if cand is not None and (items is not None or item in load_overlay()):
            await publish(cand)
        break
    return published

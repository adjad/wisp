"""Add a Day study block to Calendar, and undo it (docs/STUDY_TAB.md, "Brief and Day").

IMPORTANT rules:
- Every change is the app's own JSON request (`native_only`), never a web page's.
- The block is rebuilt from today's Day snapshot, so only a block Day shows right now (its gap
  still free) can be booked; the tap on it is the approval for exactly that event.
- Booking goes through the verified Calendar bridge (attention_calendar.create_calendar); undo
  cancels exactly the event it made, by its own identifier (autoadd.delete_event).
- Records live in study.db meta "calendar_blocks": {block_id: {source_id, title, start, end, day}}.
"""
from __future__ import annotations

import asyncio
import logging
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from service.memory_saver_api import native_only
from service.study.store import study_exists, study_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/study", tags=["study"])

# Off: the routes are not registered and Day blocks carry no "added" key (the app hides the button).
ENABLED = True
META = "calendar_blocks"
PREFIX = "study-block:"
GONE = "That study time isn't free any more. Refresh Day and try again."
NOT_ADDED = "That study block isn't on your calendar."
BUSY = "That study block is already being added."
BAD = "Wisp couldn't read that request."
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_in_flight: set[str] = set()


class AddBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: StrictStr = Field(max_length=10)
    timezone: StrictStr = Field(max_length=100)


def _audit(event: str, **fields) -> None:
    try:
        from service.safety.audit import audit
        audit(event, **fields)
    except Exception:  # noqa: BLE001 - auditing never blocks a result
        pass


def _fail(status: str, error: str) -> dict:
    return {"ok": False, "status": status, "error": error}


def records() -> dict:
    if not study_exists():
        return {}
    value = study_store().meta(META, {}) or {}
    return value if isinstance(value, dict) else {}


def _save(block_id: str, record: dict | None) -> None:
    store = study_store()
    value = dict(store.meta(META, {}) or {})
    if record is None:
        value.pop(block_id, None)
    else:
        value[block_id] = record
    store.set_meta(META, value)


def _valid_id(block_id: str) -> bool:
    return isinstance(block_id, str) and block_id.startswith(PREFIX) and 0 < len(block_id) <= 300


def _snapshot(day: str, timezone: str) -> dict:
    from service.assistant.today_api import snapshot
    return snapshot(day, timezone)


async def _create(plan: dict) -> dict:
    from service.assistant.attention_calendar import approve_capture, create_calendar
    return await create_calendar(plan, approval=approve_capture(plan), source_current=lambda: True)


async def _delete(record: dict) -> dict:
    from service.proactive.autoadd import delete_event
    return await delete_event({"source_id": record["source_id"], "title": record["title"],
                               "start_ts": record["start"]})


@router.post("/blocks/{block_id}/add", dependencies=[Depends(native_only)])
async def add_block(block_id: str, request: Request):
    try:
        body = AddBody.model_validate(await request.json())
        if not _DAY.fullmatch(body.day):
            raise ValueError("day")
        ZoneInfo(body.timezone)
    except Exception:  # noqa: BLE001 - any unreadable body is one gentle refusal
        return _fail("blocked", BAD)
    if not _valid_id(block_id) or not study_exists():
        return _fail("blocked", GONE)
    done = records().get(block_id)
    if done and done.get("day") == body.day:
        return {"ok": True, "status": "succeeded", "block_id": block_id, "source_id": done["source_id"]}
    if block_id in _in_flight:
        return _fail("in_progress", BUSY)
    _in_flight.add(block_id)
    try:
        snap = await asyncio.to_thread(_snapshot, body.day, body.timezone)
        block = next((b for b in snap.get("blocks") or [] if isinstance(b, dict) and b.get("id") == block_id
                      and b.get("kind") == "study" and not b.get("added")), None)
        if block is None:
            _audit("study_block_calendar_add", tool="add_calendar_event", block=block_id, ok=False, status="gone")
            return _fail("blocked", GONE)
        start, end = float(block["start"]), float(block["end"])
        plan = {"capture_key": f"capture:study:{block_id}:{body.day}", "source_id": f"study:{block_id}",
                "title": str(block.get("title") or "Study").strip()[:200], "day": body.day,
                "timezone": body.timezone, "start_ts": start, "duration_min": max(1, round((end - start) / 60)),
                "all_day": False, "automatic": False}
        result = await _create(plan)
        _audit("study_block_calendar_add", tool="add_calendar_event", block=block_id, ok=result.get("ok") is True,
               status=str(result.get("status") or "unknown"), args={"start_ts": start, "duration_min": plan["duration_min"]})
        if result.get("ok") is not True or not str(result.get("source_id") or "").strip():
            return _fail(str(result.get("status") or "failed"),
                         str(result.get("error") or "Calendar didn't confirm the event."))
        source_id = str(result["source_id"]).strip()
        _save(block_id, {"source_id": source_id, "title": plan["title"], "start": start, "end": end,
                         "day": body.day})
        return {"ok": True, "status": "succeeded", "block_id": block_id, "source_id": source_id}
    except Exception as exc:  # noqa: BLE001 - never a 500 for a tap
        log.info("Study block add failed (%s)", type(exc).__name__)
        _audit("study_block_calendar_add", tool="add_calendar_event", block=block_id, ok=False, status="unknown")
        return _fail("unknown", "Wisp couldn't confirm the calendar event. Check Calendar before trying again.")
    finally:
        _in_flight.discard(block_id)


@router.post("/blocks/{block_id}/undo", dependencies=[Depends(native_only)])
async def undo_block(block_id: str):
    record = records().get(block_id) if _valid_id(block_id) else None
    if not record:
        return _fail("blocked", NOT_ADDED)
    if block_id in _in_flight:
        return _fail("in_progress", BUSY)
    _in_flight.add(block_id)
    try:
        if not str(record.get("source_id") or "").strip():
            return _fail("blocked", NOT_ADDED)
        result = await _delete(record)
        _audit("study_block_calendar_undo", tool="cancel_calendar_event", block=block_id,
               ok=result.get("ok") is True, status=str(result.get("status") or "unknown"))
        if result.get("ok") is not True:
            return _fail(str(result.get("status") or "failed"),
                         str(result.get("error") or "Calendar didn't confirm the event was removed."))
        _save(block_id, None)
        return {"ok": True, "status": "succeeded", "block_id": block_id}
    except Exception as exc:  # noqa: BLE001
        log.info("Study block undo failed (%s)", type(exc).__name__)
        _audit("study_block_calendar_undo", tool="cancel_calendar_event", block=block_id, ok=False, status="unknown")
        return _fail("unknown", "Wisp couldn't confirm the event was removed. Check Calendar.")
    finally:
        _in_flight.discard(block_id)


def mark(blocks: list[dict], current: list[dict], day: str, booked: dict) -> tuple[list[dict], list[dict]]:
    """Today's study blocks with "added"/"event_source_id", and the Day's other blocks without
    the calendar copy of a booked one. A booked block stays where it was booked."""
    mine = {k: v for k, v in (booked or {}).items() if isinstance(v, dict) and v.get("day") == day}
    out = []
    for b in blocks:
        if b.get("id") in mine:
            continue
        out.append({**b, "added": False, "event_source_id": None})
    by_id = {b.get("id"): b for b in blocks}
    for block_id, r in mine.items():
        base = by_id.get(block_id) or {"kind": "study", "task_id": None, "warnings": []}
        out.append({**base, "id": block_id, "title": r["title"], "start": r["start"], "end": r["end"],
                    "kind": "study", "added": True, "event_source_id": r["source_id"]})
    others = [c for c in current if not any(
        isinstance(c, dict) and c.get("kind") != "study" and str(c.get("title") or "").strip() == r["title"]
        and isinstance(c.get("start"), (int, float)) and abs(c["start"] - r["start"]) < 60 for r in mine.values())]
    return sorted(out, key=lambda b: (b["start"], str(b["id"]))), others

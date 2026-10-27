"""Mail-memory endpoints: the Brief's suggestions, Accept / Dismiss, the mode, and the
app's replies to read requests.

IMPORTANT rules:
- Every write is the app's own JSON request, never a browser page's (`_native_only`).
- Accept is the user's click; no agent turn runs. It refuses in view-only mode,
  refuses when the app isn't connected, and checks Reminders and Calendar for
  the same thing on the same day first. It writes through `add_reminder`, the
  verified create path, so nothing new reaches the native writer.
- The Brief gets only important suggestions due within `importance.SHOW_DAYS`;
  in shadow mode it gets none, only counts.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from service.mail_memory import bridge, importance, runner
from service.mail_memory.store import store

router = APIRouter()
HORIZON_DAYS = importance.SHOW_DAYS


def _native_only(request: Request) -> None:
    """A dependency, so it refuses before the body is even parsed."""
    if "origin" in request.headers:
        raise HTTPException(403, "Answer this from Wisp's Brief.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


class Pick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int


class Mode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str


def _zone() -> str:
    from service.assistant.attention_runner import current_zone
    tz = current_zone()
    if tz is None:
        raise HTTPException(503, "The local time zone cannot be determined.")
    return tz


def _today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def _view(row: dict) -> dict:
    who = row.get("sender_name") or row.get("sender") or ""
    return {"id": row["id"], "what": row["what"], "due": row["due"], "precision": row["precision"],
            "quote": row["evidence"], "from": "you" if row.get("box") == "sent" else who,
            "subject": row.get("subject") or "", "message_id": row.get("rfc_message_id") or "",
            "actions": ["accept", "dismiss", "open"]}


@router.get("/assistant/mail_memory")
async def suggestions() -> dict:
    current = runner.mode()
    st = store()
    out = {"mode": current, "modes": list(runner.MODES), "stats": st.stats(),
           "health": st.health(), "suggestions": []}
    if current == "live":
        from service.assistant.attention_runner import current_zone
        tz = current_zone()
        if tz is None:          # no time zone yet: nothing is due "today", so show nothing
            return out
        today = _today(tz)
        st.expire(today.isoformat())
        rows = st.suggestions(today.isoformat(), horizon=(today + timedelta(days=HORIZON_DAYS)).isoformat())
        out["suggestions"] = [_view(r) for r in rows]
    return out


async def _changed() -> None:
    from service.assistant.hub import hub
    await hub.publish({"type": "changed"}, durable=False)


def _on_file(row: dict, tz: str) -> bool:
    from service.assistant.store import assistant_store
    from service.attention.matching import find_on_file
    from service.attention.resolve import Resolved
    day = date.fromisoformat(row["due"])
    start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz)).timestamp()
    near = assistant_store.upcoming(now=start - 86400, days=3)
    return find_on_file(Resolved(None, day, False, row["what"]), row["what"], near, tz) is not None


@router.post("/assistant/mail_memory/accept", dependencies=[Depends(_native_only)])
async def accept(body: Pick) -> dict:
    from service.safety import read_only
    if read_only():
        raise HTTPException(403, "Wisp is in view-only mode; nothing was added.")
    if runner.mode() != "live":
        raise HTTPException(409, "Mail suggestions are not switched on.")
    st = store()
    row = st.item(body.id)
    tz = _zone()
    if row is not None and row["state"] == "accepting":
        raise HTTPException(409, "That suggestion is already being added.")
    if row is None or row["state"] != "new" or row["suspicious"] or not row["important"]:
        raise HTTPException(404, "That suggestion is no longer open.")
    if row["due"] < _today(tz).isoformat():
        raise HTTPException(409, "That date has passed; nothing was added.")
    from service.assistant import outbox
    if not await outbox.app_connected():
        raise HTTPException(503, "Wisp's app isn't connected; nothing was added.")
    if not st.claim(body.id):
        raise HTTPException(409, "That suggestion is already being added.")
    try:
        if _on_file(row, tz):
            st.decide(body.id, "dismissed", "already on file", from_state="accepting")
            await _changed()
            return {"ok": True, "id": body.id, "decision": "already_on_file"}
        from service.tools.assistant_tools import add_reminder
        result = await add_reminder(row["what"], row["due"], all_day=True)
    except BaseException:
        st.release(body.id)
        raise
    if not result.startswith("Reminder set:"):
        st.release(body.id)
        raise HTTPException(502, result.strip("()")[:200])
    where = "apple_reminders" if "in Apple Reminders" in result else "wisp_only"
    st.decide(body.id, "accepted", where, from_state="accepting")
    await _changed()
    return {"ok": True, "id": body.id, "decision": "accepted", "where": where, "at": time.time()}


@router.post("/assistant/mail_memory/dismiss", dependencies=[Depends(_native_only)])
async def dismiss(body: Pick) -> dict:
    if not store().decide(body.id, "dismissed"):
        raise HTTPException(404, "That suggestion is no longer open.")
    await _changed()
    return {"ok": True, "id": body.id, "decision": "dismissed"}


@router.put("/assistant/mail_memory/settings", dependencies=[Depends(_native_only)])
async def settings(body: Mode) -> dict:
    try:
        value = runner.set_mode(body.mode)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await _changed()
    return {"ok": True, "mode": value}


@router.post("/assistant/sync/mail_memory", dependencies=[Depends(_native_only)])
async def sync(body: dict) -> dict:
    """The app's reply to one mail_memory_read request (MailMemoryReader.swift)."""
    if not bridge.deliver(body):
        raise HTTPException(404, "No such request.")
    return {"ok": True}

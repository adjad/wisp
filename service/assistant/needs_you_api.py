"""The Brief's Needs you rows: Clear, and Mark done for a reminder.

Clear hides one row from ``workspace.needs_you`` (daily_workspace.needs_you) while it
still says what it said when cleared: the store keeps the item id and a fingerprint of
its text and due time, so the row stays gone across refreshes and restarts, and comes
back if the reminder changes (a new due date, a new title) or a person texts again.
Clear writes only that record. It never completes, deletes or edits a reminder, mail or
text: the user's standing rule is that Wisp never removes their reminders on its own.

Mark done completes a dated reminder through the one verified completion path
(schedule_extras.complete_commitment, the same as Wisp's complete_reminder tool): the
app completes the exact native reminder, checking its title and due time, and reads the
completion back; the row leaves only on that verified result. Both answers run only on
the user's explicit click.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/assistant/needs_you", tags=["today"])

# daily_workspace.key: "<source>:<24 hex>"; fingerprints are 24 hex as well.
_ITEM_ID = r"^[a-z_]{1,24}:[0-9a-f]{24}$"
_FINGERPRINT = r"^[0-9a-f]{24}$"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(pattern=_FINGERPRINT)


def _native_only(request: Request) -> None:
    """The app's own JSON requests only, as for the stale-reminder answers: any Origin
    header (a page) is refused, and so is a non-JSON body a browser could send without
    a preflight. Native URLSession requests carry no Origin."""
    if "origin" in request.headers:
        raise HTTPException(403, "Answer this from Wisp's Brief.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


def _store():
    from service.assistant.store import assistant_store
    return assistant_store


async def _changed() -> None:
    from service.assistant.hub import hub
    await hub.publish({"type": "changed"})


@router.post("/{item_id}/clear")
async def clear(body: Answer, request: Request, item_id: str = Path(pattern=_ITEM_ID)):
    _native_only(request)
    _store().dismiss_needs_you(item_id, body.fingerprint)
    await _changed()
    return {"ok": True, "id": item_id, "decision": "cleared"}


def reminder_where(store, matches) -> dict | None:
    """The one active dated reminder (Reminders or Wisp-only) whose commitment id
    ``matches``, with its ``duplicate_ids``, or None. The Brief's ids are digests of
    the commitment id, so they are matched here rather than parsed."""
    with store.transaction(write=False) as db:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM scheduled_commitments WHERE status='active' AND source IN ('reminders','manual') "
            "AND when_ts IS NOT NULL AND kind NOT IN ('event','meeting')")]
    match = next((cid for cid in ids if matches(cid)), None)
    row = store.get(match) if match else None
    if row is None:
        return None
    return dict(row, duplicate_ids=store.duplicate_ids(row["id"]))


def reminder_for(store, item_id: str) -> dict | None:
    """The active dated reminder a Needs you id names, or None. The id is
    daily_workspace.key("reminders", <commitment id>), for a Reminders reminder or a
    Wisp-only one alike; a Day task ("wisp:...") is never a reminder."""
    from service.assistant.daily_workspace import key
    if not item_id.startswith("reminders:"):
        return None
    return reminder_where(store, lambda cid: key("reminders", cid) == item_id)


@router.post("/{item_id}/done")
async def done(body: Answer, request: Request, item_id: str = Path(pattern=_ITEM_ID)):
    _native_only(request)
    from service.assistant.daily_workspace import clean, needs_you_fingerprint
    store = _store()
    row = reminder_for(store, item_id)
    if row is None:
        raise HTTPException(404, "That reminder is no longer in your list.")
    if needs_you_fingerprint(clean(row["title"]), row["when_ts"]) != body.fingerprint:
        # It changed since the row was drawn: complete only what the user saw.
        raise HTTPException(409, "That reminder changed; nothing was marked done.")
    from service.tools.schedule_extras import complete_commitment
    ok, message = await complete_commitment(row, store)
    if not ok:
        raise HTTPException(502, message.strip("()").removeprefix("error: ").rstrip(".") + ".")
    await _changed()
    return {"ok": True, "id": item_id, "decision": "done"}

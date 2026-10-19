"""The "keep or delete this old reminder?" question and its two answers.

Covers undated reminders (aged from creation) and dated ones a week or more overdue
(aged from their due time); each ask carries ``reason`` "undated" or "overdue".

GET lists the open questions (see stale_reminders.py for the policy). Keep records a
permanent exemption. Delete removes the reminder from Reminders.app through the same
verified native path every other reminder write uses: the app checks the title and that
the reminder still has no due date (an overdue one: still has the due time it was asked
about), removes it, and reads back that it is gone. It runs
only on the user's explicit click; nothing in Wisp deletes a reminder on its own.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from service.assistant import stale_reminders

router = APIRouter(prefix="/assistant/stale_reminders", tags=["reminders"])


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)


def _native_only(request: Request) -> None:
    """Keep and Delete are the app's own JSON requests, never a browser page's: any Origin
    header (a page, including "null") is refused, and so is a non-JSON body, which a
    browser could send without a preflight. Native URLSession requests carry no Origin."""
    if "origin" in request.headers:
        raise HTTPException(403, "Answer this from Wisp's Day tab.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


def _store():
    from service.assistant.hub import hub
    return hub.store


def _target(source_id: str) -> dict:
    """The stored reminder an answer refers to. Only a reminder the policy currently asks
    about, or already hides, can be answered; a fresh or kept one is not a question."""
    store = _store()
    row = store.stale_reminder_target(source_id)
    if row is None:
        raise HTTPException(404, "That reminder is no longer in the current Reminders list.")
    if row["stale_state"] not in stale_reminders.EXCLUDED_STATES:
        raise HTTPException(409, "That reminder is not old enough to ask about.")
    return row


async def _changed() -> None:
    from service.assistant.hub import hub
    await hub.publish({"type": "changed"})


@router.get("")
def asks():
    store = _store()
    view = store.undated_view()
    overdue_hidden = sum(r["reason"] == "overdue" for r in store.hidden_reminders())
    return {"asks": store.stale_reminder_asks(), "hidden": view.get("stale_hidden", 0) + overdue_hidden,
            "freshness": view["freshness"], "policy": stale_reminders.policy()}


@router.post("/keep")
async def keep(body: Answer, request: Request):
    _native_only(request)
    row = _target(body.source_id)
    _store().keep_stale_reminder(row["source_id"], row["title"])
    await _changed()
    return {"ok": True, "source_id": row["source_id"], "decision": "keep", "reason": row["reason"]}


@router.post("/delete")
async def delete(body: Answer, request: Request):
    _native_only(request)
    store = _store()
    if store.undated_view()["freshness"] != "current":
        raise HTTPException(409, "Reminders hasn't synced recently; nothing was deleted.")
    row = _target(body.source_id)
    from service.assistant.outbox import request as app_request
    # Undated: an explicit null due time. Overdue: the exact due time it was asked about,
    # so a reminder rescheduled since is refused natively instead of deleted.
    expected_due = None if row["reason"] == "undated" else row["when_ts"]
    result = await app_request("delete_reminder", {
        "source_id": row["source_id"], "expected_title": row["title"],
        "expected_due_ts": expected_due})
    if result.get("ok") is not True:
        # "unknown" means the app may have deleted it without confirming; say so rather
        # than offering a retry that could act on a different reminder.
        status = 409 if result.get("status") == "unknown" else 502
        raise HTTPException(status, str(result.get("error") or "Reminders did not confirm the delete."))
    await _changed()
    return {"ok": True, "source_id": row["source_id"], "decision": "deleted",
            "reason": row["reason"], "at": time.time()}

"""HTTP routes for the proactive engine (prefix /assistant/proactive).

The integrator includes this router in service/main.py:
    from service.proactive.api import router as proactive_router
    app.include_router(proactive_router)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

router = APIRouter(prefix="/assistant/proactive", tags=["proactive"])


class Analyze(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str | None = Field(default=None, max_length=320)


class Fire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: str | None = Field(default=None, max_length=5000)
    recipient: str | None = Field(default=None, max_length=320)


class TodoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    done: bool | None = None
    add_to_day: bool | None = None
    day: str | None = None
    timezone: str | None = None


def _native_only(request: Request) -> None:
    """The app's own JSON requests only: any Origin header (a web page, even
    "null") is refused, and so is a body that isn't JSON."""
    if "origin" in request.headers:
        raise HTTPException(403, "Use Wisp's Brief for this.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")


async def _body(request: Request, model):
    _native_only(request)
    try:
        data = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "Body must be a JSON object.") from exc
    if not isinstance(data, dict):
        raise HTTPException(422, "Body must be a JSON object.")
    try:
        return model(**data)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid request.") from exc


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cleared(BaseModel):
    """What the row said when the user acted on it (cycle.item_fingerprint)."""
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{24}$")


def _item_id(item_id: str) -> str:
    if not item_id or len(item_id) > 200 or any(ch.isspace() for ch in item_id):
        raise HTTPException(422, "Invalid id.")
    return item_id


@router.get("")
def proactive(account: str | None = None):
    from service.proactive.cycle import snapshot
    return snapshot(account=account)


@router.post("/analyze")
async def analyze(request: Request):
    """Analyze now: runs in the background; progress shows in workspace.proactive."""
    body = await _body(request, Analyze)
    from service.proactive.cycle import analyze as start
    return start(body.account or None)


@router.post("/cards/{card_id}/fire")
async def fire(card_id: str, request: Request):
    body = await _body(request, Fire)
    from service.proactive.executor import fire as run
    result = await run(card_id, body.draft, body.recipient)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/cards/{card_id}/dismiss")
async def dismiss(card_id: str, request: Request):
    await _body(request, Empty)
    from service.proactive.store import store
    card = store().card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="That card is gone. Refresh the Brief.")
    store().dismiss_card(card)
    return {"ok": True, "card_id": card_id}


def _find_todo(todo_id: str) -> dict | None:
    from service.proactive.cycle import snapshot
    from service.proactive.store import store
    for todo in snapshot()["todos"]:
        if todo["id"] == todo_id:
            # The snapshot strips internal fields; the stored copy keeps them.
            for stored in store().state().get("todos") or []:
                if stored.get("id") == todo_id:
                    return {**todo, "fingerprints": stored.get("fingerprints") or []}
            return todo
    return None


@router.post("/todos/{todo_id}/clear")
async def clear_todo(todo_id: str, request: Request):
    """Hide a to-do from the Brief. Never checks it off and never writes to
    Reminders, Mail or Messages; it comes back if it changes."""
    body = await _body(request, Cleared)
    from service.proactive.store import store
    store().clear_item(_item_id(todo_id), body.fingerprint)
    return {"ok": True, "todo_id": todo_id, "decision": "cleared"}


@router.post("/threads/{thread_id}/clear")
async def clear_thread(thread_id: str, request: Request):
    """Hide a mail or text thread from Needs attention. Nothing is marked read."""
    body = await _body(request, Cleared)
    from service.proactive.store import store
    store().clear_item(_item_id(thread_id), body.fingerprint)
    return {"ok": True, "thread_id": thread_id, "decision": "cleared"}


@router.post("/todos/{todo_id}/done")
async def complete_todo(todo_id: str, request: Request):
    """Complete a dated reminder's to-do in Reminders through the verified path
    (schedule_extras.complete_commitment); the row leaves only on its verified
    result. Unlike ``{"done": true}``, which only checks the row off in Wisp."""
    body = await _body(request, Cleared)
    from service.proactive.cycle import snapshot, todo_id as make_id
    from service.proactive.store import store
    todo = next((t for t in snapshot()["todos"] if t["id"] == _item_id(todo_id)), None)
    if todo is None:
        raise HTTPException(404, "That to-do is gone. Refresh the Brief.")
    if todo.get("can_complete") is not True:
        raise HTTPException(409, "Only a dated reminder can be marked done in Reminders.")
    if todo.get("fingerprint") != body.fingerprint:
        raise HTTPException(409, "That reminder changed; nothing was marked done.")
    from service.assistant.needs_you_api import reminder_where
    from service.assistant.store import assistant_store
    row = reminder_where(assistant_store, lambda cid: make_id(str(cid)) == todo_id)
    if row is None:
        raise HTTPException(404, "That reminder is no longer in your list.")
    # The live reminder, not only the snapshot, must still be what the row showed.
    from service.proactive.cycle import item_fingerprint
    if item_fingerprint(row.get("title"), row.get("when_ts")) != item_fingerprint(todo.get("title"), todo.get("due_ts")):
        raise HTTPException(409, "That reminder changed; nothing was marked done.")
    from service.tools.schedule_extras import complete_commitment
    ok, message = await complete_commitment(row, assistant_store)
    if not ok:
        raise HTTPException(502, message.strip("()").removeprefix("error: ").rstrip(".") + ".")
    store().set_todo_done(todo, True)
    store().clear_item(todo_id, body.fingerprint)
    return {"ok": True, "todo_id": todo_id, "decision": "done"}


@router.post("/todos/{todo_id}")
async def update_todo(todo_id: str, request: Request):
    body = await _body(request, TodoUpdate)
    from service.proactive.store import store
    if body.done is None and not body.add_to_day:
        raise HTTPException(status_code=422, detail="Send done or add_to_day.")
    todo = _find_todo(todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="That to-do is gone. Refresh the Brief.")
    result: dict = {"ok": True, "todo_id": todo_id}
    if body.add_to_day:
        from service.proactive.executor import add_to_day
        added = add_to_day(todo["title"], todo.get("due_ts"), day=body.day, timezone=body.timezone)
        if added.get("ok") is not True:
            return {**added, "todo_id": todo_id}
        store().mark_added_to_day(todo_id, added.get("task_id") or "")
        result["task_id"] = added.get("task_id")
    if body.done is not None:
        store().set_todo_done(todo, body.done)
        result["done"] = body.done
    return result

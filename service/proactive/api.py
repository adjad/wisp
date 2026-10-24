"""HTTP routes for the proactive engine (prefix /assistant/proactive).

The integrator includes this router in service/main.py:
    from service.proactive.api import router as proactive_router
    app.include_router(proactive_router)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

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


@router.get("")
def proactive(account: str | None = None):
    from service.proactive.cycle import snapshot
    return snapshot(account=account)


@router.post("/analyze")
async def analyze(body: Analyze | None = None):
    """Analyze now: runs in the background; progress shows in workspace.proactive."""
    from service.proactive.cycle import analyze as start
    return start((body.account if body else None) or None)


@router.post("/cards/{card_id}/fire")
async def fire(card_id: str, body: Fire):
    from service.proactive.executor import fire as run
    result = await run(card_id, body.draft, body.recipient)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/cards/{card_id}/dismiss")
def dismiss(card_id: str):
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


@router.post("/todos/{todo_id}")
def update_todo(todo_id: str, body: TodoUpdate):
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

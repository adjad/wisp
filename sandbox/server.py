"""The sandbox server: FastAPI app that is simultaneously

  1. the fake Swift app talking to the sandbox backend (sync.SyncScheduler
     pushes world state in; outbound.OutboundConsumer consumes actions out
     and posts results back), and
  2. the API the browser-based iPhone mockup + Wisp Dev chat talk to
     (everything under /sandbox/*), plus
  3. a streaming reverse proxy (/api/*) so the browser can reach the real
     backend endpoints (chiefly /agent) without service/main.py needing any
     CORS changes — same-origin, because the browser never leaves :8766.

Route namespaces are disjoint on purpose: /sandbox/* is sandbox-owned state,
/api/* is proxied straight through, static files own /. No route can
accidentally shadow another.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from sandbox import inject, proxy
from sandbox.outbound import OutboundConsumer
from sandbox.sync import CADENCES, SyncScheduler
from sandbox.world import World

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    world = World()
    client = proxy.make_client()
    sync = SyncScheduler(world, client)
    outbound = OutboundConsumer(world, client, sync=sync)
    app.state.world = world
    app.state.client = client
    app.state.sync = sync
    app.state.outbound = outbound
    sync.start()
    outbound.start()
    try:
        yield
    finally:
        await outbound.stop()
        await sync.stop()
        await client.aclose()


app = FastAPI(title="Wisp Sandbox", lifespan=lifespan)


def _world(request: Request) -> World:
    return request.app.state.world


def _sync(request: Request) -> SyncScheduler:
    return request.app.state.sync


def _outbound(request: Request) -> OutboundConsumer:
    return request.app.state.outbound


# --- world read / stream / reset -------------------------------------------

@app.get("/sandbox/world")
async def get_world(request: Request) -> JSONResponse:
    return JSONResponse(_world(request).snapshot())


@app.get("/sandbox/world/stream")
async def world_stream(request: Request) -> StreamingResponse:
    world = _world(request)
    q = world.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'hello'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            world.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                     "X-Accel-Buffering": "no"})


@app.post("/sandbox/world/reset")
async def reset_world(request: Request) -> dict:
    body = await _json_body(request)
    await _world(request).reset(rebase=bool(body.get("rebase")))
    return {"ok": True}


async def _json_body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 — empty body is a valid no-op request
        return {}


# --- act-as writes -----------------------------------------------------

@app.post("/sandbox/messages")
async def send_as(request: Request) -> dict:
    """The user acting as any character — 'send as Mom' — appends directly
    to a thread. Distinct from an outbound `send_message` action, which is
    Wisp speaking as the user; this is the user authoring incoming content."""
    body = await _json_body(request)
    world = _world(request)
    thread_id = body.get("thread_id")
    sender = str(body.get("from") or "")
    text = str(body.get("text") or "")
    if not text:
        return {"ok": False, "error": "text is required"}

    async def _do(state: dict) -> set[str]:
        thread = state["threads"].get(thread_id) if thread_id else None
        if thread is None and thread_id is None and sender:
            # No explicit thread_id: reuse the existing DM with this sender
            # (matched by name, case-insensitively) rather than silently
            # forking a duplicate "Mom" conversation every time.
            thread = next((t for t in state["threads"].values()
                          if t["kind"] == "dm" and
                          (t.get("context_label") or "").casefold() == sender.casefold()),
                         None)
        if thread is None:
            tid = world.new_id("t")
            thread = {"id": tid, "kind": "dm", "title": None,
                     "context_label": sender or "Unknown", "participants": [],
                     "draft": None, "messages": []}
            state["threads"][tid] = thread
        who = "me" if sender.lower() in ("me", "") else sender
        thread["messages"].append({"id": world.new_id("m"), "ts_off": world.rel_ts(),
                                   "from": who, "text": text, "via": "sandbox_ui"})
        return {"messages"}

    dirty = await world.mutate(_do)
    _sync(request).kick(*dirty)
    return {"ok": True}


@app.post("/sandbox/threads")
async def create_thread(request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)
    is_group = bool(body.get("is_group"))
    title = body.get("title")
    participant_ids = body.get("participants") or []

    async def _do(state: dict) -> set[str]:
        tid = world.new_id("t")
        state["threads"][tid] = {"id": tid, "kind": "group" if is_group else "dm",
                                 "title": title, "context_label": None,
                                 "participants": participant_ids, "draft": None,
                                 "messages": []}
        return set()  # empty thread — nothing to sync until it has messages
    await world.mutate(_do)
    return {"ok": True}


@app.post("/sandbox/emails")
async def add_email(request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)

    async def _do(state: dict) -> set[str]:
        eid = world.new_id("e")
        state["emails"][eid] = {
            "id": eid, "message_id": f"<{world.new_id('inj')}@sandbox.wisp.test>",
            "account": body.get("account", "Personal"), "ts_off": world.rel_ts(),
            "from_name": body.get("from_name", ""), "from_addr": body.get("from_addr", ""),
            "to": body.get("to") or [], "cc": body.get("cc") or [],
            "subject": body.get("subject", ""), "body": body.get("body", ""),
            "unread": bool(body.get("unread", True)), "mailbox": "inbox",
            "deep": False, "in_reply_to": None,
        }
        return {"email_headers", "email_raw"}
    dirty = await world.mutate(_do)
    _sync(request).kick(*dirty)
    return {"ok": True}


@app.post("/sandbox/emails/{email_id}")
async def patch_email(email_id: str, request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)
    if email_id not in world.state["emails"]:
        return {"ok": False, "error": "not found"}

    async def _do(state: dict) -> set[str]:
        e = state["emails"][email_id]
        for key in ("unread", "mailbox", "subject", "body"):
            if key in body:
                e[key] = body[key]
        return {"email_headers", "email_raw"}
    dirty = await world.mutate(_do)
    _sync(request).kick(*dirty)
    return {"ok": True}


@app.post("/sandbox/notes")
async def add_note(request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)

    async def _do(state: dict) -> set[str]:
        nid = world.new_id("n")
        state["notes"][nid] = {"id": nid, "ts_off": world.rel_ts(),
                               "title": body.get("title", ""),
                               "folder": body.get("folder", ""),
                               "body": body.get("body", "")}
        return {"notes"}
    dirty = await world.mutate(_do)
    _sync(request).kick(*dirty)
    return {"ok": True}


@app.post("/sandbox/notes/{note_id}")
async def edit_note(note_id: str, request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)
    if note_id not in world.state["notes"]:
        return {"ok": False, "error": "not found"}

    async def _do(state: dict) -> set[str]:
        n = state["notes"][note_id]
        for key in ("title", "folder", "body"):
            if key in body:
                n[key] = body[key]
        n["ts_off"] = world.rel_ts()  # editing bumps modification time, like Notes.app
        return {"notes"}
    dirty = await world.mutate(_do)
    _sync(request).kick(*dirty)
    return {"ok": True}


@app.post("/sandbox/calendar")
async def add_calendar(request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)
    kind = body.get("kind", "event")
    coll_name = "reminders" if kind == "reminder" else "calendar"

    async def _do(state: dict) -> set[str]:
        cid = world.new_id("sbx")
        row = {"id": cid, "source_id": cid, "kind": kind, "title": body.get("title", ""),
              "context": body.get("context"), "organizer": body.get("organizer"),
              "account": body.get("account"),
              "ts_off": world.rel_ts(body.get("when_ts")) if body.get("when_ts")
                       else world.rel_ts(),
              "all_day": bool(body.get("all_day")), "location": body.get("location")}
        state[coll_name][cid] = row
        return {coll_name}
    dirty = await world.mutate(_do)
    _sync(request).kick(*dirty)
    return {"ok": True}


@app.delete("/sandbox/calendar/{event_id}")
async def delete_calendar(event_id: str, request: Request) -> dict:
    world = _world(request)

    async def _do(state: dict) -> set[str]:
        dirty = set()
        for coll_name in ("calendar", "reminders"):
            if event_id in state[coll_name]:
                del state[coll_name][event_id]
                dirty.add(coll_name)
        return dirty
    dirty = await world.mutate(_do)
    _sync(request).kick(*dirty)
    return {"ok": True}


@app.post("/sandbox/browser")
async def add_browser(request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)
    browser = body.get("browser", "safari")

    async def _do(state: dict) -> set[str]:
        state["browser"].setdefault(browser, []).append({
            "ts_off": world.rel_ts(), "host": body.get("host", ""),
            "path": body.get("path", "/"), "title": body.get("title", "")})
        return {"browser"}
    dirty = await world.mutate(_do)
    _sync(request).kick(*dirty)
    return {"ok": True}


# --- sync / status / clock / injection ----------------------------------

@app.post("/sandbox/sync/{source}")
async def force_sync(source: str, request: Request) -> dict:
    sync = _sync(request)
    sources = list(CADENCES) if source == "all" else [source]
    for s in sources:
        await sync.force_sync(s)
    return {"ok": True, "synced": sources}


@app.get("/sandbox/status")
async def status(request: Request) -> dict:
    world = _world(request)
    outbound = _outbound(request)
    return {
        "sync_status": world.state.get("sync_status", {}),
        "connected": outbound.connected,
        "clock": world.state["clock"],
        "now": world.now(),
        "booted_at": world.state["booted_at"],
        "injection": world.state["injection"],
    }


@app.post("/sandbox/clock")
async def set_clock(request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)
    if body.get("rebase") is True:
        await world.reset(rebase=True)

    async def _do(state: dict) -> set[str]:
        clock = state["clock"]
        if "mode" in body:
            clock["mode"] = body["mode"]
        if "frozen_at" in body:
            clock["frozen_at"] = body["frozen_at"]
        elif body.get("mode") == "frozen" and clock.get("frozen_at") is None:
            clock["frozen_at"] = time.time()
        if "sync_scale" in body:
            clock["sync_scale"] = float(body["sync_scale"])
        return set()
    await world.mutate(_do)
    return {"ok": True, "clock": world.state["clock"]}


@app.post("/sandbox/inject")
async def set_injection(request: Request) -> dict:
    body = await _json_body(request)
    world = _world(request)

    async def _do(state: dict) -> set[str]:
        inject.apply_patch(state, body)
        return set()
    await world.mutate(_do)
    await world.notify("injection", {"injection": world.state["injection"]})
    return {"ok": True, "injection": world.state["injection"]}


@app.get("/sandbox/actions")
async def get_actions(request: Request) -> dict:
    return {"actions": _world(request).state.get("action_log", [])}


@app.get("/sandbox/notifications")
async def get_notifications(request: Request) -> dict:
    return {"notifications": _world(request).state.get("notifications", [])}


# --- reverse proxy -------------------------------------------------------

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_api(path: str, request: Request):
    return await proxy.proxy_request(request.app.state.client, path, request)


# --- static (mounted last so it never shadows the routes above) --------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

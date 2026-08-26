"""The fake Swift app's other half: consumes GET /assistant/events (SSE) from
the sandbox backend and acts on it exactly like app/Sources/WispApp's
OverlayModel.handleAssistantEvent + OutboundSender would — mutating the
sandbox world and, for the seven actions the backend actually waits on,
POSTing the result back to /assistant/action_result.

Pinned against source (service/main.py, service/assistant/outbox.py,
OutboundSender.swift) — the only result shape the backend reads is
`{"action_id", "ok", "error"}`, identical across every action type, and
outbox.DEFAULT_TIMEOUT_S is 45.0 — so every result-bearing handler here is
wrapped to ALWAYS post exactly one result, including when it raises
internally. A silent handler bug should look like "the app didn't respond
for 45s", not hang the agent turn forever with no result at all.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from sandbox import inject
from sandbox.world import World

RESULT_ACTIONS = {"send_message", "send_email", "reply_to_email", "draft_email",
                  "draft_message", "mark_email_read", "archive_email"}
FIRE_AND_FORGET = {"create_calendar_event", "delete_calendar_event",
                   "create_apple_reminder", "delete_apple_reminder", "sync_emails_now"}
NOTIFY_ONLY = {"reminder", "scheduled_send_result", "scheduled_send_missed",
               "email_summary", "daily_brief"}

_ACTION_LOG_MAX = 500


class OutboundConsumer:
    def __init__(self, world: World, client: httpx.AsyncClient, sync=None) -> None:
        self.world = world
        self.client = client
        self.sync = sync  # sandbox.sync.SyncScheduler, set by server.py post-construction
        self._stopping = False
        self._task: asyncio.Task | None = None
        self.connected = False

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        backoff = 0.5
        while not self._stopping:
            try:
                async with self.client.stream("GET", "/assistant/events", timeout=None) as r:
                    self.connected = True
                    backoff = 0.5
                    await self.world.notify("connection", {"connected": True})
                    async for line in r.aiter_lines():
                        if self._stopping:
                            break
                        if not line.startswith("data: "):
                            continue
                        try:
                            ev = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        asyncio.ensure_future(self._dispatch(ev))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — reconnect on anything
                await self.world.notify("reconnect_log", {"error": str(e), "at": time.time()})
            self.connected = False
            await self.world.notify("connection", {"connected": False})
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    async def _dispatch(self, ev: dict) -> None:
        etype = ev.get("type", "")
        action_id = ev.get("action_id")
        started = time.time()
        ok, error = True, ""
        try:
            if etype in RESULT_ACTIONS:
                ok, error = await self._handle_result_action(etype, ev)
            elif etype in FIRE_AND_FORGET:
                await self._handle_fire_and_forget(etype, ev)
            elif etype in NOTIFY_ONLY:
                await self.world.notify("notification", {"kind": etype, "payload": ev})
            elif etype == "hello":
                pass
            elif etype == "changed":
                pass
        except Exception as e:  # noqa: BLE001 — a handler bug must not eat the result
            ok, error = False, f"sandbox handler error: {e}"

        if action_id:
            await self._log_action(etype, ev, ok, error, started)
        if etype in RESULT_ACTIONS and action_id:
            await self._post_result(etype, action_id, ok, error)

    async def _log_action(self, etype: str, ev: dict, ok: bool, error: str,
                          started: float) -> None:
        async def _do(state: dict) -> set[str]:
            log = state.setdefault("action_log", [])
            log.append({"action_id": ev.get("action_id"), "type": etype,
                       "payload": {k: v for k, v in ev.items()
                                   if k not in ("type", "action_id")},
                       "ok": ok, "error": error, "started_at": started,
                       "finished_at": time.time(),
                       "latency_ms": int((time.time() - started) * 1000)})
            del log[:-_ACTION_LOG_MAX]
            return set()
        await self.world.mutate(_do)
        await self.world.notify("action", {"type": etype, "ok": ok, "error": error})

    async def _post_result(self, etype: str, action_id: str, ok: bool, error: str) -> None:
        stall = inject.get_stall_ms(self.world.state, etype)
        if stall:
            await asyncio.sleep(stall / 1000.0)
        if inject.is_dropped(self.world.state, etype):
            return  # exercises outbox.DEFAULT_TIMEOUT_S — deliberately silent
        try:
            await self.client.post("/assistant/action_result",
                                   json={"action_id": action_id, "ok": ok, "error": error})
        except Exception:  # noqa: BLE001 — backend may be mid-restart; nothing to do
            pass

    # --- result-bearing actions ------------------------------------------

    async def _handle_result_action(self, etype: str, ev: dict) -> tuple[bool, str]:
        forced = inject.get_fail(self.world.state, etype)
        if forced:
            return False, forced
        handler = getattr(self, f"_h_{etype}")
        return await handler(ev)

    def _find_or_create_thread(self, state: dict, to: str) -> dict:
        norm = _norm_handle(to)
        for c in state["contacts"].values():
            if any(_norm_handle(h) == norm for h in c["handles"]):
                for t in state["threads"].values():
                    if t["kind"] == "dm" and c["id"] in (t.get("participants") or []):
                        return t
                tid = self.world.new_id("t")
                t = {"id": tid, "kind": "dm", "title": None,
                    "context_label": c["name"], "participants": [c["id"]],
                    "draft": None, "messages": []}
                state["threads"][tid] = t
                return t
        # Unsaved handle — matches the real reader's unresolved-handle path.
        for t in state["threads"].values():
            if t["kind"] == "dm" and t.get("context_label") == to:
                return t
        tid = self.world.new_id("t")
        t = {"id": tid, "kind": "dm", "title": None, "context_label": to,
            "participants": [], "draft": None, "messages": []}
        state["threads"][tid] = t
        return t

    async def _h_send_message(self, ev: dict) -> tuple[bool, str]:
        to, text = str(ev.get("to") or ""), str(ev.get("text") or "")
        if not to or not text:
            return False, "missing 'to' or 'text'"

        async def _do(state: dict) -> set[str]:
            thread = self._find_or_create_thread(state, to)
            thread["messages"].append({"id": self.world.new_id("m"),
                                       "ts_off": self.world.rel_ts(),
                                       "from": "me", "text": text, "via": "wisp"})
            return {"messages"}
        await self.world.mutate(_do)
        return True, ""

    async def _h_send_email(self, ev: dict) -> tuple[bool, str]:
        to = ev.get("to") or []
        subject, body = str(ev.get("subject") or ""), str(ev.get("body") or "")
        if not to:
            return False, "missing 'to'"

        async def _do(state: dict) -> set[str]:
            identity = state["persona"]["identity_emails"]
            eid = self.world.new_id("e")
            state["emails"][eid] = {
                "id": eid, "message_id": f"<{self.world.new_id('sent')}@sandbox.wisp.test>",
                "account": "Personal", "ts_off": self.world.rel_ts(),
                "from_name": "Me", "from_addr": identity[0] if identity else "me@sandbox.wisp.test",
                "to": to, "cc": ev.get("cc") or [], "subject": subject, "body": body,
                "unread": False, "mailbox": "sent", "deep": False, "in_reply_to": None,
            }
            return set()  # sent mail is not synced into inbox headers/raw — see settings toggle
        await self.world.mutate(_do)
        return True, ""

    async def _h_reply_to_email(self, ev: dict) -> tuple[bool, str]:
        message_id = str(ev.get("message_id") or "")
        body = str(ev.get("body") or "")
        reply_all = bool(ev.get("reply_all", False))
        original = self._find_email(message_id)
        if original is None:
            return False, ("couldn't find that message in the inbox — it may "
                          "have been moved or is older than the synced window")

        async def _do(state: dict) -> set[str]:
            identity = set(state["persona"]["identity_emails"])
            to = [original["from_addr"]]
            if reply_all:
                to += [a for a in (original["to"] + original["cc"])
                      if a not in identity and a != original["from_addr"]]
            subject = original["subject"]
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            eid = self.world.new_id("e")
            state["emails"][eid] = {
                "id": eid, "message_id": f"<{self.world.new_id('sent')}@sandbox.wisp.test>",
                "account": original["account"], "ts_off": self.world.rel_ts(),
                "from_name": "Me",
                "from_addr": next(iter(identity), "me@sandbox.wisp.test"),
                "to": to, "cc": [], "subject": subject, "body": body,
                "unread": False, "mailbox": "sent", "deep": False,
                "in_reply_to": message_id,
            }
            original["unread"] = False
            return {"email_headers", "email_raw"}
        await self.world.mutate(_do)
        return True, ""

    async def _h_draft_email(self, ev: dict) -> tuple[bool, str]:
        async def _do(state: dict) -> set[str]:
            eid = self.world.new_id("e")
            state["emails"][eid] = {
                "id": eid, "message_id": f"<{self.world.new_id('draft')}@sandbox.wisp.test>",
                "account": "Personal", "ts_off": self.world.rel_ts(),
                "from_name": "Me", "from_addr": "", "to": ev.get("to") or [],
                "cc": ev.get("cc") or [], "subject": str(ev.get("subject") or ""),
                "body": str(ev.get("body") or ""), "unread": False,
                "mailbox": "drafts", "deep": False, "in_reply_to": None,
            }
            return set()
        await self.world.mutate(_do)
        return True, ""

    async def _h_draft_message(self, ev: dict) -> tuple[bool, str]:
        to, text = str(ev.get("to") or ""), str(ev.get("text") or "")
        if not to:
            return False, "missing 'to'"

        async def _do(state: dict) -> set[str]:
            thread = self._find_or_create_thread(state, to)
            thread["draft"] = text
            return set()
        await self.world.mutate(_do)
        return True, ""

    def _find_email(self, message_id: str) -> dict | None:
        for e in self.world.state["emails"].values():
            if e["message_id"] == message_id:
                return e
        return None

    async def _h_mark_email_read(self, ev: dict) -> tuple[bool, str]:
        message_id = str(ev.get("message_id") or "")
        read = bool(ev.get("read", True))
        if self._find_email(message_id) is None:
            return False, ("couldn't find that message in the inbox — it may "
                          "have been moved or is older than the synced window")

        async def _do(state: dict) -> set[str]:
            e = next(e for e in state["emails"].values() if e["message_id"] == message_id)
            e["unread"] = not read
            return {"email_headers", "email_raw"}
        await self.world.mutate(_do)
        return True, ""

    async def _h_archive_email(self, ev: dict) -> tuple[bool, str]:
        message_id = str(ev.get("message_id") or "")
        if self._find_email(message_id) is None:
            return False, ("couldn't find that message in the inbox — it may "
                          "have been moved or is older than the synced window")

        async def _do(state: dict) -> set[str]:
            e = next(e for e in state["emails"].values() if e["message_id"] == message_id)
            e["mailbox"] = "archive"
            return {"email_headers", "email_raw"}
        await self.world.mutate(_do)
        return True, ""

    # --- fire-and-forget ---------------------------------------------------

    async def _handle_fire_and_forget(self, etype: str, ev: dict) -> None:
        if etype == "sync_emails_now":
            if self.sync:
                await self.sync.force_sync("email_headers")
                await self.sync.force_sync("email_raw")
            return

        async def _do(state: dict) -> set[str]:
            if etype == "create_calendar_event":
                cid = self.world.new_id("sbx")
                key = f"{cid}@{self.world.rel_ts(ev.get('when_ts'))}"
                state["calendar"][key] = {
                    "id": key, "source_id": cid, "kind": "event",
                    "title": str(ev.get("title") or ""), "context": None,
                    "organizer": None, "account": None,
                    "ts_off": self.world.rel_ts(ev.get("when_ts")),
                    "all_day": False, "location": ev.get("location"),
                    "duration_min": ev.get("duration_min", 60),
                }
                return {"calendar"}
            if etype == "delete_calendar_event":
                sid = str(ev.get("source_id") or "")
                when_ts = ev.get("when_ts")
                to_delete = [k for k, row in state["calendar"].items()
                            if row["source_id"] == sid and
                            (when_ts is None or
                             abs(row["ts_off"] - self.world.rel_ts(when_ts)) < 60)]
                for k in to_delete:
                    del state["calendar"][k]
                return {"calendar"} if to_delete else set()
            if etype == "create_apple_reminder":
                rid = self.world.new_id("sbx")
                state["reminders"][rid] = {
                    "id": rid, "source_id": rid, "kind": "reminder",
                    "title": str(ev.get("title") or ""),
                    "ts_off": self.world.rel_ts(ev.get("when_ts")),
                }
                return {"reminders"}
            if etype == "delete_apple_reminder":
                sid = str(ev.get("source_id") or "")
                if sid in state["reminders"]:
                    del state["reminders"][sid]
                    return {"reminders"}
                return set()
            return set()
        dirty = await self.world.mutate(_do)
        if self.sync:
            self.sync.kick(*dirty)


def _norm_handle(h: str) -> str:
    from service.tools.imessage_tools import _norm_handle as norm
    return norm(h)

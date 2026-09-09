"""Pushes the sandbox world to the sandbox backend as the real Swift readers
would — one asyncio.Task per source, same independent cadences (see the
module-level SOURCES table), same "fire immediately at boot" behavior. A
`sync_scale` multiplier compresses those cadences proportionally so a test
run doesn't have to wait 5 real minutes to see a messages sync land; ratios
are preserved on purpose; a source dropping to a shorter *relative* wait
than another would look like a fidelity difference that isn't real.

Endpoints/payloads here are pinned against the actual handlers in
service/main.py (verified 2026-08 against source, not inferred):
  sync/messages   {"lines": str, "diagnostics": {...}}  AND, separately,
                  {"contacts": {handle: name}}           (short-circuits)
  sync/emails     up to 4 independent single-key payloads: headers/raw/
                  history/identity_emails, plus a `diagnostics` object
  sync/notes      {"raw": str}
  sync/browser_history  {"browser", "lines", "diagnostics"} per browser
  sync/calendar   {"events":[...], "diagnostics":{...}}  (source="calendar"),
                  and again with "source":"reminders" for reminders
"""
from __future__ import annotations

import asyncio
import time

import httpx

from sandbox.world import World
from tests.fixtures import wire

# (base_period_seconds, push_fn_name) — base_period is the REAL cadence;
# effective period = max(2.0, base * sync_scale).
CADENCES: dict[str, float] = {
    "messages": 300.0,
    "contacts": 21600.0,
    "email_headers": 300.0,
    "email_raw": 900.0,
    "email_history": 1800.0,
    "notes": 86400.0,
    "browser": 1800.0,
    "calendar": 60.0,
    "reminders": 60.0,
}


def _messages_lines(world: World) -> str:
    rows: list[dict] = []
    for thread in world.state["threads"].values():
        label = thread.get("context_label")
        if not label:
            names = [world.state["contacts"].get(p, {}).get("name", p)
                    for p in thread.get("participants") or []]
            label = wire.thread_context(thread.get("title"), names,
                                        is_group=(thread["kind"] == "group"))
        for m in thread["messages"]:
            who = "Me" if m["from"] == "me" else m["from"]
            rows.append({"ts": world.abs_ts(m["ts_off"]), "context": label,
                        "who": who, "text": m["text"]})
    return wire.messages_lines(rows)


def _contacts_raw(world: World) -> dict[str, str]:
    raw: dict[str, str] = {}
    for c in world.state["contacts"].values():
        for h in c["handles"]:
            raw[h] = c["name"]
    return raw


def _email_header_rows(world: World, *, deep: bool | None) -> list[dict]:
    out = []
    for e in world.state["emails"].values():
        if e["mailbox"] != "inbox":
            continue
        if deep is not None and bool(e.get("deep")) != deep:
            continue
        out.append({"ts": world.abs_ts(e["ts_off"]), "unread": e["unread"],
                    "account": e["account"], "sender_name": e["from_name"],
                    "sender_addr": e["from_addr"], "subject": e["subject"]})
    return out


def _email_raw_rows(world: World) -> list[dict]:
    out = []
    for e in world.state["emails"].values():
        if e["mailbox"] != "inbox":
            continue
        out.append({"ts": world.abs_ts(e["ts_off"]), "unread": e["unread"],
                    "account": e["account"], "sender_name": e["from_name"],
                    "sender_addr": e["from_addr"], "to": e["to"],
                    "subject": e["subject"], "message_id": e["message_id"],
                    "body": e["body"]})
    return out


def _notes_rows(world: World) -> list[dict]:
    return [{"ts": world.abs_ts(n["ts_off"]), "title": n["title"],
             "folder": n["folder"], "body": n["body"]}
            for n in world.state["notes"].values()]


def _browser_rows(world: World, browser: str) -> list[dict]:
    return [{"ts": world.abs_ts(b["ts_off"]), "host": b["host"],
             "path": b["path"], "title": b["title"]}
            for b in world.state["browser"].get(browser, [])]


def _calendar_events(world: World, coll: dict) -> list[dict]:
    out = []
    for row in coll.values():
        out.append({"source_id": row["source_id"], "kind": row["kind"],
                    "title": row["title"], "context": row.get("context"),
                    "organizer": row.get("organizer"), "account": row.get("account"),
                    "when_ts": world.abs_ts(row["ts_off"]),
                    "all_day": bool(row.get("all_day")),
                    "location": row.get("location")})
    return out


class SyncScheduler:
    def __init__(self, world: World, client: httpx.AsyncClient) -> None:
        self.world = world
        self.client = client
        self._events: dict[str, asyncio.Event] = {s: asyncio.Event() for s in CADENCES}
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    def start(self) -> None:
        for source in CADENCES:
            self._tasks.append(asyncio.ensure_future(self._loop(source)))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def kick(self, *sources: str) -> None:
        for s in sources:
            if s in self._events:
                self._events[s].set()

    async def push_all(self) -> None:
        await asyncio.gather(*(self._push(s) for s in CADENCES))

    def _period(self, source: str) -> float:
        scale = self.world.state["clock"].get("sync_scale", 0.05)
        return max(2.0, CADENCES[source] * scale)

    async def _loop(self, source: str) -> None:
        ev = self._events[source]
        # Fire immediately at boot, same as the real readers.
        await self._push_with_retry(source)
        while not self._stopping:
            try:
                await asyncio.wait_for(ev.wait(), timeout=self._period(source))
            except asyncio.TimeoutError:
                pass
            # A burst of kicks within ~250ms collapses into one push.
            ev.clear()
            await asyncio.sleep(0.25)
            await self._push_with_retry(source)

    async def _push_with_retry(self, source: str) -> None:
        if self.world.state["injection"].get("sync_paused"):
            return
        try:
            await self._push(source)
        except Exception as e:  # noqa: BLE001 — one bad push must not kill the loop
            self.world.state.setdefault("sync_status", {})[source] = {
                "ok": False, "status": str(e), "count": 0, "at": time.time()}

    async def force_sync(self, source: str) -> None:
        """POST /sandbox/sync/{source} — bypasses the debounce entirely."""
        await self._push(source)

    async def _post(self, path: str, body: dict) -> httpx.Response:
        latency = self.world.state["injection"].get("latency_ms", 0)
        if latency:
            await asyncio.sleep(latency / 1000.0)
        resp = await self.client.post(path, json=body)
        # A non-2xx must fail loudly, not be recorded as a successful sync —
        # this is exactly the bug that let a wrong URL silently 404 while
        # sync_status still reported "ok" (caught in Phase 2 smoke testing).
        resp.raise_for_status()
        return resp

    async def _diag(self, source: str, ok: bool = True, reason: str = "") -> dict:
        unavailable = source in self.world.state["injection"].get("unavailable", [])
        return {"available": not unavailable,
               "reason": reason or ("sandbox: marked unavailable" if unavailable else "")}

    async def _push(self, source: str) -> None:
        world = self.world
        count = 0
        if source == "messages":
            diag = await self._diag("messages")
            lines = _messages_lines(world)
            count = lines.count("\n") + 1 if lines else 0
            await self._post("/assistant/sync/messages",
                             {"lines": lines, "diagnostics": {**diag, "count": count}})
        elif source == "contacts":
            raw = _contacts_raw(world)
            count = len(raw)
            await self._post("/assistant/sync/messages", {"contacts": raw})
        elif source == "email_headers":
            diag = await self._diag("email")
            headers = wire.email_headers(_email_header_rows(world, deep=False))
            body = {"headers": headers, "diagnostics": diag}
            count = headers.count("\n")
            await self._post("/assistant/sync/emails", body)
            # Identity rides along with headers, same as MailReader.syncIdentity().
            await self._post("/assistant/sync/emails",
                             {"identity_emails": world.state["persona"]["identity_emails"]})
        elif source == "email_raw":
            raw = wire.email_raw(_email_raw_rows(world))
            count = raw.count("\x02")
            await self._post("/assistant/sync/emails", {"raw": raw, "raw_coverage": {
                "accounts": sorted({e["account"] for e in world.state["emails"].values()}),
                "failed_accounts": [], "complete": True}})
        elif source == "email_history":
            headers = wire.email_headers(_email_header_rows(world, deep=None))
            count = headers.count("\n")
            await self._post("/assistant/sync/emails", {"history": headers})
        elif source == "notes":
            raw = wire.notes_raw(_notes_rows(world))
            count = raw.count("\x02")
            await self._post("/assistant/sync/notes", {"raw": raw})
        elif source == "browser":
            for browser in ("safari", "chrome"):
                diag = await self._diag("browser")
                lines = wire.browser_lines(_browser_rows(world, browser))
                await self._post("/assistant/sync/browser_history",
                                 {"browser": browser, "lines": lines,
                                  "diagnostics": {**diag, "count": lines.count(chr(10)) + (1 if lines else 0)}})
            count = sum(len(world.state["browser"].get(b, [])) for b in ("safari", "chrome"))
        elif source == "calendar":
            events = _calendar_events(world, world.state["calendar"])
            count = len(events)
            await self._post("/assistant/sync/calendar",
                             {"events": events, "diagnostics": {"authorized": True,
                                                                "calendar_count": 1,
                                                                "events_found": count}})
        elif source == "reminders":
            events = _calendar_events(world, world.state["reminders"])
            count = len(events)
            await self._post("/assistant/sync/calendar",
                             {"source": "reminders", "events": events,
                              "diagnostics": {"authorized": True, "count": count}})
        world.state.setdefault("sync_status", {})[source] = {
            "ok": True, "status": "ok", "count": count, "at": time.time()}

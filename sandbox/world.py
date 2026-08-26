"""The sandbox's fake phone's data: one JSON file, one lock, one clock.

Single JSON file rather than SQLite — one process, one asyncio.Lock, a few
hundred records at most, and the primary read pattern (give the browser the
whole world) doesn't benefit from a query engine. A human-diffable file is a
feature in a QA harness: `cat world.json` is a legitimate debugging tool
here.

Every timestamp is stored as `ts_off` — seconds offset from `booted_at` —
never an absolute epoch. That's what makes a reset reproducible (the world
looks identical relative to whenever it's next booted) and makes `rebase`
(re-anchor `booted_at` to now, sliding the whole world forward) a one-line
operation instead of a rewrite. `World.now()`/`abs_ts()` are the only two
places that convert between the two.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from tests.fixtures import persona, wire

SCHEMA_VERSION = 1

# Every source a mutation can mark dirty — the sync scheduler kicks exactly
# these on the next debounce.
SOURCES = ("messages", "contacts", "email_headers", "email_raw",
          "email_history", "notes", "browser", "calendar", "reminders")


def _default_sandbox_home() -> Path:
    override = os.environ.get("WISP_SANDBOX_HOME")
    return Path(override).expanduser() if override else Path.home() / ".wisp-sandbox"


class World:
    """Owns sandbox/world.json. Not thread-safe across processes — this is
    meant to run as the single sandbox server, same as the real Swift app is
    the single source of Messages/Mail/Notes truth."""

    def __init__(self, path: Path | None = None, seed: int | None = None) -> None:
        self.path = path or (_default_sandbox_home() / "world.json")
        self._lock = asyncio.Lock()
        self._subs: set[asyncio.Queue] = set()
        self._save_task: asyncio.Task | None = None
        env_seed = os.environ.get("WISP_SANDBOX_SEED")
        self._seed = seed if seed is not None else (int(env_seed) if env_seed else None)
        self._rng = random.Random(self._seed)
        self.state: dict[str, Any] = self._load_or_seed()

    # --- persistence ---------------------------------------------------

    def _load_or_seed(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") == SCHEMA_VERSION:
                    return data
            except Exception:  # noqa: BLE001 — corrupt/old file, reseed
                pass
        return self._fresh_state(time.time())

    def _fresh_state(self, booted_at: float) -> dict[str, Any]:
        return seed_state(booted_at, self._rng)

    def _persist_now(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    async def _persist_debounced(self) -> None:
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        self._persist_now()

    def _schedule_persist(self) -> None:
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        self._save_task = asyncio.ensure_future(self._persist_debounced())

    # --- clock -----------------------------------------------------------

    def now(self) -> float:
        clock = self.state["clock"]
        if clock["mode"] == "frozen" and clock.get("frozen_at") is not None:
            return clock["frozen_at"]
        return time.time()

    def abs_ts(self, ts_off: float) -> float:
        return self.state["booted_at"] + ts_off

    def rel_ts(self, abs_ts: float | None = None) -> float:
        return (abs_ts if abs_ts is not None else self.now()) - self.state["booted_at"]

    # --- ids ---------------------------------------------------------------

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{self._rng.randrange(16**8):08x}"

    # --- mutation / subscription --------------------------------------

    async def mutate(
        self, fn: Callable[[dict], set[str] | None] | Callable[[dict], Awaitable[set[str] | None]]
    ) -> set[str]:
        """Run `fn(state)` under the lock. `fn` mutates self.state in place
        and returns the set of dirty source names (or None for none). Bumps
        rev, schedules a debounced save, and notifies subscribers.

        Accepts both a plain function and a coroutine function — every call
        site in this codebase writes `async def _do(state): ...` (natural
        muscle memory inside an `async def` handler, even though `_do` never
        actually awaits anything), so this awaits the result when it's a
        coroutine rather than silently handing `sorted()` a coroutine
        object."""
        async with self._lock:
            result = fn(self.state)
            if asyncio.iscoroutine(result):
                result = await result
            dirty = result or set()
            self.state["rev"] = self.state.get("rev", 0) + 1
            self._schedule_persist()
        if dirty:
            await self._publish({"type": "world", "rev": self.state["rev"],
                                 "dirty": sorted(dirty)})
        return dirty

    def snapshot(self) -> dict[str, Any]:
        """A deep-enough copy for JSON serving — the state dict is already
        JSON-safe, so this just adds the computed `now`."""
        return {**self.state, "now": self.now()}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def _publish(self, event: dict) -> None:
        for q in list(self._subs):
            await q.put(event)

    async def notify(self, kind: str, payload: dict) -> None:
        """Push a non-world event (a phone notification banner, an action
        log entry, a sync-status update) straight to subscribers without
        going through mutate() — these don't need debounced persistence."""
        await self._publish({"type": kind, **payload})

    # --- reset ---------------------------------------------------------

    async def reset(self, rebase: bool = False) -> None:
        async def _do(state: dict) -> set[str]:
            new_booted = self.now() if rebase else time.time()
            fresh = self._fresh_state(new_booted)
            state.clear()
            state.update(fresh)
            return set(SOURCES)
        await self.mutate(_do)
        self._persist_now()


def seed_state(booted_at: float, rng: random.Random) -> dict[str, Any]:
    """The full fresh world: persona cast + commitments/facts source data +
    message/email/note content, all keyed by id and stored with ts_off
    relative to booted_at."""
    now = booted_at  # persona builders resolve "today" off this reference

    norm_contacts, raw_contacts = persona.contacts()
    contacts = {}
    for handle, name in raw_contacts.items():
        cid = f"c_{rng.randrange(16**8):08x}"
        contacts[cid] = {"id": cid, "name": name, "handles": [handle],
                         "email": handle if "@" in handle else None}
    # Merge handles for people who have more than one (e.g. Priya: phone + email).
    by_name: dict[str, str] = {}
    merged: dict[str, dict] = {}
    for c in contacts.values():
        key = c["name"]
        if key in by_name:
            existing = merged[by_name[key]]
            existing["handles"].extend(c["handles"])
            if c["email"] and not existing["email"]:
                existing["email"] = c["email"]
        else:
            by_name[key] = c["id"]
            merged[c["id"]] = c
    contacts = merged

    def contact_id_by_name(name: str) -> str | None:
        for cid, c in contacts.items():
            if c["name"] == name:
                return cid
        return None

    # --- threads/messages, grouped by context label ---------------------
    threads: dict[str, dict] = {}
    context_to_thread: dict[str, str] = {}
    for row in persona.message_rows(now):
        ctx = row["context"]
        tid = context_to_thread.get(ctx)
        if tid is None:
            tid = f"t_{rng.randrange(16**8):08x}"
            context_to_thread[ctx] = tid
            is_group = ctx.startswith("Group")
            name = ctx.split('"')[1] if ctx.startswith('Group "') else None
            participants = [cid for cid in contacts
                           if contacts[cid]["name"] in ctx] or None
            threads[tid] = {"id": tid, "kind": "group" if is_group else "dm",
                            "title": name, "context_label": None if is_group else ctx,
                            "participants": participants or [], "draft": None,
                            "messages": []}
        who = row["who"]
        threads[tid]["messages"].append({
            "id": f"m_{rng.randrange(16**8):08x}",
            "ts_off": row["ts"] - now,
            "from": "me" if who == "Me" else who,
            "text": row["text"], "via": "seed",
        })

    # --- emails -----------------------------------------------------------
    raw_by_msgid = {r["message_id"]: r for r in persona.email_raw_rows(now)}
    emails: dict[str, dict] = {}
    for row in persona.email_header_rows(now, deep_history=True):
        raw = next((r for r in raw_by_msgid.values()
                    if r["subject"] == row["subject"]), None)
        eid = f"e_{rng.randrange(16**8):08x}"
        emails[eid] = {
            "id": eid,
            "message_id": (raw["message_id"] if raw else
                          f"<{rng.randrange(16**16):016x}@sandbox.wisp.test>"),
            "account": row["account"], "ts_off": row["ts"] - now,
            "from_name": row["sender_name"], "from_addr": row["sender_addr"],
            "to": raw["to"] if raw else list(persona.IDENTITY_EMAILS[:1]),
            "cc": [], "subject": row["subject"],
            "body": raw["body"] if raw else "",
            "unread": bool(row["unread"]), "mailbox": "inbox",
            "deep": row["ts"] < now - 30 * 86400, "in_reply_to": None,
        }

    # --- notes -------------------------------------------------------------
    notes: dict[str, dict] = {}
    for row in persona.note_rows(now):
        nid = f"n_{rng.randrange(16**8):08x}"
        notes[nid] = {"id": nid, "ts_off": row["ts"] - now, "title": row["title"],
                      "folder": row["folder"], "body": row["body"]}

    # --- calendar / reminders ----------------------------------------------
    calendar: dict[str, dict] = {}
    reminders: dict[str, dict] = {}
    for c in persona.commitment_rows(now):
        row = {"id": c["source_id"], "source_id": c["source_id"], "kind": c["kind"],
              "title": c["title"], "context": c.get("context"),
              "organizer": c.get("organizer"), "account": c.get("account"),
              "ts_off": c["when_ts"] - now, "all_day": bool(c.get("all_day")),
              "location": c.get("location")}
        if c["kind"] == "reminder":
            reminders[row["id"]] = row
        else:
            # commitment_rows can repeat a source_id for a recurring series
            # (see persona.commitment_rows) — key on (source_id, ts_off) so
            # both occurrences survive, matching AssistantStore.sync_source's
            # own upsert key.
            calendar[f"{row['id']}@{row['ts_off']}"] = row

    return {
        "version": SCHEMA_VERSION,
        "rev": 0,
        "booted_at": booted_at,
        "clock": {"mode": "live", "frozen_at": None, "sync_scale": 0.05},
        "persona": {"identity_emails": list(persona.IDENTITY_EMAILS)},
        "contacts": contacts,
        "threads": threads,
        "emails": emails,
        "notes": notes,
        "calendar": calendar,
        "reminders": reminders,
        "browser": {"safari": [], "chrome": []},
        "notifications": [],
        "action_log": [],
        "injection": {"fail": {}, "stall_ms": {}, "drop_result": [],
                      "unavailable": [], "sync_paused": False, "latency_ms": 0},
        "sync_status": {},
    }

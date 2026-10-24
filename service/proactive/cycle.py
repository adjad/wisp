"""The proactive cycle: triage → judge → prepare, in the background.

Rules:
- IMPORTANT: background only, on the resident model only. Model calls go
  through mail_gists._resident_reply: it never loads or evicts a model, sends
  thinking off the way Wisp states it elsewhere, and cancels the request the
  moment a user's turn starts.
- IMPORTANT: never alongside itself, and never alongside a mail_gists call (one
  resident model, one request at a time): a cycle waits for a running gist
  call and holds the next one off while it works.
- A scheduled tick runs only while nobody is waiting (idle.foreground_busy)
  and the app is listening; it stands down when a turn starts. "Analyze now"
  forces a full cycle that waits out a turn instead of giving up.
- Triage is incremental: an item is read once per content hash
  (store.triage_*). The judge and prepare run when their inputs changed, and
  at least every 30 minutes.
- With no model available the cycle still works by rule: calendar, reminders
  and Notes to-dos are deterministic, and each item's own verbatim ask stands
  in for its triage, so a model outage never hides a to-do.
"""
from __future__ import annotations

import asyncio
import re
import hashlib
import json
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from service.proactive import guard, judge as judge_mod, prepare as prepare_mod, sources, triage
from service.proactive.prepare import todo_id

JUDGE_EVERY_S = 30 * 60
TICK_GAP_S = 120
IDLE_GAP_S = 300
MAX_THREADS = 30
_MAIL_GIST_HOLD_S = 90
_PATIENT_WAIT_S = 600

_status = {"state": "idle", "phase": "", "progress": {"done": 0, "total": 0}, "account": None,
           "error": "", "started_at": None, "finished_at": None}
_task: asyncio.Task | None = None
_running = False
_rerun: dict | None = None
_next_tick_at = 0.0
_last_standdown = ""


def _log(text: str) -> None:
    print(f"[proactive] {text}", flush=True)


def _stood_down(reason: str) -> None:
    global _last_standdown
    if reason != _last_standdown:
        _last_standdown = reason
        _log(f"standing down: {reason}")


def status() -> dict:
    return json.loads(json.dumps(_status))


def running() -> bool:
    return _running or (_task is not None and not _task.done())


def _zone() -> str:
    try:
        from service.attention.detectors import local_timezone
        return local_timezone() or "UTC"
    except Exception:  # noqa: BLE001
        return "UTC"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:24]


# --- the model ----------------------------------------------------------------------


def _hold_mail_gists() -> None:
    try:
        from service.assistant import mail_gists
        mail_gists._next_call_at = max(mail_gists._next_call_at, time.time() + _MAIL_GIST_HOLD_S)
    except Exception:  # noqa: BLE001
        pass


async def resident_reply(system: str, material: str, abort, tokens) -> str | None:
    """One background call on the resident fast model (mail_gists' path), never
    while a gist call is in flight."""
    from service.assistant import mail_gists
    _hold_mail_gists()
    pending = mail_gists._task
    if pending is not None and not pending.done():
        await asyncio.wait({pending}, timeout=150)
    _hold_mail_gists()
    return await mail_gists._resident_reply(system, material, abort, tokens=tuple(tokens))


async def _offline(system, material, abort, tokens) -> str:
    return ""        # no model: every check fails closed, templates and rules stand in


def _foreground() -> bool:
    from service import idle
    return idle.foreground_busy()


# --- recipients --------------------------------------------------------------------------


def resolve_handle(item: dict, *, find_contacts=None) -> str | None:
    """The literal handle to text back, or None for no text card.

    The recipient is the conversation's own participant: the handle in its
    one-to-one chat id (sources.chat_handle), never a name lookup. A group or
    named chat has none. The label only confirms it: a number label must be
    that handle, and a name must be exactly one contact holding that handle.
    """
    from service.tools.imessage_tools import _norm_handle
    from service.tools.registry import _literal_message_recipient
    if item.get("group"):
        return None
    handle = _literal_message_recipient(item.get("handle") or "")
    if not handle:
        return None
    label = str((item.get("ref") or {}).get("label") or item.get("title") or "").strip()
    if _literal_message_recipient(label):
        return handle if _norm_handle(label) == _norm_handle(handle) else None
    if find_contacts is None:
        from service.tools.imessage_tools import find_contacts
    matches = [m for m in find_contacts(label) if str(m.get("name", "")).casefold() == label.casefold()]
    if len(matches) != 1:
        return None                      # unknown, or several contacts share the name
    if _norm_handle(handle) not in {_norm_handle(h) for h in matches[0].get("handles") or []}:
        return None
    return handle


def freeze_reply(reply_ref):
    """W3's envelope freeze (pure: no model, no Mail), or None without it."""
    try:
        from service.assistant.email_reply_api import freeze_reply as freeze
    except ImportError:
        return None
    return freeze(reply_ref)


# --- to-dos ---------------------------------------------------------------------------------


def todo_from_item(item: dict, card_ids: dict[str, str]) -> dict:
    return {"id": todo_id(item["primary"]), "title": guard.clean(item["title"], 120), "source": item["source"],
            "account": item.get("account", ""), "who": item.get("who", ""), "quote": item.get("quote", ""),
            "due_ts": item.get("due_ts"), "due_label": item.get("due_label", ""), "kind": item["kind"],
            "done": False, "card_id": card_ids.get(item["primary"]),
            "fingerprints": list(item.get("fingerprints") or [])}


def deterministic_todos(sched: dict, notes: list[dict], now: float, tz: str) -> list[dict]:
    """Calendar events and reminders, and Notes TODO lines, as to-dos by rule."""
    out = []
    for event in sched.get("calendar", []):
        out.append({"id": todo_id(event["id"]), "title": event["title"], "source": "calendar",
                    "account": event.get("account", ""), "who": "", "quote": "", "due_ts": event["when_ts"],
                    "due_label": guard.due_label(event["when_ts"], now, tz, has_clock=not event.get("all_day")),
                    "kind": "event", "done": False, "card_id": None, "fingerprints": [f"calendar:{event['id']}"],
                    "timed": not event.get("all_day")})
    for reminder in sched.get("reminders", []):
        due, fixed, has_clock = (reminder["when_ts"], "", False) if reminder.get("when_ts") is not None else (None, "", False)
        out.append({"id": todo_id(reminder["id"]), "title": reminder["title"], "source": "reminders",
                    "account": reminder.get("account", ""), "who": "", "quote": reminder["title"], "due_ts": due,
                    "due_label": guard.due_label(due, now, tz, has_clock=has_clock, fixed=fixed),
                    "kind": "reminder", "done": False, "card_id": None,
                    "fingerprints": [f"reminder:{reminder['id']}"]})
    for note in notes:
        out.append({"id": todo_id(note["id"]), "title": note["title"], "source": "notes", "account": note["account"],
                    "who": note.get("who", ""), "quote": note["title"], "due_ts": None, "due_label": "",
                    "kind": "task", "done": False, "card_id": None,
                    "fingerprints": [f"{note['id']}:{note['hash']}"]})
    return out


# --- one cycle ---------------------------------------------------------------------------------


async def _wait_out_turn(abort, patient: bool) -> bool:
    """True to go on. A patient (Analyze now) cycle waits out a user's turn."""
    if not abort():
        return True
    if not patient:
        return False
    deadline = time.monotonic() + _PATIENT_WAIT_S
    while abort() and time.monotonic() < deadline:
        await asyncio.sleep(1.0)
    return not abort()


def _survivor(item: dict, store, offline_results: dict) -> dict | None:
    if item["source"] == "notes":
        result = {"junk": False, "sensitive": False, "title": item["title"], "summary": "",
                  "asks_of_me": item["title"]}
        return {"item": item, "result": result}
    entry = store.triage_get(item["id"], item["hash"])
    result = (entry or {}).get("result") if entry else offline_results.get(item["id"])
    if not isinstance(result, dict) or result.get("junk") or result.get("sensitive"):
        return None
    return {"item": item, "result": result}


def _threads(survivors: list[dict]) -> list[dict]:
    out = []
    for survivor in sorted(survivors, key=lambda s: -(s["item"].get("received_ts") or 0)):
        item, result = survivor["item"], survivor["result"]
        if item["source"] not in ("mail", "messages"):
            continue
        out.append({"id": "thread:" + item["id"], "source": item["source"], "account": item["account"],
                    "who": item.get("who", ""), "title": guard.clean(result.get("title") or item.get("title"), 90),
                    "summary": guard.clean(result.get("summary") or result.get("asks_of_me"), 400),
                    "needs_reply": bool(result.get("asks_of_me")) and not item.get("replied")
                                   and item.get("person", True) and not item.get("group"),
                    "received_ts": item.get("received_ts"), "reply_ref": item.get("reply_ref", "")})
        if len(out) >= MAX_THREADS:
            break
    return out


def _account_schedule(sched: dict, account: str, own: str) -> dict:
    keep = lambda row: (row.get("account") or own) == account  # noqa: E731
    return {"calendar": [r for r in sched.get("calendar", []) if keep(r)],
            "reminders": [r for r in sched.get("reminders", []) if keep(r)]}


async def run_cycle(*, now: float | None = None, tz: str | None = None, account: str | None = None,
                    force: bool = False, patient: bool = False, reply_fn=None, abort=None,
                    collect=None, schedule_fn=None, store=None, resolve=None, linked: set[str] | None = None,
                    own: str | None = None, allowed: set[str] | None = None, freeze=None) -> dict:
    """One full cycle. Every dependency can be injected (tests use a fake model
    and fixture sources); production reads the caches and the resident model."""
    global _running
    if _running:
        return {"ok": False, "status": "busy"}
    _running = True
    try:
        return await _cycle(now=now, tz=tz, account=account, force=force, patient=patient, reply_fn=reply_fn,
                            abort=abort, collect=collect, schedule_fn=schedule_fn, store=store, resolve=resolve,
                            linked=linked, own=own, allowed=allowed, freeze=freeze)
    except Exception as exc:  # noqa: BLE001 — a failed cycle leaves the last good output in place
        traceback.print_exc()
        _status.update(state="error", error=type(exc).__name__, finished_at=time.time())
        return {"ok": False, "status": "error", "error": type(exc).__name__}
    finally:
        _running = False


async def _cycle(*, now, tz, account, force, patient, reply_fn, abort, collect, schedule_fn, store, resolve,
                 linked, own, allowed, freeze) -> dict:
    from service.proactive.store import store as default_store
    store = store or default_store()
    now = time.time() if now is None else now
    tz = tz or _zone()
    abort = abort or _foreground
    reply_fn = reply_fn or resident_reply
    linked = sources.gmail_accounts() if linked is None else linked
    own = sources.own_account() if own is None else own
    allowed = triage.allowed_words() if allowed is None else allowed
    freeze = freeze or freeze_reply
    started = time.time()
    _status.update(state="working", phase="reading", account=account, error="", started_at=started,
                   finished_at=None, progress={"done": 0, "total": 0})
    # Reading the caches parses megabytes of mail and texts: off the event loop.
    items = await asyncio.to_thread(collect or (lambda n, z: sources.collect(n, z)), now, tz)
    if account:
        items.sort(key=lambda i: str(i.get("account", "")).casefold() != account.casefold())

    # 1. Triage, incrementally.
    pending = []
    for item in items:
        if item["source"] not in ("mail", "messages"):
            continue
        entry = store.triage_get(item["id"], item["hash"])
        if entry and (entry["status"] in ("ok", "sensitive") or entry.get("attempts", 0) >= 2):
            continue
        pending.append(item)
    _status["progress"] = {"done": 0, "total": len(pending)}
    model_ok, yielded = True, False
    offline_results: dict[str, dict] = {}
    for index, item in enumerate(pending):
        if model_ok and not await _wait_out_turn(abort, patient):
            yielded = True
            _stood_down("a turn is in progress")
            break
        if model_ok:
            state, result = await triage.triage_item(item, reply_fn, abort)
            if state == "standdown":
                if abort() and await _wait_out_turn(abort, patient):
                    state, result = await triage.triage_item(item, reply_fn, abort)
                if state == "standdown":
                    if abort():
                        yielded = True
                        break
                    model_ok = False        # the model isn't resident: go on by rule
                    _stood_down("the model isn't available; using rules")
            if state != "standdown" and not store.erased_since(item.get("account", ""), started):
                store.triage_put(item["id"], item["hash"], result, status=state, account=item.get("account", ""))
        if not model_ok:
            rule = triage.fallback(item)
            if rule is not None:
                offline_results[item["id"]] = rule
        _status["progress"] = {"done": index + 1, "total": len(pending)}
    if yielded:
        # Leave the last output in place; the next tick resumes from the checkpoint.
        _status.update(state="idle", phase="", finished_at=time.time())
        return {"ok": False, "status": "yielded", "progress": _status["progress"]}

    # 2. Judge and prepare, per account.
    survivors = [s for s in (_survivor(i, store, offline_results) for i in items) if s is not None]
    sched = (schedule_fn or (lambda n, z: _default_schedule(n, z, own=own, linked=linked)))(now, tz)
    accounts = sorted({own} | {i.get("account") or own for i in items}
                      | {r.get("account") or own for r in sched.get("calendar", []) + sched.get("reminders", [])},
                      key=lambda a: (a != own, a))
    suppressed = store.suppressed_fingerprints()
    previous = store.state()
    judged = dict(previous.get("judged") or {})
    cards_by_account = {a: [c for c in previous.get("cards") or [] if c.get("account") == a] for a in accounts}
    todos_by_account = {a: [t for t in previous.get("todos") or [] if t.get("account") == a] for a in accounts}
    summaries = dict(previous.get("summaries") or {})
    _status["phase"] = "judging"
    for acct in accounts:
        mine = [s for s in survivors if (s["item"].get("account") or own) == acct]
        acct_sched = _account_schedule(sched, acct, own)
        key = _digest({"s": sorted(f"{s['item']['id']}:{s['item']['hash']}:{_digest(s['result'])}" for s in mine),
                       "c": [r["id"] for r in acct_sched["calendar"]], "r": [r["id"] for r in acct_sched["reminders"]],
                       "x": sorted(suppressed), "day": datetime.fromtimestamp(now, ZoneInfo(tz)).date().isoformat(),
                       "m": model_ok})
        last = judged.get(acct) or {}
        if not force and last.get("key") == key and now - float(last.get("at") or 0) < JUDGE_EVERY_S:
            continue
        if model_ok:
            if not await _wait_out_turn(abort, patient):
                yielded = True
                break
            state, ranked, summary = await judge_mod.judge(mine, acct_sched, reply_fn, abort, now=now, tz=tz,
                                                           suppressed=suppressed, allowed=allowed)
            if state == "standdown":
                if abort():
                    yielded = True
                    break
                model_ok = False
        if not model_ok:
            ranked = judge_mod.rank(judge_mod.by_rule(mine, acct_sched, now=now, tz=tz, suppressed=suppressed,
                                                      allowed=allowed), now)
            summary = ""
        _status["phase"] = "preparing"
        state, cards = await prepare_mod.prepare(ranked, acct_sched, reply_fn if model_ok else _offline, abort,
                                                 now=now, tz=tz, linked=linked, resolve_handle=resolve or resolve_handle,
                                                 allowed=allowed, freeze=freeze)
        if state == "standdown":
            if abort():
                yielded = True
                break
            state, cards = await prepare_mod.prepare(ranked, acct_sched, _offline, abort, now=now, tz=tz,
                                                     linked=linked, resolve_handle=resolve or resolve_handle,
                                                     allowed=allowed, freeze=freeze)
        dismissed = set(store.decisions()["dismissed"]) | set(store.decisions()["fired"])
        cards = [c for c in cards if c["id"] not in dismissed]
        from service.proactive import executor
        for card in cards:
            sealed = card.pop("_sealed", None)
            if sealed:
                executor.remember(card["id"], *sealed)
        card_ids = {c["sources"][0]["ref"]: c["id"] for c in cards}
        cards_by_account[acct] = cards
        todos_by_account[acct] = [todo_from_item(i, card_ids) for i in ranked]
        summaries[acct] = summary
        judged[acct] = {"key": key, "at": now, "model": model_ok}
    # An account erased while this cycle ran (Disconnect & erase) keeps nothing.
    erased = {a for a in accounts if a != own and store.erased_since(a, started)}
    accounts = [a for a in accounts if a not in erased]
    survivors = [s for s in survivors if (s["item"].get("account") or own) not in erased]
    notes = [i for i in items if i["source"] == "notes"]
    summaries = {k: v for k, v in summaries.items() if k not in erased}
    judged = {k: v for k, v in judged.items() if k not in erased}
    store.prune(now)
    store.save_state(
        as_of=now, timezone=tz, accounts=accounts,
        cards=[c for a in accounts for c in cards_by_account.get(a, [])],
        todos=[t for a in accounts for t in todos_by_account.get(a, [])],
        notes=[{k: n[k] for k in ("id", "title", "account", "who", "hash")} for n in notes],
        threads=_threads(survivors), summaries=summaries, judged=judged, model=model_ok)
    _status.update(state="idle" if not yielded else "idle", phase="", finished_at=time.time())
    _log(f"cycle done: {len(items)} items, {len(pending)} read, {len(survivors)} kept, "
         f"{sum(len(v) for v in cards_by_account.values())} cards{' (by rule)' if not model_ok else ''}")
    return {"ok": not yielded, "status": "yielded" if yielded else "done", "model": model_ok,
            "items": len(items), "read": len(pending), "survivors": len(survivors)}


def _default_schedule(now: float, tz: str, *, own: str, linked: set[str]) -> dict:
    from service.assistant.store import assistant_store
    return sources.schedule(assistant_store, now, tz, own=own, linked=linked)


# --- background scheduling ---------------------------------------------------------------------


async def _tick() -> None:
    global _next_tick_at, _rerun
    result = await run_cycle()
    _next_tick_at = time.time() + (TICK_GAP_S if result.get("status") in ("yielded",) else IDLE_GAP_S)
    while _rerun is not None:
        request, _rerun = _rerun, None
        await run_cycle(force=True, patient=True, account=request.get("account"))


def schedule_tick() -> asyncio.Task | None:
    """Single-flight background tick for the scheduler loop; never awaited there."""
    global _task, _rerun
    from service.assistant.hub import hub
    if running():
        return None
    if _rerun is not None:
        # An Analyze request made where no event loop was running (request_analyze).
        request, _rerun = _rerun, None
        analyze(request.get("account"))
        return _task
    if time.time() < _next_tick_at:
        return None
    if _foreground():
        _stood_down("a turn is in progress")
        return None
    if not hub.has_subscribers:
        _stood_down("the app isn't connected to the event stream")
        return None
    try:
        from service.assistant import mail_gists
        if mail_gists._task is not None and not mail_gists._task.done():
            return None
    except Exception:  # noqa: BLE001
        pass
    _task = asyncio.create_task(_tick())
    return _task


def analyze(account: str | None = None, **kwargs) -> dict:
    """Analyze now: a forced, patient cycle in the background. One already
    running gets a forced rerun queued behind it, never a second cycle."""
    global _task, _rerun
    if running():
        _rerun = {"account": account}
        return {"ok": True, "state": "working", "queued": True, "progress": _status["progress"]}

    async def go():
        global _rerun
        await run_cycle(force=True, patient=True, account=account, **kwargs)
        while _rerun is not None:
            request, _rerun = _rerun, None
            await run_cycle(force=True, patient=True, account=request.get("account"), **kwargs)

    _status.update(state="working", phase="starting", account=account, progress={"done": 0, "total": 0})
    _task = asyncio.create_task(go())
    return {"ok": True, "state": "working", "queued": False, "progress": _status["progress"]}


def request_analyze(account: str | None = None) -> dict:
    """For callers outside this package (W4's Connect Google): start Analyze now
    for an account. Sync; safe with or without a running event loop: without
    one, the request waits for the scheduler's next tick."""
    global _rerun
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _rerun = {"account": account}
        return {"ok": True, "state": "working", "queued": True, "progress": _status["progress"]}
    return analyze(account)


# --- what the Brief shows -------------------------------------------------------------------------


def _public(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in ("fingerprints",)}


_NUMBER = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]


def _count(n: int) -> str:
    return _NUMBER[n] if n < len(_NUMBER) else str(n)


def _clock_words(ts: float, zone) -> str:
    local = datetime.fromtimestamp(ts, zone)
    if local.hour == 12 and local.minute == 0:
        return "noon"
    return local.strftime("%-I:%M %p").replace(":00 ", " ").lower().replace(" am", "am").replace(" pm", "pm") \
        if local.minute == 0 else local.strftime("%-I:%M")


def _short_title(title: str, limit: int = 36) -> str:
    """An event name short enough for a sentence: no bracketed tail, cut at a word."""
    text = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*", " ", title or "").strip() or (title or "")
    text = guard.clean(text, 200)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" -–—:,x")
    return text


def _day_summary(cards: list[dict], todos: list[dict], threads: list[dict], now: float, tz: str) -> str:
    """A natural one- or two-sentence overview, built by code: "Two events
    today — CruzHacks Hack Day at 11:45 and Hacktoberfest at noon. Three
    people are waiting on replies." """
    zone = ZoneInfo(tz)
    today = datetime.fromtimestamp(now, zone).date()
    events = sorted((t for t in todos if t["source"] == "calendar" and t.get("due_ts") and t.get("timed", True)
                     and datetime.fromtimestamp(t["due_ts"], zone).date() == today and t["due_ts"] >= now - 3600),
                    key=lambda t: t["due_ts"])
    sentences = []
    if events:
        named = [f"{_short_title(e['title'])} at {_clock_words(e['due_ts'], zone)}" for e in events[:2]]
        lead = f"{_count(len(events))} event{'s' if len(events) != 1 else ''} today"
        rest = f", plus {len(events) - 2} more" if len(events) > 2 else ""
        sentences.append(f"{lead} — {' and '.join(named)}{rest}.")
    else:
        sentences.append("Nothing on your calendar for the rest of today.")
    waiting = {t["who"] for t in threads if t.get("needs_reply") and t.get("who")}
    if waiting:
        n = len(waiting)
        sentences.append(f"{_count(n)} {'people are' if n != 1 else 'person is'} waiting on a reply.")
    else:
        open_todos = [t for t in todos if not t["done"] and t["source"] not in ("calendar",)]
        if open_todos:
            n = len(open_todos)
            sentences.append(f"{_count(n)} thing{'s' if n != 1 else ''} on your to-do list.")
    return " ".join(sentences)


def snapshot(*, account: str | None = None, now: float | None = None, tz: str | None = None, store=None,
             schedule_fn=None, own: str | None = None, linked: set[str] | None = None) -> dict:
    """workspace.proactive, per the shared contract."""
    from service.proactive.store import store as default_store
    store = store or default_store()
    now = time.time() if now is None else now
    state = store.state()
    tz = tz or state.get("timezone") or _zone()
    linked = sources.gmail_accounts() if linked is None else linked
    own = sources.own_account() if own is None else own
    try:
        sched = (schedule_fn or (lambda n, z: _default_schedule(n, z, own=own, linked=linked)))(now, tz)
    except Exception:  # noqa: BLE001
        sched = {"calendar": [], "reminders": []}
    notes = state.get("notes") or []
    merged: dict[str, dict] = {}
    for todo in deterministic_todos(sched, notes, now, tz) + list(state.get("todos") or []):
        if todo["id"] in merged:
            base = merged[todo["id"]]
            merged[todo["id"]] = {**base, "card_id": base.get("card_id") or todo.get("card_id"),
                                  "fingerprints": base.get("fingerprints") or todo.get("fingerprints")}
        else:
            merged[todo["id"]] = dict(todo)
    done = store.done_todos(now)
    for todo_id_, entry in done.items():
        if todo_id_ in merged:
            merged[todo_id_]["done"] = True
            merged[todo_id_]["card_id"] = None
        elif isinstance(entry.get("todo"), dict):
            merged[todo_id_] = {**entry["todo"], "done": True, "card_id": None}
    added = store.added_to_day()
    for todo in merged.values():
        todo["added_to_day"] = todo["id"] in added
    cards = [c for c in state.get("cards") or [] if c.get("todo_id") not in done]
    todos = sorted(merged.values(), key=lambda t: (t["done"], t.get("due_ts") is None, t.get("due_ts") or 0,
                                                   t["title"].casefold()))
    threads = list(state.get("threads") or [])
    accounts = list(state.get("accounts") or [own])
    if account:
        match = lambda x: str(x.get("account") or "").casefold() == account.casefold()  # noqa: E731
        cards, todos, threads = [c for c in cards if match(c)], [t for t in todos if match(t)], \
            [t for t in threads if match(t)]
    summary = _day_summary(cards, todos, threads, now, tz)
    if running():
        view_state = "working"
    elif state.get("as_of") is not None:
        view_state = "ready"
    else:
        view_state = "unavailable"
    result = {"state": view_state, "as_of": state.get("as_of"), "progress": dict(_status["progress"]),
              "accounts": accounts, "summary": summary, "cards": [_public(c) for c in cards],
              "todos": [_public(t) for t in todos], "threads": threads}
    if view_state == "unavailable":
        result["reason"] = "Wisp hasn't read your mail and texts yet. Tap Analyze now."
    if _status.get("error"):
        result["error"] = _status["error"]
    return result


def attach(workspace: dict, now: float | None = None) -> None:
    """Add workspace["proactive"] to the Today workspace. Never fatal."""
    try:
        workspace["proactive"] = snapshot(now=now)
    except Exception:  # noqa: BLE001
        traceback.print_exc()

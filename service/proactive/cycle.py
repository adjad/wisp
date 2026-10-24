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
    entry = store.triage_get(item["id"], _tkey(item))
    result = (entry or {}).get("result") if entry else offline_results.get(item["id"])
    if not isinstance(result, dict) or result.get("junk") or result.get("sensitive"):
        return None
    return {"item": item, "result": result}


def _tkey(item: dict) -> str:
    """The triage checkpoint key: content hash plus the triage rules' version,
    so a summary written under older rules is read again."""
    return f"{item['hash']}:{triage.VERSION}"


WAITING_S = 7 * 86400


def _any_result(item: dict, store, offline_results: dict) -> dict | None:
    """An item with its triage result, junk included (a real contact's last
    text can read as small talk and still be waiting on the user)."""
    if item["source"] != "messages" or triage.withheld(item):
        return None                      # a withheld conversation never becomes a card
    entry = store.triage_get(item["id"], _tkey(item))
    result = (entry or {}).get("result") if entry else offline_results.get(item["id"])
    if result is None:
        result = {"junk": False, "sensitive": False, "title": "", "summary": "", "asks_of_me": ""}
    if not isinstance(result, dict) or result.get("sensitive"):
        return None
    return {"item": item, "result": result}


def with_waiting_people(ranked: list[dict], pool: list[dict], sched: dict, *, now: float, tz: str,
                        suppressed: set[str], allowed: set[str], debug: list | None = None) -> list[dict]:
    """Card order, made deterministic for people: the judge's items from
    people, then every one-to-one conversation whose last message is an
    unanswered incoming text under 3 days old (even when the judge left it
    out), then everything else. The 5-card cap applies after this."""
    have = {i["primary"] for i in ranked if not i.get("unranked")}
    waiting = []
    for candidate in sorted(pool, key=lambda s: -(s["item"].get("received_ts") or 0)):
        item = candidate["item"]
        reason = ("group chat" if item.get("group") else "you answered last" if item.get("replied")
                  else "not a person" if not item.get("person") else "already judged" if item["id"] in have
                  else "older than 7 days" if now - (item.get("received_ts") or 0) > WAITING_S else "")
        made = None
        if not reason:
            made = judge_mod._finalize({"title": "", "action": "reply", "why": "", "due": "", "urgency": "high"},
                                       {"kind": "item", "survivor": candidate}, [], now=now, tz=tz, sched=sched,
                                       suppressed=suppressed, allowed=allowed)
            reason = "" if made is not None else "dismissed or handled"
        if debug is not None:
            debug.append({"who": item.get("who", ""), "waiting": not reason, "reason": reason or "card candidate"})
        if made is not None:
            made["waiting"] = True
            waiting.append(made)
    taken = {w["primary"] for w in waiting}
    judged = [i for i in ranked if not i.get("unranked")]
    people = [i for i in judged if (i.get("item") or {}).get("person")]
    others = [i for i in judged if not (i.get("item") or {}).get("person")]
    rest = [i for i in ranked if i.get("unranked") and i["primary"] not in taken]
    return people + waiting + others + rest


def _served_thread(thread: dict, names: list[str]) -> dict:
    """The backstop at serve time: re-attribute the summary, and replace one
    that still reads badly with the plain code sentence."""
    user_wrote = thread.get("user_wrote", thread.get("source") == "messages")
    summary = triage.attribute(thread.get("summary", ""), names, thread.get("who", ""), user_wrote)
    like = {"who": thread.get("who"), "source": thread.get("source"), "title": thread.get("title"),
            "user_wrote": user_wrote, "group": " in " in str(thread.get("who") or "")}
    if summary and not triage.sane(summary, like):
        summary = triage.template_summary(like, {"title": thread.get("title")})
    title = re.sub(r"^\s*you\s*:\s*", "", thread.get("title", ""), flags=re.I)
    if like["group"]:
        title = re.sub(r"^group\s+(?=\S)", "", title, flags=re.I)    # "Group camp larp"
    return {**thread, "summary": summary, "title": guard.second_person(title, names)}


def _attended(thread: dict, now: float, tz: str) -> dict:
    kind, reason = attention(thread, now, tz)
    public = {k: v for k, v in thread.items() if k not in ("asked", "deadline")}
    return {**public, "attention": kind, "attention_reason": reason}


_DUE_WORD = re.compile(r"\b(exam|midterm|final|quiz|deadline|due|assignment|homework|application|"
                       r"registration|rsvp|interview|appointment|submission|submit|expires?|last day|closes?)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WAITING_MAIL_S = 14 * 86400
_ANSWER = re.compile(r"\b(?:reply|respond|answer|confirm|let (?:him|her|them|\w+) know|get back)\b", re.I)


def needs_reply(item: dict, result: dict, now: float) -> bool:
    """A real person's last message is incoming and unanswered: a one-to-one
    text, or an email from a person that asks the user something."""
    if item.get("replied") or item.get("group") or not item.get("person", True):
        return False
    age = now - float(item.get("received_ts") or 0)
    if item["source"] == "messages":
        return age <= WAITING_S
    asks = str(result.get("asks_of_me") or "")
    return (item["source"] == "mail" and bool(asks) and age <= _WAITING_MAIL_S
            and ("?" in (item.get("text") or "") or bool(_ANSWER.search(asks))))


def mentioned_deadline(item: dict, now: float, tz: str) -> dict | None:
    """The first sentence naming a deadline word ("exam", "due") with a date
    that resolve.py reads from that same sentence, still ahead. The date is
    always the source's own words."""
    arrival = item.get("received_ts")
    if type(arrival) not in (int, float):
        return None
    for sentence in _SENTENCE.split(item.get("text") or ""):
        word = _DUE_WORD.search(sentence)
        if not word or len(sentence) > 400:
            continue
        due, _label, _clock = guard.resolve_due(sentence, float(arrival), tz)
        if due is None or due < now or due > now + 21 * 86400:
            continue
        return {"due_ts": due, "word": word.group(1).lower()}
    return None


def _when_words(due_ts: float, now: float, tz: str) -> str:
    zone = ZoneInfo(tz)
    today, day = datetime.fromtimestamp(now, zone).date(), datetime.fromtimestamp(due_ts, zone).date()
    if day == today:
        return "today"
    if (day - today).days == 1:
        return "tomorrow"
    if (day - today).days < 7:
        same_week = day.isocalendar()[:2] == today.isocalendar()[:2]
        return day.strftime("%a") if same_week else "next " + day.strftime("%a")
    return day.strftime("%b %-d")


def attention(thread: dict, now: float, tz: str) -> tuple[str, str]:
    """("reply"|"deadline"|"fyi", a short reason), by code, as of `now`."""
    if thread.get("needs_reply"):
        days = int((now - float(thread.get("received_ts") or now)) // 86400)
        if days >= 1:
            return "reply", f"waiting {days} day{'s' if days != 1 else ''}"
        if thread.get("asked"):
            return "reply", "asked you a question"
        return "reply", "texted you" if thread.get("source") == "messages" else "emailed you"
    due = thread.get("deadline") or {}
    if type(due.get("due_ts")) in (int, float) and due["due_ts"] >= now:
        word = {"due": "due", "submit": "due", "submission": "due"}.get(due.get("word"), due.get("word") or "due")
        return "deadline", f"{word} {_when_words(due['due_ts'], now, tz)}"
    if thread.get("source") == "messages" and thread.get("replied"):
        return "fyi", "you answered last"
    return "fyi", ""


def order_threads(threads: list[dict]) -> list[dict]:
    rank = {"reply": 0, "deadline": 1, "fyi": 2}
    return sorted(threads, key=lambda t: (rank.get(t.get("attention"), 2), -(t.get("received_ts") or 0)))


def _threads(survivors: list[dict], now: float | None = None, tz: str = "UTC") -> list[dict]:
    now = time.time() if now is None else now
    out = []
    for survivor in sorted(survivors, key=lambda s: -(s["item"].get("received_ts") or 0)):
        item, result = survivor["item"], survivor["result"]
        if item["source"] not in ("mail", "messages"):
            continue
        out.append({"id": "thread:" + item["id"], "source": item["source"], "account": item["account"],
                    "who": item.get("who", ""), "title": guard.clean(result.get("title") or item.get("title"), 90),
                    "summary": guard.clean(result.get("summary") or result.get("asks_of_me")
                                           or (item.get("title") if item["source"] == "mail" else ""), 400),
                    "user_wrote": bool(item.get("user_wrote")),
                    "needs_reply": needs_reply(item, result, now),
                    "asked": "?" in (item.get("text") or "") and bool(result.get("asks_of_me")),
                    "replied": bool(item.get("replied")),
                    "deadline": mentioned_deadline(item, now, tz),
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
                    own: str | None = None, allowed: set[str] | None = None, freeze=None,
                    deadline_fn=None) -> dict:
    """One full cycle. Every dependency can be injected (tests use a fake model
    and fixture sources); production reads the caches and the resident model."""
    global _running
    if _running:
        return {"ok": False, "status": "busy"}
    _running = True
    try:
        return await _cycle(now=now, tz=tz, account=account, force=force, patient=patient, reply_fn=reply_fn,
                            abort=abort, collect=collect, schedule_fn=schedule_fn, store=store, resolve=resolve,
                            linked=linked, own=own, allowed=allowed, freeze=freeze,
                            deadline_fn=deadline_fn if deadline_fn is not None else
                            (_default_deadlines if schedule_fn is None else None))
    except Exception as exc:  # noqa: BLE001 — a failed cycle leaves the last good output in place
        traceback.print_exc()
        _status.update(state="error", error=type(exc).__name__, finished_at=time.time())
        return {"ok": False, "status": "error", "error": type(exc).__name__}
    finally:
        _running = False


async def _cycle(*, now, tz, account, force, patient, reply_fn, abort, collect, schedule_fn, store, resolve,
                 linked, own, allowed, freeze, deadline_fn=None) -> dict:
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
    looked: dict = {}
    items = await asyncio.to_thread(collect or (lambda n, z: sources.collect(n, z, counts=looked)), now, tz)
    if account:
        items.sort(key=lambda i: str(i.get("account", "")).casefold() != account.casefold())

    # 1. Triage, incrementally.
    pending = []
    for item in items:
        if item["source"] not in ("mail", "messages"):
            continue
        entry = store.triage_get(item["id"], _tkey(item))
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
                store.triage_put(item["id"], _tkey(item), result, status=state, account=item.get("account", ""))
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
    accounts = account_list(own, linked, [i.get("account") for i in items]
                            + [r.get("account") for r in sched.get("calendar", []) + sched.get("reminders", [])])
    suppressed = store.suppressed_fingerprints()
    previous = store.state()
    judged = dict(previous.get("judged") or {})
    cards_by_account = {a: [c for c in previous.get("cards") or [] if c.get("account") == a] for a in accounts}
    todos_by_account = {a: [t for t in previous.get("todos") or [] if t.get("account") == a] for a in accounts}
    summaries = dict(previous.get("summaries") or {})
    debug_by_account: dict = dict(previous.get("debug") or {})
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
        waiting_debug: list = []
        waiting_pool = []
        for i in items:
            if (i.get("account") or own) != acct or i["source"] != "messages":
                continue
            found = _any_result(i, store, offline_results)
            if found is None:
                waiting_debug.append({"who": i.get("who", ""), "waiting": False,
                                      "reason": "withheld or sensitive"})
            else:
                waiting_pool.append(found)
        ranked = with_waiting_people(ranked, waiting_pool, acct_sched, now=now, tz=tz, suppressed=suppressed,
                                     allowed=allowed, debug=waiting_debug)
        _status["phase"] = "preparing"
        state, cards = await prepare_mod.prepare(ranked, acct_sched, reply_fn if model_ok else _offline, abort,
                                                 now=now, tz=tz, linked=linked, resolve_handle=resolve or resolve_handle,
                                                 allowed=allowed, freeze=freeze, debug=waiting_debug)
        debug_by_account[acct] = waiting_debug
        if state == "standdown":
            if abort():
                yielded = True
                break
            state, cards = await prepare_mod.prepare(ranked, acct_sched, _offline, abort, now=now, tz=tz,
                                                     linked=linked, resolve_handle=resolve or resolve_handle,
                                                     allowed=allowed, freeze=freeze)
        dismissed = set(store.decisions()["dismissed"]) | set(store.decisions()["fired"])
        cards = [c for c in cards if c["id"] not in dismissed]
        for card in cards:
            if store.locked(card, now):
                card["action"]["locked"] = "unknown"     # an unconfirmed send is never offered again
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
    # 3. Deadline cards: a real free slot for work due soon. Code only, every
    # cycle (a slot goes stale as the day moves), filling what replies leave.
    if not yielded and deadline_fn is not None:
        decided = store.decisions()
        skip = set(decided["dismissed"]) | set(decided["fired"])
        try:
            # On the loop thread: the store's SQLite connection belongs to it,
            # and this is a few indexed reads plus pure planning.
            found = deadline_fn(now, tz, skip, own, linked)
        except Exception:  # noqa: BLE001 — no deadline cards this time
            traceback.print_exc()
            found = []
        for acct in accounts:
            keep = [c for c in cards_by_account.get(acct, []) if c["action"]["method"] != "block_time"]
            taken = {c.get("todo_id") for c in keep}
            mine = [c for c in found if (c.get("account") or own) == acct and c.get("todo_id") not in taken]
            cards_by_account[acct] = order_cards(keep, mine)
    # What Wisp looked at, bulk mail it skipped included. An injected collect
    # (tests) reports nothing, so its items stand in.
    if not looked:
        for i in items:
            if i["source"] in ("mail", "messages", "notes"):
                key = {"mail": "mail", "messages": "texts", "notes": "notes"}[i["source"]]
                sources.tally(looked, i.get("account") or own, **{key: 1})
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
        threads=_threads(survivors, now, tz), summaries=summaries, judged=judged, model=model_ok,
        debug={a: v for a, v in debug_by_account.items() if a in accounts},
        # Every account looked at counts, even one whose mail was all bulk.
        stats={a: _looked_stats(looked.get(a)) for a in sorted(set(accounts) | (set(looked) - erased))})
    _status.update(state="idle" if not yielded else "idle", phase="", finished_at=time.time())
    _log(f"cycle done: {len(items)} items, {len(pending)} read, {len(survivors)} kept, "
         f"{sum(len(v) for v in cards_by_account.values())} cards{' (by rule)' if not model_ok else ''}")
    return {"ok": not yielded, "status": "yielded" if yielded else "done", "model": model_ok,
            "items": len(items), "read": len(pending), "survivors": len(survivors)}


_REPLY_METHODS = ("email_reply", "text_reply")


def order_cards(cards: list[dict], deadline_cards: list[dict], limit: int = prepare_mod.MAX_CARDS) -> list[dict]:
    """Someone waiting on the user comes first; deadline cards fill the slots
    that leaves, ahead of the rest."""
    replies = [c for c in cards if c["action"]["method"] in _REPLY_METHODS]
    rest = [c for c in cards if c["action"]["method"] not in _REPLY_METHODS]
    return (replies + deadline_cards + rest)[:limit]


def _default_deadlines(now: float, tz: str, skip: set[str], own: str, linked: set[str]) -> list[dict]:
    from service.proactive import deadlines
    rows = [{**r, "account": sources.calendar_account(r, linked=linked, own=own)
             if r.get("source") == "calendar" else own} for r in deadlines.default_rows(now)]
    return deadlines.deadline_cards(rows, deadlines.default_day_state, now, tz, skip=skip)


_OWNER_ONLY = ("messages", "notes", "reminders")


def account_list(own: str, linked: set[str], seen) -> list[str]:
    """The owner's bucket first, then each linked Google account. A label
    that is neither (an address from an older build) is not an account."""
    keep = {str(a).casefold() for a in seen if a} & set(linked)
    return [own] + sorted(keep | set(linked))


def _source_kind(record: dict) -> str:
    if "sources" in record:
        return str(((record.get("sources") or [{}])[0] or {}).get("kind") or "")
    return str(record.get("source") or "")


def owned(record: dict, own: str, linked: set[str]) -> dict:
    """The record with its real bucket. Texts, Notes and Reminders are always
    the owner's, whatever an older build tagged them; a linked account keeps
    only what came from its own Gmail or Google Calendar."""
    account = str(record.get("account") or "").casefold()
    if _source_kind(record) in _OWNER_ONLY or account not in linked:
        if record.get("account") != own:
            return {**record, "account": own}
    return record


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


def _lede(cards: list[dict], todos: list[dict], summary: str, now: float, tz: str) -> str:
    """One line that leads with the most proactive thing, built by code:
    "Hack Day at 11:45. Worksheet 3 for STAT 5 is due tomorrow, and I found
    you an hour for it." A Wisp test reminder never leads."""
    from service.proactive import deadlines
    zone = ZoneInfo(tz)
    today = datetime.fromtimestamp(now, zone).date()
    events = sorted((t for t in todos if t["source"] == "calendar" and t.get("timed", True) and t.get("due_ts")
                     and not t["done"] and not deadlines.is_test(t["title"])
                     and datetime.fromtimestamp(t["due_ts"], zone).date() == today and t["due_ts"] >= now - 900),
                    key=lambda t: t["due_ts"])
    opener = f"{_short_title(events[0]['title'])} at {_clock_words(events[0]['due_ts'], zone)}." if events else ""
    live = [c for c in cards if c["action"].get("locked") != "unknown"
            and not deadlines.is_test(c.get("title", ""))]
    reply = next((c for c in live if c["action"]["method"] in _REPLY_METHODS), None)
    block = next((c for c in live if c["action"]["method"] == "block_time"), None)
    point = ""
    if reply is not None:
        who = prepare_mod._first_name((reply.get("sources") or [{}])[0].get("who", "")) or "Someone"
        point = (f"{who} is waiting on you, and I drafted a reply." if reply["action"].get("draft")
                 else f"{who} is waiting on you.")
    elif block is not None:
        minutes = (block.get("action", {}).get("envelope") or {}).get("duration_min") or 30
        due = block.get("due_words") or "due soon"
        point = f"{block['title']} is {due}, and I found you {deadlines.length_words(minutes)} for it."
    if point:
        return f"{opener} {point}".strip()
    return summary


def _looked_stats(row: dict | None) -> dict:
    row = row or {}
    breakdown = {k: int(row.get(k) or 0) for k in ("mail", "texts", "notes")}
    return {"read": breakdown["mail"] + breakdown["texts"], "filtered": int(row.get("filtered") or 0),
            "read_breakdown": breakdown}


def _stats(state: dict, cards: list[dict], todos: list[dict], account: str | None) -> dict:
    """ "Wisp read 102 emails and texts · skipped 90 newsletters · found 3
    things · drafted 1 reply". `read` is every mail and text conversation
    looked at in the window, bulk included; `filtered` is the bulk left out."""
    per = [v or {} for a, v in (state.get("stats") or {}).items()
           if not account or str(a).casefold() == account.casefold()]
    read = sum(int(v.get("read") or 0) for v in per)
    filtered = sum(int(v.get("filtered") or 0) for v in per)
    breakdown = {k: sum(int((v.get("read_breakdown") or {}).get(k) or 0) for v in per)
                 for k in ("mail", "texts", "notes")}
    from service.proactive.deadlines import is_test
    open_todos = {t["id"] for t in todos if not t["done"] and t["source"] != "calendar" and not is_test(t["title"])}
    extra = {c["id"] for c in cards if c.get("todo_id") not in open_todos}
    return {"read": read, "found": len(open_todos) + len(extra),
            "drafted": sum(1 for c in cards if (c.get("action") or {}).get("draft")),
            "filtered": filtered, "read_breakdown": breakdown, "as_of": state.get("as_of")}


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
    cards = [{**c, "action": {**c["action"], "locked": "unknown"}} if store.locked(c, now) else c for c in cards]
    # A held slot that has started is no offer; the next cycle finds another.
    cards = [c for c in cards if c["action"].get("method") != "block_time"
             or float((c["action"].get("slot") or {}).get("start") or 0) > now]
    for card in cards:
        if card["action"].get("method") == "block_time" and card.get("todo_id") in merged \
                and not merged[card["todo_id"]].get("card_id"):
            merged[card["todo_id"]]["card_id"] = card["id"]
    todos = sorted(merged.values(), key=lambda t: (t["done"], t.get("due_ts") is None, t.get("due_ts") or 0,
                                                   t["title"].casefold()))
    names = triage.user_names()
    threads = order_threads([_attended(_served_thread(t, names), now, tz) for t in state.get("threads") or []])
    erased = store.erased_accounts()
    seen = {str(a).casefold() for a in state.get("accounts") or []}
    accounts = account_list(own, {a for a in linked if a in seen or a not in erased}, seen)
    cards = [owned(c, own, linked) for c in cards]
    todos = [owned(t, own, linked) for t in todos]
    threads = [owned(t, own, linked) for t in threads]
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
              "accounts": accounts, "owner": {"id": own, "label": sources.owner_label()}, "summary": summary, "cards": [_public(c) for c in cards],
              "todos": [_public(t) for t in todos], "threads": threads,
              "lede": _lede(cards, todos, summary, now, tz), "stats": _stats(state, cards, todos, account)}
    if view_state == "unavailable":
        result["reason"] = "Wisp hasn't read your mail and texts yet. Tap Analyze now."
    # Why a waiting person did or didn't get a card ("you answered last",
    # "no contact handle"): for Debug Mode and the integrator, names only.
    result["debug"] = {"waiting": (state.get("debug") or {}).get(account or own, [])}
    if _status.get("error"):
        result["error"] = _status["error"]
    return result


def attach(workspace: dict, now: float | None = None) -> None:
    """Add workspace["proactive"] to the Today workspace. Never fatal."""
    try:
        workspace["proactive"] = snapshot(now=now)
    except Exception:  # noqa: BLE001
        traceback.print_exc()

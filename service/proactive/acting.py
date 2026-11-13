"""Let Wisp act for me: the one gate for everything Wisp does without being asked.

Auto-add (autoadd.py), holding time for a deadline, follow-up reminders and
ready reply drafts all stand behind this one yes. This module is the gate, the
shared record of what Wisp did, and the one "Wisp did this" card with Undo.

IMPORTANT rules:
- Off until the person says yes (first-run setup, the one-time card, or the
  switch in Settings). The yes is stored with its time, `{enabled: true,
  enabled_at: <unix time>, asked: true}`, in its own private file,
  ~/.moe/act_for_me.json, read from disk on every check. A file that is
  missing, damaged or of the wrong shape means off. It is not in config.yaml
  on purpose: see `_path`.
- Only what ARRIVES after the yes. `allows(received_ts)` is False for any
  message received before `enabled_at`, so turning this on never reaches back
  into old mail or texts. Turning it off and on again moves the line forward.
- Off stops everything at once: every writer calls `still_ok()` again right
  before it dispatches.
- Wisp never sends, replies, deletes or pays by itself. Nothing behind this
  gate is outbound. What an action may be is decided by its own module's code;
  message text never instructs, and recipients, times and methods never come
  from model text.
- Every action is recorded BEFORE its card exists, with what Undo needs, by
  `record()`. A card is built only from the record (`did_card`), never from a
  request. Undo runs the kind's registered undoer on the stored record.
- One shared daily cap (`MAX_PER_DAY`, a rolling day) across every kind, on top
  of each kind's own limits. `room()` says how many more may happen now.
- Every action and every undo writes an audit line (`audit`).

FOR A NEW KIND OF ACTION (the interface; see tests/test_acting.py):
    from service.proactive import acting
    if not acting.allows(item["received_ts"]):       # the gate, per message
        ...leave it as a tap card...
    if acting.room(store, now) <= 0 or acting.still_ok() is not None:
        ...stop...                                    # right before the write
    acting.record(store, fingerprint, kind="follow_up", status="added", now=now,
                  title="Reply to Maya", quote="...", who="Maya Chen", source="messages",
                  item_id=item["id"], received_ts=item["received_ts"], timezone=tz,
                  card={"meta": "REMINDER", "title": "Set a reminder to answer Maya",
                        "did": "I'll remind you tomorrow at 9 AM."},
                  show_until=due_ts, undo={...whatever your undoer needs...})
    acting.register_undoer("follow_up", my_async_undo)   # async (record) -> {"ok": bool, "status": str}
`autoadd.done_cards` then shows the card, and the card's Undo reaches your undoer.
Also name your module in `_HOME` below (it must expose `async def undo(record)`), so Undo
still works after a restart, before your step has run.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Awaitable, Callable

from service.proactive import guard

FILE = "act_for_me.json"               # in Wisp's home; see _path()
MAX_PER_DAY = 10                       # every kind together, a rolling day
DAY_S = 86400
SHOWN_S = 3 * DAY_S                    # how long a "Wisp did this" card stays
KINDS = ("calendar", "reminder", "hold", "follow_up", "draft")
# Statuses that may have changed something, so they count toward the cap and block a repeat.
LANDED = ("added", "undone", "unknown", "undo_unknown")

# What Wisp does on its own once the person says yes, in the words every surface shows
# (setup, Settings, the one-time card, About). ONE list, so no surface can claim more than
# is built: a module adds its line here in the same change that makes it act.
DOES: list[str] = [
    "Adds an event or deadline you missed in a text or email to Calendar or Reminders.",
    "Holds a free hour on your Day plan for work due today or in the next three days (in Wisp only, never on your calendar).",
]
NEVER = "Wisp never sends, replies, deletes or pays for anything by itself."
# What the yes reaches, said the same way everywhere. True for the list above: adds read
# messages (acting.allows on the line's own arrival); a hold reads the person's own calendar
# and reminders, where "arrived" has no meaning, so it covers deadlines already there.
SCOPE = ("Adding from texts and emails covers only messages that arrive after you say yes, never older ones. "
         "Holding time is for deadlines on your calendar or in Reminders, including ones already there.")

_undoers: dict[str, Callable[[dict], Awaitable[dict]]] = {}
_lock = threading.Lock()


# --- the gate ------------------------------------------------------------------------------

def _path():
    """The gate's own file. Not config.yaml: under Full access the assistant's file tools may
    write there without a card, and a line written by a model must never be able to give or
    backdate this consent. This file is a protected root in service/safety/policy.py (a write
    asks in every mode, a grant cannot lift it), and /resetwisp deletes it."""
    from service import paths
    return paths.moe_dir() / FILE


def _setting() -> dict[str, Any]:
    """Read from disk every time: no cache, so off, a reset or a damaged file counts at once.
    Anything but a well-formed file means off."""
    import json
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(setting: dict[str, Any]) -> None:
    import json
    from service import paths
    paths.private_write_bytes(_path(), json.dumps(setting).encode("utf-8"))


def enabled() -> bool:
    """The person said yes and has not taken it back. Read every time, never cached."""
    setting = _setting()
    return setting.get("enabled") is True and type(setting.get("enabled_at")) in (int, float)


def _born() -> float:
    """When the gate's file itself was created. Every save replaces the file with a fresh one
    (paths.private_write_bytes), so this is the time of the last write, kept by the filesystem."""
    info = _path().stat()
    return float(getattr(info, "st_birthtime", info.st_mtime))


def enabled_at() -> float | None:
    """The time the yes counts from: the recorded time, but never earlier than the file itself
    was written. So a file forged or edited by anything other than the switch cannot backdate
    the yes to reach mail that arrived before it existed; it can only ever move it later."""
    if not enabled():
        return None
    try:
        return max(float(_setting()["enabled_at"]), _born())
    except OSError:
        return None


def asked() -> bool:
    """The question has been put to the person once (setup or the card), whatever they answered."""
    return _setting().get("asked") is True or enabled()


def allows(received_ts: Any) -> bool:
    """Wisp may act on something that arrived at `received_ts`: on, and it arrived after the yes.
    Pass the arrival time of the very line or message being acted on, never a conversation's
    latest. A time that is not known is never allowed."""
    since = enabled_at()
    return since is not None and type(received_ts) in (int, float) and float(received_ts) >= since


def still_ok() -> str | None:
    """Checked again right before every write: still on, and no turn of the person's is running."""
    if not enabled():
        return "Acting for you was turned off"
    try:
        from service import idle
        if idle.foreground_busy():
            return "A turn started; Wisp waits for the background"
    except Exception:  # noqa: BLE001 — unknown counts as busy
        return "A turn may be running"
    return None


def set_enabled(on: bool, *, now: float | None = None) -> dict[str, Any]:
    """The person's answer. Yes records the time it was given; no clears it."""
    if type(on) is not bool:
        raise ValueError("enabled must be a boolean")
    stamp = (time.time() if now is None else float(now)) if on else None
    with _lock:
        _write({"enabled": on, "enabled_at": stamp, "asked": True})
    audit("act_for_me.setting", enabled=on)
    return state()


def mark_asked() -> None:
    """The question was shown and put aside without a yes; it is not asked again. Only `asked`
    changes: a yes given a moment earlier (another window, a race) is never overwritten."""
    with _lock:
        setting = _setting()
        if setting.get("asked") is True:
            return
        _write({"enabled": setting.get("enabled") is True and type(setting.get("enabled_at")) in (int, float),
                "enabled_at": setting.get("enabled_at") if type(setting.get("enabled_at")) in (int, float) else None,
                "asked": True})


def state() -> dict[str, Any]:
    return {"enabled": enabled(), "enabled_at": enabled_at(), "asked": asked(), "per_day": MAX_PER_DAY,
            "does": list(DOES), "never": NEVER, "scope": SCOPE}


def audit(event: str, **fields) -> None:
    try:
        from service.safety.audit import audit as write
        write(event, **fields)
    except Exception:  # noqa: BLE001 — auditing never blocks a result
        pass


# --- the record of what Wisp did --------------------------------------------------------------

def records(store, *, kind: str | None = None, statuses: tuple[str, ...] | None = None,
            since: float | None = None) -> list[dict]:
    """The stored actions, newest first, optionally of one kind, in given statuses, since a time.
    Read-only copies. Each has "fingerprint", "kind", "status", "at", "item_id", "who" and
    whatever its module stored (a follow-up's person key, say, passed to record() as **more)."""
    rows = [dict(v) for v in store.auto_added().values() if isinstance(v, dict)
            and (kind is None or v.get("kind") == kind)
            and (statuses is None or v.get("status") in statuses)
            and (since is None or float(v.get("at") or 0) >= since)]
    return sorted(rows, key=lambda r: -float(r.get("at") or 0))


def done_since(store, since: float, kind: str | None = None) -> int:
    """Actions since `since` that may have changed something (LANDED), of every kind or one."""
    return len(records(store, kind=kind, statuses=LANDED, since=since))


def room(store, now: float) -> int:
    """How many more actions of any kind may happen now under the shared daily cap."""
    return max(0, MAX_PER_DAY - done_since(store, now - DAY_S))


def record(store, fingerprint: str, *, kind: str, status: str, now: float, title: str, quote: str = "",
           who: str = "", source: str = "", item_id: str = "", received_ts: float | None = None,
           timezone: str = "UTC", card: dict | None = None, show_until: float | None = None,
           undo: dict | None = None, **more) -> dict:
    """Write one action to the store and its audit line. Returns the stored record.

    `card` = {"meta", "title", "did"}: the words of its "Wisp did this" card. `undo` is
    whatever the kind's undoer needs; it stays in the store and never goes on a card."""
    if kind not in KINDS:
        raise ValueError("unknown kind of action")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("an action needs a fingerprint")
    entry = {**more, "kind": kind, "status": status, "at": float(now), "title": str(title), "quote": str(quote),
             "who": str(who), "source": str(source), "item_id": str(item_id), "received_ts": received_ts,
             "timezone": timezone, "fingerprint": fingerprint}
    if card is not None:
        entry["card"] = {k: str(card.get(k) or "") for k in ("meta", "title", "did")}
    if show_until is not None:
        entry["show_until"] = float(show_until)
    if undo is not None:
        entry["undo"] = dict(undo)
    store.auto_add_put(fingerprint, entry)
    audit("proactive_act", kind=kind, status=status, ok=status == "added", fingerprint=fingerprint)
    return entry


def register_undoer(kind: str, undoer: Callable[[dict], Awaitable[dict]]) -> None:
    if kind not in KINDS:
        raise ValueError("unknown kind of action")
    _undoers[kind] = undoer


# The module that registers each kind's undoer, so Undo works in a process that has not
# run that kind's step yet (after a restart, the card is still there).
_HOME = {"hold": "service.proactive.autohold"}


def undoer(kind: str):
    if kind not in _undoers and kind in _HOME:
        try:
            import importlib
            found = getattr(importlib.import_module(_HOME[kind]), "undo", None)
            if callable(found):
                _undoers[kind] = found
        except Exception:  # noqa: BLE001 — no undoer: Undo says it can't, and nothing changes
            pass
    return _undoers.get(kind)


# --- the one card ------------------------------------------------------------------------------

def _digest(*parts) -> str:
    import hashlib
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def shown(record: dict, now: float) -> bool:
    """A record of a kind other than calendar/reminder still earns its card."""
    if not isinstance(record, dict) or record.get("status") != "added" or not isinstance(record.get("card"), dict):
        return False
    until = record.get("show_until")
    if type(until) in (int, float) and until <= now:
        return False
    return now - float(record.get("at") or 0) <= SHOWN_S


def did_card(record: dict, now: float) -> dict:
    """The "Wisp did this" card for one record: what was done, the source's own words, and Undo.
    Every word comes from the stored record; the button carries only the record's fingerprint."""
    words = record.get("card") or {}
    quote, who = record.get("quote") or "", record.get("who") or ""
    did = words.get("did") or ""
    body = f"“{quote}” {did}".strip() if quote else did
    return {
        "id": "card:" + _digest(record["fingerprint"], "undo_add"),
        "meta": words.get("meta") or "DONE",
        "title": guard.clean_title(words.get("title") or record.get("title") or ""),
        "body": body,
        "why": "You let Wisp act for you, so it did this. Undo takes back exactly what Wisp did.",
        "account": record.get("account", ""),
        "sources": [{"kind": record.get("source", ""), "ref": "acted:" + str(record.get("item_id") or ""),
                     "who": who, "quote": quote}],
        "action": {"method": "undo_add", "button": "Undo?", "draft": "", "recipient": "", "event": None,
                   "envelope": {"kind": "undo_add"}, "to": [], "cc": [], "subject": "", "reply_all": False,
                   "reply_ref": None, "open_url": "", "open_label": "", "record": record["fingerprint"],
                   "added": record["kind"]},
        "todo_id": None,
        "fingerprints": [],
        "due_ts": record.get("show_until"),
        "auto_added": True,
    }


def recent(store, now: float, limit: int = 20) -> list[dict]:
    """What Wisp did lately, newest first, for the list in Settings. Words only: no identifiers."""
    rows = [r for r in store.auto_added().values()
            if isinstance(r, dict) and r.get("status") in ("added", "undone") and r.get("title")]
    rows.sort(key=lambda r: -float(r.get("at") or 0))
    return [{"kind": r.get("kind", ""), "title": guard.clean_title(str((r.get("card") or {}).get("title") or r["title"])),
             "who": str(r.get("who") or ""), "at": float(r.get("at") or 0), "undone": r.get("status") == "undone"}
            for r in rows[:limit]]

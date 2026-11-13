"""Let Wisp act for me: the one gate for everything Wisp does without being asked.

Auto-add (autoadd.py), holding time for a deadline, follow-up reminders and
ready reply drafts all stand behind this one yes. This module is the gate, the
shared record of what Wisp did, and the one "Wisp did this" card with Undo.

IMPORTANT rules:
- Off until the person says yes (first-run setup, the one-time card, or the
  switch in Settings). The yes is stored with its time: `act_for_me: {enabled:
  true, enabled_at: <unix time>}` in the person's settings file. A settings
  file that cannot be read means off.
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

import time
from typing import Any, Awaitable, Callable

from service.proactive import guard

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
    "Holds a free hour on your Day plan for work due in the next three days (in Wisp only, never on your calendar).",
]
NEVER = "Wisp never sends, replies, deletes or pays for anything by itself."

_undoers: dict[str, Callable[[dict], Awaitable[dict]]] = {}


# --- the gate ------------------------------------------------------------------------------

def _setting() -> dict[str, Any]:
    try:
        from service import config
        if not config.user_overlay_valid():
            return {}
        value = config.models_config().get("act_for_me")
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001 — a setting that can't be read means off
        return {}


def enabled() -> bool:
    """The person said yes and has not taken it back. Read every time, never cached."""
    setting = _setting()
    return setting.get("enabled") is True and type(setting.get("enabled_at")) in (int, float)


def enabled_at() -> float | None:
    return float(_setting()["enabled_at"]) if enabled() else None


def asked() -> bool:
    """The question has been put to the person once (setup or the card), whatever they answered."""
    return _setting().get("asked") is True or enabled()


def allows(received_ts: Any) -> bool:
    """Wisp may act on a message that arrived at `received_ts`: on, and it arrived after the yes."""
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
    from service import config
    stamp = (time.time() if now is None else float(now)) if on else None
    config._save_overlay({"act_for_me": {"enabled": on, "enabled_at": stamp, "asked": True}})
    audit("act_for_me.setting", enabled=on)
    return state()


def mark_asked() -> None:
    """The question was shown and put aside without a yes; it is not asked again."""
    from service import config
    if not asked():
        config._save_overlay({"act_for_me": {"enabled": False, "enabled_at": None, "asked": True}})


def state() -> dict[str, Any]:
    return {"enabled": enabled(), "enabled_at": enabled_at(), "asked": asked(), "per_day": MAX_PER_DAY,
            "does": list(DOES), "never": NEVER}


def audit(event: str, **fields) -> None:
    try:
        from service.safety.audit import audit as write
        write(event, **fields)
    except Exception:  # noqa: BLE001 — auditing never blocks a result
        pass


# --- the record of what Wisp did --------------------------------------------------------------

def done_since(store, since: float) -> int:
    return sum(1 for v in store.auto_added().values()
               if isinstance(v, dict) and v.get("status") in LANDED and float(v.get("at") or 0) >= since)


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

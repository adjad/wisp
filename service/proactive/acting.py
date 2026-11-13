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
#
# Each line is ONE ACTION with a key. A yes covers exactly the actions that were named when it
# was given (their keys are stored with it, each with its own time). An action added later, or
# an action whose meaning grows (its key must then change, e.g. "add" -> "add+mail"), is not
# covered by an older yes: it does nothing until the person is asked once more and says yes to
# it. Never widen what a key means without changing the key.
ACTIONS: dict[str, str] = {
    "add": ("Adds an event or deadline you missed in a one-to-one iMessage from someone saved in your Contacts to "
            "Calendar or Reminders. A green-bubble SMS or an email with a date shows a card and waits for your tap."),
    "hold": ("Holds a free hour on your Day plan for work due today or in the next three days "
             "(in Wisp only, never on your calendar)."),
    "follow_up": ("Reminds you to reply when someone you email asked you something by email over a day ago and "
                  "hasn't heard back (for now, only in Google accounts you linked in Wisp)."),
}
DOES: list[str] = list(ACTIONS.values())
# Which action each recorded kind of thing (KINDS) belongs to.
ACTION_OF = {"calendar": "add", "reminder": "add", "hold": "hold", "follow_up": "follow_up", "draft": "draft"}
NEVER = "Wisp never sends, replies, deletes or pays for anything by itself."
# The first line says exactly what autoadd does: a text must be an iMessage (Apple verifies
# an iMessage's sender; an SMS sender number can be forged) from a saved contact's own
# handle (autoadd._saved_contact, strict_contact_name). An email never adds an event or a
# deadline: autoadd.MAIL_ACTS is False. If that is ever set True, this line and SCOPE must
# say so in the same commit.
# What the yes reaches, said the same way everywhere. True for the list above: adds read
# messages (acting.allows on the line's own arrival); a hold reads the person's own calendar
# and reminders, where "arrived" has no meaning, so it covers deadlines already there.
SCOPE = ("Adding from iMessages, and reply reminders from emails, cover only messages that arrive after "
         "you say yes, never older ones. "
         "Holding time is for deadlines on your calendar or in Reminders, including ones already there.")

_undoers: dict[str, Callable[[dict], Awaitable[dict]]] = {}
_lock = threading.Lock()
# Set when an off (or a failed yes) could neither be written nor have its file removed: the
# gate is then off for the rest of this process whatever the file says. Only a yes that is
# actually saved clears it (set_enabled). See set_enabled for why it exists.
_revoked = False


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


def _covers(setting: dict[str, Any]) -> dict[str, float]:
    """The actions the stored yes named, each with the time it was said yes to. A yes saved
    before actions were recorded names none: it covers nothing until the person is asked again."""
    covers = setting.get("covers")
    if not isinstance(covers, dict):
        return {}
    return {k: float(v) for k, v in covers.items() if isinstance(k, str) and type(v) in (int, float)}


def enabled(action: str | None = None) -> bool:
    """The person said yes and has not taken it back; with `action`, also that their yes named
    that action. Read every time, never cached. An off this process could not save (`_revoked`)
    counts, so enabled_at, allows and still_ok are off too."""
    if _revoked:
        return False
    setting = _setting()
    on = setting.get("enabled") is True and type(setting.get("enabled_at")) in (int, float)
    if not on or action is None:
        return on
    return action in ACTIONS and action in _covers(setting)


def pending() -> list[str]:
    """The actions Wisp has now that the person's yes did not name (keys, in order). Empty while
    the gate is off: then the whole question is still to be asked."""
    if not enabled():
        return []
    covers = _covers(_setting())
    return [key for key in ACTIONS if key not in covers]


def _born() -> float:
    """The earliest time the gate's file can honestly have been written. Every save replaces the
    file with a fresh one (paths.private_write_bytes), so its creation time is the last write.
    Creation time alone can be moved back from user code (touch -t, os.utime with an earlier
    time on APFS, cp -p, a restore), so this is the later of it and st_ctime, the inode's change
    time, which the kernel sets to now on every change and nothing in user space can set back.
    mtime is never used: it is whatever the writer says."""
    info = _path().stat()
    return max(float(getattr(info, "st_birthtime", 0.0)), float(info.st_ctime))


def enabled_at(action: str | None = None) -> float | None:
    """The time the yes counts from; with `action`, the time the person said yes to THAT action
    (later than the first yes when it was agreed to afterwards). Never earlier than the file
    itself was written: max(recorded time, creation time, inode change time) (see `_born`). So
    a file forged or edited by anything other than the switch cannot backdate the yes to reach
    mail that arrived before it existed; it can only ever move it later."""
    if not enabled(action):
        return None
    try:
        setting = _setting()
        said = float(setting["enabled_at"]) if action is None else _covers(setting)[action]
        return max(said, float(setting["enabled_at"]), _born())
    except (OSError, KeyError):
        return None


def asked() -> bool:
    """Nothing is waiting to be asked: the question was put to the person (setup or the card),
    whatever they answered, and every action Wisp has now was named then or put aside since."""
    setting = _setting()
    if not (setting.get("asked") is True or enabled()):
        return False
    aside = setting.get("aside") if isinstance(setting.get("aside"), list) else []
    return all(key in aside for key in pending())


def allows(received_ts: Any, action: str | None = None) -> bool:
    """Wisp may act on something that arrived at `received_ts`: on, and it arrived after the yes
    (with `action`: after the yes that named that action). Pass the arrival time of the very
    line or message being acted on, never a conversation's latest. A time that is not known is
    never allowed. Every writer passes its action; without one this only says the gate is on."""
    since = enabled_at(action)
    return since is not None and type(received_ts) in (int, float) and float(received_ts) >= since


def still_ok(action: str | None = None) -> str | None:
    """Checked again right before every write: still on (and, with `action`, still agreed to),
    and no turn of the person's is running."""
    if not enabled():
        return "Acting for you was turned off"
    if action is not None and not enabled(action):
        return "You haven't said yes to this yet"
    try:
        from service import idle
        if idle.foreground_busy():
            return "A turn started; Wisp waits for the background"
    except Exception:  # noqa: BLE001 — unknown counts as busy
        return "A turn may be running"
    return None


def _unlink() -> bool:
    """Remove the gate's file (a missing file is off). True when it is gone afterwards."""
    try:
        _path().unlink(missing_ok=True)
        return True
    except OSError:
        return False


def set_enabled(on: bool, *, now: float | None = None) -> dict[str, Any]:
    """The person's answer. Yes records the time it was given; no clears it.

    Off ALWAYS takes effect in this process and never raises for a failed save. A yes that can't
    be saved raises, and leaves the gate off.

    What is on disk in each failure order. `_write` is paths.private_write_bytes: a temp file,
    fsync, then os.replace, so a raise means the old file is untouched (or, at worst, the new
    one is fully in place; os.replace is atomic). So after a failed write the old file may still
    say `enabled: true` (an earlier yes), and nothing may be left to chance:
    - off, write raises: the file is removed (missing = off). If that fails too, `_revoked` is set
      and enabled() is False for the rest of this process whatever the file says. Off is never
      reported as failed, because it is not: the gate is off.
    - yes, write raises: the older file may be an earlier yes with an older enabled_at, or an off.
      Keeping an earlier yes would mean a yes the person was just told failed is still in force,
      reaching from an older time; so the file is removed, and if that fails `_revoked` is set.
      Then the error is raised. The person sees "couldn't save your yes", and that is true:
      nothing acts until a yes is saved. (Removing the file also forgets `asked`; the question
      may be shown once more, which is the safe side.)
    - yes, write succeeds: only then is `_revoked` cleared, so a yes saved after an unsaved off
      works again. A crash after the write keeps the saved yes, which is what the person said.
    `_revoked` lives only in this process: after a restart the file decides, and in the one
    case it could still say yes (write AND unlink both failed) the disk refused every change.
    """
    global _revoked
    if type(on) is not bool:
        raise ValueError("enabled must be a boolean")
    stamp = (time.time() if now is None else float(now)) if on else None
    saved = True
    with _lock:
        try:
            if on:
                # A yes names every action there is NOW. One already agreed to under a yes that is
                # still in force keeps its own time; anything else counts from this yes. The first
                # yes's time is kept while the gate stays on, so agreeing to a new action never
                # moves what was already agreed.
                before = _setting() if enabled() else {}
                had = _covers(before)
                first = float(before["enabled_at"]) if before else stamp
                _write({"enabled": True, "enabled_at": first, "asked": True,
                        "covers": {key: had.get(key, stamp) for key in ACTIONS}})
            else:
                _write({"enabled": False, "enabled_at": None, "asked": True})
        except Exception:
            saved = False
            if not _unlink():
                _revoked = True
            if on:
                audit("act_for_me.setting", enabled=False, saved=False)
                raise
        else:
            if on:
                _revoked = False
    audit("act_for_me.setting", enabled=on, saved=saved)
    return state()


def mark_asked() -> None:
    """The question was shown and put aside without a yes; it is not asked again. Only `asked`
    changes: a yes given a moment earlier (another window, a race) is never overwritten."""
    with _lock:
        setting = _setting()
        on = enabled()                       # honours an unsaved off (_revoked): never writes it back on
        waiting = pending()
        aside = [k for k in (setting.get("aside") if isinstance(setting.get("aside"), list) else []) if k in ACTIONS]
        if setting.get("asked") is True and all(key in aside for key in waiting):
            return
        fresh = {"enabled": on, "enabled_at": setting.get("enabled_at") if on else None, "asked": True}
        if on:
            # New actions put aside stay off (they are not in `covers`) and are not asked about again.
            fresh["covers"] = _covers(setting)
            fresh["aside"] = sorted(set(aside) | set(waiting))
        _write(fresh)


def state() -> dict[str, Any]:
    waiting = pending()
    on = enabled()
    return {"enabled": on, "enabled_at": enabled_at(), "asked": asked(), "per_day": MAX_PER_DAY,
            # What a yes would cover (everything Wisp does on its own now), and, while the gate is
            # on, what the person HAS agreed to and what is new since: Wisp does only the former.
            "does": list(DOES), "never": NEVER, "scope": SCOPE,
            "agreed": [ACTIONS[k] for k in ACTIONS if on and k not in waiting],
            "new": [ACTIONS[k] for k in waiting]}


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
_HOME = {"hold": "service.proactive.autohold", "follow_up": "service.proactive.followups"}


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

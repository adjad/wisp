"""Let Wisp act for me: the one gate for everything Wisp does without being asked.

Auto-add (autoadd.py), holding time for a deadline, follow-up reminders and
ready reply drafts all stand behind this one yes. This module is the gate, the
shared record of what Wisp did, and the one "Wisp did this" card with Undo.

IMPORTANT rules:
- Off until the person says yes (first-run setup, the one-time card, or the
  switch in Settings). The yes is stored with its time, `{enabled: true,
  enabled_at: <unix time>, asked: true, covers: {action: <unix time>}}`, in
  its own private file, ~/.moe/act_for_me.json, read from disk on every check.
  A file that is missing, damaged or of the wrong shape means off. It is not
  in config.yaml on purpose: see `_path`. That file is written ONLY by a yes
  or a no; "Not now" is kept beside it (`_notes_path`), because every write of
  the gate's file moves every action's time forward (see `_born`).
- Only what ARRIVES after the yes. `allows(received_ts, action)` is False for
  any message received before the yes that named that action, so turning this
  on never reaches back into old mail or texts. Turning it off and on again
  moves the line forward.
- Off stops everything at once: every writer calls `still_ok(action)` again
  right before it dispatches. Every check names its action: there is no
  "the gate is on" answer for a writer to lean on.
- Wisp never sends, replies, deletes or pays by itself. Nothing behind this
  gate is outbound. What an action may be is decided by its own module's code;
  message text never instructs, and recipients, times and methods never come
  from model text.
- Every action is recorded BEFORE its card exists, with what Undo needs, by
  `record()`. A card is built only from the record (`did_card`), never from a
  request. Undo runs the kind's registered undoer on the stored record.
- No limit on how many a day (the owner's decision, 2026-11-13). Each kind keeps its
  own limits (autoadd.MAX_PER_CYCLE a pass, one follow-up a pass).
- Every action and every undo writes an audit line (`audit`).

FOR A NEW KIND OF ACTION (the interface; see tests/test_acting.py):
    from service.proactive import acting
    if not acting.allows(item["received_ts"], "follow_up"):   # the gate, per message, per action
        ...leave it as a tap card...
    if acting.still_ok("follow_up") is not None:
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
still works after a restart, before your step has run. Your action's key and its line go in
`ACTIONS`, and your kind in `ACTION_OF`.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Awaitable, Callable

from service.proactive import guard

FILE = "act_for_me.json"               # in Wisp's home; see _path()
ASKED_FILE = "act_for_me_asked.json"   # "Not now", beside it; see _notes_path()
DAY_S = 86400
SHOWN_S = 3 * DAY_S                    # how long a "Wisp did this" card stays
KINDS = ("calendar", "reminder", "hold", "follow_up", "draft")
# Statuses that may have changed something, so they count as done today and block a repeat.
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
            "Calendar or Reminders."),
    "add_more": ("Does the same for a green-bubble text (SMS or RCS) or a group-chat message from someone saved in "
                 "your Contacts, and for an email from someone you know that your mail provider verified (Google "
                 "accounts you linked in Wisp). Never from an unknown number."),
    "hold": ("Holds a free hour on your Day plan for work due today or in the next three days "
             "(in Wisp only, never on your calendar)."),
    "follow_up": ("Adds a “Reply to <name>” reminder to Reminders when someone you email asked you something by "
                  "email over a day ago and hasn't heard back (for now, only in Google accounts you linked in Wisp)."),
}
DOES: list[str] = list(ACTIONS.values())
# Which action each recorded kind of thing (KINDS) belongs to.
# (An event or reminder from "add_more" is recorded as the same kinds: undoing one needs only "add".)
ACTION_OF = {"calendar": "add", "reminder": "add", "hold": "hold", "follow_up": "follow_up", "draft": "draft"}
NEVER = "Wisp never sends, replies, deletes or pays for anything by itself."
# The first two lines say exactly what autoadd does. "add" is what the first yes named: a
# one-to-one iMessage from a saved contact's own handle (autoadd._saved_contact,
# strict_contact_name). "add_more" is what was added on 2026-11-13 (autoadd._action_of): a
# green-bubble text (an SMS sender number can be forged; the owner chose to act on a saved
# contact's all the same), a group-chat line whose own sender is saved (autoadd._group_sender),
# and an email that passes autoadd.default_mail_proof (autoadd.MAIL_ACTS). It has its own key
# so that a yes given before it existed does not cover it.
# What the yes reaches, said the same way everywhere. True for the list above: adds read
# messages (acting.allows on the line's own arrival); a hold reads the person's own calendar
# and reminders, where "arrived" has no meaning, so it covers deadlines already there.
SCOPE = ("Adding from texts and emails, and reply reminders from emails, cover only messages that arrive after "
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


def _num(value: Any) -> float | None:
    """A stored time as a float, or None for anything that is not an ordinary number: a bool,
    text, NaN, an infinity, or an integer too large to be a time at all (float() would raise)."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _covers(setting: dict[str, Any]) -> dict[str, float]:
    """The actions the stored yes named, each with the time it was said yes to. A yes saved
    before actions were recorded names none: it covers nothing until the person is asked again.
    A time that is not an ordinary number covers nothing."""
    covers = setting.get("covers")
    if not isinstance(covers, dict):
        return {}
    return {k: n for k, v in covers.items() if isinstance(k, str) and (n := _num(v)) is not None}


def _notes_path():
    """Where "Not now" is kept: that the question was shown, and which new actions were put
    aside. NOT in the gate's own file, because writing that file moves every agreed action's
    time forward (`_born`), and putting a question aside must change nothing about what was
    agreed. Nothing here can turn anything on: at most it stops a question being shown again."""
    return _path().with_name(ASKED_FILE)


def _notes() -> dict[str, Any]:
    import json
    try:
        value = json.loads(_notes_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _aside(setting: dict[str, Any], notes: dict[str, Any]) -> list[str]:
    """The new actions the person put aside (also read from the gate's file, where an earlier
    build kept them)."""
    found: list[str] = []
    for source in (notes.get("aside"), setting.get("aside")):
        if isinstance(source, list):
            found += [key for key in source if isinstance(key, str) and key in ACTIONS and key not in found]
    return found


def enabled(action: str | None = None) -> bool:
    """The person said yes and has not taken it back; with `action`, also that their yes named
    that action. Read every time, never cached. An off this process could not save (`_revoked`)
    counts, so enabled_at, allows and still_ok are off too."""
    if _revoked:
        return False
    setting = _setting()
    on = setting.get("enabled") is True and _num(setting.get("enabled_at")) is not None
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
    This is one time for the WHOLE file, so every write moves every action's time forward to
    that write: a file cannot prove that part of it is older than the rest. That is why the
    file is written only by a yes or a no, never by "Not now" (`_notes_path`), and why a yes
    that changes nothing writes nothing (`set_enabled`).
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
        first = _num(setting.get("enabled_at"))
        said = first if action is None else _covers(setting)[action]
        return max(said, first, _born())
    except (OSError, KeyError, TypeError):
        return None


def asked() -> bool:
    """Nothing is waiting to be asked: the question was put to the person (setup or the card),
    whatever they answered, and every action Wisp has now was named then or put aside since."""
    setting, notes = _setting(), _notes()
    if not (notes.get("asked") is True or setting.get("asked") is True or enabled()):
        return False
    aside = _aside(setting, notes)
    return all(key in aside for key in pending())


def allows(received_ts: Any, action: str) -> bool:
    """Wisp may do `action` on something that arrived at `received_ts`: on, the yes named that
    action, and it arrived after that yes. Pass the arrival time of the very line or message
    being acted on, never a conversation's latest. A time that is not known is never allowed.
    The action is required: a writer can never get by on "the gate is on"."""
    since = enabled_at(action)
    arrived = _num(received_ts)
    return since is not None and arrived is not None and arrived >= since


def still_ok(action: str) -> str | None:
    """Checked again right before every write: still on, `action` still agreed to, and no turn
    of the person's is running."""
    if not enabled():
        return "Acting for you was turned off"
    if not enabled(action):
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


class NotOn(RuntimeError):
    """A yes to the new actions arrived after the gate went off; nothing was turned on."""


def set_enabled(on: bool, *, now: float | None = None, only_new: bool = False) -> dict[str, Any]:
    """The person's answer. Yes records the time it was given; no clears it.

    `only_new` is the yes on the card that asks about NEW actions ("Yes, these too"): it adds to
    a yes that is in force and raises NotOn when there is none, so a tap that lands after the
    person turned everything off (another window, a race) never turns it all back on.

    What a yes does to times already agreed. Each action keeps the time it was said yes to
    (`covers`), but the file is replaced by every write, and nothing acts on a message that
    arrived before the file was written (`_born`, which user code cannot move back). So a yes
    to a NEW action also moves the actions agreed earlier forward to this moment: messages
    that arrived between the old yes and this one, and were not acted on yet, no longer will
    be. That is the safe direction and cannot be avoided with one file. A yes that names
    nothing new writes nothing, so it moves nothing.

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
      nothing acts until a yes is saved. (Removing the file also forgets its `asked`; unless
      "Not now" was recorded beside it, the question may be shown once more, the safe side.)
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
                # still in force keeps its recorded time; anything else counts from this yes. The
                # recorded times are the earliest each could count from: the file's own write time
                # is a later floor for all of them (see the docstring), so nothing is written
                # when the yes names nothing new.
                before = _setting() if enabled() else {}
                if only_new and not before:
                    raise NotOn("Letting Wisp act for you is off")
                had = _covers(before)
                if before and all(key in had for key in ACTIONS):
                    return state()
                first = _num(before.get("enabled_at")) if before else stamp
                _write({"enabled": True, "enabled_at": first, "asked": True,
                        "covers": {key: had.get(key, stamp) for key in ACTIONS}})
            else:
                _write({"enabled": False, "enabled_at": None, "asked": True})
        except NotOn:
            raise
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
    """The question was shown and put aside without a yes; it is not asked again. The gate's own
    file is never touched: a yes given a moment earlier (another window, a race) stays exactly
    as it was, times included, and an off stays off. New actions put aside stay off (they are
    not in `covers`) and are not asked about again."""
    import json
    from service import paths
    with _lock:
        setting, notes = _setting(), _notes()
        waiting = pending()
        aside = _aside(setting, notes)
        if (notes.get("asked") is True or setting.get("asked") is True) and all(key in aside for key in waiting):
            return
        paths.private_write_bytes(_notes_path(), json.dumps(
            {"asked": True, "aside": sorted(set(aside) | set(waiting))}).encode("utf-8"))


def state() -> dict[str, Any]:
    waiting = pending()
    on = enabled()
    return {"enabled": on, "enabled_at": enabled_at(), "asked": asked(),
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
    try:
        before = (store.auto_added().get(fingerprint) or {}).get("status")
    except Exception:  # noqa: BLE001 — unknown: say nothing twice rather than risk it
        before = "added"
    store.auto_add_put(fingerprint, entry)
    audit("proactive_act", kind=kind, status=status, ok=status == "added", fingerprint=fingerprint)
    if status == "added" and before != "added" and card is not None:
        announce(fingerprint, entry["card"]["title"])
    return entry


ANNOUNCED = "wisp_did"                 # the event the app turns into a notification (OverlayModel.swift)


def announce(fingerprint: str, title: str, detail: str = "") -> None:
    """Tell the app the moment Wisp did something, so the person sees it happen: one
    notification saying what ("Added Dinner at Luigi's to your calendar"). The card with Undo
    is in the Brief. Called once, right after an action is recorded as done.

    IMPORTANT rules:
    - It only says what was already done and recorded. It does nothing and decides nothing.
    - Transient: nothing is stored, so its words are kept nowhere but the action's own record.
    - Nothing is sent in the person's quiet hours; the card waits in the Brief.
    - Only from the service's own loop, and it never raises: a notice is a courtesy."""
    try:
        import asyncio
        asyncio.get_running_loop()
        from service import quiet_hold
        from service.assistant.hub import hub
        words = guard.clean_title(str(title or ""))
        if not words or not fingerprint or quiet_hold._quiet_now():
            return
        hub.publish_sync({"type": ANNOUNCED, "record": str(fingerprint), "title": words,
                          "detail": guard.clean(str(detail or ""), 80)}, durable=False)
    except Exception:  # noqa: BLE001
        pass


def settle(store, now: float) -> int:
    """Every record still "dispatching" was cut off between its write and its outcome (a cycle
    that timed out, was cancelled or crashed): the write may have landed. Each becomes
    "unknown": counted toward the caps, never written again, no Undo (nothing confirms what it
    would remove). Run at the start of a cycle, before any kind acts. Never raises; returns
    how many it settled."""
    settled = 0
    try:
        stale = records(store, statuses=("dispatching",))
    except Exception:  # noqa: BLE001 — unreadable: each kind still treats "dispatching" as landed
        return 0
    for row in stale:
        fp = row.get("fingerprint")
        if not isinstance(fp, str) or not fp:
            continue
        try:
            store.auto_add_put(fp, {**row, "status": "unknown"})
            audit("proactive_act", kind=row.get("kind"), status="unknown", ok=False, fingerprint=fp,
                  reason="a write was begun and never recorded")
            settled += 1
        except Exception:  # noqa: BLE001 — it still blocks as "dispatching"
            pass
    return settled


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
        "did_at": float(record.get("at") or 0),
        "when": "",
    }


def recent(store, now: float, limit: int = 20) -> list[dict]:
    """What Wisp did lately, newest first, for the list in Settings. Words only: no identifiers.
    A write whose outcome was never recorded ("unknown") is listed too, with where to look
    (`check`): it may have landed, there is no Undo for it, and the person should know."""
    rows = [r for r in store.auto_added().values()
            if isinstance(r, dict) and r.get("status") in ("added", "undone", "unknown") and r.get("title")]
    rows.sort(key=lambda r: -float(r.get("at") or 0))
    where = {"calendar": "Calendar", "reminder": "Reminders", "follow_up": "Reminders", "hold": "your Day plan"}
    return [{"kind": r.get("kind", ""),
             "title": guard.clean_title(str(r["title"] if r.get("status") == "unknown"
                                            else (r.get("card") or {}).get("title") or r["title"])),
             "who": str(r.get("who") or ""), "at": float(r.get("at") or 0), "undone": r.get("status") == "undone",
             # Never empty for an uncertain one: an empty `check` reads as "done" in Settings.
             "check": where.get(r.get("kind"), "Calendar and Reminders") if r.get("status") == "unknown" else ""}
            for r in rows[:limit]]

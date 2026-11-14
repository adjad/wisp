"""Hold time for a deadline: pin the free hour the Brief found, without waiting for a tap.

deadlines.py already finds a real free slot for work due within three days and
offers it as a "block_time" card. Once the person lets Wisp act for them
(acting.py), Wisp holds that slot itself and reports it on a "Wisp did this"
card with Undo.

IMPORTANT rules:
- Wisp-local only. The hold is a pinned Day task in Wisp's own plan, made by
  the same verified deadlines.hold the card's button uses. Nothing is written
  to Calendar or Reminders, and nothing leaves the Mac.
- Code only. The deadline is the person's own calendar or reminder row; no
  model is called and no message text is read.
- Never for a reminder or event Wisp's own auto-add created: one message gets
  at most one action of Wisp's own. An auto-add whose write may have landed but
  returned no id is matched by its title and a time within a day.
- One hold per deadline. The key is the source's native id (a commitment row id
  changes with the time), and when that can't be read, the source and title. Every
  hold record keeps that key and the deadline's time, and a card is the same deadline
  as the NEAREST record for its key when it is within SAME_DEADLINE_S of it (a snooze
  or a move; each such time is remembered, so a run of snoozes stays one deadline).
  A repeating reminder or event further away than that is a new occurrence, held once.
- After an Undo, no deadline with that key due within UNDONE_WINDOW_S of the undone
  one is held, or offered as a tap card.
- A hold whose outcome is unknown (the write raised) counts toward the cap and is
  never retried. A save that landed though its read-back failed is claimed on the
  retry only when the "already" task is the very task that failed save made (its id
  is stored); any other matching block is the person's own and is left alone.
- At most MAX_PER_CYCLE a cycle; no limit per day.
  The gate and a starting turn are checked again right before each hold.
- Never outside the person's day: a slot any minute of which is in their quiet
  hours, or outside their working hours (service/user_hours.py), is not held; it
  stays a card they can tap. Until the person sets hours (user_hours.is_set()), the
  defaults apply: working 9 AM–5 PM Monday to Friday, quiet 10 PM–8 AM.
- Undo removes exactly the Day task Wisp made, by the id in the store's
  record, and only while it is still where Wisp put it. A block the person
  moved or renamed is theirs: Undo leaves it and says so.
"""
from __future__ import annotations

import re
import time

from service.proactive import acting

MAX_PER_CYCLE = 2
MAX_ATTEMPTS = 2
SAME_DEADLINE_S = 3 * 86400      # a deadline moved by up to this is still the same deadline
UNDONE_WINDOW_S = 14 * 86400     # after an Undo, nothing with that key due this close is held
WISPS_OWN_S = 86400              # an unconfirmed auto-add matches a deadline this close in time
MAX_SEEN = 12                    # the deadline times one hold record remembers
UNDONE = ("undone", "undo_unknown")


def _native_id(ref: str) -> str | None:
    """The calendar event's or reminder's own id for a commitment row. The row id
    changes when the deadline is rescheduled (a snoozed reminder, a moved event);
    this one does not. None when it can't be read."""
    from service.assistant.store import assistant_store
    row = assistant_store.get(ref) if ref else None
    sid = row.get("source_id") if isinstance(row, dict) else None
    return str(sid) if isinstance(sid, str) and sid else None


# Tests replace this; production reads the commitment row by the card's ref.
resolve_native = _native_id


def _source(card: dict) -> dict:
    return ((card.get("sources") or [{}])[0] or {})


def _native(card: dict) -> str | None:
    try:
        found = resolve_native(str(_source(card).get("ref") or ""))
    except Exception:  # noqa: BLE001 — unreadable: fall back to the row id and the title key
        return None
    return found if isinstance(found, str) and found else None


def fingerprint(card: dict, native: str | None | bool = True) -> str:
    """One key per deadline that survives a reschedule: the source and its native id.
    A row whose native id can't be read falls back to the row id (the title key in
    `_prior` still catches that deadline once it is rescheduled). `native` may pass
    an id already looked up (None: none could be read)."""
    src = _source(card)
    native = _native(card) if native is True else native
    key = _native_key(card, native)
    return "hold:" + (key or acting._digest(str(src.get("kind") or ""), "row", str(src.get("ref") or "")))


def _native_key(card: dict, native: str | None) -> str | None:
    """The digest of the source and native id that every hold record for this deadline
    (each occurrence of a repeating one, too) stores as "native_key". Never the raw id."""
    return acting._digest(str(_source(card).get("kind") or ""), "native", native) if native else None


def _num(value) -> bool:
    return type(value) in (int, float) and value == value and abs(value) != float("inf")


def _times(record: dict) -> list[float]:
    """Every deadline time this hold record stands for: the one it was made for and the
    snoozed or moved times later matched to it."""
    seen = [record.get("deadline_ts"), *(record.get("deadline_seen") or [])]
    return [float(t) for t in seen if _num(t)]


def _gap(due, record: dict) -> float | None:
    """How far this card's due time is from the record's nearest time; None when either
    side has no time (then the record is taken as this deadline: no second hold)."""
    times = _times(record)
    return min(abs(float(due) - t) for t in times) if _num(due) and times else None


def title_key(card: dict) -> str:
    src = _source(card)
    words = re.sub(r"\s+", " ", str(src.get("quote") or card.get("title") or "")).strip().casefold()
    return acting._digest(str(src.get("kind") or ""), str(card.get("account") or ""), words)


def _prior(card: dict, records: dict) -> tuple[str, str, str | None, dict]:
    """(fingerprint to record under, title key, native key or None, this deadline's record or {}).

    The records for this deadline are every hold record with its native key (each
    occurrence of a repeating reminder or event shares one native id), the record under
    its plain fingerprint (older records, which have no native_key), and, by title key,
    records where one side had no native id, so two deadlines that share a title but have
    their own ids never block each other. Of those:
    - an undone one whose deadline is within UNDONE_WINDOW_S of this card's wins: it is
      neither held nor offered again;
    - else one within SAME_DEADLINE_S of this card's due time (the nearest) is this
      deadline, moved or snoozed;
    - else this is a later occurrence, under its own key (by its due day).
    A record or card with no time to compare is taken as the same deadline."""
    sid = _native(card)
    nk, tk, base = _native_key(card, sid), title_key(card), fingerprint(card, sid)
    due = card.get("due_ts")
    mine = [(k, r) for k, r in records.items() if isinstance(r, dict) and (
        k == base or (r.get("kind") == "hold" and (
            (nk is not None and r.get("native_key") == nk)
            or (r.get("title_key") == tk and not (nk is not None and r.get("native"))))))]

    def near(rows, limit):
        hits = [(k, r, _gap(due, r)) for k, r in rows]
        hits = [(k, r, g) for k, r, g in hits if g is None or g <= limit]
        # Landed before failed, then the nearest; the record under the plain key on a tie.
        hits.sort(key=lambda h: (h[1].get("status") not in acting.LANDED, -1 if h[2] is None else h[2], h[0] != base))
        return hits[0][:2] if hits else None

    found = near([(k, r) for k, r in mine if r.get("status") in UNDONE], UNDONE_WINDOW_S) \
        or near(mine, SAME_DEADLINE_S)
    if found:
        return found[0], tk, nk, found[1]
    if base not in records:
        return base, tk, nk, {}
    return "hold:" + acting._digest(base, "occurrence", int(float(due) // 86400) if _num(due) else 0), tk, nk, {}


def without_undone(cards: list[dict], store) -> list[dict]:
    """A deadline whose hold the person undid is not offered again as a tap card, even
    after it was rescheduled: the same rule as `step` (UNDONE_WINDOW_S by native key)."""
    records = store.auto_added()
    return [c for c in cards if _prior(c, records)[3].get("status") not in UNDONE]


def inside_hours(slot: dict, tz: str) -> bool:
    """Every minute of the slot is in the person's working hours and outside their quiet
    hours: checked minute by minute, so quiet hours that begin and end inside a long slot
    count. Until the person sets hours, user_hours' defaults apply (it never raises). A
    slot that can't be read, or any other error, means no hold."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from service import user_hours
        zone = ZoneInfo(tz)
        start, end = float(slot["start"]), float(slot["end"])
        if not end > start or end - start > 24 * 3600:
            return False
        working, quiet = user_hours.working_hours(), user_hours.quiet_hours()
        ts = start
        while True:
            moment = datetime.fromtimestamp(min(ts, end - 60), zone)
            if quiet.contains_time(moment) or not working.contains_time(moment):
                return False
            if ts >= end - 60:
                return True
            ts += 60
    except Exception:  # noqa: BLE001 — a slot or hours that can't be read: Wisp does not hold
        return False


def _remember_time(store, records: dict, fp: str, record: dict, due) -> None:
    """A card matched to this record as the same deadline moved (within SAME_DEADLINE_S, not
    just the undone window) adds its time to the record, so the next snooze is measured from
    here and a run of short snoozes never adds up to a second hold."""
    gap = _gap(due, record)
    if gap is None or gap <= 60 or gap > SAME_DEADLINE_S or record.get("kind") != "hold":
        return
    seen = sorted({*(float(t) for t in record.get("deadline_seen") or [] if _num(t)), float(due)})[-MAX_SEEN:]
    updated = {**record, "deadline_seen": seen}
    try:
        store.auto_add_put(fp, updated)
    except Exception:  # noqa: BLE001 — not remembered: the record still matches by its own time
        return
    records[fp] = updated


def _words(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


DISPATCHING = "dispatching"     # a hold begun and not yet recorded (acting.settle turns a stale one to "unknown")


def _unconfirmed_adds(records: dict) -> list[tuple[str, float | None]]:
    """(title, time) of each calendar or reminder auto-add that may have landed but has no
    source_id to match by (its write returned no id): the title and a nearby time stand in."""
    out = []
    for r in records.values():
        if not (isinstance(r, dict) and r.get("kind") in ("calendar", "reminder")
                and r.get("status") in acting.LANDED and not r.get("source_id")):
            continue
        title = _words(r.get("title"))
        if not title:
            continue
        times = [r.get("start_ts") if r.get("kind") == "calendar" else r.get("due_ts")]
        times += [x.get("due_ts") for x in r.get("reminders") or [] if isinstance(x, dict)]
        times = [float(t) for t in times if _num(t)]
        out += [(title, t) for t in times] or [(title, None)]
    return out


def _is_unconfirmed_add(card: dict, unconfirmed: list[tuple[str, float | None]]) -> bool:
    """The card's deadline has the title of an unconfirmed auto-add and is due within a day
    of it. A card or a record with no time matches on the title alone."""
    env = (card.get("action") or {}).get("envelope") or {}
    names = {_words(n) for n in (card.get("title"), _source(card).get("quote"), env.get("title"))} - {""}
    due = card.get("due_ts")
    return any(title in names and (t is None or not _num(due) or abs(float(due) - t) <= WISPS_OWN_S)
               for title, t in unconfirmed)


def step(cards: list[dict], *, now: float, tz: str, store, abort, hold=None, hours=None,
         clock=time.time) -> set[str]:
    """Hold the slot of each eligible block_time card. Returns the ids of the cards
    that were held, so the cycle drops them (their "Wisp did this" card takes over).
    `hold` and `hours` let tests replace deadlines.hold and the working/quiet hours check.
    The write is checked against the wall clock at that moment (`clock`), not the cycle's
    start, so a slow cycle never pins a block that has already begun."""
    held: set[str] = set()
    if not acting.enabled("hold"):         # on, and the yes named holding time
        return held
    if hold is None:
        from service.proactive.deadlines import hold as real_hold
        hold = lambda card: real_hold(card, now=clock())                  # noqa: E731
    records = store.auto_added()
    # One message, at most one action of Wisp's own: a reminder or event that auto-add itself
    # put there from a text or email is not also given a held hour.
    wisps_own = {str(r.get("source_id")) for r in records.values()
                 if isinstance(r, dict) and r.get("kind") in ("calendar", "reminder") and r.get("source_id")}
    for r in list(records.values()):
        if isinstance(r, dict) and r.get("kind") == "reminder":
            wisps_own |= {str(x.get("source_id")) for x in r.get("reminders") or []
                          if isinstance(x, dict) and x.get("source_id")}
    unconfirmed = _unconfirmed_adds(records)
    attempts = 0
    for card in cards:
        action = card.get("action") or {}
        if action.get("method") != "block_time":
            continue
        if wisps_own and _native(card) in wisps_own:
            continue
        if unconfirmed and _is_unconfirmed_add(card, unconfirmed):
            continue                                  # auto-add may have made this one: no second action
        fp, tk, nk, record = _prior(card, records)
        # "dispatching" is a hold begun and never recorded (the cycle normally settles it to
        # "unknown" first): it may be on the Day plan, so it is never tried again.
        if record.get("status") in acting.LANDED + (DISPATCHING,) or int(record.get("attempts") or 0) >= MAX_ATTEMPTS:
            if record:
                _remember_time(store, records, fp, record, card.get("due_ts"))
            continue
        if not (hours or inside_hours)(action.get("slot") or {}, tz):
            continue                                  # outside their day: it stays a card to tap
        if attempts >= MAX_PER_CYCLE:
            break
        if abort() or acting.still_ok("hold") is not None:
            break
        attempts += 1
        due = float(card["due_ts"]) if _num(card.get("due_ts")) else None
        seen = sorted({t for t in _times(record) if due is None or abs(t - due) > 60})[-MAX_SEEN:]
        title = str(card.get("title") or (action.get("envelope") or {}).get("title") or "")
        tried = int(record.get("attempts") or 0)
        mine_saved = record.get("saved_task_id")

        def mark(status: str, attempts_now: int) -> None:
            # A record with no card and no Undo: in flight, or put back as not done.
            records[fp] = acting.record(
                store, fp, kind="hold", status=status, now=now, title=title,
                source=str(_source(card).get("kind") or ""), item_id=str(_source(card).get("ref") or ""),
                timezone=tz, account=card.get("account", ""), attempts=attempts_now, title_key=tk,
                native=nk is not None, native_key=nk, deadline_ts=due, deadline_seen=seen,
                saved_task_id=mine_saved if isinstance(mine_saved, str) and mine_saved else None)

        # In flight BEFORE the write: a hold cut off between the write and its outcome leaves
        # this, which the next cycle reads as "unknown" (counted, never held again, listed in
        # Settings as something to check). A hold that can't be recorded first isn't made.
        try:
            mark(DISPATCHING, tried + 1)
        except Exception:  # noqa: BLE001
            continue
        try:
            result = hold(card)
        except Exception as exc:  # noqa: BLE001 — it may have landed: counted, never retried
            result = {"ok": False, "status": "unknown", "error": type(exc).__name__}
        result = result if isinstance(result, dict) else {"ok": False, "status": "failed"}
        task_id = result.get("task_id")
        has_task = isinstance(task_id, str) and bool(task_id)
        theirs = result.get("already") and not (has_task and record.get("status") == "failed"
                                                and isinstance(mine_saved, str) and task_id == mine_saved)
        if result.get("status") == "blocked" or theirs:
            # The slot went, or the block is the person's own (not Wisp's to claim): nothing was
            # done, so the in-flight mark comes off (the record is as it was before) and this
            # try is not counted.
            try:
                if record:
                    store.auto_add_put(fp, record)
                    records[fp] = record
                else:
                    store.auto_add_drop(fp)
                    records.pop(fp, None)
            except Exception:  # noqa: BLE001 — it stays "dispatching": safe, never held again
                pass
            continue
        # An "already" whose task is the very one Wisp's own failed attempt saved (though
        # its read-back failed) is Wisp's, so it gets its card and Undo. Any other is theirs.
        ok = result.get("ok") is True and has_task
        status = "added" if ok else "unknown" if result.get("status") == "unknown" else "failed"
        saved_id = result.get("saved_task_id")
        saved_id = saved_id if isinstance(saved_id, str) and saved_id else mine_saved
        env, slot = action.get("envelope") or {}, action.get("slot") or {}
        when = str(action.get("button") or "").removeprefix("Block ").removesuffix("?").strip()
        records[fp] = acting.record(
            store, fp, kind="hold", status=status, now=now, title=title,
            source=str(_source(card).get("kind") or ""),
            item_id=str(_source(card).get("ref") or ""), timezone=tz,
            account=card.get("account", ""), attempts=tried + 1,
            title_key=tk, native=nk is not None, native_key=nk, deadline_ts=due, deadline_seen=seen,
            saved_task_id=None if ok else (saved_id if isinstance(saved_id, str) and saved_id else None),
            card={"meta": "DAY PLAN", "title": f"Held {when} for {title}" if when else f"Held time for {title}",
                  "did": (str(card.get("why") or "").strip() + " It's on your Day plan in Wisp, not on your calendar.").strip()},
            show_until=slot.get("end") if type(slot.get("end")) in (int, float) else None,
            undo={"task_id": task_id, "start": env.get("start"), "title": env.get("title")} if ok else None)
        if ok:
            held.add(card["id"])
    return held


async def undo(record: dict) -> dict:
    """Take Wisp's hold off the Day plan: that task only, and only if it is still Wisp's."""
    from service.assistant.store import assistant_store as store
    return _undo(record, store)


def _undo(record: dict, store) -> dict:
    want = record.get("undo") or {}
    task_id = want.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return {"ok": False, "status": "blocked", "error": "Wisp has no record of that hold."}
    try:
        task = store.today_task(task_id)
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": "failed", "error": "Couldn't read your Day plan."}
    if isinstance(task, dict) and task.get("status") == "done":
        return {"ok": True, "status": "succeeded", "message": "You already finished that one, so Wisp left it"}
    if not isinstance(task, dict) or task.get("status") != "active":
        return {"ok": True, "status": "succeeded", "message": "It was already off your Day plan"}
    if task.get("pinned_start") != want.get("start") or str(task.get("title") or "").strip() != str(want.get("title") or "").strip():
        return {"ok": False, "status": "blocked",
                "error": "You've changed that block since, so Wisp left it. Remove it in Day if you don't want it."}
    try:
        store.today_save_task({"status": "dismissed"}, task_id=task_id, revision=task.get("revision"))
        after = store.today_task(task_id)
    except Exception:  # noqa: BLE001 — a conflict or a failed save: nothing is claimed
        return {"ok": False, "status": "failed", "error": "That didn't go through. Try again."}
    if not isinstance(after, dict) or after.get("status") != "dismissed":
        return {"ok": False, "status": "unknown", "error": "Wisp couldn't confirm it was removed. Check Day."}
    return {"ok": True, "status": "succeeded", "message": "Removed the hold from your Day plan"}


acting.register_undoer("hold", undo)

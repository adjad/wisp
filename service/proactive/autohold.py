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
  at most one action of Wisp's own.
- One hold per deadline, ever. A deadline Wisp held, or whose hold the person
  undid, is not held again, and an undone one is not offered as a tap card either,
  even once it is rescheduled: the key is the source's native id (a commitment row
  id changes with the time), and when that can't be read, the source and title.
- A hold whose outcome is unknown (the write raised) counts toward the cap and is
  never retried. A save that landed though its read-back failed is claimed on the
  retry ("already" after Wisp's own failed attempt); a matching block Wisp has no
  failed attempt for is the person's own and is left alone.
- At most MAX_PER_CYCLE a cycle, inside the shared daily cap (acting.room).
  The gate and a starting turn are checked again right before each hold.
- Never outside the person's day: a slot that starts or ends in their quiet
  hours, or outside their working hours (service/user_hours.py), is not held;
  it stays a card they can tap. Hours that cannot be read mean no hold.
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
    kind = str(src.get("kind") or "")
    native = _native(card) if native is True else native
    if native:
        return "hold:" + acting._digest(kind, "native", native)
    return "hold:" + acting._digest(kind, "row", str(src.get("ref") or ""))


def title_key(card: dict) -> str:
    src = _source(card)
    words = re.sub(r"\s+", " ", str(src.get("quote") or card.get("title") or "")).strip().casefold()
    return acting._digest(str(src.get("kind") or ""), str(card.get("account") or ""), words)


def _prior(card: dict, records: dict) -> tuple[str, str, bool, dict]:
    """(fingerprint, title key, native id known, this deadline's earlier record or {}).
    The title key matches only when one side had no native id, so two deadlines
    that share a title but have their own ids never block each other."""
    sid = _native(card)
    fp, tk, native = fingerprint(card, sid), title_key(card), sid is not None
    # A repeating reminder or event keeps one native id for every occurrence. A record whose
    # deadline was more than SAME_DEADLINE_S from this one is an earlier occurrence, not this
    # deadline moved: this occurrence gets its own key (by its due day), so next week's
    # worksheet is held too, while a snooze or a move of a few days stays one deadline.
    earlier, due = records.get(fp), card.get("due_ts")
    if (isinstance(earlier, dict) and type(due) in (int, float) and type(earlier.get("deadline_ts")) in (int, float)
            and abs(float(due) - float(earlier["deadline_ts"])) > SAME_DEADLINE_S):
        fp = "hold:" + acting._digest(fp, "occurrence", int(float(due) // 86400))
    found = [records[fp]] if isinstance(records.get(fp), dict) else []
    found += [r for k, r in records.items() if k != fp and isinstance(r, dict) and r.get("kind") == "hold"
              and r.get("title_key") == tk and not (native and r.get("native"))]
    landed = [r for r in found if r.get("status") in acting.LANDED]
    return fp, tk, native, (landed or found or [{}])[0]


def without_undone(cards: list[dict], store) -> list[dict]:
    """A deadline whose hold the person undid is not offered again as a tap card,
    even after it was rescheduled."""
    records = store.auto_added()
    return [c for c in cards if _prior(c, records)[3].get("status") not in ("undone", "undo_unknown")]


def inside_hours(slot: dict, tz: str) -> bool:
    """The whole slot is in the person's working hours and outside their quiet hours."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from service import user_hours
        zone = ZoneInfo(tz)
        start, end = float(slot["start"]), float(slot["end"])
        if not end > start:
            return False
        for ts in (start, end - 60):
            moment = datetime.fromtimestamp(ts, zone)
            if user_hours.in_quiet_hours(moment) or not user_hours.in_working_hours(moment):
                return False
        return True
    except Exception:  # noqa: BLE001 — hours that can't be read: Wisp does not hold
        return False


def step(cards: list[dict], *, now: float, tz: str, store, abort, hold=None, hours=None,
         clock=time.time) -> set[str]:
    """Hold the slot of each eligible block_time card. Returns the ids of the cards
    that were held, so the cycle drops them (their "Wisp did this" card takes over).
    `hold` and `hours` let tests replace deadlines.hold and the working/quiet hours check.
    The write is checked against the wall clock at that moment (`clock`), not the cycle's
    start, so a slow cycle never pins a block that has already begun."""
    held: set[str] = set()
    if not acting.enabled():
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
    attempts = 0
    for card in cards:
        action = card.get("action") or {}
        if action.get("method") != "block_time":
            continue
        if wisps_own and _native(card) in wisps_own:
            continue
        fp, tk, native, record = _prior(card, records)
        if record.get("status") in acting.LANDED or int(record.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        if not (hours or inside_hours)(action.get("slot") or {}, tz):
            continue                                  # outside their day: it stays a card to tap
        if attempts >= MAX_PER_CYCLE or acting.room(store, now) <= 0:
            break
        if abort() or acting.still_ok() is not None:
            break
        attempts += 1
        try:
            result = hold(card)
        except Exception as exc:  # noqa: BLE001 — it may have landed: counted, never retried
            result = {"ok": False, "status": "unknown", "error": type(exc).__name__}
        result = result if isinstance(result, dict) else {"ok": False, "status": "failed"}
        task_id = result.get("task_id")
        has_task = isinstance(task_id, str) and bool(task_id)
        if result.get("status") == "blocked":
            continue                                  # the slot went: nothing done
        if result.get("already") and not (has_task and record.get("status") == "failed"):
            continue                                  # the person's own block: not Wisp's to claim
        # An "already" after Wisp's own failed attempt is that attempt's task, saved
        # though its read-back failed: it is Wisp's, so it gets its card and Undo.
        ok = result.get("ok") is True and has_task
        status = "added" if ok else "unknown" if result.get("status") == "unknown" else "failed"
        env, slot = action.get("envelope") or {}, action.get("slot") or {}
        when = str(action.get("button") or "").removeprefix("Block ").removesuffix("?").strip()
        title = str(card.get("title") or env.get("title") or "")
        records[fp] = acting.record(
            store, fp, kind="hold", status=status, now=now, title=title,
            source=str(_source(card).get("kind") or ""),
            item_id=str(_source(card).get("ref") or ""), timezone=tz,
            account=card.get("account", ""), attempts=int(record.get("attempts") or 0) + 1,
            title_key=tk, native=native,
            deadline_ts=float(card["due_ts"]) if type(card.get("due_ts")) in (int, float) else None,
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

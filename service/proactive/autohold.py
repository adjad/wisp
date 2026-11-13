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
- One hold per deadline, ever. A deadline Wisp held, or whose hold the person
  undid, is not held again, and is not offered as a tap card either.
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

from service.proactive import acting

MAX_PER_CYCLE = 2
MAX_ATTEMPTS = 2


def fingerprint(card: dict) -> str:
    ref = str(((card.get("sources") or [{}])[0] or {}).get("ref") or "")
    return "hold:" + acting._digest(ref)


def without_undone(cards: list[dict], store) -> list[dict]:
    """A deadline whose hold the person undid is not offered again as a tap card."""
    records = store.auto_added()
    return [c for c in cards
            if (records.get(fingerprint(c)) or {}).get("status") not in ("undone", "undo_unknown")]


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


def step(cards: list[dict], *, now: float, tz: str, store, abort, hold=None, hours=None) -> set[str]:
    """Hold the slot of each eligible block_time card. Returns the ids of the cards
    that were held, so the cycle drops them (their "Wisp did this" card takes over).
    `hold` and `hours` let tests replace deadlines.hold and the working/quiet hours check."""
    held: set[str] = set()
    if not acting.enabled():
        return held
    if hold is None:
        from service.proactive.deadlines import hold as real_hold
        hold = lambda card: real_hold(card, now=now)                      # noqa: E731
    records = store.auto_added()
    attempts = 0
    for card in cards:
        action = card.get("action") or {}
        if action.get("method") != "block_time":
            continue
        fp = fingerprint(card)
        record = records.get(fp) or {}
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
        except Exception as exc:  # noqa: BLE001 — nothing is claimed for a hold that raised
            result = {"ok": False, "status": "failed", "error": type(exc).__name__}
        result = result if isinstance(result, dict) else {"ok": False, "status": "failed"}
        if result.get("status") == "blocked" or result.get("already"):
            continue                                  # the slot went, or it is already on the plan: nothing done
        env, slot = action.get("envelope") or {}, action.get("slot") or {}
        ok = result.get("ok") is True and isinstance(result.get("task_id"), str) and bool(result["task_id"])
        when = str(action.get("button") or "").removeprefix("Block ").removesuffix("?").strip()
        title = str(card.get("title") or env.get("title") or "")
        acting.record(
            store, fp, kind="hold", status="added" if ok else "failed", now=now, title=title,
            source=str(((card.get("sources") or [{}])[0] or {}).get("kind") or ""),
            item_id=str(((card.get("sources") or [{}])[0] or {}).get("ref") or ""), timezone=tz,
            account=card.get("account", ""), attempts=int(record.get("attempts") or 0) + 1,
            card={"meta": "DAY PLAN", "title": f"Held {when} for {title}" if when else f"Held time for {title}",
                  "did": (str(card.get("why") or "").strip() + " It's on your Day plan in Wisp, not on your calendar.").strip()},
            show_until=slot.get("end") if type(slot.get("end")) in (int, float) else None,
            undo={"task_id": result.get("task_id"), "start": env.get("start"), "title": env.get("title")} if ok else None)
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

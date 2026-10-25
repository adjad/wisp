"""Deadline cards: find a real free hour for work that is due soon.

An assignment on the calendar or a reminder due in the next three days
becomes a "block_time" card: "Worksheet 3 for STAT 5 · You're free 2–3 PM
after Hack Day. I can hold that hour." The slot comes from the Day planner
(service/assistant/today.build_plan) run over the user's real calendar,
working hours and Day tasks, so it is free by the same rules the Day tab
uses. Firing it pins a Wisp Day task there; nothing is written to Calendar.

IMPORTANT: everything here is code over the user's own calendar and
reminders. No model is called and no message text is read.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from service.proactive import guard
from service.proactive.prepare import card_id, todo_id

WINDOW_DAYS = 3
MAX_DEADLINE_CARDS = 3

# Wisp's own verification reminders: real to-dos, never a card or a lede.
TEST_TITLE = re.compile(r"^\s*Wisp (?:verification|live reminder test)\b", re.I)
_JUNK = re.compile(r"^(?:untitled|new reminder|new event|test|todo|tbd|\W*|\d+)$", re.I)
# Calendar rows that are work to do, not a place to be.
_WORK = re.compile(r"\b(?:worksheet|homework|hw\s*\d*|assignment|reading|readings|problem set|pset|essay|paper|"
                   r"lab|project|pa\s*\d+|report|draft|write[- ]?up|submit|submission|due)\b", re.I)
_LONG = re.compile(r"\b(?:worksheet|homework|hw\s*\d*|assignment|problem set|pset|essay|paper|lab|project|"
                   r"pa\s*\d+|report|write[- ]?up)\b", re.I)
_STUDY = re.compile(r"\b(?:worksheet|homework|hw\s*\d*|reading|readings|problem set|pset|essay|study)\b", re.I)
_COURSE = re.compile(r"\s*\[([A-Za-z]{2,6})-(\d{1,3}[A-Za-z]?)(?:-\d{1,3})?\]\s*")


def is_test(title: str) -> bool:
    return bool(TEST_TITLE.search(title or ""))


def work_title(title: str) -> str:
    """ "Worksheet 3 [STAT-5-02]" -> "Worksheet 3 for STAT 5". """
    text = guard.clean(title, 200)
    match = _COURSE.search(text)
    if match:
        text = (text[:match.start()] + " " + text[match.end():]).strip()
        text = re.sub(r"\s{2,}", " ", text)
        text = f"{text} for {match.group(1).upper()} {match.group(2).upper()}"
    return re.sub(r"\s{2,}", " ", text).strip()


def minutes_for(title: str) -> int:
    return 60 if _LONG.search(title or "") else 30


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def candidates(rows: list[dict], now: float, tz: str) -> list[dict]:
    """Calendar assignments and reminders due today through three days out,
    soonest first. `rows` are commitment rows (id, title, when_ts, all_day,
    source, kind, account)."""
    zone = ZoneInfo(tz)
    today = datetime.fromtimestamp(now, zone).date()
    out, seen = [], set()
    for row in rows:
        title = guard.clean(row.get("title"), 200)
        when = row.get("when_ts")
        if not title or _JUNK.match(title) or is_test(title) or not _finite(when):
            continue
        source = row.get("source")
        if source == "calendar":
            if not (row.get("all_day") or row.get("kind") == "assignment"):
                continue                 # a timed event is a place to be, not work to do
            if not (row.get("kind") == "assignment" or _WORK.search(title)):
                continue
        elif source not in ("reminders", "manual") or row.get("kind") in ("event", "meeting"):
            continue
        all_day = bool(row.get("all_day"))
        due_day = datetime.fromtimestamp(float(when), zone).date()
        if not today <= due_day <= today + timedelta(days=WINDOW_DAYS):
            continue
        if not all_day and float(when) <= now:
            continue
        key = (title.casefold(), due_day)
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": str(row["id"]), "raw_title": title, "title": work_title(title), "source":
                    "calendar" if source == "calendar" else "reminders", "account": row.get("account") or "",
                    "due_ts": float(when), "due_day": due_day, "all_day": all_day,
                    "minutes": minutes_for(title)})
    out.sort(key=lambda c: (c["due_day"], c["source"] != "calendar", c["title"].casefold()))
    return out


# --- finding the slot -----------------------------------------------------------------


def _probe(day: str, tz: str, minutes: int, due_ts: float | None, pinned: float | None = None,
           title: str = "Wisp hold") -> dict:
    return {"id": "~proactive-probe", "title": title, "day": day, "timezone": tz, "kind": "task",
            "duration_minutes": minutes, "priority": 3, "due_ts": due_ts, "pinned_start": pinned,
            "status": "active", "revision": 1, "carried_from": None, "origin_ts": None}


def _ceil_half_hour(ts: float) -> float:
    return math.ceil(ts / 1800) * 1800


def find_slot(candidate: dict, day_state, now: float, tz: str, holds: list[dict]) -> dict | None:
    """The first free stretch of `minutes` on the due day or before, inside
    working hours, after `now`, clear of the calendar, pinned Day tasks,
    planned flexible tasks and the other cards' `holds`. None when there is
    none or the planner can't vouch for the day (stale calendar, an all-day
    'away' event, events without end times)."""
    from service.assistant.today import build_plan
    zone = ZoneInfo(tz)
    today = datetime.fromtimestamp(now, zone).date()
    for offset in range((candidate["due_day"] - today).days + 1):
        day = (today + timedelta(days=offset)).isoformat()
        try:
            state = day_state(day, tz)
        except Exception:  # noqa: BLE001 — no plan, no promise
            continue
        if any(str(t.get("title") or "").casefold() in (candidate["title"].casefold(),
                                                         candidate["raw_title"].casefold())
               and t.get("status") == "active" for t in state.get("tasks") or []):
            return None                  # already on the Day plan
        prefs = dict(state.get("preferences") or {})
        if offset == 0:
            # A whole or half hour, never "2:07".
            lo = datetime.combine(today, datetime.min.time(), zone).timestamp()
            nb = max(_ceil_half_hour(now + 60), float(prefs.get("not_before") or 0))
            if nb >= lo + 86400:
                continue
            prefs["not_before"] = nb
        due = None if candidate["all_day"] else candidate["due_ts"]
        tasks = list(state.get("tasks") or []) + [
            _probe(h["day"], tz, h["minutes"], None, h["start"], h["title"]) | {"id": f"~hold-{i}"}
            for i, h in enumerate(holds) if h["day"] == day]
        tasks.append(_probe(day, tz, candidate["minutes"], due))
        try:
            plan = build_plan(day, tz, tasks, state.get("commitments") or [], prefs, state.get("sources") or {},
                              now=now)
        except (ValueError, TypeError, KeyError):
            continue
        block = next((b for b in plan["blocks"] if b.get("task_id") == "~proactive-probe"), None)
        if block is None or block["start"] < now:
            continue
        before = [b for b in plan["blocks"] if b.get("kind") == "calendar" and b.get("end") is not None
                  and abs(b["end"] - block["start"]) < 61]
        after = [b for b in plan["blocks"] if b.get("kind") == "calendar" and abs(b["start"] - block["end"]) < 61]
        return {"day": day, "start": float(block["start"]), "end": float(block["end"]),
                "minutes": candidate["minutes"],
                "after": before[0]["title"] if before else "", "before": after[0]["title"] if after else ""}
    return None


# --- the card's words -----------------------------------------------------------------


def _short(title: str, limit: int = 32) -> str:
    text = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*", " ", title or "").strip() or (title or "")
    text = re.sub(r"\s{2,}", " ", guard.clean(text, 200))
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" -–—:,x")
    return text


def _clock(ts: float, zone) -> tuple[str, str]:
    local = datetime.fromtimestamp(ts, zone)
    hour = local.strftime("%-I")
    return (hour if local.minute == 0 else f"{hour}:{local.minute:02d}"), local.strftime("%p")


def time_range(start: float, end: float, tz: str) -> str:
    """ "2–3 PM", "1:45–2:45 PM", "11:30 AM–12:30 PM". """
    zone = ZoneInfo(tz)
    (a, am), (b, bm) = _clock(start, zone), _clock(end, zone)
    return f"{a}–{b} {bm}" if am == bm else f"{a} {am}–{b} {bm}"


def _day_word(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return day.strftime("%A")


def due_words(candidate: dict, now: float, tz: str) -> str:
    """ "due today", "due tomorrow", "due Monday"."""
    today = datetime.fromtimestamp(now, ZoneInfo(tz)).date()
    word = _day_word(candidate["due_day"], today)
    return f"due {word}" if word in ("today", "tomorrow") else f"due {word}"


def length_words(minutes: int) -> str:
    return {60: "an hour", 30: "half an hour"}.get(minutes, f"{minutes} minutes")


def card(candidate: dict, slot: dict, now: float, tz: str) -> dict:
    zone = ZoneInfo(tz)
    today = datetime.fromtimestamp(now, zone).date()
    slot_day = date.fromisoformat(slot["day"])
    span = time_range(slot["start"], slot["end"], tz)
    when = span if slot_day == today else f"{span} {_day_word(slot_day, today)}"
    near = f" after {_short(slot['after'])}" if slot.get("after") else \
        f" before {_short(slot['before'])}" if slot.get("before") else ""
    hold = {60: "that hour", 30: "that half hour"}.get(slot["minutes"], "that time")
    meta_day = due_words(candidate, now, tz).upper()
    label = {"calendar": "CALENDAR", "reminders": "REMINDERS"}[candidate["source"]]
    title = candidate["title"]
    task_kind = "study" if _STUDY.search(candidate["raw_title"]) else "task"
    return {
        "id": card_id("deadline:" + candidate["id"], "block_time"),
        "meta": f"{meta_day} · {label}",
        "title": title[:1].upper() + title[1:],
        "body": f"You're free {when}{near}. I can hold {hold}.",
        "why": f"{title} is {due_words(candidate, now, tz)}.",
        "account": candidate.get("account", ""),
        "sources": [{"kind": candidate["source"], "ref": candidate["id"], "who": "", "quote": candidate["raw_title"]}],
        "action": {"method": "block_time", "button": f"Block {when}?", "draft": "", "recipient": "", "event": None,
                   "envelope": {"kind": "block_time", "title": title, "day": slot["day"], "timezone": tz,
                                "start": slot["start"], "duration_min": slot["minutes"], "task_kind": task_kind,
                                "due_ts": None if candidate["all_day"] else candidate["due_ts"]},
                   "slot": {"start": slot["start"], "end": slot["end"]},
                   "to": [], "cc": [], "subject": "", "reply_all": False, "reply_ref": None,
                   "open_url": "", "open_label": ""},
        "todo_id": todo_id(candidate["id"]),
        "fingerprints": [f"block:{candidate['id']}"],
        "due_ts": candidate["due_ts"],
        "minutes": slot["minutes"],
        "due_words": due_words(candidate, now, tz),
    }


def deadline_cards(rows: list[dict], day_state, now: float, tz: str, *, limit: int = MAX_DEADLINE_CARDS,
                   skip: set[str] | None = None) -> list[dict]:
    """Up to `limit` block_time cards, each in its own free slot. `skip` holds
    card ids already dismissed or fired."""
    skip = skip or set()
    out, holds = [], []
    for candidate in candidates(rows, now, tz):
        if len(out) >= limit:
            break
        if card_id("deadline:" + candidate["id"], "block_time") in skip:
            continue
        slot = find_slot(candidate, day_state, now, tz, holds)
        if slot is None:
            continue
        holds.append({**slot, "title": candidate["title"]})
        out.append(card(candidate, slot, now, tz))
    return out


def default_rows(now: float) -> list[dict]:
    """The user's calendar and reminders from yesterday through the window."""
    from service.assistant.store import assistant_store
    rows = assistant_store.upcoming(now - 86400, days=WINDOW_DAYS + 2)
    try:
        hidden = assistant_store.hidden_commitment_ids(now)
    except Exception:  # noqa: BLE001
        hidden = set()
    return [r for r in rows if r.get("id") not in hidden]


def default_day_state(day: str, tz: str) -> dict:
    from service.assistant.store import assistant_store
    return assistant_store.today_snapshot(day, tz)


# --- firing ---------------------------------------------------------------------------


def hold(card: dict, *, now: float, store=None) -> dict:
    """Pin the card's slot as a Wisp Day task, after checking it is still in
    the future and still clear. Verified by reading the task back. Never
    touches Calendar."""
    from service.assistant.today import build_plan
    if store is None:
        from service.assistant.store import assistant_store as store
    env = (card.get("action") or {}).get("envelope") or {}
    start, minutes, day, tz = env.get("start"), env.get("duration_min"), env.get("day"), env.get("timezone")
    title = str(env.get("title") or "").strip()[:500]
    if not (_finite(start) and type(minutes) is int and 1 <= minutes <= 240 and isinstance(day, str)
            and isinstance(tz, str) and title):
        return {"ok": False, "status": "blocked", "error": "This card is incomplete. Refresh the Brief."}
    if start < now - 60:
        return {"ok": False, "status": "blocked", "error": "That time has passed. Refresh the Brief for a new one."}
    try:
        state = store.today_snapshot(day, tz)
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": "failed", "error": "Couldn't read today's plan."}
    for task in state.get("tasks") or []:
        if (task.get("status") == "active" and task.get("pinned_start") == float(start)
                and str(task.get("title") or "").strip() == title):
            return {"ok": True, "status": "succeeded", "task_id": task.get("id"), "already": True}
    probe = _probe(day, tz, minutes, None, float(start), title)
    try:
        plan = build_plan(day, tz, list(state.get("tasks") or []) + [probe], state.get("commitments") or [],
                          state.get("preferences") or {}, state.get("sources") or {}, now=now)
    except (ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "status": "blocked", "error": str(exc)}
    block = next((b for b in plan["blocks"] if b.get("task_id") == "~proactive-probe"), None)
    if block is None or any(w.startswith("Overlaps") for w in block.get("warnings") or []):
        return {"ok": False, "status": "blocked",
                "error": "That time isn't free anymore. Refresh the Brief for a new one."}
    values = {"title": title, "day": day, "timezone": tz, "kind": env.get("task_kind") if env.get("task_kind")
              in ("study", "task") else "task", "duration_minutes": minutes, "priority": 2,
              "pinned_start": float(start)}
    if _finite(env.get("due_ts")):
        values["due_ts"] = float(env["due_ts"])
    try:
        task = store.today_save_task(values)
    except (ValueError, KeyError) as exc:
        return {"ok": False, "status": "blocked", "error": str(exc)}
    saved = store.today_task(task.get("id")) if task.get("id") else None
    if not (isinstance(saved, dict) and saved.get("pinned_start") == float(start)
            and saved.get("duration_minutes") == minutes and saved.get("status") == "active"):
        return {"ok": False, "status": "failed", "error": "The Day task wasn't saved."}
    return {"ok": True, "status": "succeeded", "task_id": saved["id"]}

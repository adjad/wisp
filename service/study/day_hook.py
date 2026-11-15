"""Study's part of ``/assistant/today`` (docs/STUDY_TAB.md "Brief and Day").

``attach`` adds the ``study`` summary, today's school deadlines and up to two
study blocks to the snapshot. It builds NEW ``deadlines``/``blocks`` lists, so
the planner's own output (and its debug replay) stays as built. A Mac that never
used Study has no study.db: then nothing is added and nothing is created.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from service.study import match, plan, ready

log = logging.getLogger(__name__)


def store_path() -> Path:
    from service.study.store import study_path
    return study_path()


def study_store():
    from service.study.store import study_store as opened
    return opened()


def _busy(blocks) -> list[tuple[float, float | None]]:
    out = []
    for b in blocks or ():
        if not isinstance(b, dict):
            continue
        start, end = b.get("start"), b.get("end")
        if isinstance(start, (int, float)) and math.isfinite(start):
            out.append((float(start), end if isinstance(end, (int, float)) else None))
    return out


def _proposed_blocks(result: dict) -> list:
    workspace = result.get("workspace")
    proposed = workspace.get("proposed_plan") if isinstance(workspace, dict) else None
    blocks = proposed.get("blocks") if isinstance(proposed, dict) else None
    return blocks if isinstance(blocks, list) else []


def _item_day(item: dict, zone) -> str | None:
    if item.get("all_day") and isinstance(item.get("due_date"), str):
        return item["due_date"]
    for field in ("due_ts", "start"):
        value = item.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return datetime.fromtimestamp(value, zone).date().isoformat()
    return None


def _drop_feed_copies(result: dict, rows: list[dict], *, timezone: str) -> None:
    """The Canvas calendar feed's copy of a school item ("Worksheet 2 [STAT-101-02]" in Calendar or
    Reminders, due the same day) is left out of the snapshot's other lists, so the item shows once,
    as the school item (or not at all once handed in). Only a title equal to a school item's after
    plan.title_key and the same local day matches: an unrelated reminder is never dropped. New
    lists only, like the rest of this hook."""
    zone = ZoneInfo(timezone)
    keys = {(plan.title_key(a.get("title")), _item_day(a, zone)) for a in rows if isinstance(a, dict)}
    keys = {k for k in keys if k[0] and k[1]}
    if not keys:
        return

    def copy(item, field="title") -> bool:
        # Never a study item, and never the person's own Day task.
        if (not isinstance(item, dict) or item.get("source") == "study" or item.get("kind") == "study"
                or item.get("task_id") is not None):
            return False
        title = item.get(field)
        return isinstance(title, str) and (plan.title_key(title), _item_day(item, zone)) in keys

    for name in ("deadlines", "all_day", "blocks"):
        if isinstance(result.get(name), list):
            result[name] = [x for x in result[name] if not copy(x)]
    workspace = result.get("workspace")
    if not isinstance(workspace, dict):
        return
    if isinstance(workspace.get("attention"), list):
        workspace["attention"] = [x for x in workspace["attention"] if not copy(x)]
    needs = workspace.get("needs_you")
    if isinstance(needs, dict) and isinstance(needs.get("items"), list):
        workspace["needs_you"] = {**needs, "items": [x for x in needs["items"] if not copy(x, "text")]}
    proposed = workspace.get("proposed_plan")
    if isinstance(proposed, dict):
        for name in ("blocks", "all_day"):
            if isinstance(proposed.get(name), list):
                workspace["proposed_plan"] = proposed = {**proposed, name: [x for x in proposed[name] if not copy(x)]}


MATCH_DAYS = 7
MATCH_CAP = 8      # assignments matched against slides per snapshot


def _slides_for(store, rows: list[dict], now: float) -> dict:
    """{assignment_id: (matches, from_phrase, covers_text)} for open work due within MATCH_DAYS
    (and today's), from this course's deck pages. Empty when the store has no pages."""
    if not hasattr(store, "pages"):
        return {}
    by_course: dict = {}
    out = {}
    soon = [a for a in rows if a.get("status") not in plan.DONE and isinstance(a.get("due_ts"), (int, float))
            and now - 86400 <= a["due_ts"] <= now + MATCH_DAYS * 86400]
    for a in sorted(soon, key=lambda a: a["due_ts"])[:MATCH_CAP]:
        cid = a.get("course_id")
        if cid not in by_course:
            by_course[cid] = match.pages_for(store, cid)
        found = match.match(a, by_course[cid])
        if found:
            out[a.get("id")] = (found, match.from_phrase(found), match.covers_text(a, found))
    return out


def _made(store, found: dict) -> dict:
    """{assignment_id: {"material_id", "count", "prepared", "from"}} for quizzes already made."""
    prepared = ready.prepared(store)
    try:
        materials = store.materials()
    except Exception:  # noqa: BLE001
        materials = []
    ids = {m.get("id") for m in materials if isinstance(m, dict)}
    out: dict = {}
    for m in materials:   # newest first
        aid = m.get("assignment_id") if isinstance(m, dict) else None
        if aid and m.get("kind") == "quiz" and aid not in out:
            out[aid] = {"material_id": m["id"], "count": m.get("count"), "prepared": False, "from": None}
    for aid, rec in prepared.items():
        if isinstance(rec, dict) and rec.get("material_id") in ids:
            hit = found.get(aid)
            phrase = match.from_phrase([{**s, "whole": True, "label": match.deck_label(s.get("deck"))}
                                        for s in rec.get("slides") or []]) if rec.get("slides") else None
            out[aid] = {"material_id": rec["material_id"], "count": rec.get("count") or ready.QUESTIONS,
                        "prepared": True, "from": phrase or (hit[1] if hit else None)}
    return out


def _weak(store, courses: dict, now: float) -> list[dict]:
    if not hasattr(store, "misses"):
        return []
    try:
        misses = store.misses(None, now - 14 * 86400)
    except Exception:  # noqa: BLE001
        return []
    try:
        material_courses = {m.get("id"): m.get("course_id") for m in store.materials() if isinstance(m, dict)}
        decks = {s.get("id"): s.get("title") or "" for s in store.sources() if isinstance(s, dict)}
    except Exception:  # noqa: BLE001
        material_courses, decks = {}, {}
    labels = {cid: (c.get("code") or c.get("name") or "") for cid, c in courses.items()}
    return plan.weak_items(misses, course_labels=labels, material_courses=material_courses, decks=decks, now=now)


def attach(result: dict, *, now: float, timezone: str) -> None:
    try:
        if not store_path().exists():
            return
        store = study_store()
        courses = {c.get("id"): c for c in store.courses() if isinstance(c, dict)}
        rows = []
        for a in store.assignments():
            if not isinstance(a, dict):
                continue
            # A missing course row (or one with no name) gives "" and no code, never a crash.
            course = courses.get(a.get("course_id")) or {}
            rows.append({**a, "course_name": a.get("course_name") or course.get("name") or "",
                         "course_code": a.get("course_code") or course.get("code") or None})
        try:
            materials = store.materials()
        except Exception:  # noqa: BLE001 - no guide named; the summary stands
            materials = []
        result["study"] = plan.day_summary(rows, now=now, timezone=timezone, materials=materials)
        found: dict = {}
        try:
            found = _slides_for(store, rows, now)
            result["study"]["ready"] = plan.ready_items(rows, found, _made(store, found), now=now,
                                                        timezone=timezone)
            result["study"]["weak"] = _weak(store, courses, now)
        except Exception:  # noqa: BLE001 - slides are extra; the summary above stands
            log.debug("study slides skipped", exc_info=True)
            result["study"].setdefault("ready", [])
            result["study"].setdefault("weak", [])
        try:
            ready.kick(now, store=store)   # background, consent-gated, never waited on
        except Exception:  # noqa: BLE001
            pass
        day = result.get("day")
        if not isinstance(day, str):
            return
        slides = {aid: (match.slides(m), covers, match.review_pages(m)) for aid, (m, _, covers) in found.items()}
        try:
            _drop_feed_copies(result, rows, timezone=timezone)
        except Exception:  # noqa: BLE001 - the copies stay; nothing else changes
            log.debug("study feed copies kept", exc_info=True)
        extra = plan.deadlines(rows, day=day, timezone=timezone, slides=slides)
        if extra:
            result["deadlines"] = sorted([*(result.get("deadlines") or []), *extra],
                                         key=lambda d: (d.get("due_ts") or 0.0, str(d.get("id"))))
        current = result.get("blocks") or []
        booked: dict = {}
        calendar_on = False
        try:
            from service.study import blocks_api
            calendar_on = bool(blocks_api.ENABLED)
            if calendar_on and hasattr(store, "meta"):
                value = store.meta(blocks_api.META, {})
                booked = value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            log.debug("study calendar records skipped", exc_info=True)
        status, events = (None, {})
        if booked:
            try:
                from service.study import blocks_api
                status, events = blocks_api._calendar_state(day, timezone)
                # A booking deleted in Calendar is forgotten first, so its block is offered again now.
                # Only bookings made in this Day's timezone are judged: the copy was read with its bounds.
                for block_id in [k for k, v in booked.items() if isinstance(v, dict) and v.get("day") == day
                                 and v.get("timezone") == timezone
                                 and blocks_api.gone(v, status, events, now)]:
                    blocks_api._forget(block_id, day)
                    booked = {k: v for k, v in booked.items() if k != block_id}
            except Exception:  # noqa: BLE001
                log.debug("study calendar check skipped", exc_info=True)
        mine = [k for k, v in booked.items() if isinstance(v, dict) and v.get("day") == day]
        # Day draws the workspace's proposed plan (its Suggested and usual-time blocks too), so a
        # study block must fit around those as well as the plan's own blocks.
        busy = _busy(current) + _busy(_proposed_blocks(result))
        blocks = plan.study_blocks(rows, busy, now=now, timezone=timezone, day=day, slides=slides,
                                   booked=mine)
        try:
            if not calendar_on:
                raise LookupError("study calendar off")
            if booked:
                blocks, current = blocks_api.mark(blocks, current, day, booked)
                proposed = _proposed_blocks(result)
                if proposed:
                    # The calendar copy of a booked block is shown once, as the study block.
                    _, kept = blocks_api.mark([], proposed, day, booked)
                    result["workspace"]["proposed_plan"]["blocks"] = kept
            else:
                blocks = [{**b, "added": False, "event_source_id": None} for b in blocks]
        except Exception:  # noqa: BLE001 - the plain blocks stand
            log.debug("study calendar marks skipped", exc_info=True)
        if blocks:
            result["blocks"] = sorted([*current, *blocks], key=lambda b: (b["start"], str(b["id"])))
    except Exception:  # noqa: BLE001 - Study never breaks the Day snapshot
        log.debug("study day hook skipped", exc_info=True)

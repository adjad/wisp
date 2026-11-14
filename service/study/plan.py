"""What is due, and where study time fits (docs/STUDY_TAB.md "Brief and Day").

Pure: every input is passed in. Assignment rows are the store's rows; when the
caller knows them it adds ``course_name`` and ``course_code`` (day_hook does).
Times are epoch seconds; days are ISO dates in the given IANA timezone.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DONE = {"submitted", "graded"}
TESTS = {"quiz", "exam"}
BLOCK_SECONDS = 45 * 60
MAX_BLOCKS = 2
WINDOW = (8, 22)          # study blocks only between 08:00 and 22:00
LINE_LIMIT = 110
_UNKNOWN_END = 60 * 60    # a busy block with no end time is treated as an hour


def _zone(timezone: str) -> ZoneInfo:
    return ZoneInfo(timezone)


def _due(a: dict) -> float | None:
    value = a.get("due_ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _open(a: dict) -> bool:
    return (a.get("status") or "upcoming") not in DONE and _due(a) is not None


def _local_day(ts: float, zone: ZoneInfo) -> date:
    return datetime.fromtimestamp(ts, zone).date()


def _bounds(day: date, zone: ZoneInfo) -> tuple[float, float]:
    return (datetime.combine(day, time(), zone).timestamp(),
            datetime.combine(day + timedelta(days=1), time(), zone).timestamp())


def _label(a: dict) -> str | None:
    code = (a.get("course_code") or a.get("code") or "").strip()
    if code:
        return code
    name = (a.get("course_name") or "").strip()
    return name if name and len(name) <= 18 else None


def _named(a: dict, *, brackets: bool) -> str:
    title = (a.get("title") or "").strip() or "Untitled"
    label = _label(a) if brackets else None
    if label and label.lower() not in title.lower():
        return f"{title} ({label})"
    return title


def _list(items: list[dict], *, brackets: bool, cap: int = 3, width: int | None = None) -> str:
    names = [_named(a, brackets=brackets) for a in items[:cap]]
    if width:
        names = [n if len(n) <= width else n[: width - 1].rstrip() + "…" for n in names]
    text = ", ".join(names)
    if len(items) > cap:
        text += f" and {len(items) - cap} more"
    return text


def _sorted(items):
    return sorted(items, key=lambda a: (_due(a), str(a.get("id") or "")))


def _day_word(ts: float, today: date, zone: ZoneInfo) -> str:
    d = _local_day(ts, zone)
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return d.strftime("%A")


def _line(today_items, tomorrow_items, near_test, *, now: float, zone: ZoneInfo, today: date) -> str:
    def build(brackets: bool, width: int | None) -> str:
        if today_items:
            head = f"{len(today_items)} due today: " + _list(today_items, brackets=brackets, width=width)
        elif tomorrow_items:
            head = ("Nothing due today. " f"{len(tomorrow_items)} due tomorrow: "
                    + _list(tomorrow_items, brackets=brackets, width=width))
        else:
            head = ""
        listed = {a.get("id") for a in (today_items or tomorrow_items)[:3]}
        if near_test and near_test.get("id") not in listed:
            word = "Exam" if near_test.get("kind") == "exam" else "Quiz"
            tail = f"{word} {_day_word(_due(near_test), today, zone)}: {(near_test.get('title') or '').strip()}"
            combined = f"{head}. {tail}" if head else tail
            if len(combined) <= LINE_LIMIT or not head:
                return combined
        return head

    for brackets, width in ((True, None), (False, None), (False, 28), (False, 18)):
        text = build(brackets, width)
        if len(text) <= LINE_LIMIT:
            return text
    return text[: LINE_LIMIT - 1].rstrip() + "…"


def day_summary(assignments: list[dict], *, now: float, timezone: str) -> dict:
    zone = _zone(timezone)
    today = _local_day(now, zone)
    tomorrow = today + timedelta(days=1)
    week_end = now + 7 * 86400
    rows = [a for a in assignments if isinstance(a, dict)]
    open_items = _sorted([a for a in rows if _open(a)])
    due_today = [a for a in open_items if _local_day(_due(a), zone) == today]
    due_tomorrow = [a for a in open_items if _local_day(_due(a), zone) == tomorrow]
    due_week = [a for a in open_items if now <= _due(a) <= week_end]
    tests = [a for a in due_week if a.get("kind") in TESTS]
    next_test = tests[0] if tests else None
    near = next(iter(a for a in tests if _local_day(_due(a), zone) <= today + timedelta(days=3)), None)
    return {
        "due_today": [{"id": a.get("id"), "title": a.get("title"), "course_name": a.get("course_name"),
                       "due_ts": _due(a), "all_day": bool(a.get("all_day")), "kind": a.get("kind") or "assignment",
                       "status": a.get("status") or "upcoming"} for a in due_today],
        "due_tomorrow_count": len(due_tomorrow),
        "due_week_count": len(due_week),
        "missing_count": sum(1 for a in rows if a.get("status") == "missing"),
        "next_test": ({"id": next_test.get("id"), "title": next_test.get("title"),
                       "course_name": next_test.get("course_name"), "due_ts": _due(next_test)}
                      if next_test else None),
        "line": _line(due_today, due_tomorrow, near, now=now, zone=zone, today=today),
        "sample": bool(rows) and all(bool(a.get("sample")) for a in rows),
    }


def deadlines(assignments: list[dict], *, day: str, timezone: str) -> list[dict]:
    zone = _zone(timezone)
    lo, hi = _bounds(date.fromisoformat(day), zone)
    out = []
    for a in _sorted([a for a in assignments if isinstance(a, dict) and _open(a)]):
        due = _due(a)
        if not lo <= due < hi:
            continue
        label = (a.get("course_code") or a.get("code") or a.get("course_name") or "").strip()
        title = (a.get("title") or "").strip() or "Untitled"
        item = {"id": f"study:{a.get('id')}", "title": f"{title} · {label}" if label else title,
                "due_ts": due, "source": "study"}
        if a.get("all_day"):
            item.update(all_day=True, due_date=day)
        out.append(item)
    return out


def _busy(busy) -> list[tuple[float, float]]:
    out = []
    for pair in busy or ():
        try:
            start, end = pair
        except (TypeError, ValueError):
            continue
        if not isinstance(start, (int, float)) or not math.isfinite(start):
            continue
        if not isinstance(end, (int, float)) or not math.isfinite(end) or end <= start:
            end = start + _UNKNOWN_END
        out.append((float(start), float(end)))
    return sorted(out)


def study_blocks(assignments: list[dict], busy: list[tuple[float, float]], *, now: float, timezone: str,
                 day: str) -> list[dict]:
    zone = _zone(timezone)
    today = _local_day(now, zone)
    if date.fromisoformat(day) != today:
        return []
    window_start = datetime.combine(today, time(WINDOW[0]), zone).timestamp()
    window_end = datetime.combine(today, time(WINDOW[1]), zone).timestamp()
    rounded = math.ceil(now / 1800) * 1800  # the next :00 or :30
    earliest = max(rounded, window_start)
    taken = _busy(busy)
    blocks: list[dict] = []
    candidates = _sorted([a for a in assignments if isinstance(a, dict) and _open(a)
                          and now < _due(a) <= now + 48 * 3600])
    for a in candidates:
        if len(blocks) >= MAX_BLOCKS:
            break
        limit = min(_due(a), window_end)
        cursor = earliest
        for start, end in sorted(taken):
            if end <= cursor:
                continue
            if cursor + BLOCK_SECONDS <= start:
                break
            cursor = max(cursor, end)
        if cursor + BLOCK_SECONDS > limit:
            continue
        block = {"id": f"study-block:{a.get('id')}", "title": f"Study: {(a.get('title') or '').strip()}",
                 "start": cursor, "end": cursor + BLOCK_SECONDS, "kind": "study", "task_id": None,
                 "warnings": []}
        blocks.append(block)
        taken.append((block["start"], block["end"]))
    return sorted(blocks, key=lambda b: (b["start"], b["id"]))

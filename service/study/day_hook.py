"""Study's part of ``/assistant/today`` (docs/STUDY_TAB.md "Brief and Day").

``attach`` adds the ``study`` summary, today's school deadlines and up to two
study blocks to the snapshot. It builds NEW ``deadlines``/``blocks`` lists, so
the planner's own output (and its debug replay) stays as built. A Mac that never
used Study has no study.db: then nothing is added and nothing is created.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from service.study import plan

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
        result["study"] = plan.day_summary(rows, now=now, timezone=timezone)
        day = result.get("day")
        if not isinstance(day, str):
            return
        extra = plan.deadlines(rows, day=day, timezone=timezone)
        if extra:
            result["deadlines"] = sorted([*(result.get("deadlines") or []), *extra],
                                         key=lambda d: (d.get("due_ts") or 0.0, str(d.get("id"))))
        current = result.get("blocks") or []
        blocks = plan.study_blocks(rows, _busy(current), now=now, timezone=timezone, day=day)
        if blocks:
            result["blocks"] = sorted([*current, *blocks], key=lambda b: (b["start"], str(b["id"])))
    except Exception:  # noqa: BLE001 - Study never breaks the Day snapshot
        log.debug("study day hook skipped", exc_info=True)

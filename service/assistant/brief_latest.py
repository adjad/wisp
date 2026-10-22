"""The latest composed Daily Summary per day, kept so the app can show it again.

The app used to hold the composed brief only in memory: set by the live
`daily_brief` event or by Refresh. Every relaunch (every install) emptied it
until the next scheduled run, so the notch Brief's summary and its MAIL and
TEXTS dropdowns silently lost the composed text. The service now records each
composed brief here and serves it at GET /assistant/daily_summary/latest.

Rules:
- IMPORTANT: the file holds mail and message content. It is private from the
  first byte (0600; a cache directory it creates is 0700) and keeps only the
  newest two days (today's and yesterday's).
- IMPORTANT: a brief is recorded only when it is real (`READY`), never a "still
  syncing" hold-back, so the app never shows a hold-back as the day's summary.
- Days are the local date the run belongs to, as the scheduler names them.
- Recording is best-effort: a failed write never fails the brief.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from datetime import date, datetime

from service.paths import MOE_DIR

_PATH = MOE_DIR / "cache" / "daily_brief_latest.json"
_KEEP_DAYS = 2


def today() -> str:
    return datetime.now().date().isoformat()


def _valid_day(day: str) -> bool:
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        return False
    return True


def _load() -> dict[str, dict]:
    try:
        data = json.loads(_PATH.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {day: entry for day, entry in data.items()
            if _valid_day(day) and isinstance(entry, dict) and isinstance(entry.get("text"), str)
            and isinstance(entry.get("composed_at"), (int, float))}


def record(text: str, part_of_day: str, *, trace_id: str | None = None,
           day: str | None = None, now: float | None = None) -> None:
    """Keep `text` as `day`'s latest composed brief."""
    if not text.strip():
        return
    day = day or today()
    if not _valid_day(day):
        return
    entries = _load()
    entries[day] = {"text": text, "part_of_day": part_of_day if part_of_day in ("morning", "evening") else "",
                    "trace_id": trace_id or "", "composed_at": time.time() if now is None else now}
    kept = dict(sorted(entries.items(), reverse=True)[:_KEEP_DAYS])
    try:
        _PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(kept))
        os.chmod(tmp, 0o600)
        tmp.replace(_PATH)
    except OSError:
        traceback.print_exc()


def latest(day: str) -> dict | None:
    """`day`'s latest composed brief as {text, composed_at, part_of_day}, or None."""
    entry = _load().get(day) if _valid_day(day) else None
    if entry is None:
        return None
    return {"text": entry["text"], "composed_at": entry["composed_at"], "part_of_day": entry.get("part_of_day", "")}

"""Reads new saved Mail attachments in the background, so a file can be found by what is inside it.

IMPORTANT rules:
- OFF by default. Only a real JSON `true` in `attachments/settings.json` turns it on; a missing,
  unreadable or odd file means off.
- No language model, ever. `mail_attachments.index_one` uses plain extraction and Apple's
  on-device text recognition only; a picture that is not text and a scan the app cannot
  recognise are skipped, not described.
- Only copies Apple Mail already saved on this Mac. Nothing is downloaded from Gmail, Messages
  files are never read here, and nothing is ever shown to the person unasked.
- One file per run, one run at a time, at most 40 files a local day (kept in `state.json`, so a
  restart does not reset it). Waits 90 s after a file (30 s while the person is away), 30 min
  after an empty listing and 15 min after a failure.
- It stands down for a user's turn, a Brief or other background model task, mail memory's own
  task, no connected app, Low Power Mode, and battery under 30%. A read is one bounded step
  (20 s: a chat turn's own file read waits behind it), and nothing new starts while a turn is in flight,
  checked again just before the file is read.
- Nothing from a file (its name, sender, subject or text) is logged, printed or put in status:
  skip reasons are short fixed tokens.

What is kept follows the store's rules (service/attachment_store.py): 30 days unused, 200 MB,
erased with its account and by "forget mail memory". Turning the switch off stops reading at
the next tick; it does NOT erase what was kept, which ages out by those rules.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from service import attachment_store, idle, paths

log = logging.getLogger(__name__)

_KEY = "read_in_background"
WINDOW_DAYS = 30
MAX_PER_DAY = 40
MAX_BYTES = 10 * 1024 * 1024
FILE_DEADLINE_S = 20.0
BETWEEN_GAP_S = 90.0
AWAY_GAP_S = 30.0
EMPTY_GAP_S = 1800.0
FAILURE_GAP_S = 900.0
FOREGROUND_GAP_S = 60.0
PRUNE_EVERY_S = 3600.0
LIST_LIMIT = 200
MAX_PAGES = 3
FUTURE_HOURS = 36
KEY_MAX = 300
STOP_WAIT_S = 25.0

_task: asyncio.Task | None = None
_next_at = 0.0              # on the monotonic clock: a wall clock that moves cannot stall or hurry the reader
_last_run: float | None = None
_last_skip = ""
_last_prune: float | None = None    # monotonic
_clock = time.time          # wall clock (the day's date, the mail window); tests replace it
_mono = time.monotonic      # tests replace it


# --- the switch and the day's count ------------------------------------------------

def _dir() -> Path:
    return paths.moe_dir() / "attachments"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or unreadable means the default
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f)
    tmp.replace(path)


def enabled() -> bool:
    """Off unless the file holds a real JSON true."""
    return _read_json(_dir() / "settings.json").get(_KEY) is True


def set_enabled(value: bool) -> bool:
    if type(value) is not bool:
        raise ValueError("read_in_background must be true or false")
    _write_json(_dir() / "settings.json", {_KEY: value})
    return value


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


CLOCK_BACK_DAYS = 2             # a stored day this far ahead is a clock that moved back; further is a wrong date


def _effective_day(now: float, state: dict) -> str:
    """Today's date, but never earlier than the latest day already counted: a clock moved
    back by a day or two does not give back the day's 40 files. A stored day further ahead
    than that is a wrong date, not a clock that went back: it is ignored (today counts from
    the stored count only if the stored day is today, else from 0)."""
    today = _day(now)
    seen = state.get("day")
    if isinstance(seen, str) and today < seen <= _day(now + CLOCK_BACK_DAYS * 86400):
        return seen
    return today


def _today(now: float) -> int:
    state = _read_json(_dir() / "state.json")
    count = state.get("count")
    if state.get("day") == _effective_day(now, state) and type(count) is int and count > 0:
        return count
    return 0


def _count_one(now: float) -> None:
    try:
        state = _read_json(_dir() / "state.json")
        _write_json(_dir() / "state.json",
                    {"day": _effective_day(now, state), "count": _today(now) + 1})
    except OSError:
        log.error("attachment reader: could not save the day's count")


def status() -> dict:
    """Blocking (one store read): the route runs it with ``asyncio.to_thread``."""
    return {"enabled": enabled(), "kept": attachment_store.count(), "last_run": _last_run,
            "last_skip": _last_skip, "today": _today(_clock())}


# --- picking the file ----------------------------------------------------------------

def _key(f) -> str:
    return f"mail:{f.rowid}:{f.attachment_id}:{f.size}:{f.name}"[:KEY_MAX]


def _eligible(f) -> bool:
    from service import mail_attachments as ma
    if not (f.has_local and f.on_disk):
        return False
    if f.size > MAX_BYTES:
        return False
    if ma.is_picture(f.name, f.mime) and 0 < f.size < ma.TINY_IMAGE_BYTES:
        return False
    return True


def _wanted(f) -> bool:
    return _eligible(f) and not attachment_store.tried(_key(f))


async def _candidate(now: float):
    """(email, file) for the newest Apple Mail attachment worth reading, or None."""
    from service import mail_attachments as ma
    from service.mail_memory import bridge
    since = now - WINDOW_DAYS * 86400
    until = now + FUTURE_HOURS * 3600           # scheduled mail dated ahead must not crowd the list
    for _page in range(MAX_PAGES):
        rows, capped = await bridge.read_attachment_index([], since, until, [], limit=LIST_LIMIT)
        found = [(email, f)
                 for email in sorted(ma.from_mail_rows(rows), key=lambda e: e.ts, reverse=True)
                 for f in email.files if _eligible(f)]
        # One connection for the whole page, off the event loop.
        # Raises StoreBusy when the store cannot be read: the run is skipped, never "nothing tried".
        done = await asyncio.to_thread(attachment_store.tried_many, [_key(f) for _e, f in found])
        for email, f in found:
            if _key(f) not in done:
                return email, f
        if not capped or not rows:
            return None
        until = min(r["ts"] for r in rows) - 0.001
    return None


# --- one run ---------------------------------------------------------------------------

async def _probe(name: str, fn):
    from service.mail_memory import runner
    return await asyncio.to_thread(runner._cached, name, fn)


def _skip(code: str) -> None:
    global _last_skip
    _last_skip = code


async def _gap_after_file() -> float:
    from service.mail_memory import runner
    try:
        seconds = await _probe("hid", runner._hid_idle_s)
        on_ac, pct, _low = await _probe("power", runner._power)
    except Exception:  # noqa: BLE001 — a failed probe says present
        return BETWEEN_GAP_S
    away = seconds is not None and seconds >= runner.AWAY_AFTER_S and (on_ac or (pct or 0) > 50)
    return AWAY_GAP_S if away else BETWEEN_GAP_S


def _rest_left(ma) -> float:
    """Seconds the app's text recognition still rests after not answering (0 when not)."""
    try:
        return max(0.0, float(getattr(ma, "_ocr_resting_until", 0.0)) - time.monotonic())
    except (TypeError, ValueError):
        return 0.0


async def stop() -> None:
    """Cancel the read in flight, if any, and wait (at most 25 s) for it to end.

    "Forget mail memory" turns the switch off first, then calls this, then erases: a
    tick that already started cannot store anything after the erase."""
    task = _task
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.wait({task}, timeout=STOP_WAIT_S)
    if task.done() and not task.cancelled():
        task.exception()                    # retrieved, so a late failure is not reported twice


async def _prune_if_due() -> None:
    """Compact the store about hourly while the app runs (stale index words after a dropped text)."""
    global _last_prune
    now = _mono()
    if _last_prune is not None and now - _last_prune < PRUNE_EVERY_S:
        return
    _last_prune = now
    try:
        await asyncio.to_thread(attachment_store.prune_if_due)
    except Exception:  # noqa: BLE001 — a failed prune is retried by the next hour
        pass


async def _run() -> None:
    global _next_at, _last_run
    from service import mail_attachments as ma
    from service.mail_memory import runner
    gap = BETWEEN_GAP_S
    try:
        on_ac, pct, low = await _probe("power", runner._power)
        if low:
            _skip("low_power")
            return
        if not on_ac and pct is not None and pct < 30:
            _skip("battery_low")
            return
        _skip("")
        await _prune_if_due()
        try:
            picked = await _candidate(_clock())
        except attachment_store.StoreBusy:
            _skip("store_busy")                 # cannot tell what was tried: read nothing this run
            gap = FAILURE_GAP_S
            picked = False
        if picked is False:
            pass
        elif picked is None:
            gap = EMPTY_GAP_S
        elif idle.foreground_busy():            # a turn began while the list was read: it comes first
            _skip("foreground")
            gap = FOREGROUND_GAP_S
        else:
            email, f = picked
            outcome = await ma.index_one(email, f, deadline=time.monotonic() + FILE_DEADLINE_S)
            if outcome == "resting":
                # Text recognition is resting: the file was not tried, so it is not marked or counted.
                _skip("ocr_resting")
                rest = _rest_left(ma)
                gap = rest if rest > 0 else FAILURE_GAP_S
            else:
                # Off the loop: a lock wait, and the first call may make the salt.
                marked = await asyncio.to_thread(attachment_store.mark_tried, _key(f), outcome)
                _count_one(_clock())
                if marked:
                    gap = await _gap_after_file()
                else:
                    _skip("store_busy")         # not recorded: slow down rather than ask for it again at once
                    gap = FAILURE_GAP_S
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the type only: a message could hold mail data
        log.error("attachment reader failed: %s", type(exc).__name__)
        _skip("read_failed")
        gap = FAILURE_GAP_S
    _last_run = _clock()
    _next_at = _mono() + gap


def schedule_tick() -> None:
    """Called from the assistant scheduler every 30 s; starts at most one read."""
    global _task
    if _task is not None and not _task.done():
        return
    if not enabled():
        return
    now = _clock()
    if _mono() < _next_at:
        return
    if _today(now) >= MAX_PER_DAY:
        _skip("daily_cap")
        return
    if idle.foreground_busy():
        _skip("foreground")
        return
    from service import stage_mode
    if stage_mode.holds_background():
        _skip("demo_mode")              # Demo mode: text recognition waits; nothing is marked tried
        return
    from service import brief_pace
    if not brief_pace.may_start("attachments"):
        _skip("foreground")             # the user is still chatting
        return
    from service.mail_memory import runner
    mail_task = getattr(runner, "_task", None)
    if runner._other_background_work() or (mail_task is not None and not mail_task.done()):
        _skip("other_background")
        return
    from service.assistant.hub import hub
    if not hub.has_subscribers:
        _skip("no_subscribers")
        return
    _task = asyncio.create_task(_run())

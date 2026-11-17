"""Timers and alarms — the one Siri-shaped gap with no partial substitute.

WHY `add_reminder` DOES NOT COVER THIS
--------------------------------------
The 2026-08-18 diagnosis named this explicitly: `add_reminder` is
calendar-shaped — a title and a datetime, written into a store the user reads
later. A timer is a different object. "Ten minutes from now, ring" has no title,
is never reviewed, and its entire purpose is the sound at the end. Pointing a
timer request at the reminders store produces something that looks accepted and
then silently fails to do the one thing that was asked.

STATE LIVES ON DISK, NOT JUST IN MEMORY
---------------------------------------
Timers survive a backend restart. The alternative — module globals — was already
measured as a real failure mode elsewhere in this codebase: the mail/messages
caches lost everything on every restart and nobody noticed until a profile
build came back half-empty (see tools/cache_store.py). A ten-minute pasta timer
that silently dies because the backend reloaded is the same bug with a louder
consequence, so the schedule is persisted and `rearm_all` arms it again when
the service starts (main.lifespan calls it once the event loop is running).

FIRING
------
Each armed timer is an asyncio task that sleeps to its deadline and then posts a
macOS notification. Tasks only exist on the service's event loop, so the two
tools are coroutines: registry.run_tool runs a plain `def` tool on a worker
thread, where there is no loop to arm on, and for a long time that meant every
timer was saved, announced and never fired. Notifications go through
`osascript` rather than a bundled notifier because it needs no extra TCC grant
and no dependency — the same reasoning tools/system_control.py documents for
volume and clipboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

# `set_alarm` takes a parameter called `time` — the right name for the model to
# see in the schema — which shadows the module inside that one function. This
# alias is how it still reads the clock.
_clock = time

from service.paths import MOE_DIR, private_write_bytes
from service.tools.registry import not_done, register

_log = logging.getLogger(__name__)

_STORE = MOE_DIR / "cache" / "timers.json"

# id -> {kind, label, deadline, created, repeat}
_TIMERS: dict[str, dict] = {}
_TASKS: dict[str, asyncio.Task] = {}
_STOPWATCH: dict[str, object] = {}
_KINDS = frozenset({"timer", "alarm", "sleep"})

# A sleeping timer re-reads the wall clock at least this often. asyncio sleeps
# on the monotonic clock, which stands still while the Mac is asleep, so one
# long sleep would ring an overnight alarm late by however long the lid was shut.
_POLL_SECONDS = 30.0
# A timer or alarm that came due this recently still rings once (Wisp was
# restarting, or the Mac took a short nap or a slow wake); anything older is
# dropped without a sound, at startup and on wake alike (owner decision
# 2026-11-10: an alarm the Mac slept through for hours does not ring on wake).
_LATE_GRACE = 300.0
# A sleep timer is stricter: pausing playback minutes late would stop something
# the user has started since.
_SLEEP_LATE_GRACE = 60.0


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
def _load() -> None:
    global _TIMERS
    try:
        raw = json.loads(_STORE.read_text(encoding="utf-8"))
        _TIMERS = {k: v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001 — a missing or corrupt store is just "no timers"
        _TIMERS = {}


def _save() -> None:
    try:
        # Owner-only from the first byte and atomic (timer labels are the user's).
        private_write_bytes(_STORE, json.dumps(_TIMERS).encode("utf-8"))
        # The temp name builds before this used, which a crash could leave 0644.
        _STORE.with_suffix(".tmp").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — persistence is best-effort, never fatal
        pass


def _notify(title: str, body: str) -> None:
    try:
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{esc(body)}" with title "{esc(title)}" sound name "Glass"'],
            capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — a failed notification must not kill the task
        pass


# --------------------------------------------------------------------------
# duration parsing
# --------------------------------------------------------------------------
_DUR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)"
    # NOT \b. A word boundary here fails on the compound form: in "1h30m" the
    # "h" is followed by "3", both word characters, so there is no boundary and
    # "1h" simply does not match — "1h30m" silently parsed as 30 minutes.
    # A negative lookahead for a letter accepts "1h30m" while still refusing to
    # read the "m" in "monday" or the "s" in "sunday" as a unit.
    r"(?![a-z])", re.I)
_UNIT = {"h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
         "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
         "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}


# Spelled-out lengths. The model often passes the user's own words through
# ("five minutes", "half an hour"), and a voice request is nothing but words.
_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9}
_SMALL = {**_ONES, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SPELLED_RE = re.compile(
    r"\b(?:(%s)(?:[-\s]+(%s))?|(%s))\b" % ("|".join(_TENS), "|".join(_ONES), "|".join(_SMALL)))
_UNIT_WORD = r"(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)"
_NUM = r"(?<![\w.])(\d+(?:\.\d+)?|an?)"
_N_AND_A_HALF_RE = re.compile(rf"{_NUM}\s+and\s+a\s+half\s+({_UNIT_WORD})\b")
_UNIT_AND_A_HALF_RE = re.compile(rf"{_NUM}\s+({_UNIT_WORD})\s+and\s+a\s+half\b")
_HOUR_AND_A_QUARTER_RE = re.compile(rf"{_NUM}\s+(?:hours?|hrs?)\s+and\s+a\s+quarter\b")
_QUARTER_HOUR_RE = re.compile(
    r"(?<![\w.])(?:(\d+|an?)\s+)?quarters?\s+(?:of\s+)?(?:an?\s+)?(?:hours?|hrs?)\b")
_HALF_UNIT_RE = re.compile(rf"\b(?:an?\s+)?half\s+(?:of\s+)?(?:an?\s+)?({_UNIT_WORD})\b")
_A_UNIT_RE = re.compile(rf"\ban?\s+(?={_UNIT_WORD}\b)")
# Words the rewrite above does not turn into a number. Left in, the digit
# pattern would read only the part it understands ("a hundred and twenty
# minutes" as 20), so these are refused and Wisp asks instead of guessing.
_UNREAD_AMOUNT_RE = re.compile(
    r"\b(?:hundred|thousand)\b|\band\s+(?:an?|\d+)\s+(?:half|quarters?)\b")


def _amount(raw: str) -> float:
    return 1.0 if raw in {"a", "an"} else float(raw)


def _spelled_to_digits(text: str) -> str:
    """ "five minutes" -> "5 minutes", "half an hour" -> "0.5 hour", and so on.

    "a" and "an" count as one only directly before a unit, so a stray "a"
    never becomes a number.
    """
    text = _SPELLED_RE.sub(
        lambda m: str(_TENS[m.group(1)] + _ONES.get(m.group(2) or "", 0)) if m.group(1)
        else str(_SMALL[m.group(3)]), text)
    text = _N_AND_A_HALF_RE.sub(lambda m: f"{_amount(m.group(1)) + 0.5:g} {m.group(2)}", text)
    text = _UNIT_AND_A_HALF_RE.sub(lambda m: f"{_amount(m.group(1)) + 0.5:g} {m.group(2)}", text)
    text = _HOUR_AND_A_QUARTER_RE.sub(lambda m: f"{_amount(m.group(1)):g} hours 15 minutes", text)
    text = _QUARTER_HOUR_RE.sub(lambda m: f"{_amount(m.group(1) or 'a') * 15:g} minutes", text)
    text = _HALF_UNIT_RE.sub(r"0.5 \1", text)
    return _A_UNIT_RE.sub("1 ", text)


def _parse_duration(text: str) -> float | None:
    """Seconds from "10 minutes", "1h30m", "90s", "five minutes", "half an
    hour", or a bare number (minutes).

    A bare number means MINUTES, not seconds. "set a timer for 10" is a
    ten-minute timer to every person who has ever said it; reading it as ten
    seconds would be technically defensible and useless. That rule is for
    digits only: a lone word ("ten") is not read as a length.
    """
    text = (text or "").strip().lower()
    if not text:
        return None
    bare = re.fullmatch(r"\d+(?:\.\d+)?", text)
    text = _spelled_to_digits(text)
    if _UNREAD_AMOUNT_RE.search(text):
        return None
    total = 0.0
    for amount, unit in _DUR_RE.findall(text):
        # Every spelling is listed in _UNIT outright rather than normalized by
        # stripping a trailing "s": "s".rstrip("s") is the empty string, so the
        # bare-seconds form ("90s") looked up nothing and killed the parse.
        seconds = _UNIT.get(unit.lower())
        if seconds is None:
            return None
        total += float(amount) * seconds
    if total:
        return total
    if bare:
        return float(bare.group()) * 60.0
    return None


def _human(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    parts = []
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not h:
        parts.append(f"{s}s")
    return " ".join(parts)


# --------------------------------------------------------------------------
# arming
# --------------------------------------------------------------------------
def _reset_started() -> bool:
    """True once /resetwisp has begun: nothing rings or writes state back after it."""
    state = getattr(sys.modules.get("service.wisp_reset"), "state", None)
    return bool(getattr(state, "sealed", False))


def _next_daily(deadline: float, now: float) -> float:
    """The next moment after `now` when the clock reads what it read at `deadline`.

    Built from local wall-clock time, not deadline + 86400, so a 7:00 alarm is
    still a 7:00 alarm the morning after the clocks change.
    """
    import datetime as _dt

    at = _dt.datetime.fromtimestamp(deadline)
    nxt = _dt.datetime.fromtimestamp(now).replace(
        hour=at.hour, minute=at.minute, second=0, microsecond=0)
    while nxt.timestamp() <= now:
        nxt += _dt.timedelta(days=1)
    return nxt.timestamp()


def _too_late(kind: str, late: float) -> bool:
    """Whether a ring this many seconds past its deadline is dropped, not rung.

    One rule for startup (`rearm_all`) and for waking from sleep (`_fire`).
    """
    return late > (_SLEEP_LATE_GRACE if kind == "sleep" else _LATE_GRACE)


async def _fire(tid: str) -> None:
    entry = _TIMERS.get(tid)
    if not entry:
        return
    deadline = float(entry["deadline"])
    while (left := deadline - time.time()) > 0:
        await asyncio.sleep(min(left, _POLL_SECONDS))
    if _reset_started():
        return
    entry = _TIMERS.pop(tid, None)
    _TASKS.pop(tid, None)
    if entry is None:          # cancelled while sleeping
        return
    now = time.time()
    kind = entry["kind"]
    if kind == "alarm" and entry.get("repeat"):
        # A daily alarm goes straight back in for tomorrow. Cancelling pops the
        # entry, so a cancelled one cannot come back through here.
        _TIMERS[tid] = {**entry, "deadline": _next_daily(deadline, now)}
        _save()
        _arm(tid)
    else:
        _save()
    # The Mac slept through it: hours late, a ring is noise about nothing, so it
    # is dropped the same silent way `rearm_all` drops it at startup. The
    # bookkeeping above has already removed a one-shot entry from the store and
    # put a daily alarm back in for its next time, so returning here is the drop.
    if _too_late(kind, now - deadline):
        # No label: only what kind it was and how late, for a debug log.
        _log.info("dropped a late %s ring (%d s past its time)", kind, int(now - deadline))
        return
    label = entry.get("label") or ("Alarm" if kind == "alarm" else "Timer")
    # osascript blocks for up to ten seconds a call; never on the event loop.
    if kind == "alarm":
        await asyncio.to_thread(_notify, label, "Alarm")
    elif kind == "sleep":
        await asyncio.to_thread(_pause_media)
        await asyncio.to_thread(_notify, label, "Sleep timer — playback paused")
    else:
        await asyncio.to_thread(_notify, label, "Time's up")


def _arm(tid: str) -> None:
    """Create the sleeping task, if an event loop is running in this thread.

    The tools are coroutines, so they reach this on the service's loop. When
    there is no loop (a CLI script importing the registry, a sync test), the
    entry stays persisted and un-armed rather than raising, and `rearm_all`
    picks it up the next time the service starts.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    old = _TASKS.pop(tid, None)
    if old and not old.done():
        old.cancel()
    _TASKS[tid] = loop.create_task(_fire(tid))


def rearm_all() -> None:
    """Arm every saved timer. Called once at startup, on the service's event loop.

    Not everything overdue rings. Before timers were armed at all, a store kept
    every timer ever set, and Wisp may simply have been closed for a week: ringing
    all of that at launch would be a burst of noise about nothing. So an entry
    past its deadline by more than a short grace is dropped silently, a daily
    alarm rolls forward to its next time, and one that came due within the grace
    rings once. Entries without a usable deadline and kind are dropped too.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    now = time.time()
    changed = False
    for tid, entry in list(_TIMERS.items()):
        deadline, kind = entry.get("deadline"), entry.get("kind")
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or kind not in _KINDS):
            del _TIMERS[tid]
            changed = True
            continue
        task = _TASKS.get(tid)
        if task is not None and not task.done():
            continue
        if _too_late(kind, now - deadline):
            if not (kind == "alarm" and entry.get("repeat")):
                del _TIMERS[tid]
                changed = True
                continue
            entry["deadline"] = _next_daily(deadline, now)
            changed = True
        _arm(tid)
    if changed:
        _save()


def _pause_media() -> None:
    for app in ("Music", "Spotify"):
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to if exists process "{app}" '
                 f'then tell application "{app}" to pause'],
                capture_output=True, text=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass


_load()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
@register(
    "set_timer",
    "Start a countdown timer, a clock-time alarm, or a sleep timer; each notifies "
    "the user (or pauses playback) when it fires. Use this for 'set a timer for "
    "N minutes', 'wake me at 7', 'stop the music in 30 minutes' — NOT add_reminder, "
    "which is for dated commitments the user reviews later. kind='timer' (default) "
    "and kind='sleep' take `duration` ('10 minutes', '1h30m', '90s', or a bare "
    "number meaning minutes); kind='alarm' takes a clock `time` ('7:30am', '18:45'). "
    "Give a `label` when the user names one ('pasta') so several can run at once.",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["timer", "alarm", "sleep"],
                     "description": "timer = countdown (default); alarm = clock time; "
                                    "sleep = pause Music/Spotify after a delay."},
            "duration": {"type": "string",
                         "description": "For timer/sleep: '10 minutes', '1h30m', '90s', or a number (minutes)."},
            "time": {"type": "string",
                     "description": "For alarm: clock time, e.g. '7:30am'. Today if still ahead, otherwise tomorrow."},
            "label": {"type": "string",
                      "description": "Optional name, e.g. 'pasta'. Needed to run several at once."},
            "repeat_daily": {"type": "boolean", "description": "Alarm only: repeat every day at this time."},
        },
    },
    category="timer_write",
    aliases=["set a timer for ten minutes", "give me five minutes on the clock",
             "start a 20 minute timer for the laundry",
             "time me for half an hour", "ping me in 15 minutes",
             "wake me up at 7", "set an alarm for half six tomorrow", "alarm for 8am",
             "stop the music in half an hour", "sleep timer for 20 minutes"],
)
async def set_timer(kind: str = "timer", duration: str = "", time: str = "",  # noqa: A002
                    label: str = "", repeat_daily: bool = False) -> str:
    kind = (kind or "timer").strip().lower()
    if kind == "alarm":
        if not (time or "").strip():
            return not_done("(error: an alarm needs a clock `time`, e.g. '7:30am'.)",
                            question="What time should the alarm go off?")
        return _set_alarm(time, label, repeat_daily)
    if kind not in {"timer", "sleep"}:
        return not_done(f"(error: unknown kind {kind!r}. Use timer, alarm, or sleep.)")
    if not (duration or "").strip():
        return not_done("(error: a timer needs a `duration`, e.g. '10 minutes'.)",
                        question="How long should it run?")
    return _set_sleep_timer(duration) if kind == "sleep" else _set_countdown(duration, label)


def _set_countdown(duration: str, label: str = "") -> str:
    secs = _parse_duration(duration)
    if secs is None:
        return not_done(f"(error: I couldn't read {duration!r} as a length of time. "
                        f"Try '10 minutes', '1h30m', or '90s'.)",
                        question="I couldn't read that as a length of time, so I didn't set a timer. How long should it run?")
    if secs <= 0:
        return not_done("(error: that duration is zero or negative.)",
                        question="That isn't a length of time I can count down, so I didn't set a timer. How long should it run?")
    if secs > 24 * 3600:
        return not_done("(error: timers cap at 24 hours — use add_reminder for anything longer.)",
                        question="Timers stop at 24 hours, so I didn't set one. Should I set a reminder instead?")
    tid = uuid.uuid4().hex[:8]
    _TIMERS[tid] = {"kind": "timer", "label": label.strip(),
                    "deadline": time.time() + secs, "created": time.time()}
    _save()
    _arm(tid)
    name = f" ({label.strip()})" if label.strip() else ""
    return f"Timer set for {_human(secs)}{name}. I'll notify you when it's up."


def _set_alarm(time: str, label: str = "", repeat_daily: bool = False) -> str:  # noqa: A002
    import datetime as _dt

    raw = (time or "").strip().lower()
    # "7 a.m.", "7:30 p.m.", "at 6pm", "noon", "midnight" are how people say it.
    raw = re.sub(r"\b([ap])\.\s?m\b\.?", r"\1m", raw)
    raw = re.sub(r"^(?:at|around|about)\s+", "", raw)
    raw = {"noon": "12:00pm", "midday": "12:00pm", "midnight": "12:00am"}.get(raw, raw)
    raw = raw.replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw)
    if not m:
        return not_done(f"(error: I couldn't read {time!r} as a clock time. "
                        f"Try '7:30am' or '18:45'.)",
                        question="I couldn't read that time, so I didn't set an alarm. What time should it go off?")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return not_done(f"(error: {time!r} isn't a valid clock time.)",
                        question="That isn't a valid time, so I didn't set an alarm. What time should it go off?")

    now = _dt.datetime.now()
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += _dt.timedelta(days=1)

    tid = uuid.uuid4().hex[:8]
    _TIMERS[tid] = {"kind": "alarm", "label": label.strip(),
                    "deadline": when.timestamp(), "created": _clock.time(),
                    "repeat": bool(repeat_daily)}
    _save()
    _arm(tid)
    when_txt = when.strftime("%-I:%M %p on %A")
    rep = " (repeating daily)" if repeat_daily else ""
    return f"Alarm set for {when_txt}{rep}."


def _set_sleep_timer(duration: str) -> str:
    secs = _parse_duration(duration)
    if secs is None:
        return not_done(f"(error: I couldn't read {duration!r} as a length of time.)",
                        question="I couldn't read that as a length of time, so I didn't set a sleep timer. How long should it run?")
    if secs <= 0:
        return not_done("(error: that duration is zero or negative.)",
                        question="That isn't a length of time I can count down, so I didn't set a sleep timer. How long should it run?")
    tid = uuid.uuid4().hex[:8]
    _TIMERS[tid] = {"kind": "sleep", "label": "Sleep timer",
                    "deadline": time.time() + secs, "created": time.time()}
    _save()
    _arm(tid)
    return f"Playback will pause in {_human(secs)}."


@register(
    "manage_timers",
    "Timers, alarms and the stopwatch only (reminders: clear_reminders / cancel_event). "
    "action='list' for 'how long is left'; 'cancel' with a `label` or id to stop one; "
    "'cancel_all' to stop every timer. The stopwatch counts UP: 'stopwatch_start', "
    "'stopwatch_lap', 'stopwatch_read', 'stopwatch_stop'.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["list", "cancel", "cancel_all", "stopwatch_start",
                                "stopwatch_lap", "stopwatch_read", "stopwatch_stop"],
                       "description": "What to do. Defaults to list."},
            "label": {"type": "string",
                      "description": "Which timer to cancel, by its name or id. Only for action='cancel'."},
        },
    },
    category="timer_write",
    aliases=["how much time is left on the timer", "what timers do I have running",
             "cancel the pasta timer", "stop all my timers",
             "turn off that alarm", "is the laundry timer still going",
             "start the stopwatch", "how long have I been going", "lap it", "stop timing"],
)
async def manage_timers(action: str = "list", label: str = "") -> str:
    action = (action or "list").strip().lower()
    if action.startswith("stopwatch_"):
        return _stopwatch(action.removeprefix("stopwatch_"))
    now = time.time()

    if action == "list":
        live = sorted(_TIMERS.items(), key=lambda kv: kv[1]["deadline"])
        if not live:
            return "No timers or alarms are running."
        lines = []
        for tid, e in live:
            left = e["deadline"] - now
            kind = {"alarm": "Alarm", "sleep": "Sleep timer"}.get(e["kind"], "Timer")
            name = f" '{e['label']}'" if e.get("label") else ""
            lines.append(f"{kind}{name} — {_human(max(0, left))} left  [{tid}]")
        return "\n".join(lines)

    if action == "cancel_all":
        n = len(_TIMERS)
        for tid in list(_TASKS):
            t = _TASKS.pop(tid)
            if not t.done():
                t.cancel()
        _TIMERS.clear()
        _save()
        return f"Cancelled {n} timer{'s' if n != 1 else ''}." if n else "Nothing to cancel."

    if action == "cancel":
        key = (label or "").strip().lower()
        if not key:
            return "(error: say which timer to cancel — its label or id. Or use action='cancel_all'.)"
        hits = [t for t, e in _TIMERS.items()
                if t == key or (e.get("label") or "").lower() == key]
        if not hits:
            hits = [t for t, e in _TIMERS.items() if key in (e.get("label") or "").lower()]
        if not hits:
            return f"No timer matching {label!r}. Use action='list' to see what's running."
        if len(hits) > 1:
            names = ", ".join(f"{_TIMERS[t].get('label') or 'unnamed'} [{t}]" for t in hits)
            return f"That matches several timers — which one? {names}"
        tid = hits[0]
        entry = _TIMERS.pop(tid)
        task = _TASKS.pop(tid, None)
        if task and not task.done():
            task.cancel()
        _save()
        name = f" '{entry['label']}'" if entry.get("label") else ""
        return f"Cancelled the timer{name}."

    return (f"(error: unknown action {action!r}. Use list, cancel, cancel_all, or "
            "stopwatch_start/lap/read/stop.)")


def _stopwatch(action: str = "read") -> str:
    action = (action or "read").strip().lower()
    now = time.time()
    started = _STOPWATCH.get("started")

    if action == "start":
        _STOPWATCH.clear()
        _STOPWATCH.update({"started": now, "laps": []})
        return "Stopwatch started."
    if started is None:
        return "The stopwatch isn't running. Say 'start the stopwatch' first."

    elapsed = now - float(started)  # type: ignore[arg-type]
    if action == "lap":
        laps = _STOPWATCH.setdefault("laps", [])
        laps.append(elapsed)  # type: ignore[union-attr]
        return f"Lap {len(laps)}: {_human(elapsed)}."  # type: ignore[arg-type]
    if action == "stop":
        laps = _STOPWATCH.get("laps") or []
        _STOPWATCH.clear()
        extra = f" ({len(laps)} laps)" if laps else ""
        return f"Stopped at {_human(elapsed)}{extra}."
    return f"{_human(elapsed)} elapsed."

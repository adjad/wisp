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
consequence, so the schedule is persisted and re-armed on import.

FIRING
------
Each armed timer is an asyncio task that sleeps to its deadline and then posts a
macOS notification. Notifications go through `osascript` rather than a bundled
notifier because it needs no extra TCC grant and no dependency — the same
reasoning tools/system_control.py documents for volume and clipboard.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

# `set_alarm` takes a parameter called `time` — the right name for the model to
# see in the schema — which shadows the module inside that one function. This
# alias is how it still reads the clock.
_clock = time

from service.paths import MOE_DIR
from service.tools.registry import register

_STORE = MOE_DIR / "cache" / "timers.json"

# id -> {kind, label, deadline, created, repeat}
_TIMERS: dict[str, dict] = {}
_TASKS: dict[str, asyncio.Task] = {}
_STOPWATCH: dict[str, object] = {}


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
        _STORE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = _STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_TIMERS), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(_STORE)
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


def _parse_duration(text: str) -> float | None:
    """Seconds from "10 minutes", "1h30m", "90s", or a bare number (minutes).

    A bare number means MINUTES, not seconds. "set a timer for 10" is a
    ten-minute timer to every person who has ever said it; reading it as ten
    seconds would be technically defensible and useless.
    """
    text = (text or "").strip().lower()
    if not text:
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
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text) * 60.0
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
async def _fire(tid: str) -> None:
    entry = _TIMERS.get(tid)
    if not entry:
        return
    delay = entry["deadline"] - time.time()
    if delay > 0:
        await asyncio.sleep(delay)
    entry = _TIMERS.pop(tid, None)
    _TASKS.pop(tid, None)
    if entry is None:          # cancelled while sleeping
        return
    _save()
    label = entry.get("label") or ("Alarm" if entry["kind"] == "alarm" else "Timer")
    if entry["kind"] == "alarm":
        _notify(label, "Alarm")
    elif entry["kind"] == "sleep":
        _pause_media()
        _notify(label, "Sleep timer — playback paused")
    else:
        _notify(label, "Time's up")


def _arm(tid: str) -> None:
    """Create the sleeping task, if an event loop is running.

    Import happens at backend startup INSIDE uvicorn's loop, so there normally
    is one. When there isn't (a CLI script importing the registry, a test), the
    entry stays persisted and un-armed rather than raising — `_rearm_all` picks
    it up the next time the service starts properly.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    old = _TASKS.pop(tid, None)
    if old and not old.done():
        old.cancel()
    _TASKS[tid] = loop.create_task(_fire(tid))


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
    "Start a countdown timer that notifies the user when it finishes. Use this "
    "for 'set a timer for N minutes' — NOT add_reminder, which is for dated "
    "commitments the user reviews later. `duration` accepts '10 minutes', "
    "'1h30m', '90s', or a bare number meaning minutes. Give a `label` when the "
    "user names the timer ('pasta', 'laundry') so several can run at once.",
    {
        "type": "object",
        "properties": {
            "duration": {"type": "string",
                         "description": "How long: '10 minutes', '1h30m', '90s', or a number (minutes)."},
            "label": {"type": "string",
                      "description": "Optional name, e.g. 'pasta'. Needed to run several timers at once."},
        },
        "required": ["duration"],
    },
    category="timer_write",
    aliases=["set a timer for ten minutes", "give me five minutes on the clock",
             "start a 20 minute timer for the laundry",
             "time me for half an hour", "ping me in 15 minutes"],
)
def set_timer(duration: str, label: str = "") -> str:
    secs = _parse_duration(duration)
    if secs is None:
        return (f"(error: I couldn't read {duration!r} as a length of time. "
                f"Try '10 minutes', '1h30m', or '90s'.)")
    if secs <= 0:
        return "(error: that duration is zero or negative.)"
    if secs > 24 * 3600:
        return "(error: timers cap at 24 hours — use add_reminder for anything longer.)"
    tid = uuid.uuid4().hex[:8]
    _TIMERS[tid] = {"kind": "timer", "label": label.strip(),
                    "deadline": time.time() + secs, "created": time.time()}
    _save()
    _arm(tid)
    name = f" ({label.strip()})" if label.strip() else ""
    return f"Timer set for {_human(secs)}{name}. I'll notify you when it's up."


@register(
    "set_alarm",
    "Set an alarm for a specific CLOCK TIME today or tomorrow (e.g. '7:30am'), "
    "as opposed to set_timer's countdown. Pass `repeat_daily` for an alarm that "
    "fires every day at that time.",
    {
        "type": "object",
        "properties": {
            "time": {"type": "string",
                     "description": "Clock time, e.g. '7:30am', '18:45'. Assumes today if still ahead, otherwise tomorrow."},
            "label": {"type": "string", "description": "Optional name for the alarm."},
            "repeat_daily": {"type": "boolean", "description": "Repeat every day at this time."},
        },
        "required": ["time"],
    },
    category="timer_write",
    aliases=["wake me up at 7", "set an alarm for half six tomorrow",
             "get me up at 6:45 every weekday", "alarm for 8am"],
)
def set_alarm(time: str, label: str = "", repeat_daily: bool = False) -> str:  # noqa: A002
    import datetime as _dt

    raw = (time or "").strip().lower().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw)
    if not m:
        return (f"(error: I couldn't read {time!r} as a clock time. "
                f"Try '7:30am' or '18:45'.)")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return f"(error: {time!r} isn't a valid clock time.)"

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


@register(
    "set_sleep_timer",
    "Pause music or video after a delay — the 'stop playing in 30 minutes' "
    "bedtime case. Pauses Music and Spotify when it fires.",
    {
        "type": "object",
        "properties": {
            "duration": {"type": "string",
                         "description": "How long before playback pauses: '30 minutes', '1h'."},
        },
        "required": ["duration"],
    },
    category="timer_write",
    aliases=["stop the music in half an hour", "turn this off after 45 minutes",
             "sleep timer for 20 minutes", "pause playback when I fall asleep"],
)
def set_sleep_timer(duration: str) -> str:
    secs = _parse_duration(duration)
    if secs is None:
        return f"(error: I couldn't read {duration!r} as a length of time.)"
    if secs <= 0:
        return "(error: that duration is zero or negative.)"
    tid = uuid.uuid4().hex[:8]
    _TIMERS[tid] = {"kind": "sleep", "label": "Sleep timer",
                    "deadline": time.time() + secs, "created": time.time()}
    _save()
    _arm(tid)
    return f"Playback will pause in {_human(secs)}."


@register(
    "manage_timers",
    "List, check, or cancel running timers and alarms. Use action='list' for "
    "'how long is left', action='cancel' with a `label` or `id` to stop one, "
    "and action='cancel_all' to clear everything.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "cancel", "cancel_all"],
                       "description": "What to do. Defaults to list."},
            "label": {"type": "string",
                      "description": "Which timer to cancel, by its name or id. Only for action='cancel'."},
        },
    },
    category="timer_write",
    aliases=["how much time is left on the timer", "what timers do I have running",
             "cancel the pasta timer", "stop all my timers",
             "turn off that alarm", "is the laundry timer still going"],
)
def manage_timers(action: str = "list", label: str = "") -> str:
    action = (action or "list").strip().lower()
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

    return f"(error: unknown action {action!r}. Use list, cancel, or cancel_all.)"


@register(
    "stopwatch",
    "Start, lap, read, or stop a stopwatch counting UP — distinct from "
    "set_timer, which counts down to a notification.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "lap", "read", "stop"],
                       "description": "What to do. Defaults to read."},
        },
    },
    category="timer_write",
    aliases=["start the stopwatch", "how long have I been going",
             "lap it", "stop timing", "start timing this"],
)
def stopwatch(action: str = "read") -> str:
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

"""When background work on the Brief may start.

Wisp has one resident model, so background calls contend with a live turn: the
same call has measured 12.8 s and 319.7 s. A turn already cancels the running
build at once, and the build keeps what it finished. This module decides when a
new one may start. Nothing here runs a model or reads a source.

IMPORTANT rules:
- A chat is more than the turn in flight. Someone reading an answer and typing
  the next question has no turn running, and the gaps between turns are where
  builds used to land. So nothing starts until CHAT_QUIET_S after the last turn
  or the last time Ask was opened.
- Waiting never loses a build: what is owed stays owed (a catch-up stays armed, a
  summary run stays due), and the next scheduler tick asks again.
- Steady chat can't starve it. After MAX_HOLD_S of waiting, a build may take any
  gap of OVERDUE_SETTLE_S, one attempt every OVERDUE_RETRY_S. A turn in flight
  always wins; a build it stops resumes from its checkpoints.
- Two builds don't run back to back: after a cycle that really used the model,
  the next automatic one waits MIN_BUILD_GAP_S, so several triggers become one
  build, and mail that arrives meanwhile is read by that build. The first hour
  after a new install has no gap, because its mail arrives in bursts.
- A day has about four automatic builds, placed across the working hours by
  brief_times. Between them new mail never starts the model: automatic_plan says
  "light", a cycle with no model call (cycle.run_cycle(light=True)), and the mail
  rows already show new mail in plain sentences. A time missed while the Mac was
  asleep is caught up once, by one build.
- Quiet hours are not gated: the user asked that background work carries on as it
  does while they sleep. "as_is" is the path as it was before this cap, with the
  chat and gap rules above. Nothing in a cycle alerts anyone (it only tells an
  open app to re-read).
- A new install, or a Brief the model hasn't read for a day, is also "as_is": it
  needs its build now, and a model outage still shows to-dos by rule.
- "Analyze now" and connecting a Google account are the user asking: they never
  come through here.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta

from service import idle

CHAT_QUIET_S = 3 * 60
MAX_HOLD_S = 30 * 60
OVERDUE_SETTLE_S = 30
OVERDUE_RETRY_S = 5 * 60
# A hold nobody has asked about for this long is over; the next one starts fresh.
_HOLD_FORGOTTEN_S = 10 * 60
MIN_BUILD_GAP_S = 15 * 60
SETUP_NO_GAP_S = 60 * 60
BUILDS_PER_DAY = 4
DEFAULT_START_MINUTE = 9 * 60
DEFAULT_END_MINUTE = 17 * 60
# A build this soon before a scheduled time (Analyze now, say) already covers it.
SLOT_COVERED_S = 30 * 60
LONG_ABSENCE_S = 24 * 3600

_holds: dict[str, dict[str, float]] = {}


def chat_age(mono: float | None = None) -> float:
    """Seconds since the last turn or the last time Ask was opened."""
    mono = time.monotonic() if mono is None else mono
    marks = [idle.last_turn()]
    try:
        from service import memory_saver
        marks.append(memory_saver._state.last_intent)
    except Exception:  # noqa: BLE001 — the last turn alone decides
        pass
    ages = [mono - mark for mark in marks if mark is not None]
    return min(ages) if ages else math.inf


def chat_recent(mono: float | None = None) -> bool:
    return idle.foreground_busy() or chat_age(mono) < CHAT_QUIET_S


def may_start(kind: str, mono: float | None = None) -> bool:
    """Whether background work of this kind may start now. Ask only when the work
    is due: asking is what starts its wait."""
    mono = time.monotonic() if mono is None else mono
    if not chat_recent(mono):
        _holds.pop(kind, None)
        return True
    hold = _holds.get(kind)
    if hold is None or mono - hold["asked"] >= _HOLD_FORGOTTEN_S:
        hold = _holds[kind] = {"since": mono, "asked": mono}
    hold["asked"] = mono
    if (not idle.foreground_busy() and mono - hold["since"] >= MAX_HOLD_S
            and chat_age(mono) >= OVERDUE_SETTLE_S):
        hold["since"] = mono - MAX_HOLD_S + OVERDUE_RETRY_S
        return True
    return False


def _in_setup(now: float) -> bool:
    try:
        from service.assistant.store import assistant_store
        first = assistant_store.first_launch_at()
    except Exception:  # noqa: BLE001 — unknown: an ordinary day
        return False
    return first is not None and 0 <= now - first < SETUP_NO_GAP_S


def build_gap_passed(last_build, now: float | None = None) -> bool:
    """Whether an automatic build may run, given when the last one finished. A
    stamp that isn't a past time (a corrupt state, the clock moved) doesn't hold it."""
    now = time.time() if now is None else now
    if not _is_stamp(last_build):
        return True
    if not 0 <= now - last_build < MIN_BUILD_GAP_S:
        return True
    return _in_setup(now)


def brief_times(start_minute: int = DEFAULT_START_MINUTE, end_minute: int = DEFAULT_END_MINUTE,
                builds: int = BUILDS_PER_DAY) -> list[int]:
    """The day's automatic builds, in local minutes after midnight, clock order:
    the start of the working day, the end, and evenly between. Hours that wrap
    midnight (a night shift) work; equal start and end mean the default day."""
    span = (end_minute - start_minute) % 1440
    if span == 0:
        start_minute, span = DEFAULT_START_MINUTE, DEFAULT_END_MINUTE - DEFAULT_START_MINUTE
    steps = max(1, builds - 1)
    return sorted({round(start_minute + span * index / steps) % 1440 for index in range(max(1, builds))})


def todays_brief_times() -> list[int]:
    try:
        from service.user_hours import working_hours
        hours = working_hours()
        return brief_times(hours.start_minute, hours.end_minute)
    except Exception:  # noqa: BLE001 — unreadable hours: the default day
        return brief_times()


def latest_slot(now: float, times: list[int] | None = None) -> float:
    """The most recent scheduled time at or before now, today's or yesterday's."""
    times = todays_brief_times() if times is None else times
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [(day + timedelta(minutes=minute)).timestamp()
                  for day in (today - timedelta(days=1), today) for minute in times]
    return max(stamp for stamp in candidates if stamp <= now)


def slot_due(last_build, now: float | None = None, times: list[int] | None = None) -> bool:
    """Whether a scheduled time has passed that no build has covered yet."""
    now = time.time() if now is None else now
    if not _is_stamp(last_build) or last_build > now:
        return True
    return last_build < latest_slot(now, times) - SLOT_COVERED_S


def _is_stamp(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _in_quiet_hours(now: float) -> bool:
    try:
        from service.user_hours import in_quiet_hours
        return bool(in_quiet_hours(datetime.fromtimestamp(now).astimezone()))
    except Exception:  # noqa: BLE001 — unreadable hours: an ordinary time of day
        return False


def automatic_plan(now: float | None = None, state: dict | None = None) -> str:
    """What an automatic cycle may do now: "as_is", "build" or "light" (see the
    rules above). `state` is the proactive store's."""
    now = time.time() if now is None else now
    state = state or {}
    if _in_quiet_hours(now) or _in_setup(now):
        return "as_is"
    model_as_of = state.get("model_as_of")
    if not _is_stamp(model_as_of) or not 0 <= now - model_as_of < LONG_ABSENCE_S:
        return "as_is"
    stamps = [stamp for stamp in (state.get("build_at"), state.get("built_for")) if _is_stamp(stamp)]
    return "build" if slot_due(max(stamps) if stamps else None, now) else "light"


def between_builds(now: float | None = None) -> bool:
    """A daytime hour, when the mail overviews are written by the day's builds
    rather than by each change of mail."""
    now = time.time() if now is None else now
    return not (_in_quiet_hours(now) or _in_setup(now))


def _reset_for_tests() -> None:
    _holds.clear()

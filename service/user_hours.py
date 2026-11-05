"""The person's working hours and quiet hours: one setting, read by everything
that plans or alerts.

Working hours say when to plan and place things: the Day planner's default
window, where the Brief spends its full builds, and which times Wisp will
suggest for a move. Quiet hours say when to leave the person alone: no alerts
and no background model work, roughly while they sleep. They are separate on
purpose. An evening outside work can still want a reminder.

Stored in $WISP_HOME/user_hours.json, owner-only. A section that is absent means
the default, so a person who skipped the question picks up a better default if
one ever ships. The first read seeds the file from where these values used to
live, so nothing a person already chose is lost:

- quiet hours from attention/settings.json (`quiet_start`, `quiet_end`);
- working hours from the Day planner's per-day rows in assistant.db. Those rows
  also record one-off days ("Plan tonight", a short Friday), so the most common
  recent window wins rather than the latest. Someone whose rows hold the planner's
  old 09:00-18:00 keeps it; only a person with no rows gets the new default.

Both old locations are left as they were, so rolling back loses nothing.

Reading never raises: bad, partial or missing data reads as the default for that
section. This module imports no store and calls no model, so a scheduler tick can
read it freely.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from service.paths import moe_dir, private_write_bytes

_log = logging.getLogger(__name__)

MINUTES_PER_DAY = 24 * 60
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)
WEEKDAYS = (0, 1, 2, 3, 4)
_FILE_VERSION = 1
_MIGRATION_ROWS = 30            # recent Day rows considered when seeding working hours
_PLAN_TONIGHT_END = MINUTES_PER_DAY  # "Plan tonight" stretches one day to midnight
_lock = threading.RLock()


@dataclass(frozen=True)
class Hours:
    """A daily window in local minutes after midnight.

    `start_minute > end_minute` wraps midnight (22:00-08:00). `days` are Python
    weekdays, Monday=0, and name the day the window STARTS on, so Friday's
    22:00-06:00 shift includes 03:00 on Saturday. `start == end` is an empty
    window: for quiet hours that means the person has none.
    """
    start_minute: int
    end_minute: int
    days: tuple[int, ...] = ALL_DAYS

    @property
    def wraps(self) -> bool:
        return self.start_minute > self.end_minute

    @property
    def is_empty(self) -> bool:
        return self.start_minute == self.end_minute

    def contains(self, minute: int, weekday: int | None = None) -> bool:
        """Is this local minute inside the window? With `weekday`, also on a listed day."""
        if self.is_empty:
            return False
        if not self.wraps:
            inside = self.start_minute <= minute < self.end_minute
            return inside and (weekday is None or weekday in self.days)
        if minute >= self.start_minute:
            return weekday is None or weekday in self.days
        if minute < self.end_minute:
            return weekday is None or (weekday - 1) % 7 in self.days
        return False

    def contains_time(self, moment: datetime) -> bool:
        return self.contains(moment.hour * 60 + moment.minute, moment.weekday())

    def day_span(self) -> tuple[int, int]:
        """This window as one stretch inside a single calendar day, start before
        end, for the Day planner: it plans one day, so a window that wraps midnight
        uses its longer side."""
        if not self.wraps:
            return self.start_minute, self.end_minute
        before, after = self.end_minute, MINUTES_PER_DAY - self.start_minute
        return (0, self.end_minute) if before >= after else (self.start_minute, MINUTES_PER_DAY)


DEFAULT_WORKING = Hours(9 * 60, 17 * 60, WEEKDAYS)
DEFAULT_QUIET = Hours(22 * 60, 8 * 60, ALL_DAYS)


def path() -> Path:
    return moe_dir() / "user_hours.json"


# ---------------------------------------------------------------------------
# Parsing: one strict check, used by both reads and writes


def _minute(value, *, upper: int) -> int:
    if type(value) is not int or not 0 <= value <= upper:
        raise ValueError("Times must be whole minutes within one day.")
    return value


def _days(value, default: tuple[int, ...]) -> tuple[int, ...]:
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or any(type(d) is not int or not 0 <= d <= 6 for d in value):
        raise ValueError("Days must be weekday numbers, Monday=0.")
    return tuple(sorted(set(value)))


def _working(section: dict) -> Hours:
    start = _minute(section.get("start_minute"), upper=MINUTES_PER_DAY - 1)
    end = _minute(section.get("end_minute"), upper=MINUTES_PER_DAY)
    if start == end:
        raise ValueError("Working hours need a start and an end that differ.")
    days = _days(section.get("days"), DEFAULT_WORKING.days)
    if not days:
        raise ValueError("Pick at least one working day.")
    return Hours(start, end, days)


def _quiet(section: dict) -> Hours:
    """Quiet hours run every day; start == end is allowed and means none."""
    return Hours(_minute(section.get("start_minute"), upper=MINUTES_PER_DAY - 1),
                 _minute(section.get("end_minute"), upper=MINUTES_PER_DAY - 1), ALL_DAYS)


def _section(data: dict, name: str, parse, default: Hours) -> Hours:
    section = data.get(name)
    if not isinstance(section, dict):
        return default
    try:
        return parse(section)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Seeding from the old locations


def _old_quiet(base: Path) -> dict | None:
    try:
        data = json.loads((base / "attention" / "settings.json").read_text(encoding="utf-8"))
        start, end = data["quiet_start"], data["quiet_end"]
        if type(start) is not int or type(end) is not int or not (0 <= start <= 23 and 0 <= end <= 23):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"start_minute": start * 60, "end_minute": end * 60}


def _old_working(base: Path) -> tuple[dict | None, bool]:
    """(the Day planner's usual window or None, whether the read could be trusted).

    False means assistant.db exists but could not be read: the caller must not
    record the migration as done, or the person's hours would be lost for good."""
    db = base / "assistant.db"
    if not db.exists():
        return None, True
    try:
        connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return None, False
    try:
        rows = connection.execute("SELECT payload FROM today_preferences ORDER BY day DESC LIMIT ?",
                                  (_MIGRATION_ROWS,)).fetchall()
    except sqlite3.OperationalError as exc:
        return None, "no such table" in str(exc)    # an older store: nothing was ever saved
    except sqlite3.Error:
        return None, False
    finally:
        connection.close()
    windows = []
    for (payload,) in rows:
        try:
            saved = json.loads(payload)
            start, end = saved["start_minute"], saved["end_minute"]
        except (ValueError, KeyError, TypeError):
            continue
        if type(start) is int and type(end) is int and 0 <= start < end <= MINUTES_PER_DAY:
            windows.append((start, end))
    usual = [w for w in windows if w[1] != _PLAN_TONIGHT_END] or windows
    if not usual:
        return None, True
    counts = Counter(usual)
    # Most common; on a tie the most recent, since rows arrive newest first.
    start, end = max(usual, key=lambda w: (counts[w], -usual.index(w)))
    return {"start_minute": start, "end_minute": end}, True


_warned = False


def _load() -> dict:
    """The stored settings, seeding them on the first read. Never raises."""
    global _warned
    with _lock:
        target = path()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            return {}                                # unreadable: defaults, until the next save
        except RecursionError:
            return {}
        try:
            base = target.parent
            seeded: dict = {"version": _FILE_VERSION}
            quiet = _old_quiet(base)
            if quiet is not None:
                seeded["quiet"] = quiet
            working, trusted = _old_working(base)
            if working is not None:
                seeded["working"] = {**working, "days": list(DEFAULT_WORKING.days)}
            if trusted:
                _write(seeded)
            elif not _warned:
                _warned = True
                _log.warning("user_hours: the Day planner's saved hours could not be read yet; will retry")
            return seeded
        except Exception as exc:                     # noqa: BLE001 - reading never raises
            if not _warned:
                _warned = True
                _log.warning("user_hours: could not seed the setting: %s", exc)
            return {}


def _write(data: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    private_write_bytes(target, json.dumps(data, indent=1, sort_keys=True, allow_nan=False).encode("utf-8"))


# ---------------------------------------------------------------------------
# Reading


def working_hours() -> Hours:
    """When to plan and place things. Defaults to 09:00-17:00, Monday to Friday."""
    try:
        return _section(_load(), "working", _working, DEFAULT_WORKING)
    except Exception:                                # noqa: BLE001 - reading never raises
        return DEFAULT_WORKING


def quiet_hours() -> Hours:
    """When to send no alerts and do no background model work. Defaults to 22:00-08:00."""
    try:
        return _section(_load(), "quiet", _quiet, DEFAULT_QUIET)
    except Exception:                                # noqa: BLE001 - reading never raises
        return DEFAULT_QUIET


def _moment(moment: datetime | None) -> datetime:
    return datetime.now().astimezone() if moment is None else moment


def in_quiet_hours(moment: datetime | None = None) -> bool:
    """`moment` is read on its own clock face, so pass it in the person's zone."""
    return quiet_hours().contains_time(_moment(moment))


def in_working_hours(moment: datetime | None = None) -> bool:
    return working_hours().contains_time(_moment(moment))


def offers_moves() -> bool:
    """Whether Wisp may offer to move an event that falls outside working hours."""
    try:
        return _load().get("offer_moves") is not False
    except Exception:                                # noqa: BLE001 - reading never raises
        return False


def is_set() -> bool:
    """Did the person answer the question? False until they save hours themselves
    or have hours that differ from the planner's old default on file."""
    try:
        data = _load()
    except Exception:                                # noqa: BLE001 - reading never raises
        return False
    if data.get("set") is True:
        return True
    quiet = data.get("quiet") or {}
    if quiet and (quiet.get("start_minute"), quiet.get("end_minute")) != (DEFAULT_QUIET.start_minute, DEFAULT_QUIET.end_minute):
        return True
    working = data.get("working") or {}
    return bool(working) and (working.get("start_minute"), working.get("end_minute")) != (540, 1080)


def snapshot() -> dict:
    """What the app shows and edits."""
    working, quiet = working_hours(), quiet_hours()
    return {"working": {"start_minute": working.start_minute, "end_minute": working.end_minute,
                        "days": list(working.days)},
            "quiet": {"start_minute": quiet.start_minute, "end_minute": quiet.end_minute},
            "offer_moves": offers_moves(), "is_set": is_set()}


# ---------------------------------------------------------------------------
# Writing


def save(*, working: dict | None = None, quiet: dict | None = None, offer_moves: bool | None = None,
         now: float | None = None) -> dict:
    """Save what was given and keep the rest. Raises ValueError on a bad value,
    leaving the file as it was."""
    if offer_moves is not None and type(offer_moves) is not bool:
        raise ValueError("offer_moves must be true or false.")
    with _lock:
        data = dict(_load())
        if working is not None:
            parsed = _working(working)
            data["working"] = {"start_minute": parsed.start_minute, "end_minute": parsed.end_minute,
                               "days": list(parsed.days)}
        if quiet is not None:
            parsed = _quiet(quiet)
            data["quiet"] = {"start_minute": parsed.start_minute, "end_minute": parsed.end_minute}
        if offer_moves is not None:
            data["offer_moves"] = offer_moves
        data.update(version=_FILE_VERSION, set=True, updated_at=time.time() if now is None else now)
        _write(data)
    return snapshot()


def clear() -> dict:
    """Back to the defaults, as About you's Erase asks. The file stays, holding
    nothing, so the old locations are not read back in."""
    with _lock:
        _write({"version": _FILE_VERSION})
    return snapshot()

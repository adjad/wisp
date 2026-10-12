"""The pure half of the live path: settings, adapters, and the reminder a decision implies.

Nothing here performs an effect or reads a live source. `service/assistant/attention_runner.py`
supplies the sources and the effect functions; this module only decides and describes.

Settings default to SHADOW: the detector runs and every would-be action is recorded, but
nothing is created and nobody is alerted until the user switches the mode to "live". A
missing settings file means shadow; a corrupt one means "off", because when the user's
choice cannot be read the safe reading is that nothing should happen.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from service.attention.corpus import Item
from service.attention.resolve import Resolved

MODES = ("off", "shadow", "live")
LOOKBACK_S = 7 * 86400          # re-evaluate recent messages: a reply can come hours later,
                                # and a message about Friday becomes "soon" on Wednesday
MIN_LEAD_S = 120                # a reminder due sooner than this after "now" is pointless
TITLE_MAX = 110


@dataclass(frozen=True)
class Settings:
    mode: str = "shadow"
    daily_cap: int = 3          # reminders created (and alerts sent) per local day
    quiet_start: int = 22       # local hour; alerts are suppressed from here...
    quiet_end: int = 8          # ...until here (wraps midnight)
    lead_minutes: int = 30      # reminder fires this long before a timed event
    default_hour: int = 9       # reminder hour for a date with no clock time

    def validate(self) -> "Settings":
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        for name, lo, hi in (("daily_cap", 0, 50), ("quiet_start", 0, 23), ("quiet_end", 0, 23),
                             ("lead_minutes", 0, 24 * 60), ("default_hour", 0, 23)):
            value = getattr(self, name)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{name} must be a whole number from {lo} to {hi}")
        return self


def settings_path(moe_dir: Path) -> Path:
    return Path(moe_dir) / "attention" / "settings.json"


def load_settings(path: Path) -> Settings:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except Exception:                    # noqa: BLE001 — unreadable, undecodable: the user's choice is unknown
        return Settings(mode="off")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        known = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
        return Settings(**known).validate()
    except Exception:                    # noqa: BLE001 — includes RecursionError from hostile nesting
        return Settings(mode="off")


def save_settings(path: Path, settings: Settings) -> Settings:
    settings.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(settings), f, indent=1, sort_keys=True)
    tmp.replace(path)
    return settings


def updated(settings: Settings, **changes) -> Settings:
    return replace(settings, **changes).validate()


def items_from_records(records: list[dict]) -> list[Item]:
    """Records from imessage_tools.structured_messages_snapshot() -> Items."""
    items: list[Item] = []
    for r in records:
        try:
            # `identity` is the feed's stable key; `guid` can be absent (content fingerprint).
            text, direction = r["text"], r["direction"]
            guid = str(r.get("identity") or r["guid"])
            ts = float(r["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if guid in ("None", ""):
            continue
        if direction not in ("incoming", "outgoing") or not isinstance(text, str) or not text.strip():
            continue
        items.append(Item(f"msg:{guid}", "messages", ts, direction, str(r.get("sender") or ""),
                          str(r.get("conversation") or ""), text))
    return items


def in_quiet_hours(now: datetime, s: Settings) -> bool:
    if s.quiet_start == s.quiet_end:
        return False
    h = now.hour
    return (s.quiet_start <= h < s.quiet_end if s.quiet_start < s.quiet_end
            else h >= s.quiet_start or h < s.quiet_end)


def local_midnight(now_ts: float, tz: str) -> float:
    d = datetime.fromtimestamp(now_ts, ZoneInfo(tz))
    return datetime.combine(d.date(), dtime(0, 0), ZoneInfo(tz)).timestamp()


@dataclass(frozen=True)
class Plan:
    title: str                  # what the reminder says
    due_ts: float               # when the reminder fires
    event_ts: float | None      # the event itself, when it has a clock time
    when_label: str             # "today at 6:00 PM"
    quote: str                  # the user's own words that justified this


def who_label(sender: str) -> str:
    """A name, or for a bare number a masked handle, so a reminder never looks like it came
    from the user themselves when it came from an unknown number."""
    if re.search(r"[A-Za-z]", sender or ""):
        return sender
    digits = re.sub(r"\D", "", sender or "")
    return f"Text from …{digits[-4:]}" if len(digits) >= 4 else "Text"


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+|;\s+|\s(?=\d{1,2}\.\s)")
_ENUMERATOR = re.compile(r"^\s*\d{1,2}[.)]\s*")


def _clause(text: str, at: int | None, n: int = 90) -> str:
    """The part of a message that states the plan.

    A long message is cut at sentence, line and list-item boundaries and the piece holding
    the time is used, so a paragraph of updates becomes the one line about this commitment
    rather than its first 90 characters.
    """
    if at is None or len(" ".join(text.split())) <= n:
        return _clip(text, n)
    start = 0
    for m in list(_BOUNDARY.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        if start <= at < end or m is None:
            piece = _ENUMERATOR.sub("", text[start:end]).strip()
            return _clip(piece if len(piece) >= 12 else text, n)
        start = m.end()
    return _clip(text, n)


def plan_reminder(source: Item, r: Resolved, now_ts: float, s: Settings, tz: str,
                  who: str | None = None) -> Plan | None:
    """The reminder for a decided-to-alert message, or None when it is already too late.

    `source` is the message whose words state the plan: the alert itself, or the user's own
    proposal when the time was inherited from the thread. `who` is the other person's name
    (the contact, not a phone number) when it is known."""
    zone = ZoneInfo(tz)
    now = datetime.fromtimestamp(now_ts, zone)
    quote = _clause(source.text, r.at)
    label = who or who_label(source.sender)
    if r.has_clock and r.start is not None:
        event = r.start.astimezone(zone)
        if event.timestamp() <= now_ts + 30:
            return None
        due = max(event - timedelta(minutes=s.lead_minutes), now + timedelta(seconds=MIN_LEAD_S))
        due = min(due, event)
        label_day = "today" if event.date() == now.date() else (
            "tomorrow" if event.date() == (now + timedelta(days=1)).date() else event.strftime("%a %b %-d"))
        when_label = f"{label_day} at {event.strftime('%-I:%M %p').lstrip('0')}"
        title = _clip(f"{label}: {quote} ({event.strftime('%-I:%M %p').lstrip('0')})", TITLE_MAX)
        return Plan(title, due.timestamp(), event.timestamp(), when_label, quote)
    if r.day is None:
        return None
    morning = datetime.combine(r.day, dtime(s.default_hour, 0), zone)
    due = max(morning, now + timedelta(seconds=MIN_LEAD_S))
    if due.date() > r.day:
        return None             # the day is already over
    when_label = "today" if r.day == now.date() else (
        "tomorrow" if r.day == (now + timedelta(days=1)).date() else r.day.strftime("%a %b %-d"))
    return Plan(_clip(f"{label}: {quote}", TITLE_MAX), due.timestamp(), None, when_label, quote)

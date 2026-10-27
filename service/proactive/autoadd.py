"""Auto-add: put a time-sensitive event or deadline the user missed in a text or
email onto Calendar or Reminders, then report it on a "Wisp did this" card.

Today's cards wait for a tap. This acts first and reports after, so it is
deliberately narrow: a wrong add is worse than a missed one.

IMPORTANT rules:
- An item qualifies only when EVERY check in `plan_for` holds: a real person, a
  school, or transactional mail (never promos, bulk mail or a critical alert);
  not withheld or sensitive; not a group chat; an explicit date in the
  source's own words, resolved against when the message ARRIVED, never today;
  at least 30 minutes ahead and at most 60 days out; missed (unanswered and
  unread or read over a day ago, or answered with a plain yes); not already on
  Calendar or in Reminders (fuzzy title within a time window); and never
  auto-added, undone, dismissed or tapped before (fingerprinted in the store).
- An event with a clock goes to Calendar (the default calendar, the stated
  length or an hour); a deadline goes to Reminders (its stated time, else 9 AM
  that day). Never both for one item.
- At most 3 writes a cycle and 10 a rolling day. Background cycle only: the
  cycle's abort (a user's turn) is checked before every write, and again right
  before dispatch.
- Every write goes through the verified native paths (attention_calendar's
  create_calendar; the outbox's verified create_reminder), is audit-logged,
  and counts only when the native readback verified it. Only then is a done
  card made. An unconfirmed write is fingerprinted and never retried, so it
  can never land twice.
- Undo deletes exactly the item Wisp created, by the identifier stored here
  (never one from a request or a card), through the verified cancel/delete
  paths, and blocks the item from ever being added again.
- Off by default: writing needs `proactive_auto_add: true` in config.yaml (the
  user's standing approval). Off, the same items become ordinary tap-to-add
  cards (the executor's calendar_event/reminder).
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from service.proactive import guard

MAX_PER_CYCLE = 3
MAX_PER_DAY = 10
DAY_S = 86400
MIN_LEAD_S = 30 * 60
MAX_AHEAD_S = 60 * DAY_S
DEFAULT_EVENT_MIN = 60
DONE_SHOWN_S = 3 * DAY_S
MAX_ATTEMPTS = 2
# Records never block forever, but they outlive every source window (7 days of
# mail and texts) many times over.
KEEP_S = 90 * DAY_S
_RECORD_STATES_BLOCKING = ("added", "undone", "unknown", "undo_unknown")


def _digest(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def enabled() -> bool:
    try:
        from service.config import proactive_auto_add_enabled
        return proactive_auto_add_enabled()
    except Exception:  # noqa: BLE001 — a setting that can't be read writes nothing
        return False


def _audit(event: str, **fields) -> None:
    try:
        from service.safety.audit import audit
        audit(event, **fields)
    except Exception:  # noqa: BLE001 — auditing never blocks a result
        pass


# --- reading the source -------------------------------------------------------------

_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_ABSOLUTE = re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}"
                       r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b", re.I)
_WEEKDAY = (r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|"
            r"sat(?:urday)?|sun(?:day)?)")
_RELATIVE = re.compile(rf"\b(?:tomorrow|tmrw|{_WEEKDAY})\b", re.I)
_RECURRING = re.compile(r"\b(?:every|each|weekly|daily|biweekly|monthly|recurring)\b", re.I)
# A proposal still being worked out is not a plan. A plain invitation ("can you
# come to my recital Sat Oct 31 at 6pm?") is: it names the event and the time.
_HEDGE = re.compile(r"\b(?:maybe|might|possibly|perhaps|tentative(?:ly)?|how about|what about|should we|"
                    r"could we|can we|shall we|wanna|thinking (?:of|about)|not sure|tbd|if you(?:'re| are)? free|"
                    r"lmk if|let me know if)\b", re.I)
_DEADLINE = re.compile(r"\b(due|deadline|submit(?:ted)?|submission|turn in|hand in|pay(?:ment)?|rsvp|"
                       r"register|registration|apply|application|expires?|last day)\b", re.I)
_EVENT = re.compile(r"\b(meeting|exam|midterm|final exam|quiz|test|appointment|appt|call|interview|class|lecture|"
                    r"lab|section|seminar|session|recital|concert|performance|game|party|dinner|lunch|breakfast|"
                    r"brunch|reservation|ceremony|office hours|orientation|rehearsal|practice|tour|checkup|"
                    r"check-up|dentist|doctor|consultation|visit|meetup|meet-up|workshop|presentation)\b", re.I)
_PROMO = re.compile(r"\bunsubscribe\b|\d+\s?% off\b|\b(?:sale|deals?|promo(?:tion)?|coupon|discount|"
                    r"limited time|shop now|order now|free shipping|webinar|newsletter)\b", re.I)
_TRANSACTIONAL = re.compile(r"\b(?:confirm(?:ed|ation)|booked|booking|reservation|appointment|scheduled)\b", re.I)
_DURATION = re.compile(r"\bfor\s+(?:an?\s+)?(\d{1,3}(?:\.\d)?)?\s*(hours?|hrs?|minutes?|mins?)\b|"
                       r"\b(\d{1,3})[- ](hour|minute|min)\b", re.I)
_CODE = re.compile(r"\b([A-Z]{2,5})\s?-?(\d{1,3}[A-Z]?)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_BLOCK = re.compile(r"(?:^|\n)From (.+?) · [^\n]*?:\n")
_STOP = {"the", "a", "an", "and", "or", "of", "for", "to", "with", "at", "on", "in", "my", "your", "our", "is",
         "are", "be", "due", "by", "from", "reminder", "event", "please", "you", "can", "come", "will", "this",
         "that", "it", "i", "we", "am", "pm", "re", "fwd"}
_CONFIRM = re.compile(r"\b(?:yes|yeah|yep|yup|sure|sounds good|see you|i'?ll be there|count me in|confirmed?|"
                      r"will do|definitely|of course|absolutely|works for me|i'?m in|perfect)\b", re.I)
_DECLINE = re.compile(r"\b(?:no|nope|can'?t|cannot|won'?t|not|sorry|unfortunately|busy|maybe|might|"
                      r"later|unsure|reschedule|conflict)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if len(s.strip()) >= 3]


def _latest_mail_block(item: dict) -> tuple[str, str]:
    """(the subject, the newest incoming message's text) from a mail item."""
    text = item.get("text") or ""
    subject = str(item.get("title") or "")
    parts = _BLOCK.split(text)
    incoming = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)
                if not parts[i].startswith("You (the user)")]
    body = incoming[-1][1] if incoming else ""
    return subject, "" if body.strip() == "(subject only)" else body


def _user_reply(item: dict) -> str:
    """The user's own newest message in the thread, or ""."""
    text = item.get("text") or ""
    if item.get("source") == "mail":
        parts = _BLOCK.split(text)
        mine = [parts[i + 1] for i in range(1, len(parts) - 1, 2) if parts[i].startswith("You (the user)")]
        return mine[-1] if mine else ""
    # Rendered sender-first: "[Adi (you) -> Maya] yes!". Only the sender counts:
    # "[Maya -> Adi (you)]" is Maya's line addressed to the user.
    lines = [line for line in text.splitlines() if line.startswith("[") and "]" in line]
    mine = [line.split("]", 1)[1] for line in lines
            if re.search(r"\(you\)|you \(the user\)", line[1:].split("]", 1)[0].split("->", 1)[0], re.I)]
    return mine[-1].strip() if mine else ""


def _confirmed(item: dict) -> bool:
    """The user answered with a plain yes, nothing that hedges or declines."""
    reply = _user_reply(item)
    return bool(reply) and len(reply) <= 200 and bool(_CONFIRM.search(reply)) and not _DECLINE.search(reply)


def _candidates(item: dict) -> list[tuple[str, bool]]:
    """(sentence, may use a relative date) in reading order, newest message only
    for relative dates: "Friday" means the Friday after THAT message arrived,
    and only the newest message is stamped with its arrival time."""
    if item["source"] == "mail":
        subject, body = _latest_mail_block(item)
        return [(subject, True)] + [(s, True) for s in _sentences(body)]
    lines = list(item.get("lines") or [])[-3:]
    out = []
    for index, line in enumerate(lines):
        newest = index == len(lines) - 1
        out += [(s, newest) for s in _sentences(line)]
    return out


def _resolve(sentence: str, arrival: float, tz: str):
    from service.attention.resolve import resolve
    try:
        return resolve(guard.normalize_slang(sentence), datetime.fromtimestamp(arrival, ZoneInfo(tz)), tz)
    except Exception:  # noqa: BLE001 — an unreadable phrase dates nothing
        return None


def _saved_contact(item: dict) -> bool:
    """A text conversation labelled by a contact's name, not a bare number."""
    if item.get("source") != "messages":
        return False
    label = str(item.get("title") or "")
    return bool(label) and not re.match(r"^[+\d(][\d() .+-]{2,}$|^\S+@\S+$", label)


_FREE_MAIL = frozenset({"gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com", "yahoo.com",
                        "outlook.com", "hotmail.com", "live.com", "aol.com", "proton.me", "protonmail.com"})


def _same_org(item: dict) -> bool:
    """Mail from the user's own organisation (a professor at the user's
    university): the sender's domain is one of the user's own, and not a
    free-mail provider anyone can sign up at."""
    if item.get("source") != "mail":
        return False
    sender = str((item.get("ref") or {}).get("to") or "").rpartition("@")[2].casefold()
    if not sender or sender in _FREE_MAIL:
        return False
    try:
        from service.memory.identity import user_emails
        own = {str(e).rpartition("@")[2].casefold() for e in user_emails() or []}
    except Exception:  # noqa: BLE001 — no identity: no organisation to match
        own = set()
    own.add(str(item.get("account") or "").rpartition("@")[2].casefold())
    return sender in own or any(sender.endswith("." + d) for d in own if d)


def _explicit(got, relative_ok: bool) -> bool:
    """A date the sender actually wrote: a month and day, a numeric date, or (in
    the newest message) a weekday or "tomorrow". A bare clock ("at 7"), "today",
    "tonight", "this week", a guessed am/pm, or anything the resolver had to
    infer beyond the year never counts."""
    if got is None or got.blocked is not None or got.guessed or got.day is None:
        return False
    if any(i in ("meridiem", "date:next_occurrence") for i in got.inferred):
        return False
    if _ABSOLUTE.search(got.quote or ""):
        return True
    return relative_ok and bool(_RELATIVE.search(got.quote or ""))


def _duration(sentence: str) -> int:
    match = _DURATION.search(sentence)
    if not match:
        return DEFAULT_EVENT_MIN
    amount, unit = (match.group(1), match.group(2)) if match.group(2) else (match.group(3), match.group(4))
    value = float(amount) if amount else 1.0
    minutes = int(round(value * (60 if unit.lower().startswith("h") else 1)))
    return minutes if 15 <= minutes <= 8 * 60 else DEFAULT_EVENT_MIN


def _first_name(who: str) -> str:
    word = re.split(r"[\s,]+", (who or "").strip())[0] if who else ""
    return word if re.fullmatch(r"[A-Za-z][A-Za-z'-]{0,30}", word or "") else ""


def _strip_dates(sentence: str, quote: str) -> str:
    text = sentence
    for piece in (quote or "").split(" / "):
        if piece.strip():
            text = text.replace(piece.strip(), " ")
    text = _ABSOLUTE.sub(" ", text)
    text = re.sub(rf"\b(?:tomorrow|tmrw|{_WEEKDAY})\b,?", " ", text, flags=re.I)
    text = re.sub(r"\b(?:at|@)?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", " ", text, flags=re.I)
    text = re.sub(r"^(?:hey|hi|hello)\b[^,!]*[,!]\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"^(?:reminder|fyi|note|heads up|update)\s*[:\-–]\s*", "", text, flags=re.I)
    text = re.sub(r"^(?:please|pls|remember to|don'?t forget to)\s+", "", text, flags=re.I)
    text = re.sub(r"^(?:can|could|would|will) you (?:come|make it|join (?:me|us))(?: to| for)?\s+", "", text,
                  flags=re.I)
    text = re.sub(r"[\s,;:.!?()\-–—]+$", "", re.sub(r"\s+", " ", text)).strip()
    text = re.sub(r"\s+\b(?:is|are|on|at|by|before|for|this|next|the)$", "", text, flags=re.I).strip()
    text = re.sub(r"\s+\b(?:is|are|on|at|by|before|for)$", "", text, flags=re.I).strip()
    return text


def _title(item: dict, sentence: str, quote: str, kind: str, word: str) -> str:
    """The event's or reminder's name, from the source's own words, by code."""
    subject = re.sub(r"^\s*(?:(?:re|fwd?)\s*:\s*)+", "", str(item.get("title") or ""), flags=re.I).strip()
    mail = item["source"] == "mail"
    code = _CODE.search(subject if mail else "") or _CODE.search(sentence)
    code_text = f"{code.group(1)} {code.group(2)}" if code else ""
    first = _first_name(item.get("who", ""))
    noun = word.lower()
    mine = re.search(rf"\bmy\s+(?:\w+\s+)?{re.escape(noun)}\b", sentence, re.I) if noun else None
    if kind == "calendar" and not mail and mine:
        title = f"{first}'s {noun}" if first else noun.capitalize()
    else:
        title = _strip_dates(sentence, quote)
        if mail and title.casefold() == subject.casefold():
            title = _strip_dates(subject, quote)
        words = title.split()
        if not 1 <= len(words) <= 8 or len(title) < 2 or re.search(r"\b(?:you|your)\b", title, re.I) and not mail:
            title = (subject if mail and subject and not _ABSOLUTE.search(subject) else
                     f"{noun.capitalize()} with {first}" if first and kind == "calendar" else noun.capitalize())
        if code_text and code_text.replace(" ", "").casefold() not in title.replace(" ", "").casefold():
            title = f"{code_text} {title}"
    title = guard.clean(title, 80)
    return title[:1].upper() + title[1:] if title else ""


# --- dedupe ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").casefold())
            if t not in _STOP and (len(t) >= 3 or t.isdigit() or re.search(r"\d", t))}


def already_there(plan: dict, rows: list[dict], tz: str) -> dict | None:
    """The existing Calendar event or reminder this plan would duplicate, or None.

    Errs toward "already there": an event at the same start (±5 min) whatever
    its name; anything sharing a meaningful word within 3 hours (an event), the
    same day (an all-day event), or a day (a reminder)."""
    zone = ZoneInfo(tz)
    at = plan["start_ts"] if plan["kind"] == "calendar" else plan["due_ts"]
    words = _tokens(plan["title"]) | _tokens(plan.get("word", ""))
    day = datetime.fromtimestamp(at, zone).date()
    for row in rows or []:
        when = row.get("when_ts")
        if type(when) not in (int, float):
            continue
        shared = bool(words & _tokens(row.get("title", "")))
        is_event = row.get("source") == "calendar" or row.get("kind") in ("event", "meeting")
        if is_event and plan["kind"] == "calendar" and abs(when - at) <= 5 * 60:
            return row
        if not shared:
            continue
        if row.get("all_day") and datetime.fromtimestamp(when, zone).date() == day:
            return row
        if abs(when - at) <= (3 * 3600 if is_event else DAY_S):
            return row
    return None


def default_existing(now: float) -> list[dict]:
    """Every synced Calendar event and reminder from yesterday to 61 days out:
    the cycle's own schedule only covers today and tomorrow."""
    try:
        from service.assistant.store import assistant_store
        return assistant_store.upcoming(now - DAY_S, days=62)
    except Exception:  # noqa: BLE001 — nothing readable: nothing is "already there", so the cap and checks still hold
        return []


# --- the plan for one item --------------------------------------------------------------


def fingerprint(item: dict, kind: str, at: float) -> str:
    """Stable across new messages in the same thread: the thread, the kind and
    the minute. An undone add is never re-added, whatever else arrives."""
    return "autoadd:" + _digest(item.get("id"), kind, int(at // 60))


def _missed(item: dict, now: float) -> bool:
    from service.proactive.cycle import card_due
    if item.get("replied"):
        return _confirmed(item)
    return card_due(item, now)


def plan_for(item: dict, result: dict | None, *, now: float, tz: str) -> tuple[dict | None, str]:
    """(plan, "") for an eligible item, else (None, why not). Pure: no store, no I/O."""
    from service.proactive import triage
    source = item.get("source")
    if source not in ("mail", "messages"):
        return None, "not mail or texts"
    if item.get("critical"):
        return None, "account alert"
    if item.get("group"):
        return None, "group chat"
    if triage.withheld(item) or item.get("secret"):
        return None, "withheld"
    if not isinstance(result, dict) or result.get("sensitive") or result.get("junk"):
        return None, "sensitive or junk"
    text = f"{item.get('title') or ''}\n{item.get('text') or ''}"
    person = bool(item.get("person", False))
    if source == "mail":
        if _PROMO.search(text):
            return None, "promotional"
        if not person and not (_TRANSACTIONAL.search(text) or triage._DEADLINE.search(text)):
            return None, "not a person, school or confirmation"
    elif not person:
        return None, "not a person"
    # A stranger never writes to the user's calendar: a person must be someone
    # the user has written to, or a saved contact (a text labelled by name).
    if person and not (item.get("user_wrote") or _saved_contact(item) or _same_org(item)):
        return None, "unknown sender: tap to add"
    arrival = item.get("received_ts")
    if type(arrival) not in (int, float):
        return None, "no arrival time"
    if not _missed(item, now):
        return None, "already handled"
    reason = "no explicit date"
    for sentence, relative_ok in _candidates(item):
        if len(sentence) > 300 or _RECURRING.search(sentence) or guard.source_sensitive(sentence):
            continue
        deadline, event = _DEADLINE.search(sentence), _EVENT.search(sentence)
        if not deadline and not event:
            continue
        if _HEDGE.search(sentence):
            reason = "only a proposal"
            continue
        got = _resolve(sentence, float(arrival), tz)
        if not _explicit(got, relative_ok):
            continue
        zone = ZoneInfo(tz)
        if deadline:
            kind, word = "reminder", deadline.group(1)
            due = got.start.timestamp() if got.has_clock and got.start is not None else \
                datetime.combine(got.day, datetime.min.time(), zone).replace(hour=9).timestamp()
            at = due
        elif got.has_clock and got.start is not None:
            kind, word = "calendar", event.group(1)
            at = got.start.timestamp()
        else:
            reason = "an event with no start time"
            continue
        if at < now + MIN_LEAD_S:
            reason = "in the past or too soon"
            continue
        if at > now + MAX_AHEAD_S:
            reason = "too far ahead"
            continue
        title = _title(item, sentence, got.quote, kind, word)
        if not title or guard.output_has_pii(title) or guard.has_contact_or_link(title):
            reason = "no safe title"
            continue
        quote = guard.clean(sentence, 160)
        plan = {"kind": kind, "title": title, "word": word.lower(), "quote": quote,
                "fingerprint": fingerprint(item, kind, at), "item_id": item.get("id"), "source": source,
                "who": item.get("who", ""), "account": item.get("account", ""), "received_ts": float(arrival),
                "has_clock": bool(got.has_clock), "timezone": tz,
                "item_fingerprint": f"{item.get('id')}:{item.get('hash')}"}
        if kind == "calendar":
            plan.update(start_ts=float(at), duration_min=_duration(sentence),
                        day=datetime.fromtimestamp(at, zone).date().isoformat())
        else:
            plan.update(due_ts=float(at))
            if got.has_clock:
                plan["alerts"] = deadline_alerts(float(at), now, tz)
        return plan, ""
    return None, reason


# --- the writes (production) -------------------------------------------------------------


DEADLINE_LEAD_S = 3 * 3600


def deadline_alerts(due: float, now: float, tz: str) -> list[float]:
    """When to be reminded of a deadline with a clock time: 9 AM on the day it
    is due and 3 hours before it, whichever are still ahead (one when they
    nearly coincide). The deadline itself when neither is."""
    zone = ZoneInfo(tz)
    day = datetime.fromtimestamp(due, zone).date()
    nine = datetime.combine(day, datetime.min.time(), zone).replace(hour=9).timestamp()
    times = sorted({t for t in (nine, due - DEADLINE_LEAD_S) if now + 5 * 60 <= t < due})
    if len(times) == 2 and times[1] - times[0] < 3600:
        times = times[1:]
    return times or [due]


def _app_connected_now() -> bool:
    """Only while the app is subscribed: a background cycle never waits out a
    reconnect grace with a write pending."""
    try:
        from service.assistant.hub import hub
        return bool(hub.has_subscribers)
    except Exception:  # noqa: BLE001
        return False


def _turn_running() -> bool:
    try:
        from service import idle
        return bool(idle.foreground_busy())
    except Exception:  # noqa: BLE001 — unknown counts as busy
        return True


def _still_ok() -> str | None:
    """Re-checked at dispatch: the setting is still on and no turn has started."""
    if not enabled():
        return "Auto-add was turned off"
    if _turn_running():
        return "A turn started; auto-add waits for the background"
    return None


async def create_event(plan: dict) -> dict:
    """The verified EventKit create: attention_calendar.create_calendar, for
    exactly this plan. `proactive_auto_add` is the user's standing approval
    (checked again at dispatch); view-only mode and the calendar_write policy
    are enforced inside create_calendar."""
    if not _app_connected_now():
        return {"ok": False, "status": "blocked", "error": "The Wisp app isn't connected"}
    from service.assistant.attention_calendar import approve_capture, create_calendar
    capture = {"capture_key": "capture:" + plan["fingerprint"], "source_id": "proactive:" + plan["fingerprint"],
               "title": plan["title"], "day": plan["day"], "timezone": plan["timezone"],
               "start_ts": plan["start_ts"], "duration_min": plan["duration_min"], "all_day": False,
               "automatic": False}
    result = await create_calendar(capture, approval=approve_capture(capture),
                                   source_current=lambda: _still_ok() is None)
    return result if isinstance(result, dict) else {"ok": False, "status": "unknown", "error": "unreadable"}


def _reminder_problem() -> str | None:
    from service.proactive.executor import _reminder_problem as policy
    return _still_ok() or policy()


async def create_reminder(plan: dict) -> dict:
    """The verified native reminder write the attention capture's live mode
    uses: the outbox's create_reminder with a policy check just before dispatch."""
    if not _app_connected_now():
        return {"ok": False, "status": "blocked", "error": "The Wisp app isn't connected"}
    if problem := _reminder_problem():
        return {"ok": False, "status": "blocked", "error": problem}
    from service.assistant.outbox import request
    made: list[dict] = []
    result: dict = {}
    for when in plan.get("alerts") or [plan["due_ts"]]:
        result = await request("create_reminder", {"title": plan["title"][:200], "due_ts": float(when),
                                                   "commitment_kind": "reminder"},
                               before_dispatch=_reminder_problem)
        result = result if isinstance(result, dict) else {"ok": False, "status": "unknown", "error": "unreadable"}
        if result.get("ok") is not True or not str(result.get("source_id") or "").strip():
            # One that landed before this miss is still Wisp's: undo finds it.
            return {**result, "ok": False, "reminders": made}
        made.append({"source_id": str(result["source_id"]).strip(), "due_ts": float(when),
                     "title": result.get("title") if isinstance(result.get("title"), str) else plan["title"]})
    return {**result, "ok": True, "source_id": made[0]["source_id"], "title": made[0]["title"], "reminders": made}


async def delete_event(record: dict) -> dict:
    """The verified one-occurrence cancel: the app checks the event is still the
    one Wisp made (identifier, title, start, no invitees), removes it, and
    reads back that it is gone."""
    from service.safety import read_only
    if read_only():
        return {"ok": False, "status": "blocked", "error": "View-only mode: Calendar can't be changed."}
    from service.assistant.outbox import request
    return await request("cancel_calendar_event", {
        "source_id": record["source_id"], "expected_title": record["title"],
        "expected_start_ts": float(record["start_ts"]), "invitee_count": 0})


async def delete_reminder(record: dict) -> dict:
    """The verified reminder delete: exact identifier, title and due time, read
    back as absent. A deadline's every alert, each by its own identifier."""
    from service.proactive.executor import _reminder_problem as policy
    if problem := policy():
        return {"ok": False, "status": "blocked", "error": problem}
    from service.assistant.outbox import request
    made = record.get("reminders") or [{"source_id": record["source_id"], "title": record["title"],
                                         "due_ts": record["due_ts"]}]
    result: dict = {"ok": True, "status": "succeeded"}
    for one in made:
        result = await request("delete_reminder", {
            "source_id": one["source_id"], "expected_title": one.get("title") or record["title"],
            "expected_due_ts": float(one["due_ts"])}, before_dispatch=policy)
        if not isinstance(result, dict) or result.get("ok") is not True:
            return result if isinstance(result, dict) else {"ok": False, "status": "unknown"}
    return result


WRITERS = {"calendar": create_event, "reminder": create_reminder}
UNDOERS = {"calendar": delete_event, "reminder": delete_reminder}


# --- the cards ---------------------------------------------------------------------------------


def _clock(dt: datetime) -> str:
    return dt.strftime("%-I") if dt.minute == 0 else dt.strftime("%-I:%M")


def when_words(record: dict, tz: str) -> str:
    """"Wed, Oct 28, 10–11 AM" for an event; "Fri, Oct 30 at 11:59 PM" for a reminder."""
    zone = ZoneInfo(tz)
    if record["kind"] == "calendar":
        start = datetime.fromtimestamp(record["start_ts"], zone)
        end = start + timedelta(minutes=int(record.get("duration_min") or DEFAULT_EVENT_MIN))
        day = start.strftime("%a, %b %-d")
        if end.date() != start.date():
            return f"{day}, {_clock(start)} {start:%p}–{end:%a} {_clock(end)} {end:%p}"
        if start.strftime("%p") == end.strftime("%p"):
            return f"{day}, {_clock(start)}–{_clock(end)} {end:%p}"
        return f"{day}, {_clock(start)} {start:%p}–{_clock(end)} {end:%p}"
    due = datetime.fromtimestamp(record["due_ts"], zone)
    return f"{due:%a, %b %-d} at {_clock(due)} {due:%p}"


def _received_words(ts: float, now: float, tz: str) -> str:
    zone = ZoneInfo(tz)
    day, today = datetime.fromtimestamp(ts, zone).date(), datetime.fromtimestamp(now, zone).date()
    if day == today:
        return "today"
    if (today - day).days == 1:
        return "yesterday"
    if 0 < (today - day).days < 7:
        return day.strftime("%a")
    return day.strftime("%b %-d")


def _alert_words(times: list[float], tz: str) -> str:
    """"Fri, Oct 30 at 9 AM and 8:59 PM": the second time alone on the same day."""
    zone = ZoneInfo(tz)
    out = []
    for i, t in enumerate(times):
        moment = datetime.fromtimestamp(t, zone)
        same = i and datetime.fromtimestamp(times[i - 1], zone).date() == moment.date()
        out.append(f"{_clock(moment)} {moment:%p}" if same else f"{moment:%a, %b %-d} at {_clock(moment)} {moment:%p}")
    return " and ".join(out)


def done_card(record: dict, now: float) -> dict:
    tz = record.get("timezone") or "UTC"
    first = _first_name(record.get("who", ""))
    noun = "email" if record.get("source") == "mail" else "text"
    origin = f"{first}'s {noun}" if first else f"an {noun}" if noun == "email" else "a text"
    quote = record.get("quote") or ""
    when = when_words(record, tz)
    if record["kind"] == "calendar":
        meta, title = "ADDED · CALENDAR", f"Added {record['title']} to your calendar"
        did = f"I added it for {when}."
    else:
        meta, title = "ADDED · REMINDER", f"Added {record['title']} to your reminders"
        alerts = [r.get("due_ts") for r in record.get("reminders") or [] if isinstance(r, dict)]
        if len(alerts) > 1:
            did = f"It's due {when}; I'll remind you {_alert_words(alerts, tz)}."
        else:
            did = f"I set a reminder for {when}."
    body = (f"From {origin} {_received_words(record['received_ts'], now, tz)}: “{quote}” {did}" if quote
            else f"From {origin}. {did}")
    return {
        "id": "card:" + _digest(record["fingerprint"], "undo_add"),
        "meta": meta,
        "title": guard.clean(title, 110),
        "body": body,
        "why": "It was time-sensitive and still waiting on you, so Wisp added it. Undo removes exactly what Wisp added.",
        "account": record.get("account", ""),
        "sources": [{"kind": record.get("source", ""), "ref": "autoadd:" + str(record.get("item_id") or ""),
                     "who": record.get("who", ""), "quote": quote}],
        "action": {"method": "undo_add", "button": "Undo?", "draft": "", "recipient": "", "event": None,
                   "envelope": {"kind": "undo_add"}, "to": [], "cc": [], "subject": "", "reply_all": False,
                   "reply_ref": None, "open_url": "", "open_label": "", "record": record["fingerprint"],
                   "added": record["kind"]},
        "todo_id": None,
        "fingerprints": [],
        "due_ts": record.get("start_ts") if record["kind"] == "calendar" else record.get("due_ts"),
        "auto_added": True,
    }


def tap_card(plan: dict, now: float) -> dict:
    """With auto-add off (or the caps reached): the ordinary tap-to-add card,
    fired by the executor's own calendar_event / reminder paths."""
    from service.proactive.prepare import card_id, todo_id
    when = when_words(plan, plan["timezone"])
    first = _first_name(plan.get("who", ""))
    noun = "email" if plan["source"] == "mail" else "text"
    origin = f"{first}'s {noun}" if first else f"an {noun}" if noun == "email" else "a text"
    if plan["kind"] == "calendar":
        method = "calendar_event"
        event = {"title": plan["title"], "start_ts": plan["start_ts"], "duration_min": plan["duration_min"],
                 "day": plan["day"], "timezone": plan["timezone"], "all_day": False}
        envelope, button = {"kind": "calendar", **event}, "Add to calendar?"
        did = f"I can add it for {when}."
    else:
        method, event = "reminder", None
        envelope, button = {"kind": "reminder", "title": plan["title"], "due_ts": plan["due_ts"]}, "Set a reminder?"
        did = f"I can remind you {when}."
    zone = ZoneInfo(plan["timezone"])
    at = plan.get("start_ts") or plan.get("due_ts")
    return {
        "id": card_id(plan["item_fingerprint"], method),
        "meta": f"{datetime.fromtimestamp(at, zone):%a}".upper() + (" · MAIL" if plan["source"] == "mail" else " · TEXTS"),
        "title": guard.clean(plan["title"], 90),
        "body": f"From {origin} {_received_words(plan['received_ts'], now, plan['timezone'])}: “{plan['quote']}” {did}",
        "why": "Time-sensitive and still waiting on you.",
        "account": plan.get("account", ""),
        "sources": [{"kind": plan["source"], "ref": plan["item_id"], "who": plan.get("who", ""),
                     "quote": plan["quote"]}],
        "action": {"method": method, "button": button, "draft": "", "recipient": "", "event": event,
                   "envelope": envelope, "to": [], "cc": [], "subject": "", "reply_all": False, "reply_ref": None,
                   "open_url": "", "open_label": ""},
        "todo_id": todo_id(plan["item_id"]),
        "fingerprints": [plan["item_fingerprint"]],
        "due_ts": at,
    }


def done_cards(store, now: float) -> list[dict]:
    """The "Wisp did this" cards still worth showing: verified adds, not undone
    or dismissed, under three days old and not yet past."""
    decided = store.decisions()
    gone = set(decided["dismissed"]) | set(decided["fired"])
    out = []
    for record in sorted(store.auto_added().values(), key=lambda r: -float(r.get("at") or 0)):
        if not isinstance(record, dict) or record.get("status") != "added" or not record.get("title"):
            continue
        at = record.get("start_ts") if record.get("kind") == "calendar" else record.get("due_ts")
        if type(at) not in (int, float) or at <= now or now - float(record.get("at") or 0) > DONE_SHOWN_S:
            continue
        card = done_card(record, now)
        if card["id"] not in gone:
            out.append(card)
    return out


_TAP_METHODS = ("calendar_event", "reminder", "add_to_day", "block_time")
_REPLY_METHODS = ("email_reply", "text_reply")


def merge_cards(done: list[dict], tap: list[dict], cards: list[dict], added_items: set[str],
                limit: int) -> list[dict]:
    """Done cards first, ahead of alerts, counting toward the cap. A tap card
    for an item Wisp already added goes (the done card says it), and a tap card
    of ours steps aside for a calendar/reminder card prepare already made."""
    def ref(card):
        return str(((card.get("sources") or [{}])[0] or {}).get("ref") or "")
    kept = [c for c in cards if not (ref(c) in added_items and c["action"].get("method") in _TAP_METHODS)]
    taken = {ref(c) for c in kept if c["action"].get("method") in _TAP_METHODS}
    ours = [c for c in tap if ref(c) not in taken and c["id"] not in {k["id"] for k in kept}]
    alerts = [c for c in kept if c.get("critical")]
    replies = [c for c in kept if c["action"].get("method") in _REPLY_METHODS and not c.get("critical")]
    rest = [c for c in kept if c not in alerts and c not in replies]
    return (done + alerts + replies + ours + rest)[:limit]


# --- one pass, from the background cycle ------------------------------------------------------


async def step(survivors: list[dict], sched: dict, *, now: float, tz: str, store, abort,
               writers: dict | None = None, existing=None, is_enabled=None, debug: list | None = None) -> dict:
    """Plan every eligible item, write up to the caps, record each outcome.

    Returns {"tap": tap-to-add cards (auto-add off, or over the caps),
    "added": fingerprints added this pass, "items": item ids Wisp has added}.
    `writers`, `existing` and `is_enabled` let tests replace the native writes,
    the synced schedule and the setting; production uses the verified paths."""
    writers = writers or WRITERS
    on = (is_enabled or enabled)()
    rows = list(sched.get("calendar") or []) + list(sched.get("reminders") or [])
    rows += list((existing or default_existing)(now) or [])
    decided = store.decisions()
    gone = set(decided["dismissed"]) | set(decided["fired"])
    suppressed = set(decided["suppressed"])
    records = store.auto_added()
    # An undone add retires its whole thread: a later message with a nudged
    # time, or the same plan as a reminder, is never added again.
    retired = {r.get("item_id") for r in records.values()
               if isinstance(r, dict) and r.get("status") in ("undone", "undo_unknown") and r.get("item_id")}
    today = store.auto_added_since(now - DAY_S)
    from service.proactive.prepare import card_id
    tap, added = [], []
    attempts = 0
    seen: set[str] = set()

    def note(item, reason):
        if debug is not None:
            debug.append({"who": item.get("who", ""), "item": item.get("id"), "auto_add": reason})

    for survivor in sorted(survivors, key=lambda s: -(s["item"].get("received_ts") or 0)):
        item, result = survivor["item"], survivor.get("result")
        plan, reason = plan_for(item, result, now=now, tz=tz)
        if plan is None:
            note(item, reason)
            continue
        if item.get("id") in seen:
            continue
        seen.add(item["id"])
        if item.get("id") in retired:
            note(item, "undone before")
            continue
        fp = plan["fingerprint"]
        record = records.get(fp) or {}
        if record.get("status") in _RECORD_STATES_BLOCKING or int(record.get("attempts") or 0) >= MAX_ATTEMPTS:
            note(item, f"already {record.get('status') or 'tried'}")
            continue
        # The user waved this item away, or already tapped its card.
        if plan["item_fingerprint"] in suppressed or any(
                card_id(plan["item_fingerprint"], m) in gone
                for m in ("calendar_event", "reminder", "add_to_day", "text_reply", "email_reply", "read")):
            note(item, "dismissed or handled")
            continue
        if (dup := already_there(plan, rows, tz)) is not None:
            note(item, f"already on {'Calendar' if dup.get('source') == 'calendar' else 'your list'}")
            continue
        if not on or attempts >= MAX_PER_CYCLE or today + attempts >= MAX_PER_DAY:
            tap.append(tap_card(plan, now))
            note(item, "tap to add" if not on else "cap reached: tap to add")
            continue
        if abort():
            note(item, "a turn started")
            break
        attempts += 1
        write = writers["calendar" if plan["kind"] == "calendar" else "reminder"]
        try:
            outcome = await write(plan)
        except Exception as exc:  # noqa: BLE001 — after a possible dispatch the outcome is unknown
            outcome = {"ok": False, "status": "unknown", "error": type(exc).__name__}
        outcome = outcome if isinstance(outcome, dict) else {"ok": False, "status": "unknown"}
        verified = outcome.get("ok") is True and isinstance(outcome.get("source_id"), str) \
            and bool(outcome["source_id"].strip())
        status = "added" if verified else "unknown" if outcome.get("status") == "unknown" else \
            "blocked" if outcome.get("status") == "blocked" else "failed"
        _audit("proactive_auto_add", tool="add_calendar_event" if plan["kind"] == "calendar" else "add_reminder",
               source="proactive_auto_add", status=status, ok=verified,
               args={"fingerprint": fp, "title_sha256": hashlib.sha256(plan["title"].encode()).hexdigest()[:16],
                     "when_ts": plan.get("start_ts") or plan.get("due_ts")})
        if status == "blocked":
            # Nothing was dispatched (the app away, view-only, the policy, a
            # turn): a later cycle may try again; the attempt cap still binds.
            note(item, "blocked: " + str(outcome.get("error") or ""))
            store.auto_add_put(fp, {**_record(plan, now), "status": "blocked",
                                    "attempts": int(record.get("attempts") or 0)})
            continue
        entry = {**_record(plan, now), "status": status, "attempts": int(record.get("attempts") or 0) + 1}
        if isinstance(outcome.get("reminders"), list) and outcome["reminders"]:
            entry["reminders"] = [r for r in outcome["reminders"] if isinstance(r, dict)]
        if verified:
            entry["source_id"] = outcome["source_id"].strip()
            if plan["kind"] == "reminder" and isinstance(outcome.get("title"), str) and outcome["title"].strip():
                entry["title"] = outcome["title"]        # what Reminders read back: undo matches it exactly
            added.append(fp)
            note(item, "added")
        else:
            note(item, f"write {status}")
        store.auto_add_put(fp, entry)
    items = {r.get("item_id") for r in store.auto_added().values()
             if isinstance(r, dict) and r.get("status") == "added" and r.get("item_id")}
    return {"tap": tap, "added": added, "items": {"autoadd:" + i for i in items} | items}


def _record(plan: dict, now: float) -> dict:
    keep = ("kind", "title", "quote", "item_id", "source", "who", "account", "received_ts", "timezone",
            "start_ts", "duration_min", "day", "due_ts", "fingerprint", "item_fingerprint")
    return {**{k: plan[k] for k in keep if k in plan}, "at": now}


# --- undo (the executor's undo_add) -------------------------------------------------------------


async def undo(card: dict, store, *, undoers: dict | None = None, now: float | None = None) -> dict:
    """Delete exactly what Wisp added for this card, by the identifier in the
    store's record. Verified, or nothing changes; an unconfirmed delete is
    never retried blindly (the record says so)."""
    now = time.time() if now is None else now
    fp = (card.get("action") or {}).get("record")
    record = store.auto_added().get(fp) if isinstance(fp, str) else None
    if not isinstance(record, dict) or record.get("status") != "added" or not record.get("source_id"):
        return {"ok": False, "status": "blocked", "error": "Wisp has no record of adding this, so nothing was removed."}
    call = {**UNDOERS, **(undoers or {})}["calendar" if record.get("kind") == "calendar" else "reminder"]
    try:
        result = await call(record)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "status": "unknown", "error": type(exc).__name__}
    result = result if isinstance(result, dict) else {"ok": False, "status": "unknown"}
    ok = result.get("ok") is True
    _audit("proactive_auto_add_undo", tool="cancel_calendar_event" if record.get("kind") == "calendar"
           else "delete_reminder", source="proactive_card", ok=ok,
           status="succeeded" if ok else result.get("status", "failed"), args={"fingerprint": fp})
    if ok:
        store.auto_add_put(fp, {**record, "status": "undone", "undone_at": now})
        where = "your calendar" if record.get("kind") == "calendar" else "your reminders"
        return {"ok": True, "status": "succeeded", "message": f"Removed it from {where}"}
    if result.get("status") == "unknown":
        store.auto_add_put(fp, {**record, "status": "undo_unknown"})
        return {"ok": False, "status": "unknown",
                "error": "Wisp couldn't confirm it was removed. Check before trying again."}
    return {"ok": False, "status": result.get("status") or "failed",
            "error": str(result.get("error") or "That didn't go through.")}

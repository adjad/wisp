"""Completing reminders, and finding a gap in the day.

`complete_reminder` vs `cancel_event` — WHY BOTH
------------------------------------------------
`cancel_event` DELETES. Marking something done is a different act with a
different meaning: the commitment happened, and the record of it should survive.
Routing "I've done that" to a delete tool destroys the history the store exists
to keep, and it is not recoverable. The store already distinguishes them —
`set_status(cid, "done")` against `delete(cid)` — so this is exposing a
distinction that existed rather than inventing one.

`find_free_time` computes gaps from the same commitment store `get_upcoming`
reads, so it agrees with what the user was just told about their day. It does
not consult EventKit directly: that would be a second source of truth for the
same question, and the two would drift the moment a sync lagged.
"""
from __future__ import annotations

from service.assistant.verified_reminders import expected_date_fields

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from service.assistant.store import AssistantStore
from service.tools.registry import register

_store = AssistantStore()

# Capabilities live only in this process and originating request. Losing the
# process loses authority; a saved transcript is never an execution queue.
_MEETING_CAPACITY = 256
_MEETING_TTL_SECONDS = 600.0
_MEETING_TOKEN = re.compile(r"wisp_meeting_cap_[0-9a-f]{64}", re.I)
_meeting_capabilities: dict[str, dict] = {}
_meeting_lock = threading.Lock()
_meeting_approval = ContextVar("private_meeting_approval", default=None)
_capability_clock = time.monotonic


def redact_meeting_capabilities(value):
    """Redact recognizable capabilities, including JSON keys, without changing clean objects."""
    if isinstance(value, str):
        if not _MEETING_TOKEN.search(value):
            return value  # Preserve trusted display-only string subclasses.
        return _MEETING_TOKEN.sub(lambda m: "[private meeting capability " +
            hashlib.sha256(m.group(0).encode()).hexdigest()[:12] + "]", value)
    if isinstance(value, dict):
        pairs = [(redact_meeting_capabilities(k), redact_meeting_capabilities(v)) for k, v in value.items()]
        if all(k2 is k and v2 is v for (k, v), (k2, v2) in zip(value.items(), pairs)):
            return value
        return dict(pairs)
    if isinstance(value, (list, tuple)):
        items = [redact_meeting_capabilities(item) for item in value]
        if all(item is original for item, original in zip(items, value)):
            return value
        return tuple(items) if isinstance(value, tuple) else items
    return value


def _meeting_context():
    from service.memory.capture import current_source
    source = current_source.get()
    if (not isinstance(source, dict) or source.get("source_type") != "user_request"
            or any(not isinstance(source.get(key), str) or not source[key]
                   for key in ("source_id", "session_id", "quote"))):
        raise ValueError("Private meeting creation needs its originating host request; request a new preview.")
    if not _private_block_request(source["quote"]):
        raise ValueError("The human request must explicitly authorize creating a private Calendar block; request a new preview.")
    return (source["source_id"], source["session_id"],
            hashlib.sha256(source["quote"].encode()).hexdigest())


def _private_block_request(quote):
    """Bounded explicit private-create intent; names and quoted source text are not authority."""
    text = re.sub(r'"[^"\n]*"|(?<!\w)\x27[^\x27\n]*\x27(?!\w)|`[^`\n]*`|“[^”\n]*”|‘[^’\n]*’', " ", quote)
    write = r"(?:create|add|schedule|book|reserve|put|make|block)"
    if re.search(rf"\b(?:do\s+not|don['’]t|never)\s+(?:actually\s+)?{write}\b", text, re.I):
        return False
    # Explicitly declining invitations is compatible with a private block.
    invitation_text = re.sub(r"\b(?:do\s+not|don['’]t|never|without|no)\s+(?:send\s+)?(?:any\s+)?(?:attendee\s+invitations?|invites?|invitations?|inviting|attendees?|invitees?|guests?)\b", " ", text, flags=re.I)
    if re.search(r"\b(?:invites?|invitations?|inviting|invited|invitees?|attendees?|guests?)\b", invitation_text, re.I):
        return False
    command = (rf"(?:^\s*(?:(?:can|could|would|will)\s+you\s+|i\s+want\s+you\s+to\s+)?(?:please\s+)?"
               rf"|\b(?:and|then)\s+(?:please\s+)?)({write})\b[^.!?\n]{{0,160}}"
               r"\bprivate\s+(?:(?:personal|calendar)\s+){0,2}block\b")
    return bool(re.search(command, text, re.I))


def _meeting_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _prune_meeting_capabilities(now):
    # Called under the lock. Consumed records stay as tombstones until expiry.
    for token in list(_meeting_capabilities):
        if _meeting_capabilities[token]["expires"] <= now:
            del _meeting_capabilities[token]


def _meeting_record(args, *, claim=False):
    from service.calendar_planning import private_meeting_arguments
    frozen = private_meeting_arguments(args)
    context = _meeting_context()
    canonical = _meeting_json(frozen)
    with _meeting_lock:
        _prune_meeting_capabilities(_capability_clock())
        record = _meeting_capabilities.get(frozen["capability_token"])
        if (record is None or record["consumed"] or record["context"] != context
                or record["arguments"] != canonical):
            raise ValueError("Private meeting proposal is invalid, changed, expired or already used; request a new preview.")
        if claim:
            if _meeting_approval.get() != (context, canonical):
                raise ValueError("Private meeting creation requires exact host approval; nothing was created.")
            record["consumed"] = True
        return frozen, record.copy()


def private_meeting_review(args) -> str:
    """Host preflight: never offer an approvable card for invalid proposal args."""
    frozen, record = _meeting_record(args)
    result = record["result"]
    reference = result.get("reference")
    text = (f"Create a private Calendar block: {frozen['title']}\n"
            f"Start: {frozen['when_iso']} ({record['timezone']})\n"
            f"End: {result['end_iso']}\nDuration: {frozen['duration_min']} minutes\n")
    if reference:
        text += (f"After: {reference['title']} [occurrence {reference['id']}]\n"
                 f"Reference ends: {dt.datetime.fromtimestamp(reference['end_ts'], ZoneInfo(record['timezone'])).isoformat()}\n")
    return text + "Private block only. No attendees will be invited; their availability is unverified."


@contextmanager
def approved_private_meeting(args):
    """Only the host enters this after approving its frozen exact action snapshot."""
    frozen, record = _meeting_record(args)
    handle = _meeting_approval.set((record["context"], _meeting_json(frozen)))
    try:
        yield
    finally:
        _meeting_approval.reset(handle)


@register(
    "plan_meeting",
    "Read-only private Calendar block proposal with conflict checks. Supply an exact local when_iso "
    "OR after_event_title plus event_period to use a uniquely selected event's real stored end. "
    "Never infer an end from get_upcoming. offset_minutes is an explicit elapsed delay after that end. "
    "Does not create an event, invite attendees, or verify anyone else's availability. "
    "If ready and the user requests a private block, pass the complete commit_args to commit_private_meeting for explicit approval. "
    "Attendee invitations are unsupported; ask before substituting a private block for an invitation.",
    {"type": "object", "properties": {
        "title": {"type": "string"}, "duration_min": {"type": "integer"},
        "when_iso": {"type": "string", "description": "Exact local ISO start, not a natural-language phrase."},
        "after_event_title": {"type": "string"},
        "event_period": {"type": "string", "description": "today/tomorrow, this/next week/month, or YYYY-MM-DD."},
        "expected_event_id": {"type": "string", "description": "Optional exact occurrence ID from prior Calendar evidence."},
        "offset_minutes": {"type": "integer", "description": "Explicit delay after the reference end, 0–1440. Default 0."}},
     "required": ["title", "duration_min"]},
    category="assistant_read",
)
async def plan_meeting(title: str, duration_min: int, when_iso: str = "",
                       after_event_title: str = "", event_period: str = "",
                       expected_event_id: str = "", offset_minutes: int = 0) -> str:
    inputs = dict(title=title, duration_min=duration_min, when_iso=when_iso,
                  after_event_title=after_event_title, event_period=event_period,
                  expected_event_id=expected_event_id, offset_minutes=offset_minutes)
    try:
        result, evidence, timezone = await _meeting_proposal(**inputs)
        if result["status"] == "ready":
            try:
                context = _meeting_context()
            except ValueError as exc:
                # Read-only callers without a host request may still inspect a
                # proposal, but cannot acquire authority to create it.
                result["commit_unavailable"] = str(exc)
            else:
                with _meeting_lock:
                    now = _capability_clock()
                    _prune_meeting_capabilities(now)
                    if len(_meeting_capabilities) >= _MEETING_CAPACITY:
                        raise ValueError("Private meeting preview capacity reached; wait for previews to expire.")
                    token = "wisp_meeting_cap_" + secrets.token_hex(32)
                    if token in _meeting_capabilities:
                        raise ValueError("Private meeting preview could not be issued; request a new preview.")
                    arguments = {"capability_token": token, **result["private_block"]}
                    _meeting_capabilities[token] = {
                        "context": context, "arguments": _meeting_json(arguments),
                        "result": json.loads(_meeting_json(result)), "evidence": evidence,
                        "inputs": inputs.copy(), "timezone": timezone,
                        "expires": now + _MEETING_TTL_SECONDS, "consumed": False}
                result["commit_args"] = arguments
        prefix = "Ask the user: " if result["status"] in {"needs_input", "conflict"} else "Private meeting proposal: "
        return prefix + json.dumps(result, ensure_ascii=False)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        return f"(error: {redact_meeting_capabilities(str(exc))}; no event created.)"


async def _meeting_proposal(title, duration_min, when_iso="", after_event_title="", event_period="",
                            expected_event_id="", offset_minutes=0):
    """Build fresh evidence without issuing a capability or creating an event."""
    from service.calendar_planning import exact_start, plan_private_block, select_occurrence, unique_calendar_rows
    from service.assistant.today import day_bounds, source_health
    from service.assistant.sync_status import ensure_sources
    from service.tools.timeranges import resolve_span

    try:
        if any(not isinstance(value, str) for value in
               (title, when_iso, after_event_title, event_period, expected_event_id)):
            raise ValueError("Meeting text fields must be strings.")
        if (not title.strip() or len(title) > 500 or type(duration_min) is not int
                or not 1 <= duration_min <= 1440):
            raise ValueError("Provide a title and duration of 1–1440 whole minutes.")
        if any(_MEETING_TOKEN.search(value) for value in (title, when_iso, after_event_title, event_period, expected_event_id)):
            raise ValueError("Private meeting capability cannot appear in event content or source queries.")
        if type(offset_minutes) is not int or not 0 <= offset_minutes <= 1440:
            raise ValueError("offset_minutes must be 0–1440 whole minutes.")
        if bool(when_iso.strip()) == bool(after_event_title.strip()):
            return {"status": "needs_input", "reason": "Provide one exact start or one Calendar event-end reference; no event created."}, None, None
        if when_iso and (event_period or expected_event_id or offset_minutes):
            raise ValueError("An exact start cannot also specify an event reference or offset.")
        timezone = _availability_timezone()
        if timezone is None:
            raise ValueError("Local IANA timezone is unavailable; no slot verified.")
        zone = ZoneInfo(timezone)
        bounds = None
        if after_event_title:
            if not re.fullmatch(r"today|tomorrow|(?:this|next)\s+(?:week|month)|\d{4}-\d{2}-\d{2}", event_period.strip(), re.I):
                return {"status": "needs_input", "reason": "Provide the reference event's exact day or bounded week/month; no event created."}, None, timezone
        else:
            start_ts = exact_start(when_iso, timezone)
        readiness = await ensure_sources(("calendar",))
        if not any(s.get("id") == "calendar" and s.get("state") == "ready" for s in readiness.get("sources", [])):
            raise ValueError("Current Calendar evidence is unavailable; no slot verified.")
        # A successful refresh can complete after the request clock, including
        # across midnight. Freshness and relative periods share this new clock.
        now = dt.datetime.now(zone)
        if after_event_title:
            lo, hi, _ = resolve_span(event_period, now=now.replace(tzinfo=None), forward=True)
            bounds = (lo, hi)

        snapshots = {}
        calendar_receipt = None
        def read_day(day):
            nonlocal calendar_receipt
            key = day.isoformat()
            if key not in snapshots:
                snapshot = _store.today_snapshot(key, timezone)
                sources = snapshot.get("sources", {})
                # Each day is a coherent store transaction, but multiple days
                # must also share one receipt: inserts/deletes need not leave
                # a duplicate ID that row comparison could detect.
                receipt = json.dumps(sources.get("calendar", {}), sort_keys=True, allow_nan=False)
                if calendar_receipt is not None and receipt != calendar_receipt:
                    raise ValueError("Calendar sync changed between snapshots; refresh before planning.")
                health = source_health(sources, now.timestamp(), day_bounds(key, timezone))["calendar"]
                if not health["ready"]:
                    raise ValueError("Calendar evidence is incomplete or stale: " + health["reason"])
                calendar_receipt = receipt
                snapshots[key] = snapshot
            return snapshots[key]["commitments"]

        reference = None
        rows = []
        if bounds:
            day = dt.datetime.fromtimestamp(bounds[0], zone).date()
            stop = dt.datetime.fromtimestamp(bounds[1], zone).date()
            while day < stop:
                rows.extend(read_day(day))
                day += dt.timedelta(days=1)
            selected = select_occurrence(rows, after_event_title,
                                         expected_id=expected_event_id, bounds=bounds)
            if selected["status"] != "selected":
                return selected, None, timezone
            reference = selected["reference"]
            start_ts = reference["end_ts"] + offset_minutes * 60
        # Read every touched local day, including overnight target intervals.
        day = dt.datetime.fromtimestamp(start_ts, zone).date()
        last = dt.datetime.fromtimestamp(start_ts + duration_min * 60 - 0.001, zone).date()
        while day <= last:
            rows.extend(read_day(day))
            day += dt.timedelta(days=1)
        result = plan_private_block(rows, title=title, start_ts=start_ts,
                                    duration_min=duration_min, timezone=timezone,
                                    now_ts=now.timestamp(), reference=reference)
        if result["status"] != "ready":
            return result, None, timezone
        # Bind every relevant row and receipt without retaining a full month
        # of Calendar data for each of the bounded pending capabilities.
        evidence = hashlib.sha256(_meeting_json({"receipt": json.loads(calendar_receipt), "timezone": timezone,
            "rows": sorted(unique_calendar_rows(rows), key=lambda row: str(row["id"]))}).encode()).hexdigest()
        return result, evidence, timezone
    except (ValueError, TypeError, OverflowError, OSError):
        raise


@register(
    "commit_private_meeting",
    "Create the exact private Calendar block returned by plan_meeting, only after explicit host confirmation. "
    "Supply all four unchanged commit_args. Capabilities expire and can be used once in the originating request. "
    "Calendar evidence, the reference event and conflicts are checked again after approval. "
    "This never invites attendees or verifies their availability. Unknown outcomes must not be retried.",
    {"type": "object", "properties": {
        "capability_token": {"type": "string"}, "title": {"type": "string"},
        "when_iso": {"type": "string"}, "duration_min": {"type": "integer"}},
     "required": ["capability_token", "title", "when_iso", "duration_min"],
     "additionalProperties": False},
    category="calendar_write",
)
async def commit_private_meeting(capability_token: str, title: str, when_iso: str, duration_min: int) -> str:
    args = dict(capability_token=capability_token, title=title, when_iso=when_iso, duration_min=duration_min)
    try:
        frozen, record = _meeting_record(args, claim=True)
        # Claim before any await: concurrent/reentrant attempts cannot both
        # pass. Any failure now needs a new preview and a new approval.
        result, evidence, timezone = await _meeting_proposal(**record["inputs"])
        if (result["status"] != "ready" or evidence != record["evidence"]
                or timezone != record["timezone"] or result != record["result"]
                or _capability_clock() >= record["expires"]):
            raise ValueError("Calendar evidence or proposal changed after approval; request a new preview and approval.")
        # The trusted context must still be identical after the refresh await.
        if _meeting_context() != record["context"]:
            raise ValueError("The originating meeting request changed; request a new preview and approval.")
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        return f"(error: {redact_meeting_capabilities(str(exc))}; no event created.)"
    except Exception:
        return "(error: Calendar revalidation failed; no event created. Request a new preview and approval.)"

    from service.assistant.outbox import request as app_request
    from service.calendar_planning import exact_start
    try:
        native = await app_request("create_calendar_event", {
            "title": frozen["title"], "when_ts": exact_start(frozen["when_iso"], timezone),
            "duration_min": frozen["duration_min"], "location": ""})
    except BaseException as exc:
        # Cancellation is propagated, but the already consumed capability
        # remains unusable. A timeout/exception cannot establish no effect.
        import asyncio
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return "(error: Private Calendar creation outcome unknown; no retry. Check Calendar before creating another block.)"
    try:
        # Reuse the bridge's exact receipt contract, including status/error
        # consistency. A bare {ok: true} is never proof of creation.
        native = AssistantStore.calendar_result("create_calendar_event", native)
    except (ValueError, TypeError):
        native = None
    if (isinstance(native, dict) and native.get("ok") is True
            and native.get("status") == "succeeded"
            and isinstance(native.get("source_id"), str) and native["source_id"].strip()
            and not _MEETING_TOKEN.search(native["source_id"])):
        receipt = {"source_id": native["source_id"], "title": frozen["title"],
                   "when_iso": frozen["when_iso"], "end_iso": result["end_iso"],
                   "duration_min": frozen["duration_min"], "scope": "Private block only; no attendees invited."}
        return "Private calendar block created: " + json.dumps(redact_meeting_capabilities(receipt), ensure_ascii=False)
    if (isinstance(native, dict) and native.get("ok") is False
            and native.get("status") == "failed"):
        return "(error: Private Calendar creation failed; no retry. Request a new preview and approval.)"
    return "(error: Private Calendar creation outcome unknown; no retry. Check Calendar before creating another block.)"


def _availability_timezone() -> str | None:
    """Use the process/system IANA zone, never a PDT/+14 abbreviation."""
    zone = os.environ.get("TZ", "").lstrip(":")
    if not zone:
        parts = Path("/etc/localtime").resolve().parts
        for index, part in enumerate(parts):
            if part.startswith("zoneinfo"):
                zone = "/".join(parts[index + 1:])
                break
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return zone


def _fmt_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%-I:%M %p")


def _fmt_day(d: dt.date, today: dt.date) -> str:
    if d == today:
        return "today"
    if d == today + dt.timedelta(days=1):
        return "tomorrow"
    return d.strftime("%A %-d %b")


@register(
    "complete_reminder",
    "Mark a reminder or task DONE, matched by part of its title. Use this when "
    "the user says they've finished something — NOT cancel_event, which deletes "
    "the record entirely. If several match, it asks which rather than guessing.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": "Any distinctive part of the reminder's title."},
            "expected_id": {"type": "string",
                            "description": "optional typed-plan guard: exact visible reminder id selected before execution"},
        },
        "required": ["title"],
    },
    category="assistant_write",
    # The pronoun-only forms ("cross it off", "that's sorted") matter most and
    # are the hardest: with no noun to anchor, they sit in embedding space right
    # next to delete_path, cancel_event and archive_email, which mean something
    # materially different. Measured — without these, "that's sorted, cross it
    # off" ranked complete_reminder 11th, behind four tools that would have
    # destroyed the record instead of completing it.
    aliases=["I've done that already", "mark the dentist thing as done",
             "tick off the laundry reminder", "finished the report, check it off",
             "that one's handled now", "I took my medication",
             "cross it off my list", "that's done, sorted",
             "check that one off"],
)
async def complete_reminder(title: str, expected_id: str = "") -> str:
    needle = (title or "").strip().lower()
    if not needle:
        return "(error: complete_reminder needs part of the reminder's `title`.)"

    reminder_sources = {"manual", "reminders"}
    rows = [row for row in _store.active_future(horizon_days=365)
            if row.get("source") in reminder_sources
            or bool(reminder_sources & set(row.get("duplicate_sources") or []))]
    hits = [r for r in rows if needle in (r.get("title") or "").lower()]
    exact = [r for r in hits if " ".join((r.get("title") or "").split()).casefold()
             == " ".join(title.split()).casefold()]
    if exact:
        hits = exact
    if expected_id:
        hits = [row for row in hits
                if str(row.get("id") or "") == expected_id
                or expected_id in {str(value) for value in row.get("duplicate_ids") or []}]
    if not hits:
        if expected_id:
            return ("(error: the selected reminder changed before it could be completed; "
                    "nothing was changed.)")
        return (f"Nothing active matches {title!r}. "
                f"Ask me what's upcoming if you're not sure of the wording.")
    if len(hits) > 1:
        listed = "\n".join(f"  - {r['title']} ({_fmt_time(r['when_ts'])})" for r in hits[:8])
        return f"Several match {title!r} — which one?\n{listed}"

    _, message = await complete_commitment(hits[0])
    return message


async def complete_commitment(row: dict, store=None) -> tuple[bool, str]:
    """Mark one exact commitment done: the verified path every completion takes
    (this tool, and the Brief's Mark done, service/assistant/needs_you_api.py).

    A native reminder is completed in Reminders.app first, through the app's
    exact-identity write that reads the completion back; only a verified result
    marks it done here. ``row`` carries ``duplicate_ids`` (a collapsed read).
    Returns ``(done, message)``."""
    store = store if store is not None else _store
    group = [row] + [member for member in
                     (store.get(cid) for cid in row.get("duplicate_ids") or []) if member]
    native = [member for member in group if member.get("source") == "reminders"]
    if len(native) > 1:
        return False, "(error: several native reminders share this item; select one exact reminder before completing.)"
    if native:
        from service.assistant.outbox import request as app_request
        for member in native:
            if not member.get("source_id") or member.get("when_ts") is None:
                return False, "(error: exact Reminders identity is unavailable; nothing was marked done.)"
            result = await app_request("complete_reminder", {
                "source_id": member["source_id"], "expected_title": member["title"],
                "expected_due_ts": member["when_ts"], **expected_date_fields(member)})
            if result.get("ok") is not True:
                return False, f"(error: {result.get('error') or 'Native completion was not verified'}.)"
    ok = store.set_status(row["id"], "done")
    # Duplicates are one commitment seen through several sources (a calendar
    # event that is also a reminder). Leaving the siblings active would make the
    # item reappear on the next read, looking like the completion didn't stick.
    for dup in row.get("duplicate_ids") or []:
        store.set_status(dup, "done")
    if not ok:
        return False, f"(couldn't update {row['title']!r} — it may already be gone.)"
    return True, f"Marked “{row['title']}” done."


@register(
    "find_free_time",
    "Find open gaps in the user's schedule — for 'when am I free', 'do I have "
    "time for a call', or picking a slot for something. Looks at the same "
    "commitments get_upcoming reports. Requires fresh, complete Calendar coverage "
    "and real event ends; pinned Wisp Today tasks also block time. Reminders "
    "without duration are deadlines, not busy intervals.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer",
                     "description": "How many days ahead to consider. Default 3."},
            "period": {"type": "string", "description":
                       "Exact local range: today, tomorrow, a weekday (friday, next monday), this weekend, "
                       "this week, next week, this month, next month, a date (nov 3) or YYYY-MM-DD. "
                       "Overrides days; returns duration-specific candidate slots."},
            "minutes": {"type": "integer",
                        "description": "Minimum length of gap to report, in minutes. Default 30."},
            "day_start": {"type": "integer",
                          "description": "Earliest hour to count as available, 0-23. Default 9."},
            "day_end": {"type": "integer",
                        "description": "Latest hour, 0-23. Default 18."},
        },
    },
    category="assistant_read",
    aliases=["when am I free this week", "do I have a gap tomorrow afternoon",
             "find me an hour for the gym", "when could I fit in a call",
             "what does my afternoon look like", "am I free at 3"],
)
def find_free_time(days: int = 3, minutes: int = 30,
                   day_start: int = 9, day_end: int = 18, period: str = ""):
    # Keep the synchronous default API. It can use a current receipt but never
    # invent freshness; callers wanting a refresh use the awaitable period API.
    if not period:
        return _legacy_find_free_time(days, minutes, day_start, day_end)
    return _find_free_time_for_day(days, minutes, day_start, day_end, period)


def _availability_options(days, minutes, day_start, day_end, *, legacy=False):
    try:
        if any(isinstance(v, bool) for v in (days, minutes, day_start, day_end)):
            raise ValueError()
        days = max(1, min(14, int(days)))
        minutes = max(5, int(minutes)) if legacy else int(minutes)
        if not 1 <= minutes <= 1440:
            raise ValueError("minutes must be between 1 and 1440.")
        start = max(0, min(23, int(day_start)))
        end = max(start + 1, min(24, int(day_end)))
        return days, minutes, start, end
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(str(exc) or "days/minutes/day_start/day_end must be numbers.") from exc


def _availability_days(now, days, period):
    today = now.date()
    value = period.strip().casefold()
    if not value:
        return [today + dt.timedelta(days=n) for n in range(days)]
    if value in {"today", "tomorrow"}:
        return [today + dt.timedelta(days=value == "tomorrow")]
    if value in {"this week", "next week"}:
        first = today - dt.timedelta(days=today.weekday())
        if value == "next week":
            first += dt.timedelta(days=7)
        stop = first + dt.timedelta(days=7)
    elif value in {"this month", "next month"}:
        first = today.replace(day=1)
        if value == "next month":
            first = (first + dt.timedelta(days=32)).replace(day=1)
        stop = (first + dt.timedelta(days=32)).replace(day=1)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        first = dt.datetime.fromisoformat(value).date()
        stop = first + dt.timedelta(days=1)
    else:
        # A weekday, a weekend or a date ("friday", "this weekend", "nov 3"),
        # read forward like any schedule question.
        from service.tools.timeranges import BadPeriod, resolve_span
        try:
            lo, hi, _ = resolve_span(period, now=now.replace(tzinfo=None), forward=True)
        except BadPeriod:
            raise ValueError("period must be today, tomorrow, a weekday (friday, next monday), "
                             "this weekend, this/next week, this/next month, a date (nov 3) "
                             "or YYYY-MM-DD.") from None
        first = dt.datetime.fromtimestamp(lo).date()
        stop = dt.datetime.fromtimestamp(hi).date()
    first = max(first, today)
    if first >= stop:
        raise ValueError("the requested availability range is in the past.")
    return [first + dt.timedelta(days=n) for n in range((stop - first).days)]


def _availability_result(options, period, timezone, now):
    """One coherent Calendar generation, real intervals, no source/native effects."""
    import math
    from service.assistant.today import day_bounds, source_health, wall_time

    days, minutes, start_h, end_h = options
    zone = ZoneInfo(timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=zone)
    target_days = _availability_days(now, days, period)
    snapshots, receipt, identities = {}, None, {}
    for day in target_days:
        key = day.isoformat()
        snapshot = _store.today_snapshot(key, timezone)
        sources = snapshot.get("sources", {})
        info = sources.get("calendar", {})
        diagnostics = info.get("diagnostics") or {}
        if (info.get("available") is not True or info.get("syncing") not in (None, False)
                or type(info.get("last_sync")) not in (int, float)):
            raise ValueError("Calendar readiness receipt is invalid; no availability was verified.")
        if (info.get("complete") is False or diagnostics.get("complete") is False
                or info.get("partial") or diagnostics.get("partial")):
            raise ValueError("Calendar evidence is partial; no availability was verified.")
        health = source_health(sources, now.timestamp(), day_bounds(key, timezone))["calendar"]
        if not health["ready"]:
            raise ValueError("Calendar evidence is incomplete or stale: " + health["reason"])
        current = json.dumps(info, sort_keys=True, allow_nan=False)
        if receipt is not None and receipt != current:
            raise ValueError("Calendar sync changed between snapshots; refresh before checking availability.")
        receipt = current
        # Same-ID comparisons also detect altered rows under an unchanged
        # receipt. Distinct simultaneous events remain separate busy blocks.
        for row in snapshot.get("commitments", []):
            if row.get("source") != "calendar":
                continue
            identity = row.get("id")
            if not isinstance(identity, str) or not identity:
                raise ValueError("Calendar occurrence identity is missing; refresh Calendar.")
            payload = json.dumps(row, sort_keys=True, allow_nan=False)
            if identity in identities and identities[identity] != payload:
                raise ValueError("Calendar changed between snapshots; refresh before checking availability.")
            identities[identity] = payload
        snapshots[key] = snapshot

    def number(value):
        return type(value) in (int, float) and math.isfinite(value)

    def interval(row, lo, hi):
        if row.get("status") in {"done", "dismissed", "cancelled", "declined"}:
            return None
        if row.get("status") not in (None, "active", "tentative"):
            raise ValueError("An availability item has an unknown status; no slot was verified.")
        source = row.get("source")
        # Due timestamps do not prove an occupied interval. Calendar and
        # explicit manual events, plus pinned tasks below, provide durations.
        if source == "reminders":
            return None
        if source == "manual" and row.get("kind") not in {"event", "meeting"}:
            if row.get("kind") not in {"reminder", "assignment", "exam", "task", "todo"}:
                raise ValueError("A manual item has an unknown kind; no slot was verified.")
            return None
        if source not in {"calendar", "manual", "today"}:
            raise ValueError("An availability item has an unknown source; no slot was verified.")
        begin, finish = row.get("when_ts"), row.get("end_ts")
        if not number(begin):
            raise ValueError("Calendar has an invalid start; no availability was verified.")
        if begin >= hi:
            return None
        if not number(finish) or finish < begin or (row.get("all_day") and finish == begin):
            raise ValueError("Calendar has items with missing or invalid ends; no availability was verified.")
        a, b = max(begin, lo), min(finish, hi)
        return (a, b) if a < b else None

    need, out = minutes * 60, []
    for day in target_days:
        key = day.isoformat()
        # wall_time uses the named IANA zone and rejects folds/gaps, including
        # midnight/end=24. Durations below use epoch arithmetic across DST.
        lo = max(wall_time(key, timezone, start_h * 60), now.timestamp())
        hi = wall_time(key, timezone, end_h * 60)
        snapshot = snapshots[key]
        rows = list(snapshot.get("commitments", []))
        for task in snapshot.get("tasks", []):
            if task.get("status") not in (None, "active") or task.get("pinned_start") is None:
                continue
            begin, duration = task["pinned_start"], task.get("duration_minutes")
            if not number(begin) or type(duration) is not int or not 1 <= duration <= 1440:
                raise ValueError("A pinned task has an invalid interval; no slot was verified.")
            rows.append({"when_ts": begin, "end_ts": begin + duration * 60, "source": "today"})
        blocks = [block for row in rows if (block := interval(row, lo, hi)) is not None]
        if lo >= hi:
            continue
        cursor, gaps = lo, []
        for begin, finish in sorted(blocks):
            if begin > cursor and begin - cursor >= need:
                gaps.append((cursor, begin))
            cursor = max(cursor, finish)
        if hi - cursor >= need:
            gaps.append((cursor, hi))
        spans = []
        for begin, finish in gaps:
            selected_end = begin + need if period else finish
            a, b, available_end = (dt.datetime.fromtimestamp(t, zone) for t in (begin, selected_end, finish))
            spans.append(f"{a:%-I:%M %p}–{b:%-I:%M %p} ({a.isoformat()} → {b.isoformat()}; free until {available_end.isoformat()})")
        if spans:
            label = day.strftime("%A, %Y-%m-%d") if period else _fmt_day(day, now.date()) + f" ({key})"
            out.append(f"  {label}: " + ", ".join(spans))
    scope = (target_days[0].isoformat() if len(target_days) == 1 else
             f"{target_days[0].isoformat()} through {target_days[-1].isoformat()}")
    qualification = ("\nScope: user Calendar, explicit manual events and pinned Wisp tasks only. Reminders without durations are deadlines, not busy intervals. "
        "All active Calendar entries count as busy, including tentative or response-unknown entries; "
        "attendee responses/free-busy flags and other people's availability are unverified. "
        "These are candidate times; a Calendar event or reminder has not been created.")
    if not out:
        return f"No matches: No gaps of {minutes}+ minutes in {scope} between {start_h}:00 and {end_h}:00 ({timezone})." + qualification
    heading = f"{minutes}-minute candidate slots" if period else f"Free for {minutes}+ minutes"
    return f"{heading}, {start_h}:00–{end_h}:00 ({timezone}):\n" + "\n".join(out) + qualification


def _legacy_find_free_time(days: int = 3, minutes: int = 30,
                   day_start: int = 9, day_end: int = 18) -> str:
    try:
        options = _availability_options(days, minutes, day_start, day_end, legacy=True)
        timezone = _availability_timezone()
        if timezone is None:
            raise ValueError("the local timezone could not be resolved; no availability was verified.")
        return _availability_result(options, "", timezone, dt.datetime.now(ZoneInfo(timezone)))
    except (ValueError, TypeError, OverflowError, OSError, KeyError, AttributeError) as exc:
        return f"(error: {exc})"


async def _find_free_time_for_day(days: int = 3, minutes: int = 30,
                         day_start: int = 9, day_end: int = 18,
                         period: str = "") -> str:
    try:
        options = _availability_options(days, minutes, day_start, day_end)
        timezone = _availability_timezone()
        if timezone is None:
            raise ValueError("the local timezone could not be resolved; no availability was verified.")
        # Validate syntax before refresh, then derive dates only after it.
        _availability_days(dt.datetime.now(ZoneInfo(timezone)), options[0], period)
        from service.assistant.sync_status import ensure_sources
        readiness = await ensure_sources(("calendar", "reminders"))
        if (not any(s.get("id") == "calendar" and s.get("state") == "ready" for s in readiness.get("sources", []))
                or any(s.get("state") != "ready" for s in readiness.get("sources", []))
                or not readiness.get("reminders_fresh", True)):
            raise ValueError("Wisp could not check current Calendar and Reminders availability. No study slot has been verified; try again after the sources finish syncing.")
        return _availability_result(options, period, timezone, dt.datetime.now(ZoneInfo(timezone)))
    except (ValueError, TypeError, OverflowError, OSError, KeyError, AttributeError) as exc:
        return f"(error: {exc})"

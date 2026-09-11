"""Deterministic compiler for typed reminder operations."""
from __future__ import annotations

from datetime import datetime, timedelta
import re

from service.reminder_intent import (
    CAPABILITY_INVENTORY_RE, REMINDER_CREATE_RE, has_unsupported_alert_clock,
    reminder_command_parts, reminder_temporal_text,
)
from service.tasks.models import SlotValue, TaskPlan, TemporalValue
from service.tasks.temporal import (
    local_timezone_name, parse_delay_seconds, parse_lead_seconds,
    resolve_named_time,
)


_NEGATED = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\s+"
    r"(?:set|add|create|make|schedule|send|give|remind|delete|remove|clear|"
    r"complete|finish|mark|check|cross|tick|cancel|update|change|rename|reschedule|move)\b",
    re.I)
_OTHER_REMINDER_OPERATION = re.compile(
    r"\b(?:delete|remove|clear|complete|finish|mark|cancel|update|change|rename|reschedule|move(?![ -]in\b))\b"
    r"[^.?!]{0,80}\breminders?\b|"
    r"\breminders?\b[^.?!]{0,80}\b(?:delete|remove|clear|complete|done|cancel|update|change|rename|reschedule)\b",
    re.I)
_COMPOUND_EFFECT = re.compile(
    r"\band\s+(?:also\s+)?(?:tell|notify|let\b[^.?!]{0,30}\bknow|text|message|email|send)\b",
    re.I)
_REFERENCE = re.compile(
    r"\b(?:before|ahead\s+of|earlier\s+than)\s+"
    r"(?P<reference>(?:my|the)\s+[a-z0-9][a-z0-9 '\-]{0,70}?"
    r"(?:date|event|appointment|meeting|reservation|move[ -]?in))"
    r"(?=\s+to\b|[,.?!]|$)", re.I)
_TRAILING_TIME = re.compile(
    r"\s+(?:(?:by|at|on|for)\s+)?(?:(?:this|next)\s+)?"
    r"(?:today|tomorrow|tonight|morning|afternoon|evening|night|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:morning|afternoon|evening|night))?"
    r"(?:\s+at\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight))?\s*[.!?]*$|"
    r"\s+(?:by|at|on|for)\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)\s*[.!?]*$",
    re.I)

# A send whose body has to be READ from somewhere stays on the workflow path,
# which owns source execution and grounded composition.  The typed outbound
# slice covers literal bodies only: the user supplied the words themselves.
_SOURCE_BACKED = re.compile(
    r"\b(?:calendars?|schedules?|agenda|summar(?:y|ies)|brief(?:ing)?|"
    r"e-?mails?|inbox|mail|weather|forecast|stocks?|tickers?|news|headlines?|"
    r"reminders?|notes?|what'?s\s+on|my\s+day)\b", re.I)
_POLITE = (r"(?:(?:hey|hi|ok|okay|please|can\s+you|could\s+you|would\s+you|"
           r"i\s+need\s+you\s+to|go\s+ahead\s+and)[,\s]+)*")
_WHO = r"[A-Za-z0-9'’.\-+@_]+(?:\s+[A-Za-z0-9'’.\-+@_]+){0,2}"
# Pre-introducer times are delivery instructions. Unquoted trailing times are
# preserved for an explicit interpretation question by outbound_language.
_WHEN_PHRASE = (
    r"in\s+(?:\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"fifteen|twenty|thirty|forty|sixty)\s*"
    r"(?:minutes?|mins?|hours?|hrs?|days?|weeks?)|"
    r"(?:at|on|by)?\s*(?:this|next|tomorrow|tonight|later\s+today)?\s*"
    r"(?:\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|noon|midnight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening|night)"
    r"(?:\s+at\s+(?:\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|noon|midnight))?")
# The ordinary email-send compiler must not consume reply/forward requests.
# Explicit email replies have their own source-resolution path below.
_REPLY_INTENT = re.compile(
    r"\b(?:repl(?:y|ies)|respond(?:\s+to)?|forward|fwd)\b", re.I)
_BODY_INTRO = (r"saying|that\s+says|and\s+say|to\s+say|"
               r"and\s+tell\s+(?:them|him|her)|that|:")
# An explicit body introducer lets the recipient run to several words.
_MESSAGE_SEND_INTRO = re.compile(
    rf"^\s*{_POLITE}"
    rf"(?:send\s+(?:an?\s+)?(?:text|message|imessage)\s+to\s+(?P<who>{_WHO}?)|"
    rf"send\s+(?P<who_b>{_WHO}?)\s+an?\s+(?:text|message|imessage)|"
    rf"(?:text|message|imessage)\s+(?P<who_c>{_WHO}?))"
    rf"(?:\s+(?P<when>{_WHEN_PHRASE}))?"
    rf"\s+(?:{_BODY_INTRO})\s+(?P<body>.+)$", re.I)
_EMAIL_SEND_INTRO = re.compile(
    rf"^\s*{_POLITE}"
    rf"(?:send\s+(?:an?\s+)?e-?mail\s+to\s+(?P<who>{_WHO}?)|"
    rf"send\s+(?P<who_b>{_WHO}?)\s+an?\s+e-?mail|"
    rf"e-?mail\s+(?P<who_c>{_WHO}?))"
    r"(?:\s+about\s+(?P<subject>[^:]{1,60}?))?"
    rf"(?:\s+(?P<when>{_WHEN_PHRASE}))?"
    rf"\s+(?:{_BODY_INTRO})\s+(?P<body>.+)$", re.I)
# Without an introducer the recipient is a single token, so "text mom I'll be
# late" cannot swallow the first words of its own body.  `message` is excluded
# here because a bare "message ..." is too easily an ordinary noun.
_MESSAGE_SEND_BARE = re.compile(
    rf"^\s*{_POLITE}(?:text|imessage)\s+"
    r"(?P<who>[A-Za-z0-9'’.\-+@_]+)\s+(?P<body>.+)$", re.I)

_DELETE_REMINDER = re.compile(
    r"\b(?:delete|remove|clear)\b[^.?!]{0,120}\breminders?\b|"
    r"\breminders?\b[^.?!]{0,80}\b(?:delete|remove|clear)\b", re.I)
_COMPLETE_REMINDER = re.compile(
    r"\b(?:complete|finish|mark|check|cross|tick)\b[^.?!]{0,100}\breminders?\b|"
    r"\b(?:mark|check|cross|tick)\b[^.?!]{0,100}\b(?:done|complete|off)\b", re.I)
_UPDATE_REMINDER = re.compile(
    r"\b(?:update|change|rename|reschedule|move)\b[^.?!]{0,120}\breminders?\b|"
    r"\breminders?\b[^.?!]{0,100}\b(?:update|change|rename|reschedule|move)\b", re.I)


def _slot(value: str, *, turn: int = 0, source: str = "explicit") -> SlotValue:
    value = " ".join((value or "").split()).strip(" .,!?:;\"'")
    return SlotValue(value, source if value else "", turn=turn,
                     confidence=1.0 if value else 0.0, original=value)


def _clean_target(value: str) -> str:
    while True:
        cleaned = re.sub(
            r"^(?:(?:all|every|any)(?:\s+of)?|my|the|a|an)\b\s*", "", value,
            count=1, flags=re.I)
        if cleaned == value:
            break
        value = cleaned
    value = re.sub(
        r"\b(?:all|every|active|old|overdue|past[ -]?due|upcoming|future|today'?s|"
        r"tomorrow'?s)\b", "", value, flags=re.I)
    return " ".join(value.split()).strip(" .,!?:;\"'")


def _operation_target(text: str, verbs: str) -> str:
    match = re.search(
        rf"\b(?:{verbs})\b\s+(?P<body>[^.?!]{{0,100}}?)\s+reminders?\b", text, re.I)
    if match:
        return _clean_target(match.group("body"))
    match = re.search(r"\breminders?\b\s+(?:called|named|about)\s+(?P<body>[^.?!]+)",
                      text, re.I)
    return _clean_target(match.group("body")) if match else ""


def compile_reminder_delete(text: str, *, now: datetime | None = None,
                            turn: int = 0) -> TaskPlan | None:
    del now
    if (reminder_command_parts(text) or _NEGATED.search(text)
            or _COMPOUND_EFFECT.search(text)
            or not _DELETE_REMINDER.search(text)):
        return None
    lowered = text.casefold()
    if re.search(r"\b(?:all|every)\b", lowered):
        scope = "all"
    elif re.search(r"\b(?:past[ -]?due|overdue|old)\b", lowered):
        scope = "past_due"
    elif re.search(r"\btomorrow\b", lowered):
        scope = "tomorrow"
    elif re.search(r"\b(?:upcoming|future)\b", lowered):
        scope = "upcoming"
    elif re.search(r"\btoday\b", lowered):
        scope = "today"
    target = _operation_target(text, "delete|remove|clear")
    if not any(re.search(pattern, lowered) for pattern in (
            r"\b(?:all|every)\b", r"\b(?:past[ -]?due|overdue|old)\b",
            r"\btomorrow\b", r"\b(?:upcoming|future)\b", r"\btoday\b")):
        scope = "all" if target else ""
    plan = TaskPlan(
        kind="task.reminder.delete", intent="reminder.delete",
        original_request=text, target=_slot(target, turn=turn),
        parameters={"scope": _slot(scope, turn=turn)},
    )
    plan.recompute_status()
    return plan


def compile_reminder_complete(text: str, *, now: datetime | None = None,
                              turn: int = 0) -> TaskPlan | None:
    del now
    if (reminder_command_parts(text) or _NEGATED.search(text)
            or _COMPOUND_EFFECT.search(text)
            or not _COMPLETE_REMINDER.search(text)):
        return None
    target = _operation_target(
        text, r"complete|finish|mark|check(?:\s+off)?|cross(?:\s+off)?|tick(?:\s+off)?")
    if not target:
        match = re.search(
            r"\b(?:mark|check|cross|tick)(?:\s+off)?\s+(?P<body>.+?)\s+"
            r"(?:as\s+)?(?:done|complete|off)\b", text, re.I)
        target = _clean_target(match.group("body")) if match else ""
    plan = TaskPlan(
        kind="task.reminder.complete", intent="reminder.complete",
        original_request=text, target=_slot(target, turn=turn),
    )
    plan.recompute_status()
    return plan


def compile_reminder_update(text: str, *, now: datetime | None = None,
                            turn: int = 0) -> TaskPlan | None:
    if (reminder_command_parts(text) or _NEGATED.search(text)
            or _COMPOUND_EFFECT.search(text)
            or not _UPDATE_REMINDER.search(text)):
        return None
    target = _operation_target(text, "update|change|rename|reschedule|move")
    parameters: dict[str, SlotValue] = {}
    temporal = TemporalValue(original=text, timezone=local_timezone_name(now))

    rename = re.search(r"\brename\b[^.?!]*?\breminder\b\s+(?:to|as)\s+(?P<title>.+)$",
                       text, re.I)
    if rename:
        parameters["new_title"] = _slot(rename.group("title"), turn=turn)
    else:
        destination = re.search(
            r"\b(?:to|for)\s+(?P<when>(?:today|tomorrow)(?:\s+(?:morning|afternoon|evening|night))?"
            r"(?:\s+at\s+[^.?!]+)?|(?:this|next)\s+(?:morning|afternoon|evening|night)|"
            r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)[^.?!]*)$",
            text, re.I)
        when_text = destination.group("when") if destination else ""
        if has_unsupported_alert_clock(when_text):
            pass  # Preserve the target, but leave update.change unresolved.
        elif re.fullmatch(r"today|tomorrow", when_text.strip(), re.I):
            parameters["day"] = _slot(when_text.casefold(), turn=turn)
        elif when_text:
            resolved, defaulted = resolve_named_time(when_text, now=now)
            if resolved is not None:
                temporal.absolute_iso = resolved.isoformat(timespec="minutes")
                temporal.source = "explicit"
                temporal.defaulted_part_of_day = defaulted

    plan = TaskPlan(
        kind="task.reminder.update", intent="reminder.update",
        original_request=text, target=_slot(target, turn=turn),
        temporal=temporal, parameters=parameters,
    )
    plan.recompute_status()
    return plan


def _clean_body(value: str) -> str:
    """Keep the user's own words; strip only wrapping quotes and whitespace."""
    body = (value or "").strip()
    for opener, closer in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if len(body) > 1 and body.startswith(opener) and body.endswith(closer):
            body = body[1:-1].strip()
            break
    return body


def _scheduled_at(when_text: str, *, now: datetime | None) -> str:
    """A send time the user stated explicitly, or "" — never a default."""
    if not when_text:
        return ""
    base = now or datetime.now()
    if (delay := parse_delay_seconds(when_text)) is not None:
        return (base + timedelta(seconds=delay)).isoformat(timespec="minutes")
    resolved, _defaulted = resolve_named_time(when_text, now=base)
    return resolved.isoformat(timespec="minutes") if resolved else ""


def _outbound_plan(match: re.Match, text: str, *, channel: str,
                   now: datetime | None, turn: int) -> TaskPlan | None:
    groups = match.groupdict()
    who = next((groups[key] for key in ("who", "who_b", "who_c")
                if groups.get(key)), "")
    who = " ".join(who.split()).strip(" .,!?:;\"'")
    body = _clean_body(groups.get("body") or "")
    if not who or not body:
        return None
    # Bare source deliveries remain owned by the grounded workflow. With an
    # explicit body introducer, inspect the speech act instead of rejecting
    # ordinary literal sentences merely for containing "email" or "mail".
    if match.re is _MESSAGE_SEND_BARE and _SOURCE_BACKED.search(f"{who} {body}"):
        return None
    intent = "email.send" if channel == "email" else "message.send"
    subject = _clean_body(groups.get("subject") or "")
    when_text = " ".join((groups.get("when") or "").split())
    plan = TaskPlan(
        kind=f"task.{intent}", intent=intent, original_request=text,
        owner=SlotValue("user", "default", turn=turn, original="me"),
        subject=SlotValue(body, "explicit", turn=turn, original=body),
        # The raw handle only.  Resolution happens in the engine, which can
        # read Contacts and ask a question; the compiler must never guess.
        recipient=SlotValue(who, "explicit", turn=turn, original=who),
        channel=SlotValue(channel, "intent_default", turn=turn,
                          original=channel),
        temporal=TemporalValue(
            original=when_text,
            absolute_iso=_scheduled_at(when_text, now=now),
            timezone=local_timezone_name(now),
            source="explicit" if when_text else ""),
        # Three states, not two.  An empty timestamp is how "send now" is
        # represented, so a time phrase we could not resolve ("at 25pm",
        # "at 6 p.m.") must be recorded as a REQUEST for a schedule — otherwise
        # a failed parse silently becomes an immediate send.
        parameters={
            **({"email_subject": _slot(subject, turn=turn)} if subject else {}),
            **({"schedule_requested": SlotValue(
                when_text, "explicit", turn=turn, original=when_text)}
               if when_text else {}),
        },
    )
    from service.tasks.outbound_language import mark_body_ambiguity
    mark_body_ambiguity(plan, groups.get("body") or "", scheduled=bool(when_text))
    plan.recompute_status()
    return plan


def compile_message_send(text: str, *, now: datetime | None = None,
                         turn: int = 0) -> TaskPlan | None:
    """Compile a literal-body iMessage send. Never a source-backed delivery."""
    if _NEGATED.search(text):
        return None
    match = _MESSAGE_SEND_INTRO.search(text) or _MESSAGE_SEND_BARE.search(text)
    return _outbound_plan(match, text, channel="messages", now=now,
                          turn=turn) if match else None


def compile_email_send(text: str, *, now: datetime | None = None,
                       turn: int = 0) -> TaskPlan | None:
    """Compile a literal-body email send. A missing subject is asked for."""
    if _NEGATED.search(text) or _REPLY_INTENT.search(text):
        return None
    match = _EMAIL_SEND_INTRO.search(text)
    return _outbound_plan(match, text, channel="email", now=now,
                          turn=turn) if match else None


def compile_task(text: str, *, now: datetime | None = None,
                 turn: int = 0) -> TaskPlan | None:
    """Compile one unambiguous reminder intent in guarded operation order."""
    if CAPABILITY_INVENTORY_RE.search(text):
        return None
    if reply := compile_email_reply(text, now=now, turn=turn):
        return reply
    # An explicit literal-body introducer fixes the outer speech act. A
    # quoted capability question/reminder phrase is message content, not a
    # reminder command to intercept before the addressed send compiler.
    if _MESSAGE_SEND_INTRO.match(text) or _EMAIL_SEND_INTRO.match(text):
        literal = (compile_message_send(text, now=now, turn=turn)
                   or compile_email_send(text, now=now, turn=turn))
        if literal:
            return literal
    compound = re.search(
        r"(?:\band\s+|\.\s+)(?:also\s+)?(?P<notify>(?:let\b.{0,40}?\bknow|notify|tell|text|message|email)\b.*)$",
        text, re.I)
    if compound and REMINDER_CREATE_RE.search(text[:compound.start()]):
        plan = compile_reminder_create(text[:compound.start()], now=now, turn=turn)
        if plan:
            plan.original_request = text
            plan.parameters["notify_request"] = _slot(compound.group("notify"), turn=turn)
            return plan
    # Creation precedes update so a subject/reference containing a natural
    # word such as "move-in" cannot be mistaken for the verb "move". The
    # operation compilers decline an outer creation command, while creation
    # rejects actual operation verbs in its command rather than its content.
    for compiler in (compile_reminder_delete, compile_reminder_complete,
                     compile_reminder_create, compile_reminder_update,
                     compile_message_send, compile_email_send):
        if plan := compiler(text, now=now, turn=turn):
            return plan
    return None


def compile_email_reply(text: str, *, now: datetime | None = None,
                        turn: int = 0) -> TaskPlan | None:
    """Bounded, explicit reply commands; source selection is never inferred here."""
    match = re.match(rf"^\s*{_POLITE}(?:reply(?P<all>\s+all)?\s+to|respond\s+to)\s+(?P<rest>.+)$", text, re.I | re.S)
    scheduled_command = False
    if not match:
        match = re.match(
            rf"^\s*{_POLITE}schedule\s+(?:an?\s+)?(?:e-?mail\s+)?"
            rf"(?:reply|response)(?P<all>\s+all)?\s+to\s+(?P<rest>.+)$",
            text, re.I | re.S,
        )
        scheduled_command = match is not None
    if not match or _NEGATED.search(text):
        return None
    from service.tasks.reply_parser import parse_reply_parts
    parts = parse_reply_parts(match.group("rest"))
    reference = parts.reference
    if not re.search(r"\be-?mail\b", reference, re.I):
        return None  # a bare "reply to Dan" has not specified its channel
    body = _clean_body(parts.raw_body)
    # Preserve a scheduled-reply request as a typed, terminal limitation. It
    # must not fall through to the generic router, where schedule_send could
    # create a new standalone email or reply_to_email could send immediately.
    scheduled = parts.schedule_requested
    parameters = {"reply_all": SlotValue(bool(match.group("all")), "explicit")}
    if parts.selector_error:
        parameters["reply_selector_error"] = SlotValue(parts.selector_error, "unresolved")
    if scheduled_command or scheduled:
        requested = scheduled or "scheduled reply"
        parameters["schedule_requested"] = SlotValue(requested, "explicit")
    plan = TaskPlan(kind="task.email.reply", intent="email.reply", original_request=text,
                    channel=SlotValue("email", "intent_default"),
                    target=_slot(reference, turn=turn), subject=SlotValue(body, "explicit" if body else ""),
                    parameters=parameters)
    from service.tasks.outbound_language import mark_body_ambiguity
    mark_body_ambiguity(plan, parts.raw_body)
    if parts.body_timing and "time_clarification" not in plan.parameters:
        literal, when = parts.body_timing
        plan.parameters["time_clarification"] = SlotValue({"when": when, "body": literal}, "unresolved")
    plan.recompute_status()
    return plan


def _clean_subject(value: str) -> str:
    value = _TRAILING_TIME.sub("", value.strip().strip('*_'))
    value = re.sub(r"\s+", " ", value).strip(" .,!?:;\"'*_")
    return value


def extract_reminder_subject(text: str) -> str:
    parts = reminder_command_parts(text)
    if parts and parts[1]:
        return _clean_subject(_COMPOUND_EFFECT.split(parts[1], maxsplit=1)[0])
    patterns = [
        r"\b(?:remind\s+me|(?:send|give)\s+me\s+(?:an?\s+)?reminder)\b"
        r".*?\bto\s+(?P<subject>.+)$",
        r"\b(?:add|create|make|schedule|set(?:\s+up)?)\s+"
        r"(?:me\s+)?(?:(?:an?|the|my)\s+)?(?:reminder|alarm)\b"
        r"(?:\s+for\s+me)?\s+.*?\bto\s+(?P<subject>.+)$",
        r"\b(?:remember|don'?t\s+forget)\s+to\s+(?P<subject>.+)$",
        # Greedy before `for`: "set an alarm for me tomorrow for the repair"
        # uses the last `for` for the subject; the first only marks ownership.
        r"\bset\s+(?:an?\s+)?alarm\b.*\bfor\s+(?P<subject>.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            subject = match.group("subject")
            # The supported slice is single-effect.  This also keeps a person
            # in "ask Trishy" inside the subject instead of making them a
            # recipient.
            subject = _COMPOUND_EFFECT.split(subject, maxsplit=1)[0]
            return _clean_subject(subject)
    return ""


def extract_event_reference(text: str) -> str:
    match = _REFERENCE.search(text)
    return " ".join(match.group("reference").split()) if match else ""


def compile_reminder_create(text: str, *, now: datetime | None = None,
                            turn: int = 0) -> TaskPlan | None:
    parts = reminder_command_parts(text)
    command = parts[0] if parts else text
    if (CAPABILITY_INVENTORY_RE.search(text)
            or not REMINDER_CREATE_RE.search(text) or _NEGATED.search(command)
            or _OTHER_REMINDER_OPERATION.search(command)
            or _COMPOUND_EFFECT.search(text)):
        return None

    subject = extract_reminder_subject(text)
    reference = extract_event_reference(text)
    temporal_text = reminder_temporal_text(text)
    unsupported_clock = has_unsupported_alert_clock(temporal_text)
    lead = parse_lead_seconds(temporal_text) if reference and not unsupported_clock else None
    resolved, defaulted = ((None, "") if reference or unsupported_clock
                           else resolve_named_time(temporal_text, now=now))
    temporal_source = "explicit" if (resolved or reference) else ""
    plan = TaskPlan(
        original_request=text,
        owner=SlotValue("user", "explicit" if re.search(r"\b(?:me|my)\b", text, re.I)
                        else "default", turn=turn, original="me"),
        subject=SlotValue(subject, "explicit" if subject else "", turn=turn,
                          confidence=1.0 if subject else 0.0, original=subject),
        recipient=None,
        channel=SlotValue("reminders", "intent_default", turn=turn,
                          original="reminder"),
        temporal=TemporalValue(
            original=text,
            absolute_iso=(resolved.isoformat(timespec="minutes") if resolved else ""),
            reference=reference,
            lead_seconds=lead,
            timezone=local_timezone_name(now),
            source=temporal_source,
            defaulted_part_of_day=defaulted,
        ),
    )
    plan.recompute_status()
    return plan

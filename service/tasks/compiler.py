"""Deterministic compiler for typed reminder operations."""
from __future__ import annotations

from datetime import datetime, timedelta
import re

from service.utterance_shape import deliberate, quoted_spans
from service.reminder_intent import (
    CAPABILITY_INVENTORY_RE, REMINDER_CREATE_RE, has_unsupported_alert_clock,
    reminder_command_parts, reminder_temporal_text, _NAMED_DATE,
)
from service.tasks.models import SlotValue, TaskPlan, TemporalValue
from service.tasks.temporal import (
    local_timezone_name, parse_delay_seconds, parse_lead_seconds,
    resolve_named_time,
)


_NEGATED = re.compile(
    r"\b(?:do\s+not|don['’‘ʼ＇]t|dont|never)\s+"
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
_CLAUSE_JOIN = re.compile(
    r"[,;]\s*(?:(?:and|then|also|but)\s+)?|[.!?]\s+|"
    r"\s+(?:and(?:\s+then)?|then|also|but|or)\s+", re.I)
# Only a proven noun-list grammar may consume an unquoted conjunction as
# reminder content. Unknown coordination is ambiguous and belongs to the
# model, not a verb denylist that eventually misses another device action.
_GROCERY_OBJECT = r"(?:milk|eggs|bread|butter|cheese|rice|apples|bananas|fruit|vegetables)"
_GROCERY_LIST = re.compile(
    rf"(?:buy|get|pick\s+up)\s+{_GROCERY_OBJECT}"
    rf"(?:\s*(?:,\s*(?:and\s+)?|and\s+){_GROCERY_OBJECT})+[.!?]*", re.I)
# A calendar date is bounded temporal grammar too: "September 28th", "Oct 5",
# "28th", "12th of October". The month must be followed by its day so a bare "may"
# (a modal verb) is never mistaken for one.
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
          r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_CALENDAR_DATE = (rf"(?:{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?!\d)|"
                  rf"\d{{1,2}}(?:st|nd|rd|th)(?:\s+of\s+{_MONTH})?)")
_TEMPORAL_ONLY = re.compile(
    rf"(?:(?:{_CALENDAR_DATE}|on|at|by|for|in|from|between|to|and|until|through|till|this|next|later|"
    r"today|tomorrow|tommorow|tommorrow|tmrw|tmrow|tonight|morning|afternoon|evening|night|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"half|a|an|quarter|past|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|oh|o['’]?clock|"
    r"minutes?|mins?|hours?|hrs?|days?|weeks?|noon|midnight|"
    r"\d+(?::\d*)?(?:\s*[ap]\.?m\.?)?|[ap]\.?m\.?|[-–—/])\s*)+", re.I)
_ALERT_DAY = re.compile(
    r"\b(?:today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
# Complete existing calendar-day vocabulary, including resolver-supported
# tomorrow aliases. This excludes clocks/dayparts, offsets and event lead times.
_LITERAL_DAY = re.compile(
    rf"{_NAMED_DATE.pattern}|{_ALERT_DAY.pattern}|"
    r"\b(?:tommorow|tommorrow|tmrw|tmrow)\b", re.I)
_IMMEDIATE_SCOPE = re.compile(r"\b(?:now|immediately|right\s+away|at\s+once)\b", re.I)
_CONDITIONAL_SCOPE = re.compile(
    r"\b(?:if|unless|otherwise|provided\s+that|depending\s+on)\b", re.I)
# Receipt requests have no authored body. Consume their complete grammar so
# 'email Mom saying hello' cannot silently become a generated reminder receipt.
_RECEIPT_RECIPIENT = (
    r"(?:\+?\d[\d ()-]{5,}\d|[^\s@]+@[^\s@]+|"
    r"[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){1,2}|[A-Za-z'’-]+)")
_RECEIPT_REQUEST = re.compile(
    rf"(?:(?i:let)\s+(?i:my\s+)?{_RECEIPT_RECIPIENT}\s+(?i:know)|"
    rf"(?i:notify|tell|text|message|email)\s+(?i:my\s+)?{_RECEIPT_RECIPIENT})"
    r"(?:\s+(?i:about\s+(?:it|this\s+reminder|the\s+reminder)))?"
    r"(?:\s+(?i:too|as\s+well))?"
    r"(?:\s+(?i:via|through|using)\s+(?i:messages|imessage|text|email))?"
    r"[.!?*_\s]*")


def _unquoted(text: str) -> str:
    """Mask literal content without changing clause offsets."""
    chars = list(text)
    for start, end in quoted_spans(text):
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _reminder_parts(text: str) -> tuple[str, str] | None:
    # The golden corpus supports "can u send me a reminder". Normalize only
    # its leading courtesy, never a reminder embedded after another action.
    # Markdown emphasis is presentation, not an unrelated leading action.
    normalized = text.lstrip().lstrip("*_").lstrip()
    normalized = re.sub(
        r"^(\s*(?:(?:hey|hi|ok|okay|please)[,\s]+)*)(can|could|would)\s+u\b",
        r"\1\2 you", normalized, count=1, flags=re.I)
    return reminder_command_parts(normalized)
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
_TRAILING_NAMED_DATE = re.compile(
    r"\s+(?:(?:on|at|for)\s+)?(?:\d{4}-\d{2}-\d{2}|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?"
    r"(?:,?\s+\d{4})?)"
    r"(?:\s+(?:at|from)\s+\d{1,2}(?::\d{2})?"
    r"(?:\s*(?:-|–|—|to)\s*\d{1,2}(?::\d{2})?)?\s*(?:am|pm))?"
    r"\s*[.!?]*$", re.I)


def _request_clauses(text: str) -> tuple[str, ...]:
    """Consume literal spans, noun lists and clock syntax before clause joins.

    A temporal clause ends reminder content: unexplained text after it is not
    silently turned into a title. Offsets refer to the original authored text.
    """
    masked = _unquoted(text)
    if re.search(r"\b(?:and(?:\s+then)?|or|then|also|but)[.!?*_\s]*$", masked, re.I):
        return ()  # An unfinished connector has not supplied its second task.
    # A closing courtesy belongs to the preceding clause, not a new task.
    courtesy = re.search(r"(?:,\s*|\s+)(?:please|thanks|thank\s+you)[.!?*_\s]*$", masked, re.I)
    if courtesy:
        masked = masked[:courtesy.start()] + " " * (len(masked) - courtesy.start())
    prefix = re.match(rf"^\s*{_POLITE}", masked.lstrip("*_"), re.I)
    courtesy_end = prefix.end() if prefix else 0
    parts = _reminder_parts(text)
    content_start = len(text) - len(parts[1]) if parts and parts[1] else len(text)
    header_alert = bool(parts and (
        _REFERENCE.search(parts[0]) or resolve_named_time(parts[0])[0] is not None
        or has_unsupported_alert_clock(parts[0])))
    if parts and parts[1]:
        title = _TRAILING_TIME.sub("", parts[1].strip().strip("*_"))
        title = _TRAILING_NAMED_DATE.sub("", title).strip()
        if _GROCERY_LIST.fullmatch(title):
            return (text,)
    boundaries = []
    # Quoted spans are opaque, not whitespace belonging to a following join.
    # Otherwise the leading \s+ in ' and ' consumes the whole masked body in
    # 'text Mom saying "hello" and remind me ...', dropping authored content.
    join_mask = list(masked)
    for start, end in quoted_spans(text):
        join_mask[start:end] = "\0" * (end - start)
    for match in _CLAUSE_JOIN.finditer("".join(join_mask)):
        if match.start() < courtesy_end:
            continue
        # A schedule BEFORE the infinitive scopes coordinated future content.
        # A later day/alert clause closes that content again. Without this
        # bracket, unquoted coordination is ambiguous (apart from noun lists).
        if (header_alert and match.start() >= content_start
                and masked[match.start()].isspace()
                and not _ALERT_DAY.search(masked[content_start:match.start()])
                and not _IMMEDIATE_SCOPE.search(masked[match.end():])):
            continue
        if match.group().strip().casefold() == "or":
            if (match.start() < content_start
                    and re.search(r"\b(?:at|for)\s+\d{1,2}(?::\d{2})?$", masked[:match.start()], re.I)
                    and re.match(r"\d{1,2}(?::\d{2})?\b", masked[match.end():])):
                continue  # An unresolved clock choice, not alternative effects.
            return ()
        # A comma in an explicit named date and 'between 6 and 7' are grammar,
        # not independent effects. Unsupported ranges still ask for one time.
        if masked[match.start()] == "," and re.match(r"\s*\d{4}\b", masked[match.end():]):
            continue
        if (re.search(r"\bbetween\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?$",
                      masked[:match.start()], re.I)
                and re.match(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", masked[match.end():], re.I)):
            continue
        if masked[match.end():].strip(" .!?*_"):
            boundaries.append(match.span())
    # Also reject a temporal prefix followed by unexplained outer prose even
    # without a conjunction: '...medicine tonight mute my volume'.
    if parts and parts[1]:
        temporal = re.compile(_TRAILING_TIME.pattern.replace(
            r"\s*[.!?]*$", r"(?![\w'’])"), re.I)
        # A separate companion owns its own clock/body grammar. Do not apply
        # reminder-title tail validation to 'text Mom tomorrow saying hello'.
        reminder_end = min((left for left, _ in boundaries if left >= content_start),
                           default=len(masked))
        for match in temporal.finditer(masked, content_start, reminder_end):
            tail = masked[match.end():reminder_end]
            if ((header_alert and not _ALERT_DAY.search(match.group()))
                    or _TEMPORAL_ONLY.fullmatch(tail.strip(" .!?*_"))):
                continue
            if (tail.strip(" .!?*_") and not re.match(r"[\w'’]", tail)
                    and not any(start <= match.end() <= end for start, end in boundaries)):
                boundaries.append((match.end(), match.end()))
    if not boundaries:
        return (text,)
    out, start = [], 0
    for left, right in sorted(set(boundaries)):
        if left < start:
            continue
        if clause := text[start:left].strip(" ,;.!?*_"):
            out.append(clause)
        start = right
    if clause := text[start:].strip(" ,;.!?*_"):
        out.append(clause)
    return tuple(out)


def reminder_request_clauses(text: str) -> tuple[str, ...]:
    """Shared eligibility/router boundary for an outer reminder task list.

    Literal addressed sends and named existing-item operations are not outer
    creation requests. A single future subject remains one reminder clause.
    """
    if CAPABILITY_INVENTORY_RE.search(text) or deliberate(text) is not None:
        return (text,)
    addressed = _MESSAGE_SEND_INTRO.match(text) or _EMAIL_SEND_INTRO.match(text)
    if addressed and CAPABILITY_INVENTORY_RE.fullmatch(addressed.group("body")):
        return (text,)  # A complete authored capability question is literal content.
    clauses = _request_clauses(text)
    masked = _unquoted(text)
    creation = REMINDER_CREATE_RE.search(masked)
    # Conditions and alternatives relate whole actions. Do not erase that
    # relationship by turning their fragments into independent obligations.
    if creation and (not clauses
            or _CONDITIONAL_SCOPE.search(masked[:creation.start()])
            or (len(clauses) > 1 and any(
                _CONDITIONAL_SCOPE.search(_unquoted(clause))
                or re.match(r"(?:or|either|alternatively)\b", clause, re.I)
                for clause in clauses))):
        return ()
    if parts := _reminder_parts(text):
        if parts[1] and not _reminder_header_consumed(parts[0]):
            return ()
    return clauses if any(_reminder_parts(part) for part in clauses) else (text,)


def _reminder_header_consumed(command: str) -> bool:
    """The outer command contains only courtesy, creation and alert grammar.

    Clock attempts stay eligible for the existing clarification policy. Extra
    prose before the subject introducer is not part of a deterministic task.
    """
    normalized = command.lstrip().lstrip("*_").lstrip()
    normalized = re.sub(r"\b(can|could|would)\s+u\b", r"\1 you", normalized,
                        count=1, flags=re.I)
    prefix = re.match(rf"^\s*{_POLITE}", normalized, re.I)
    match = REMINDER_CREATE_RE.match(normalized, prefix.end())
    if not match:
        return False
    rest = normalized[match.end():].strip()
    if re.match(r"(?:remember|don'?t\s+forget)\b", match.group(), re.I):
        return True
    rest = re.sub(r"\s*\bto\s*$", "", rest, flags=re.I).strip()
    rest = re.sub(r"^for\s+me\b\s*", "", rest, flags=re.I)
    if not rest:
        return True
    if reference := _REFERENCE.search(rest):
        lead = rest[:reference.start()].strip()
        return reference.end() == len(rest) and (not lead or re.fullmatch(
            r"(?:the\s+day|(?:a|an|one|two|three|\d+)\s+(?:minutes?|hours?|days?|weeks?))",
            lead, re.I) is not None)
    # Named dates are consumed as a whole, including a year/comma. Everything
    # else must consist solely of bounded temporal tokens, not arbitrary words
    # whose presence a permissive time search would otherwise ignore.
    # Consume the date using the same lexer as reminder temporal parsing.
    # The remaining clock tokens may be unresolved (a range, for example);
    # completeness does not mean that a single executable time was supplied.
    rest = _NAMED_DATE.sub("", rest).strip()
    rest = re.sub(r"\b(\d{1,2}(?::\d{2})?)\s+or\s+\d{1,2}(?::\d{2})?\b",
                  r"\1", rest, flags=re.I)
    return not rest or _TEMPORAL_ONLY.fullmatch(rest) is not None

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
# A phone number as people type it ("+1 650 555 0134", "(650) 555-0134") is ONE
# recipient. Without this the name pattern took "+1" as the recipient and the rest
# of the number became the body of the message.
_PHONE_WHO = r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
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
    rf"(?:send\s+(?:an?\s+)?(?:text|message|imessage)\s+to\s+(?P<who>{_PHONE_WHO}|{_WHO}?)|"
    rf"send\s+(?P<who_b>{_PHONE_WHO}|{_WHO}?)\s+an?\s+(?:text|message|imessage)|"
    rf"(?:text|message|imessage)\s+(?P<who_c>{_PHONE_WHO}|{_WHO}?))"
    rf"(?:\s+(?P<when>{_WHEN_PHRASE}))?"
    rf"\s+(?:{_BODY_INTRO})\s+(?P<body>.+)$", re.I | re.S)
_EMAIL_SEND_INTRO = re.compile(
    rf"^\s*{_POLITE}"
    rf"(?:send\s+(?:an?\s+)?e-?mail\s+to\s+(?P<who>{_WHO}?)|"
    rf"send\s+(?P<who_b>{_WHO}?)\s+an?\s+e-?mail|"
    rf"e-?mail\s+(?P<who_c>{_WHO}?))"
    r"(?:\s+about\s+(?P<subject>[^:]{1,60}?))?"
    rf"(?:\s+(?P<when>{_WHEN_PHRASE}))?"
    rf"\s+(?:{_BODY_INTRO})\s+(?P<body>.+)$", re.I | re.S)
# Without an introducer the recipient is a single token, so "text mom I'll be
# late" cannot swallow the first words of its own body.  `message` is excluded
# here because a bare "message ..." is too easily an ordinary noun.
_MESSAGE_SEND_BARE = re.compile(
    rf"^\s*{_POLITE}(?:text|imessage)\s+"
    rf"(?P<who>{_PHONE_WHO}|[A-Za-z0-9'’.\-+@_]+)\s+(?P<body>.+)$", re.I | re.S)

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
        target = _clean_target(match.group("body"))
        if target:
            return target
    match = re.search(r"\breminders?\b\s+(?:called|named|about)\s+(?P<body>[^.?!]+)",
                      text, re.I)
    return _clean_target(match.group("body")) if match else ""


def compile_reminder_delete(text: str, *, now: datetime | None = None,
                            turn: int = 0) -> TaskPlan | None:
    del now
    if (_reminder_parts(text) or _NEGATED.search(text)
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
    if (_reminder_parts(text) or _NEGATED.search(text)
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
    if (_reminder_parts(text) or _NEGATED.search(text)
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
    if len(reminder_request_clauses(text)) != 1:
        return None  # Outer reminder coordination is not part of this send body.
    groups = match.groupdict()
    who = next((groups[key] for key in ("who", "who_b", "who_c")
                if groups.get(key)), "")
    who = " ".join(who.split()).strip(" .,!?:;\"'")
    body = _clean_body(groups.get("body") or "")
    if not who or not body:
        return None
    raw_body = _unquoted(groups.get("body") or "").strip(" .!?")
    if (match.re is _MESSAGE_SEND_BARE and raw_body
            and (_IMMEDIATE_SCOPE.fullmatch(raw_body) or _TEMPORAL_ONLY.fullmatch(raw_body))):
        return None  # Bare timing is not an authored message; quotes/introducer disambiguate.
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
    # A prohibition or a sentence about words ('Say "remind me to call Mom
    # tomorrow"') must not become a real task: the model reads those.
    if deliberate(text) is not None:
        return None
    if reply := compile_email_reply(text, now=now, turn=turn):
        return reply
    # An addressed literal-body command fixes the outer speech act. A
    # quoted capability question/reminder phrase is message content, not a
    # reminder command to intercept before the addressed send compiler.
    if (_MESSAGE_SEND_INTRO.match(text) or _MESSAGE_SEND_BARE.match(text)
            or _EMAIL_SEND_INTRO.match(text)):
        literal = (compile_message_send(text, now=now, turn=turn)
                   or compile_email_send(text, now=now, turn=turn))
        if literal:
            return literal
    unquoted = _unquoted(text)
    clauses = reminder_request_clauses(text)
    if (len(clauses) == 2 and _RECEIPT_REQUEST.fullmatch(clauses[1])
            and not re.search(rf"\b(?:{_BODY_INTRO})\b", clauses[1], re.I)):
        notify_request = clauses[1]
        plan = compile_reminder_create(clauses[0], now=now, turn=turn)
        if plan:
            plan.original_request = text
            plan.parameters["notify_request"] = _slot(notify_request, turn=turn)
            return plan
    # The typed engine owns only one complete task (or the supported reminder
    # plus notification above). Declining creation must not re-arm another
    # reminder operation on a substring of a compound request.
    if REMINDER_CREATE_RE.search(unquoted) and (
            len(clauses) != 1 or (
                not _reminder_parts(text) and not (re.match(
                    rf"^\s*{_POLITE}(?:delete|remove|clear|complete|finish|mark|check|"
                    r"cross|tick|cancel|update|change|rename|reschedule|move)\b",
                    unquoted.lstrip().lstrip("*_"), re.I)
                    and re.search(r"\breminders?\s+(?:called|named|about)\s+", unquoted, re.I)))):
        return None
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
    value = _TRAILING_NAMED_DATE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip(" .,!?:;\"'*_")
    return value


def extract_reminder_subject(text: str) -> str:
    parts = _reminder_parts(text)
    if parts and parts[1]:
        return _clean_subject(parts[1])
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
            return _clean_subject(subject)
    return ""


def extract_event_reference(text: str) -> str:
    match = _REFERENCE.search(text)
    return " ".join(match.group("reference").split()) if match else ""


def _reminder_temporal_evidence(text: str, *, parts: tuple[str, str] | None = None) -> str:
    """Retain final dates already consumed by this compiler's subject cleanup."""
    # Split before masking; the introducer's whitespace can otherwise consume
    # a fully masked title and erase the boundary before its trailing day.
    parts = parts or _reminder_parts(text)
    if parts:
        parts = (_unquoted(parts[0]), _unquoted(parts[1]))
    named = None
    outer_day = None
    if parts:
        header = re.sub(r"\b(?:tommorow|tommorrow|tmrw|tmrow)\b", "tomorrow", parts[0], flags=re.I)
        tail = _TRAILING_TIME.search(parts[1].rstrip().strip('*_'))
        outer_day = _ALERT_DAY.search(header) or (tail and _ALERT_DAY.search(tail.group()))
    if parts and parts[1]:
        # Use the SAME bounded suffix grammar/order as _clean_subject, never
        # arbitrary dates embedded in authored content or inside quoted titles.
        # Keep left padding from quote masking: it is the original boundary
        # before a suffix when the entire title was quoted.
        subject = _TRAILING_TIME.sub("", parts[1].rstrip().strip('*_'))
        suffix = _TRAILING_NAMED_DATE.search(subject)
        named = _NAMED_DATE.search(suffix.group()) if suffix else None
    resolution_text = text
    if named or outer_day or (parts and _NAMED_DATE.search(parts[0])):
        # A recognized outer day is authoritative. Mask every supported literal
        # day token before projection, including clock-adjacent relative days
        # and weekdays. Keep quoted clocks and all subject text behavior.
        chars = list(text)
        for start, end in quoted_spans(text):
            for day in _LITERAL_DAY.finditer(text, start, end):
                chars[day.start():day.end()] = " " * (day.end() - day.start())
        resolution_text = "".join(chars)
    evidence = reminder_temporal_text(resolution_text)
    retained = {match.group().casefold() for match in _NAMED_DATE.finditer(evidence)}
    if named and named.group().casefold() not in retained:
        evidence = evidence.rstrip() + " " + named.group()
    return evidence


def _conflicting_reminder_days(scoped_alerts: str, temporal_text: str, now: datetime) -> bool:
    """Reconcile day constraints without treating subject words as dates.

    Absolute dates already scoped as alert evidence can repeat consistently.
    Relative days and weekday labels come from the header and consumed alert
    spans, never the temporal helper's fallback containing arbitrary title text.
    A weekday beside an explicit date labels that date, not the next occurrence.
    Offsets and event-reference lead times are not calendar-day equalities.
    """
    dates = re.findall(rf"{_NAMED_DATE.pattern}|{_CALENDAR_DATE}",
                       _unquoted(temporal_text), re.I)
    alerts = re.sub(r"\b(?:tommorow|tommorrow|tmrw|tmrow)\b", "tomorrow",
                    _unquoted(scoped_alerts), flags=re.I)
    relative = re.findall(r"\b(?:today|tonight|tomorrow)\b", alerts, re.I)
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    labels = re.findall(r"\b(?:" + "|".join(weekdays) + r")\b", alerts, re.I)
    if len(dates) + len(relative) + len(labels) < 2:
        return False
    chosen = set()
    for token in dates:
        resolved, _ = resolve_named_time(token, now=now)
        if resolved is None:
            return True
        chosen.add(resolved.date())
    chosen.update(now.date() + timedelta(days=word.lower() == "tomorrow") for word in relative)
    if len(chosen) > 1:
        return True
    label_days = {weekdays.index(label.lower()) for label in labels}
    if len(label_days) > 1:
        return True
    return bool(chosen and label_days and next(iter(chosen)).weekday() not in label_days)


def compile_reminder_create(text: str, *, now: datetime | None = None,
                            turn: int = 0) -> TaskPlan | None:
    parts = _reminder_parts(text)
    command = parts[0] if parts else text
    if (not parts or CAPABILITY_INVENTORY_RE.search(text)
            or not REMINDER_CREATE_RE.search(text) or _NEGATED.search(command)
            or _OTHER_REMINDER_OPERATION.search(command)
            or len(reminder_request_clauses(text)) != 1):
        return None

    subject = extract_reminder_subject(text)
    reference = extract_event_reference(text)
    temporal_text = _reminder_temporal_evidence(text)
    unsupported_clock = has_unsupported_alert_clock(temporal_text)
    # A parser finding one valid date is not permission to choose it over
    # another supplied date. Retain the subject and ask for a single alert.
    now = now or datetime.now()
    # Mask quotes BEFORE projection: extraction can otherwise remove quote
    # delimiters and make title words look like alert evidence. A reduced
    # projection contains header/consumed tail spans; the unchanged fallback
    # still includes arbitrary subject words, so use only its header labels.
    unquoted_request = _unquoted(text)
    day_constraints = _reminder_temporal_evidence(unquoted_request, parts=parts)
    scoped_alerts = day_constraints if day_constraints != unquoted_request else command
    ambiguous_dates = _conflicting_reminder_days(scoped_alerts, day_constraints, now)
    lead = parse_lead_seconds(temporal_text) if reference and not (unsupported_clock or ambiguous_dates) else None
    resolved, defaulted = ((None, "") if reference or unsupported_clock or ambiguous_dates
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

"""Deterministic routes for the most common single-intent everyday asks.

Each matcher is a fully-consumed grammar (``re.fullmatch`` over the cleaned
sentence), so anything with an extra clause, an unknown qualifier, or another
source falls through to the ordinary router untouched. Nothing here executes a
tool or touches a send/calendar confirmation: a claim only decides which tools
are offered and, for an unambiguous ask, which one is forced.

``claim()`` returns a ``CommonClaim``; router.route() turns it into a
``RouteDecision``.  The weather and brief grammars are also reused by
``service.workflows.workflow20`` for direct (pre-model) reads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from service.utterance_shape import mask_quoted


@dataclass(frozen=True)
class CommonClaim:
    subset: tuple[str, ...]
    reason: str
    force: str | None = None
    expect: bool = True
    bindings: dict = field(default_factory=dict)
    forbid: frozenset = frozenset()
    # True when the web classifier's "current information" reading must be
    # overridden (the answer is local: clock, arithmetic, weather tool, ...).
    private: bool = True


_PREFIX = (r"(?:(?:hey|ok|okay|please|pls|can you|could you|would you|will you)[,\s]+)*")
_COMPOUND = re.compile(r"[;\n]|\b(?:and|then|also|after that)\b", re.I)


def clean(text: str) -> str:
    """Quote-masked sentence without trailing punctuation or filler."""
    t = mask_quoted(text).strip()
    t = re.sub(r"[\s.!?]+$", "", t)
    t = re.sub(r"[’]", "'", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r",?\s*(?:please|thanks|thank you)$", "", t, flags=re.I)
    return t.strip()


# ---------------------------------------------------------------- timers
_TIMER = re.compile(
    rf"{_PREFIX}(?:set|start|create|make|begin|put on|run)\s+(?:me\s+)?(?:an?\s+|the\s+)?"
    r"(?:[\w-]+\s+){0,3}?timer(?:\s+(?:for|of|to|at)\s+[^,;]{1,60})?", re.I)
_TIMER_BARE = re.compile(
    rf"{_PREFIX}(?:(?:an?\s+)?\d+(?:\.\d+)?[\s-]*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)\s+timer|"
    r"timer\s+(?:for\s+)?\d+(?:\.\d+)?\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)|"
    r"countdown\s+(?:of|for)\s+\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?))", re.I)
_ALARM = re.compile(
    rf"{_PREFIX}(?:(?:set|create|make|add|schedule)\s+(?:me\s+)?(?:an?\s+)?(?:[\w-]+\s+){{0,2}}?alarm"
    r"(?:\s+(?:for|at|to go off at|by)\s+[^,;]{1,40})?|"
    r"alarm\s+(?:for|at)\s+[^,;]{1,40}|"
    r"wake\s+me(?:\s+up)?\s+(?:at|by|around|tomorrow|in the morning)\b[^,;]{0,40})", re.I)
_NOT_ALARM = re.compile(r"\b(?:security|house|home|car|fire|smoke|burglar|door|gate)\s+alarm|alarm\s+system", re.I)

_REMIND_RELATIVE = re.compile(
    rf"{_PREFIX}remind\s+me\s+(?:in|after)\s+(?:an?|\d+(?:\.\d+)?|half an?)\s*"
    r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)\b.*", re.I)


def is_relative_reminder(text: str) -> bool:
    t = clean(text)
    return bool(_REMIND_RELATIVE.fullmatch(t)) and not re.search(r"[;\n]|\bthen\b|\balso\b", t, re.I)


# ---------------------------------------------------------------- local clock
_PLACE = r"(?P<place>[A-Za-z][A-Za-z .,'-]{1,40})"
_CLOCK_LOCAL = [
    re.compile(r"(?:what|whats)(?:'s|\s+is|\s+are)?\s+(?:the\s+)?(?:current\s+|today'?s\s+)?(?:date|day|time)"
               r"(?:\s+(?:today|now|right\s+now|is\s+it))?", re.I),
    re.compile(r"(?:what|whats)(?:'s|\s+is)?\s+(?:the\s+)?(?:day|date|time)\s+is\s+(?:it|today)"
               r"(?:\s+(?:today|now|right\s+now))?", re.I),
    re.compile(r"what\s+(?:day|date|time)\s+is\s+(?:it|today)(?:\s+(?:today|now|right\s+now))?", re.I),
    re.compile(r"(?:what|whats)(?:'s|\s+is)?\s+today(?:'s\s+date)?", re.I),
    re.compile(r"(?:do you have|have you got|got)\s+the\s+time", re.I),
    re.compile(r"(?:tell me|give me)\s+(?:the\s+)?(?:current\s+)?(?:date|time)(?:\s+(?:today|now))?", re.I),
]
_CLOCK_PLACE = [
    re.compile(rf"what\s+time\s+is\s+it\s+(?:right\s+now\s+)?in\s+{_PLACE}", re.I),
    re.compile(rf"(?:what|whats)(?:'s|\s+is)?\s+(?:the\s+)?(?:current\s+|local\s+)?time\s+in\s+{_PLACE}", re.I),
    re.compile(rf"what\s+day\s+is\s+it\s+in\s+{_PLACE}", re.I),
    re.compile(rf"current\s+time\s+in\s+{_PLACE}", re.I),
]
_BAD_PLACE = re.compile(r"\b(?:morning|afternoon|evening|night|hours?|minutes?|my|the|a|an|this|that|"
                        r"calendar|meeting|schedule|mail|email)\b", re.I)


def _local_clock(t: str) -> CommonClaim | None:
    if any(rx.fullmatch(t) for rx in _CLOCK_LOCAL):
        return CommonClaim(("world_time",), "local date/time -> world_time", force="world_time")
    for rx in _CLOCK_PLACE:
        m = rx.fullmatch(t)
        if m and not _BAD_PLACE.search(m["place"]):
            place = m["place"].strip(" .,")
            return CommonClaim(("world_time",), "time in a named place -> world_time",
                               force="world_time", bindings={"world_time": {"place": place}})
    return None


# ---------------------------------------------------------------- weather
_W_PERIOD = (r"(?:today|tomorrow|tonight|this\s+(?:week|weekend|morning|afternoon|evening)|"
             r"next\s+week|right\s+now|now|currently|later|later\s+today)")
_W_CITY = r"(?:in|for|at|around)\s+(?P<city>[A-Za-z][A-Za-z .,'-]{1,50}?)"
_W_TAIL = rf"(?:\s+(?P<p1>{_W_PERIOD}))?(?:\s+{_W_CITY})?(?:\s+(?P<p2>{_W_PERIOD}))?"
_WEATHER = [
    re.compile(rf"(?:how(?:'s|\s+is)|what(?:'s|\s+is)|whats)\s+(?:the\s+)?(?:weather|temperature|forecast)"
               rf"(?:\s+(?:like|looking|outside|out))*{_W_TAIL}", re.I),
    re.compile(rf"(?:(?:show|give|get|check|tell)\s+(?:me\s+)?)?(?:the\s+)?(?:weather|forecast)"
               rf"(?:\s+(?:report|update))?{_W_TAIL}", re.I),
    re.compile(rf"(?:will it|is it going to|is it gonna|is it)\s+(?:rain|raining|snow|snowing|storm|"
               rf"be\s+(?:rainy|sunny|cold|hot|warm|windy|cloudy|nice|clear)|"
               rf"(?:rainy|sunny|cold|hot|warm|windy|cloudy)(?:\s+outside)?){_W_TAIL}", re.I),
    re.compile(rf"(?:should i|do i need to|do i need|do i have to)\s+(?:bring|take|carry|grab|pack|wear|"
               rf"need)\s+(?:an?\s+|my\s+|a\s+)?(?:umbrella|jacket|coat|raincoat|sunscreen|sweater|scarf){_W_TAIL}", re.I),
    re.compile(rf"what\s+should\s+i\s+wear{_W_TAIL}", re.I),
    re.compile(rf"is it\s+(?:cold|hot|warm|raining|snowing|nice)\s+outside{_W_TAIL}", re.I),
]
_W_NOCITY = re.compile(r"^(?:here|outside|my area|my location|me|home|town|the area)$", re.I)
_W_DIRECT_PERIOD = {"today": "", "right now": "", "now": "", "currently": "", "later": "",
                    "later today": "", "tomorrow": "tomorrow", "tonight": "tonight",
                    "this week": "this week", "next week": "next week"}


def parse_weather(text: str) -> tuple[str, str | None] | None:
    """(city or '', period) for a bounded weather ask; period None = unsupported
    for a direct read (hourly/weekend) but still a weather ask."""
    t = clean(text)
    if _COMPOUND.search(t) or len(t) > 120:
        return None
    for rx in _WEATHER:
        m = rx.fullmatch(t)
        if not m:
            continue
        city = (m.groupdict().get("city") or "").strip(" .,")
        if city and _W_NOCITY.match(city):
            city = ""
        if city and re.search(r"\b(?:my|the|this|that|it|work|office|tomorrow|today|tonight|weekend|"
                              r"morning|afternoon|evening|week)\b", city, re.I):
            return None
        periods = [p.casefold() for p in (m["p1"], m["p2"]) if p]
        if len(periods) > 1:
            return None
        period: str | None = ""
        if periods:
            period = _W_DIRECT_PERIOD.get(re.sub(r"\s+", " ", periods[0]))
        return city, period
    return None


def _weather(t: str, original: str) -> CommonClaim | None:
    parsed = parse_weather(original)
    if parsed is None:
        return None
    city, period = parsed
    if city and period is not None:
        # A complete city + whole-day ask is read directly by workflow20.weather_read.
        return None
    forbid = frozenset({"web_search", "web_fetch", "http_request", "run_shell", "recall"})
    if city:
        return CommonClaim(("get_weather",), "weather in a named place -> get_weather",
                           force="get_weather", forbid=forbid)
    # No stored home location exists, so get_weather's own contract applies:
    # ask which city instead of guessing. Not forced, so a clarifying question
    # stays possible; web search is withheld so it cannot answer from the web.
    return CommonClaim(("get_weather",), "weather without a place -> get_weather (ask which city)",
                       expect=False, forbid=forbid)


# ---------------------------------------------------------------- daily brief
_BRIEF = [
    re.compile(rf"{_PREFIX}(?:(?:what|whats)(?:'s|\s+is|\s+does)?|how(?:'s|\s+is|\s+does))\s+my\s+"
               r"(?:day|today|morning|schedule\s+and\s+day)\s+(?:look(?:ing)?\s*(?:like)?|shaping\s+up)"
               r"(?:\s+(?P<period>today|tomorrow))?", re.I),
    re.compile(rf"{_PREFIX}(?:give|show|get)\s+me\s+(?:my\s+|the\s+|a\s+)?(?:morning\s+|daily\s+|evening\s+)?"
               r"(?:brief(?:ing)?|rundown|run[- ]?down|overview)(?:\s+(?:for\s+)?(?P<period>today|tomorrow))?", re.I),
    re.compile(rf"{_PREFIX}brief\s+me(?:\s+(?:on|for)\s+(?P<period>today|tomorrow))?", re.I),
    re.compile(rf"{_PREFIX}(?:my\s+)?(?:morning|daily)\s+(?:brief(?:ing)?|rundown)", re.I),
    re.compile(rf"{_PREFIX}(?:run|walk)\s+me\s+through\s+(?:my\s+)?(?P<period>today|tomorrow)", re.I),
]


def parse_brief(text: str) -> dict | None:
    """daily_brief arguments for a bare brief ask, or None."""
    t = clean(text)
    if _COMPOUND.search(t):
        return None
    for rx in _BRIEF:
        m = rx.fullmatch(t)
        if m:
            period = (m.groupdict().get("period") or "").casefold()
            return {"period": period} if period else {}
    return None


# ---------------------------------------------------------------- compute
_NUM = r"\$?\(?-?\d[\d,]*(?:\.\d+)?\)?"
_OPW = (r"(?:\s*(?:\+|\*|×|÷|\^)\s*|\s+(?:x|-|/|plus|minus|times|multiplied by|divided by|over|"
        r"to the power of|mod|modulo)\s+|\s*/\s*)")
_LEAD = r"(?:(?:what|whats)(?:'s|\s+is)?\s+|calculate\s+|compute\s+|work out\s+|how much is\s+)?"
_ARITH = re.compile(rf"{_PREFIX}{_LEAD}{_NUM}(?:{_OPW}{_NUM})+", re.I)
_PERCENT = re.compile(rf"{_PREFIX}{_LEAD}{_NUM}\s*(?:%|percent)\s+of\s+{_NUM}", re.I)
_ROOT = re.compile(rf"{_PREFIX}{_LEAD}(?:the\s+)?(?:(?:square|cube)\s+root\s+of|sqrt\s*\(?)\s*{_NUM}\)?", re.I)
_POWER = re.compile(rf"{_PREFIX}{_LEAD}{_NUM}\s+(?:squared|cubed|to the power of\s+{_NUM})", re.I)

_UNIT = (r"(?:miles?|mi|kilomet(?:er|re)s?|km|met(?:er|re)s?|cm|centimet(?:er|re)s?|mm|millimet(?:er|re)s?|"
         r"feet|foot|ft|inch(?:es)?|in|yards?|yd|pounds?|lbs?|kilograms?|kgs?|kg|grams?|g|ounces?|oz|stones?|"
         r"liters?|litres?|ml|milliliters?|gallons?|gal|cups?|quarts?|pints?|tablespoons?|teaspoons?|tbsp|tsp|"
         r"mph|kph|km/h|m/s|knots?|celsius|fahrenheit|kelvin|°\s*[cfk]|degrees?\s+(?:celsius|fahrenheit|kelvin)|"
         r"bytes?|kb|mb|gb|tb|kilobytes?|megabytes?|gigabytes?|terabytes?|acres?|hectares?|"
         r"[cfk])")
_CONVERT = [
    re.compile(rf"{_PREFIX}(?:convert\s+|what(?:'s|\s+is)\s+)?{_NUM}\s*{_UNIT}\s+(?:to|in|into|as)\s+{_UNIT}", re.I),
    re.compile(rf"{_PREFIX}how\s+many\s+{_UNIT}\s+(?:are\s+)?(?:in|is)\s+(?:an?\s+|one\s+)?(?:{_NUM}\s*)?{_UNIT}", re.I),
]
_DEFINE = [
    re.compile(r"define\s+(?:the\s+word\s+)?[\"']?(?P<w>[a-z][a-z' -]{1,40})[\"']?", re.I),
    re.compile(r"(?:what(?:'s|\s+is)\s+)?the\s+(?:definition|meaning)\s+of\s+(?:the\s+word\s+)?[\"']?(?P<w>[a-z][a-z' -]{1,40})[\"']?", re.I),
    re.compile(r"what\s+does\s+[\"']?(?P<w>[a-z][a-z'-]{1,30})[\"']?\s+mean", re.I),
]
_BAD_WORD = re.compile(r"\b(?:my|our|your|this|that|these|those|file|folder|code|function|error|"
                       r"calendar|email|mail|message|note|reminder)\b", re.I)


def _compute(t: str) -> CommonClaim | None:
    for rx in (_PERCENT, _ROOT, _POWER, _ARITH):
        if rx.fullmatch(t):
            return CommonClaim(("calculate",), "arithmetic -> calculate", force="calculate")
    for rx in _CONVERT:
        if rx.fullmatch(t):
            return CommonClaim(("convert_units",), "unit conversion -> convert_units",
                               force="convert_units")
    for rx in _DEFINE:
        m = rx.fullmatch(t)
        if m and not _BAD_WORD.search(m["w"]):
            return CommonClaim(("wikipedia_summary", "web_search"),
                               "word definition -> wikipedia_summary/web_search",
                               forbid=frozenset({"recall", "run_shell"}), private=False)
    return None


# ---------------------------------------------------------------- timers / alarms
def _timer(t: str) -> CommonClaim | None:
    if _NOT_ALARM.search(t):
        return None
    if _ALARM.fullmatch(t):
        return CommonClaim(("set_timer",), "alarm -> set_timer(kind=alarm)", force="set_timer",
                           bindings={"set_timer": {"kind": "alarm"}},
                           forbid=frozenset({"add_reminder", "add_calendar_event"}))
    if _TIMER.fullmatch(t) or _TIMER_BARE.fullmatch(t):
        return CommonClaim(("set_timer",), "countdown timer -> set_timer", force="set_timer",
                           forbid=frozenset({"add_reminder", "add_calendar_event"}))
    return None


# ---------------------------------------------------------------- volume
_VOLUME = re.compile(
    rf"{_PREFIX}(?:(?:set|turn|put|change|make|adjust)\s+(?:the\s+|my\s+)?(?:mac'?s?\s+)?"
    r"(?:volume|sound)\s+(?:to|at|up to|down to)|volume\s+(?:to|at))\s*(?P<n>\d{1,3})\s*(?:%|percent)?", re.I)


def _volume(t: str) -> CommonClaim | None:
    m = _VOLUME.fullmatch(t)
    if not m or int(m["n"]) > 100:
        return None
    from service.tools.registry import is_tool_routable
    if not is_tool_routable("mac_set_volume"):
        return None
    return CommonClaim(("mac_set_volume",), "set volume to a level -> mac_set_volume",
                       force="mac_set_volume", bindings={"mac_set_volume": {"level": int(m["n"])}})


# ---------------------------------------------------------------- email lookup
_EMAIL_LOOKUP = [
    re.compile(r"who\s+(?:e-?mailed|emailed|wrote|sent)\s+me(?:\s+(?:an?\s+)?(?:e-?mail|message|mail))?\s+"
               r"(?:about|regarding|re|on|concerning|with)\s+.{2,80}", re.I),
    re.compile(r"(?:did|has|have)\s+(?:anyone|anybody|\w+)\s+(?:e-?mail(?:ed)?|write|wrote|sent)\s+me\s+"
               r"(?:about|regarding|re)\s+.{2,80}", re.I),
    re.compile(r"(?:find|search|look up|look for|show)\s+(?:me\s+)?(?:the\s+|that\s+|any\s+|my\s+)?"
               r"e-?mails?\s+(?:from|about|regarding|re)\s+.{2,80}", re.I),
]


def _email_lookup(t: str) -> CommonClaim | None:
    if not any(rx.fullmatch(t) for rx in _EMAIL_LOOKUP):
        return None
    return CommonClaim(("view_emails", "summarize_emails"), "email lookup -> view_emails(query)",
                       force="view_emails",
                       forbid=frozenset({"write_document", "create_note", "append_note", "write_file",
                                         "send_email", "send_message", "draft_email"}))


# ---------------------------------------------------------------- calendar moves
_MOVE_VERB = r"(?:move|push|shift|bump|postpone|delay|reschedule)"
_TIME_REF = (r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|noon|midnight|tomorrow|today|tonight|"
             r"(?:next|this)\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
             r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}:\d{2}|"
             r"later|earlier|(?:an?|one|\d+)\s+(?:hour|day|week)s?\s+(?:later|earlier|from now)|"
             r"(?:the\s+)?(?:morning|afternoon|evening))")
_EVENT_NOUN = (r"(?:meeting|appointment|event|call|class|lunch|dinner|breakfast|standup|stand-up|interview|"
               r"session|dentist|doctor|checkup|check-up|sync|1:1|one-on-one|review|workshop|lecture|"
               r"practice|game|flight|reservation)")
_MOVE_SUBJECT_TIME = rf"\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm|a\.m\.|p\.m\.)(?:\s+{_EVENT_NOUN})?"
_CAL_MOVE = re.compile(
    rf"{_PREFIX}{_MOVE_VERB}\s+(?:my\s+|the\s+|our\s+)?(?P<subject>.+?)\s+(?:to|until|till|for|from\s+.+?\s+to)\s+"
    rf"(?P<dest>{_TIME_REF}(?:\s+{_TIME_REF})*(?:\s+(?:at\s+)?{_TIME_REF})?)", re.I)
_NOT_CAL_SUBJECT = re.compile(r"\b(?:files?|folders?|notes?|emails?|mail|messages?|reminders?|windows?|"
                              r"tabs?|apps?|cursor|mouse|photos?|pictures?|docs?|documents?|downloads?)\b", re.I)


def is_calendar_move(text: str) -> bool:
    """A move/push/shift/reschedule of a calendar event to a new time."""
    t = clean(text)
    if _COMPOUND.search(t):
        return False
    m = _CAL_MOVE.fullmatch(t)
    if not m or _NOT_CAL_SUBJECT.search(m["subject"]):
        return False
    subject = m["subject"].strip()
    return bool(re.fullmatch(_MOVE_SUBJECT_TIME, subject, re.I)
                or re.search(rf"\b{_EVENT_NOUN}\b", subject, re.I)
                or re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", subject, re.I))


# ---------------------------------------------------------------- add meeting
_INVITE_OR_FIND = re.compile(r"\b(?:invit\w*|find\s+(?:a\s+)?time|when\s+(?:are|is)\s+\w+\s+(?:we\s+)?free|"
                             r"free\s+time|availab\w*|both\s+free|private\s+(?:calendar\s+)?block|"
                             r"propos\w*|suggest\w*|options?)\b", re.I)
_EXPLICIT_CLOCK = re.compile(r"\bat\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|noon|midnight)\b", re.I)


def plain_timed_add(text: str) -> bool:
    """A plain 'add a meeting ... at <time>' that names no invitation or search."""
    value = mask_quoted(text)
    # "schedule/plan a meeting with X" is coordination (invitations unsupported,
    # checked proposal only); "add/put/create ... at <time>" is a plain calendar add.
    if not re.match(r"\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:add|put|create)\b", value, re.I):
        return False
    return bool(_EXPLICIT_CLOCK.search(value) and not _INVITE_OR_FIND.search(value))


# ---------------------------------------------------------------- no-tool asks
_JOKE = re.compile(
    rf"{_PREFIX}(?:tell|give|say|crack|got|do you know)\s+(?:me\s+|us\s+)?(?:an?\s+|any\s+|another\s+|one\s+)?"
    r"(?:(?:funny|good|dad|short|bad|silly|random|new|cheesy|clean)\s+)*"
    r"(?:jokes?|puns?|riddles?|knock[- ]knock\s+jokes?|fun\s+facts?|tongue[- ]twisters?)"
    r"(?:\s+(?:about|on)\s+[\w' -]{2,40})?", re.I)
_LANG = (r"(?:spanish|french|german|italian|portuguese|japanese|chinese|mandarin|cantonese|korean|hindi|"
         r"arabic|russian|dutch|swedish|norwegian|danish|finnish|polish|turkish|greek|hebrew|thai|"
         r"vietnamese|indonesian|latin|urdu|bengali|tamil|punjabi|gujarati|english|swahili|ukrainian)")
_TRANSLATE = [
    re.compile(rf"{_PREFIX}translate\s+(?P<body>[\w'’,.! -]{{1,80}}?)\s+(?:in)?to\s+{_LANG}", re.I),
    re.compile(rf"{_PREFIX}how\s+(?:do\s+(?:you|i)|would\s+you)\s+say\s+(?P<body>[\w'’,.! -]{{1,80}}?)\s+in\s+{_LANG}", re.I),
    re.compile(rf"{_PREFIX}what(?:'s|\s+is)\s+(?P<body>[\w'’,.! -]{{1,60}}?)\s+in\s+{_LANG}", re.I),
]
_TRANSLATE_REFERS = re.compile(r"\b(?:this|that|these|those|it|them|file|email|e-?mail|message|text|note|"
                               r"document|doc|pdf|page|selection|clipboard|screen|attachment|thread|chat)\b", re.I)


def is_no_tool_ask(text: str) -> bool:
    t = clean(text)
    if _COMPOUND.search(t):
        return False
    if _JOKE.fullmatch(t):
        return True
    for rx in _TRANSLATE:
        m = rx.fullmatch(t)
        if m and not _TRANSLATE_REFERS.search(m["body"]):
            return True
    return False


# ---------------------------------------------------------------- entry
def claim(text: str) -> CommonClaim | None:
    t = clean(text)
    if not t or len(t) > 160:
        return None
    if _COMPOUND.search(t):
        return None
    for fn in (_timer, _local_clock, _compute, _volume, _email_lookup):
        if (c := fn(t)) is not None:
            return c
    return _weather(t, text)

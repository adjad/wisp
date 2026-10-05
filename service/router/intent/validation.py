"""Strict shape and request-grounding checks; model confidence grants nothing."""
from __future__ import annotations
import re
from datetime import date, datetime, timedelta
from .grammar import personal_agenda_period, flexible_personal_agenda
from .schema import DOMAINS, NAMED_PERIODS, SCHEMA, SOURCE_PROPERTIES, TIME_PROPERTIES, Intent, SourceIntent, TimeScope


class InvalidIntent(ValueError):
    pass


SOURCE_WORDS = {
    "calendar": r"\b(?:calendar|calender|cal|agenda|schedule|appointments?)\b",
    "reminders": r"\breminders?\b",
    "email": r"\b(?:e-?mails?|inbox|mail)\b",
    "messages": r"\b(?:messages?|texts?|imessage|sms)\b",
    "notes": r"\bnotes?\b",
}
_NEGATIVE = r"(?:without|excluding|exclude|except|skip|no|not|don't (?:include|check|read)|do not (?:include|check|read))"
_ACTION = re.compile(r"\b(?:send|text\s+\S+\s+(?:that|saying)|email\s+\S+\s+(?:that|saying)|create|add|set|remind|delete|remove|cancel|update|save|remember|forward|reply|call|draft|compose|write|nuke|clear|wipe|purge|forget|erase|complete|finish|mark)\b", re.I)


def source_requirements(prompt: str) -> tuple[set[str], set[str]]:
    """Conservative source mentions, with nearby source-negation respected.

    Quoted literals are data, not source instructions. Complex mixed clauses
    remain the existing action/web boundary's responsibility.
    """
    from service.utterance_shape import mask_quoted
    text = mask_quoted(prompt)
    excluded, required = set(), set()
    for domain, pattern in SOURCE_WORDS.items():
        matches = list(re.finditer(pattern, text, re.I))
        for match in matches:
            prefix = text[max(0, match.start() - 65):match.start()]
            if re.search(_NEGATIVE + r"\s+(?:(?:my|the|any)\s+)?$", prefix, re.I):
                excluded.add(domain)
            else:
                required.add(domain)
    # A trailing coordinated exclusion such as "without email or texts".
    for match in re.finditer(_NEGATIVE + r"\s+([^.;!?]+)", text, re.I):
        tail = re.split(r"\b(?:but|then)\b", match.group(1), maxsplit=1, flags=re.I)[0]
        if re.fullmatch(r"[\w\s,&-]+", tail):
            for domain, pattern in SOURCE_WORDS.items():
                if re.search(pattern, tail, re.I):
                    excluded.add(domain)
    return required - excluded, excluded


def applicable_read(prompt: str, context=(), prior_tools=()) -> bool:
    from service.utterance_shape import deliberate, mask_quoted
    if deliberate(prompt) is not None:
        return False
    # Action semantics stay with the durable workflows. A negative action clause
    # can be carried as read-only context, but never opens an action capability.
    positive = re.sub(r"\b(?:don't|do not|never)\s+(?:send|email|text|save|reply|forward)[^.;!?]*", "", mask_quoted(prompt), flags=re.I)
    if _ACTION.search(positive):
        return False
    required, excluded = source_requirements(prompt)
    if re.search(r"\b(?:news|headlines|public events|web|internet|online|weather)\b", positive, re.I):
        return False
    if personal_agenda_period(prompt) or flexible_personal_agenda(prompt):
        return True
    if required or excluded:
        # Talking about a capability is not asking for a source read.
        return not bool(re.search(r"\b(?:how (?:do|does|can)|explain|what (?:is|are) (?:an? |the )?(?:email|calendar|reminder|note))\b", positive, re.I))
    fragment = bool(re.fullmatch(r"\s*(?:and |actually |no[, ]+|instead[, ]+)?(?:from |for |on |make that |only |just )?.{1,100}", prompt, re.I))
    read_tools = {"get_upcoming", "search_reminders", "view_emails", "summarize_emails", "view_messages", "summarize_messages", "search_notes", "find_free_time"}
    return fragment and bool(set(prior_tools) & read_tools) and bool(re.search(
        r"\b(?:today|tomorrow|yesterday|week|month|unread|instead|actually|only|same|those|that|them)\b", prompt, re.I))


def _object(value, allowed, required=()):
    if type(value) is not dict or set(value) - set(allowed) or not set(required) <= set(value):
        raise InvalidIntent("Unexpected or missing fields")


def _time(value) -> TimeScope:
    _object(value, TIME_PROPERTIES)
    keys = set(value)
    if not keys or (keys != {"start", "end"} and len(keys) != 1):
        raise InvalidIntent("Choose one time scope, or paired start/end dates")
    for key, item in value.items():
        if key in {"last_n_days", "rolling_days"}:
            maximum = 366 if key == "last_n_days" else 60
            if type(item) is not int or not 1 <= item <= maximum:
                raise InvalidIntent("Invalid time count")
        elif type(item) is not str:
            raise InvalidIntent("Invalid date type")
        elif key == "named":
            if item not in NAMED_PERIODS:
                raise InvalidIntent("Unsupported named period")
        elif key == "month":
            if not re.fullmatch(r"\d{4}-\d{2}", item):
                raise InvalidIntent("Invalid month")
            try:
                date.fromisoformat(item + "-01")
            except ValueError as exc:
                raise InvalidIntent("Invalid month") from exc
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item):
                raise InvalidIntent("Invalid date")
            try:
                date.fromisoformat(item)
            except ValueError as exc:
                raise InvalidIntent("Invalid date") from exc
    if keys == {"start", "end"} and value["end"] < value["start"]:
        raise InvalidIntent("Date range is reversed")
    return TimeScope(**value)


_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "thirty": 30}


def _number_requested(number: int, text: str, *, duration=False) -> bool:
    words = [str(number)] + [word for word, value in _NUMBERS.items() if value == number]
    if duration and number % 60 == 0:
        hours = number // 60
        hour_words = [str(hours)] + [word for word, value in _NUMBERS.items() if value == hours]
        if re.search(r"\b(?:" + "|".join(hour_words) + r")[- ]hours?\b", text, re.I) or number == 60 and re.search(r"\ban? (?:free )?hour\b", text, re.I):
            return True
    suffix = r"[- ](?:minutes?|mins?)\b" if duration else r"\b"
    return bool(re.search(r"\b(?:" + "|".join(words) + ")" + suffix, text, re.I))


def _requested_count(domain: str, text: str) -> int | None:
    number = r"(\d{1,3}|" + "|".join(_NUMBERS) + r")"
    words = {"calendar": r"(?:calendar )?events?|appointments?", "reminders": r"reminders?",
             "email": r"e-?mails?", "messages": r"messages?|texts?", "notes": r"notes?"}
    match = re.search(r"\b" + number + r"\s+(?:latest |recent |unread )?(?:" + words[domain] + r")\b", text, re.I)
    if not match:
        match = re.search(r"\b(?:limit(?: to)?|at most|top|latest)\s+" + number + r"\b", text, re.I)
    if not match:
        return None
    raw = match.group(1).lower()
    return int(raw) if raw.isdigit() else _NUMBERS[raw]


def _ground_time(scope: TimeScope, evidence: str, *, now=None):
    if scope.named and scope.named not in evidence.casefold():
        raise InvalidIntent("Unrequested named period")
    for key in ("last_n_days", "rolling_days"):
        count = getattr(scope, key)
        if count:
            prefix = r"(?:last|past)" if key == "last_n_days" else r"next"
            words = [str(count)] + [word for word, value in _NUMBERS.items() if value == count]
            if not re.search(r"\b" + prefix + r"\s+(?:" + "|".join(words) + r")\s+days?\b", evidence, re.I):
                raise InvalidIntent("Unrequested rolling date count")
    # Model arithmetic may only canonicalize explicit dates or named days.
    from service.workflows.reads import _MONTH_DAY, _DAY_MONTH, _MONTHS
    dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", evidence))
    anchor = now or datetime.now()
    for word, delta in (("today", 0), ("tomorrow", 1), ("yesterday", -1)):
        if re.search(r"\b" + word + r"\b", evidence, re.I):
            dates.add((anchor.date() + timedelta(days=delta)).isoformat())
    for pattern in (_MONTH_DAY, _DAY_MONTH):
        for match in pattern.finditer(evidence):
            month = next((i + 1 for i, name in enumerate(_MONTHS)
                          if name.startswith(match.group("month")[:3].lower())), None)
            try:
                dates.add(date(int(match.group("year") or anchor.year), month, int(match.group("day"))).isoformat())
            except (TypeError, ValueError):
                raise InvalidIntent("Invalid explicit date") from None
    from service.tools.timeranges import resolve_when, BadWhen
    for match in re.finditer(r"\b(?:(?:this|next)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", evidence, re.I):
        try:
            resolved, _ = resolve_when(match.group().lower() + " at 12pm", now=anchor)
            dates.add(resolved.date().isoformat())
        except (BadWhen, TypeError, ValueError):
            raise InvalidIntent("Unsupported weekday date") from None
    for key in ("date", "start", "end"):
        if getattr(scope, key) and getattr(scope, key) not in dates:
            raise InvalidIntent("Date was changed or invented")
    if scope.month and scope.month not in evidence:
        months = set()
        for match in re.finditer(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", evidence, re.I):
            months.add(f"{int(match.group(2)):04d}-{_MONTHS.index(match.group(1).lower()) + 1:02d}")
        if scope.month not in months:
            raise InvalidIntent("Month was changed or invented")


def validate_intent(value, prompt: str, *, context=(), prior_tools=(), now: datetime | None = None) -> Intent:
    _object(value, SCHEMA["properties"], SCHEMA["required"])
    if type(value["version"]) is not int or value["version"] != 1:
        raise InvalidIntent("Unknown intent version")
    if type(value["kind"]) is not str or value["kind"] not in {"read", "inline", "none", "unsupported"}:
        raise InvalidIntent("Unsupported intent kind")
    if type(value["sources"]) is not list or len(value["sources"]) > 8:
        raise InvalidIntent("Invalid sources")
    excluded = value["excluded_sources"]
    if type(excluded) is not list or any(type(d) is not str or d not in DOMAINS | {"web"} for d in excluded) or len(set(excluded)) != len(excluded):
        raise InvalidIntent("Invalid exclusions")
    unsupported = value["unsupported_constraints"]
    if type(unsupported) is not list or len(unsupported) > 8 or any(type(v) is not str or not 1 <= len(v) <= 200 for v in unsupported):
        raise InvalidIntent("Invalid unsupported constraints")
    required, explicit_excluded = source_requirements(prompt)
    if not explicit_excluded <= set(excluded):
        raise InvalidIntent("Missing explicit source exclusion")
    # Only the adjacent same-source correction may retain an old literal.
    # A new complete request or source change resets stale locations/filters.
    prior_users = [str(m.get("content", "")) for m in context if m.get("role") == "user"]
    prior_user = prior_users[-1] if prior_users else ""
    prior_required, _ = source_requirements(prior_user)
    correction = bool(re.match(r"^\s*(?:and|actually|now|instead|no[, ]|only|just|make that|same|those)\b", prompt, re.I))
    inherit = bool(prior_user and (not required or correction and required <= prior_required))
    evidence = prompt + "\n" + prior_user if inherit else prompt
    time_evidence = re.sub(r"\b(?:this|the) wk\b|\bthe week\b", "this week", evidence, flags=re.I)
    time_evidence = re.sub(r"\bnext wk\b", "next week", time_evidence, flags=re.I)
    if flexible_personal_agenda(prompt) and not re.search(r"\b(?:this|next|last) (?:week|month)\b", time_evidence, re.I):
        if re.search(r"\b(?:my|our) (?:week|wk)\b|\bfor (?:the )?(?:week|wk)\b", prompt, re.I):
            time_evidence += " this week"
    # A new sender/name replaces the previous literal instead of requiring both.
    current_named = bool(re.search(r'"[^"\n]+"|“[^”\n]+”|`[^`\n]+`|\b(?:from|with|by)\s+(?!(?:today|tomorrow|yesterday|this|last|next)\b)[+\w@]', prompt, re.I))
    filter_evidence = prompt if current_named else evidence
    contextual_sources = set()
    if inherit and not required:
        tool_domains = {"get_upcoming": {"calendar", "reminders"}, "find_free_time": {"calendar"},
                        "search_reminders": {"reminders"}, "view_emails": {"email"},
                        "summarize_emails": {"email"}, "view_messages": {"messages"},
                        "summarize_messages": {"messages"}, "search_notes": {"notes"}}
        contextual_sources = prior_required or set().union(
            *(tool_domains.get(name, set()) for name in prior_tools))
    sources = []
    for raw in value["sources"]:
        _object(raw, SOURCE_PROPERTIES, ("domain", "operation"))
        if type(raw["domain"]) is not str or type(raw["operation"]) is not str or raw["domain"] not in DOMAINS or raw["operation"] not in {"overview", "records", "free_time"}:
            raise InvalidIntent("Unsupported source or operation")
        for key in {"query", "conversation", "account", "scope"} & set(raw):
            literal = raw[key]
            if type(literal) is not str or not 1 <= len(literal) <= 200:
                raise InvalidIntent("Invalid literal filter")
            if key != "scope" and not re.search(r"(?<![\w+])" + re.escape(literal) + r"(?!\w)", evidence):
                raise InvalidIntent("Literal filter was changed or invented")
        if "scope" in raw:
            if raw["scope"] not in {"all", "today", "tomorrow", "overdue", "upcoming"}:
                raise InvalidIntent("Invalid reminder scope")
            if raw["scope"] != "all" and not re.search(r"\b" + raw["scope"] + r"\b", evidence, re.I):
                raise InvalidIntent("Unrequested reminder scope")
        if "unread" in raw and type(raw["unread"]) is not bool:
            raise InvalidIntent("Unread must be Boolean")
        for key, maximum in (("count", 100), ("minutes", 1440)):
            if key in raw and (type(raw[key]) is not int or not 1 <= raw[key] <= maximum):
                raise InvalidIntent("Invalid positive result limit or duration")
        if raw.get("query") and re.search(r"\b(?:unread|is:unread|from:)\b", raw["query"], re.I):
            raise InvalidIntent("Unread and sender constraints cannot be search syntax")
        scope = _time(raw["time"]) if "time" in raw else None
        if scope:
            _ground_time(scope, time_evidence, now=now)
        requested_count = _requested_count(raw["domain"], evidence)
        if requested_count is not None and raw.get("count") != requested_count and not unsupported:
            raise InvalidIntent("Requested result count was dropped or changed")
        if "count" in raw and raw["count"] != requested_count:
            raise InvalidIntent("Unrequested result count")
        if "minutes" in raw and not _number_requested(raw["minutes"], evidence, duration=True):
            raise InvalidIntent("Unrequested duration")
        sources.append(SourceIntent(**{**raw, **({"time": scope} if scope else {})}))
    represented = {s.domain for s in sources}
    if represented & (set(excluded) | explicit_excluded):
        raise InvalidIntent("Excluded source requested")
    if value["kind"] == "read":
        if not sources or not required <= represented:
            raise InvalidIntent("Missing requested source")
        authority = required or contextual_sources
        if authority and (represented - authority or not authority <= represented):
            raise InvalidIntent("Unrequested or missing contextual source")
        if not authority and not (personal_agenda_period(prompt) or flexible_personal_agenda(prompt)):
            raise InvalidIntent("No explicit or contextual source authority")
        unread_requested = bool(re.search(r"\bunread\b", evidence, re.I))
        if unread_requested:
            direct_domains = {domain for domain, pattern in SOURCE_WORDS.items()
                              if re.search(r"\bunread\s+(?:(?:e-?mail|text|message)\s+)?" + pattern, evidence, re.I)}
            unread_domains = direct_domains or {source.domain for source in sources
                                                if source.domain in {"email", "messages"}}
            for source in sources:
                if source.domain in unread_domains and source.unread is not True and not unsupported:
                    raise InvalidIntent("Missing typed unread filter")
        elif any(source.unread is not None for source in sources):
            raise InvalidIntent("Unrequested unread filter")
        from service.utterance_shape import mask_quoted
        filter_requested = bool(re.search(
            r"\b(?:find|search|look up|lookup|locate|named|titled|called|contains|containing|about|code|password|receipt|invoice)\b", mask_quoted(prompt), re.I))
        if filter_requested and not unsupported and any(
                source.operation != "free_time" and not (source.query or source.conversation)
                for source in sources):
            raise InvalidIntent("A requested lookup filter was dropped")
        # Exact quoted literals and explicit sender/conversation phrases must
        # survive extraction; accepting only a source would drop the request.
        literal_filters = [next(v for v in match if v) for match in re.findall(
            r'"([^"\n]+)"|“([^”\n]+)”|`([^`\n]+)`', filter_evidence)]
        for literal in literal_filters:
            if not any(literal == field for source in sources for field in
                       (source.query, source.conversation, source.account)) and not unsupported:
                raise InvalidIntent("Missing exact quoted filter")
        for match in re.finditer(r"\b(?:from|with|by)\s+([+\w@.'’-]+(?:[ \t]+[A-Z][\w.'’-]+){0,3})", filter_evidence):
            literal = match.group(1).rstrip(".,!?")
            if literal.casefold() in {"today", "tomorrow", "yesterday", "this", "last", "next", "my", "the", "unread", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"} or re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", literal):
                continue
            if not any(literal == field for source in sources for field in
                       (source.query, source.conversation, source.account)) and not unsupported:
                raise InvalidIntent("Missing exact named filter")
        if re.search(r"\b(?:except|excluding|without)\s+(?!" +
                     r"(?:my |the )?(?:calendar|reminders?|email|mail|messages?|texts?|notes?|sending|send|web)\b)\S+", prompt, re.I) and not unsupported:
            raise InvalidIntent("Negative entity filters cannot be silently dropped")
        if re.search(r"\b(?:starred|flagged|archived|read emails|unread messages|unread texts|cancelled|canceled|in [A-Z][a-z]+)\b", prompt) and not unsupported:
            raise InvalidIntent("Unsupported filter must be represented explicitly")
        period = personal_agenda_period(prompt)
        if (period or flexible_personal_agenda(prompt)) and not {"calendar"} <= represented <= {"calendar", "reminders"}:
            raise InvalidIntent("Personal agenda must use its declared local sources")
        if period and any(s.domain == "calendar" and (not s.time or s.time.named != period) for s in sources):
            raise InvalidIntent("Personal agenda date scope changed")
        # A single shared date constraint binds every requested source; compare
        # resolved ranges so inclusive exact dates and named days can agree.
        from service.workflows.compiler import _date_range
        from service.tools.timeranges import resolve_span, BadPeriod
        requested_period = _date_range(prompt)
        if requested_period:
            phrase = re.search(r"\b(?:today|tomorrow|yesterday)\s+(?:and|to|through)\s+(?:today|tomorrow|yesterday)\b", prompt, re.I)
            requested_period = phrase.group().lower() if phrase else requested_period
            other_dates = re.findall(r"\b(?:today|tomorrow|yesterday|(?:this|last|next) (?:week|month))\b", prompt, re.I)
            if len(set(v.lower() for v in other_dates)) <= 1 or phrase:
                try:
                    expected = resolve_span(requested_period, now=now)[:2]
                except BadPeriod:
                    expected = None
                def covers_shared_time(source):
                    if source.domain == "reminders" and source.scope in {"today", "tomorrow"}:
                        return resolve_span(source.scope, now=now)[:2] == expected
                    if not source.time or not source.time.period():
                        return False
                    return resolve_span(source.time.period(), now=now)[:2] == expected
                if expected is not None and any(not covers_shared_time(source) for source in sources):
                    raise InvalidIntent("Requested time scope was dropped or changed")
    elif sources:
        raise InvalidIntent("Non-read intent cannot carry source calls")
    elif value["kind"] in {"none", "inline"} and (required or contextual_sources or personal_agenda_period(prompt) or flexible_personal_agenda(prompt)):
        raise InvalidIntent("A requested source read was incorrectly dropped")
    return Intent(1, value["kind"], tuple(sources), frozenset(excluded), tuple(unsupported))

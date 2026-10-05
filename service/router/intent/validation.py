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
    if (not keys or (keys & {"start", "end"} and keys != {"start", "end"})
            or (keys != {"start", "end"} and len(keys) != 1)):
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
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
            "forty five": 45, "sixty": 60, "ninety": 90}


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
    anchor = now or datetime.now()
    requested = {_time_identity(item, now=anchor) for _, _, item in _requested_times(evidence, now=anchor)}
    if _time_identity(scope, now=anchor) not in requested:
        raise InvalidIntent("Time was changed or invented")


def _requested_times(text: str, *, now: datetime) -> list[tuple[int, int, TimeScope]]:
    """Supported user time phrases with exact bounds, independent of the model.

    ISO/natural dates, month/year, named periods, weekdays and day counts are
    supported. Open-ended comparisons, dayparts, ambiguous short dates and
    unsupported units clarify rather than compile a wider read.
    """
    from service.workflows.reads import _MONTH_DAY, _DAY_MONTH, _MONTHS
    from service.tools.timeranges import resolve_when, BadWhen
    spans = []
    def add(start, end, scope):
        if not any(start < right and end > left for left, right, _ in spans):
            spans.append((start, end, scope))
    for match in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{4}-\d{2}\b", text):
        field = "date" if len(match.group()) == 10 else "month"
        add(*match.span(), _time({field: match.group()}))
    month_words = sorted({*_MONTHS, *(m[:3] for m in _MONTHS), "sept"}, key=len, reverse=True)
    for match in re.finditer(r"\b(" + "|".join(month_words) + r")\.?\s+(\d{4})\b", text, re.I):
        month = next(i + 1 for i, name in enumerate(_MONTHS) if name.startswith(match[1][:3].lower()))
        add(*match.span(), _time({"month": f"{int(match[2]):04d}-{month:02d}"}))
    for pattern in (_MONTH_DAY, _DAY_MONTH):
        for match in pattern.finditer(text):
            if match["month"].lower() not in month_words:
                raise InvalidIntent("Unsupported date wording")
            month = next(i + 1 for i, name in enumerate(_MONTHS) if name.startswith(match["month"][:3].lower()))
            try:
                exact = date(int(match["year"] or now.year), month, int(match["day"])).isoformat()
            except ValueError:
                raise InvalidIntent("Invalid requested date") from None
            add(*match.span(), TimeScope(date=exact))
    periods = sorted(NAMED_PERIODS, key=len, reverse=True)
    for match in re.finditer(r"\b(?:" + "|".join(periods) + r")\b", text, re.I):
        add(*match.span(), TimeScope(named=match.group().lower()))
    numbers = r"(\d{1,4}|" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r")"
    for match in re.finditer(r"\b(last|past|next)\s+" + numbers + r"\s+days?\b", text, re.I):
        raw = match[2].lower()
        count = int(raw) if raw.isdigit() else _NUMBERS[raw]
        field = "rolling_days" if match[1].lower() == "next" else "last_n_days"
        add(*match.span(), _time({field: count}))
    for match in re.finditer(r"\b(?:(?:this|next)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, re.I):
        try:
            when, _ = resolve_when(match.group().lower() + " at 12pm", now=now)
        except (BadWhen, ValueError, TypeError):
            raise InvalidIntent("Unsupported requested weekday") from None
        add(*match.span(), TimeScope(date=when.date().isoformat()))
    spans.sort()
    merged = []
    for item in spans:
        if merged:
            left = merged[-1]
            between = text[left[1]:item[0]].strip().lower()
            day_pair = {left[2].named, item[2].named} == {"today", "tomorrow"}
            if (between in {"to", "through", "until", "-"} or between == "and" and
                    (day_pair or re.search(r"\bbetween\s*$", text[:left[0]], re.I))):
                def exact(scope):
                    if scope.date:
                        return scope.date
                    if scope.named in {"today", "tomorrow", "yesterday"}:
                        return (now.date() + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[scope.named])).isoformat()
                    raise InvalidIntent("Unsupported range endpoints")
                scope = _time({"start": exact(left[2]), "end": exact(item[2])})
                merged[-1] = (left[0], item[1], scope)
                continue
        merged.append(item)
    return merged



def _mask_literals(text: str, *, keep_quotes=False) -> str:
    from service.utterance_shape import quoted_spans
    for start, end in reversed(quoted_spans(text)):
        replacement = " " * (end - start)
        if keep_quotes:
            replacement = text[start] + " " * (end - start - 2) + text[end - 1]
        text = text[:start] + replacement + text[end:]
    return text

def _unquoted_queries(text: str, *, now: datetime) -> list[tuple[str, str, int, int]]:
    """Bound source about/contains/named/from/with/by/for query phrases.

    Explicit date and limit suffixes are separate constraints. Unknown query
    boundaries clarify; never approximate them by a nonempty substring.
    """
    masked = _mask_literals(text, keep_quotes=True)
    found = []
    for domain, word in SOURCE_WORDS.items():
        pattern = word + r"\s+(?:(?:that|which)\s+)?(?:are\s+)?(?P<cue>about|containing|contains|named|titled|called|from|with|by|for)\s+(?P<literal>[^;!?\n]+)"
        for match in re.finditer("(?=" + pattern + ")", masked, re.I):
            start = match.start("literal")
            candidate = text[start:match.end("literal")]
            if candidate[:1] in {'"', "'", '“', '‘', '`'}:
                continue
            if match["cue"].lower() in {"from", "for"}:
                times = _requested_times(candidate, now=now)
                if times and times[0][0] == 0:
                    continue  # from yesterday / for October 2026 is a time filter
            stops = []
            for boundary in re.finditer(r"\b(?:for|from|on|during|since|between)\s+", candidate, re.I):
                tail_times = _requested_times(candidate[boundary.end():], now=now)
                if tail_times and tail_times[0][0] == 0:
                    stops.append(boundary.start())
            for boundary in re.finditer(r"\b(?:and|plus|then)\s+|\b(?:limit(?:\s+to)?|at most)\s+|\b(?:in|using|on)\s+(?:the\s+)?account\s+", candidate, re.I):
                tail = candidate[boundary.end():]
                if boundary.group().lower().startswith(("limit", "at most", "in", "using", "on")) or any(re.search(word, tail, re.I) for word in SOURCE_WORDS.values()):
                    stops.append(boundary.start())
            end = min(stops) if stops else len(candidate)
            literal = candidate[:end].strip().rstrip(".,")
            if literal:
                found.append((domain, literal, start, start + end))
    return found



def _instruction_text(text: str, *, now: datetime) -> str:
    masked = _mask_literals(text)
    for _, _, start, end in _unquoted_queries(text, now=now):
        masked = masked[:start] + " " * (end - start) + masked[end:]
    return masked

def _source_time_requirements(text: str, domains: set[str], *, now: datetime):
    # Dates inside a complete search literal are data, not a date constraint.
    masked = _instruction_text(text, now=now)
    times = _requested_times(masked, now=now)
    anchors = sorted((m.start(), m.end(), domain) for domain, pattern in SOURCE_WORDS.items()
                     for m in re.finditer(pattern, masked, re.I))
    expected = {domain: [] for domain in domains}
    if len(domains) == 1 or not anchors:
        for domain in domains:
            expected[domain] = [scope for _, _, scope in times]
    elif len(times) == 1 and (times[0][0] >= anchors[-1][1] or times[0][1] <= anchors[0][0]):
        for domain in domains:
            expected[domain] = [times[0][2]]  # one shared leading/trailing range
    else:
        for start, end, scope in times:
            before = [a for a in anchors if a[1] <= start]
            after = [a for a in anchors if a[0] >= end]
            following = after[0] if after else None
            if following and re.fullmatch(r"[\s'’]*", masked[end:following[0]]):
                owner = following[2]
            else:
                owner = before[-1][2] if before else (following[2] if following else None)
            if owner in expected:
                expected[owner].append(scope)
    # Detect unrepresentable temporal clauses rather than silently omit them.
    unsupported_time = re.search(
        r"\b(?:this|last|next)\s+year\b|\b(?:last|past|next)\s+(?:\d+|\w+)\s+(?:weeks|months|years|hours)\b|"
        r"(?<![\d-])\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|"
        r"\b(?:at|after|before|between)\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?\b|"
        r"\b(?:morning|afternoon|evening|night|noon|midnight)\b", masked, re.I)
    for match in re.finditer(r"\b(?:before|after|since|older than|newer than)\s+", masked, re.I):
        tail_times = _requested_times(masked[match.end():], now=now)
        if tail_times and tail_times[0][0] == 0:
            unsupported_time = True
    return expected, bool(times), bool(unsupported_time)


def _time_identity(scope: TimeScope, *, now: datetime):
    if scope.rolling_days is not None:
        return ("rolling_days", scope.rolling_days)
    from .compiler import canonical_period
    from service.tools.timeranges import resolve_span, BadPeriod
    try:
        return resolve_span(canonical_period(scope, now=now), now=now)[:2]
    except (BadPeriod, ValueError, OverflowError) as exc:
        raise InvalidIntent("Unsupported or invalid requested date range") from exc


def _requested_minutes(text: str) -> set[int]:
    number = r"(?:\d+(?:\.\d+)?|" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r"|an?|half(?: an?)?)"
    pattern = r"\b(" + number + r")[-\s]+(hours?|minutes?|mins?)\b(?:\s+and\s+(" + number + r")[-\s]+(?:minutes?|mins?)\b)?"
    values = set()
    def numeric(raw):
        raw = raw.lower()
        return 0.5 if raw.startswith("half") else 1 if raw in {"a", "an"} else _NUMBERS.get(raw, float(raw) if re.fullmatch(r"\d+(?:\.\d+)?", raw) else 0)
    for match in re.finditer(pattern, text, re.I):
        minutes = numeric(match[1]) * (60 if match[2].lower().startswith("hour") else 1)
        if match[3]:
            minutes += numeric(match[3])
        if minutes != int(minutes) or not 1 <= minutes <= 1440:
            raise InvalidIntent("Unsupported requested duration")
        values.add(int(minutes))
    return values



def _reminder_scopes(text: str, *, sole=False) -> set[str]:
    if sole:
        words = re.findall(r"\b(?:overdue|past[ -]due|upcoming|all)\b", text, re.I)
    else:
        words = [next(v for v in m.groups() if v) for m in re.finditer(
            r"\b(overdue|past[ -]due|upcoming|all)\s+(?:(?:my|the)\s+)?reminders?\b|"
            r"\breminders?\s+(overdue|past[ -]due|upcoming|all)\b", text, re.I)]
    return {"overdue" if word.lower().startswith("past") else word.lower() for word in words}


def _occurrence_clauses(text: str, *, now: datetime) -> dict[str, list[str]]:
    """Retain independent repeated-source clauses, never a field cross product.

    Separators must occur outside literal data. A common leading operation and
    a sole trailing date in a coordinated source list can be shared; semicolon
    reads are independent. Unknown repeated-source boundaries clarify.
    """
    masked = _instruction_text(text, now=now)
    anchors = sorted((m.start(), m.end(), domain) for domain, pattern in SOURCE_WORDS.items()
                     for m in re.finditer(pattern, masked, re.I))
    # Adjacent aliases and full source-reference bridges name one occurrence:
    # "text messages", "appointments are on my calendar", "email in my inbox".
    # Dates, quantities, query cues, punctuation, coordination and new read
    # verbs cannot fit this grammar and retain independent clause validation.
    reference_bridge = re.compile(
        r"(?:(?:that|which)\s+)?(?:(?:is|are|was|were)\s+)?"
        r"(?:on|in|from)(?:\s+(?:my|our|the))?", re.I)
    compact = []
    for anchor in anchors:
        bridge = masked[compact[-1][1]:anchor[0]].strip() if compact else None
        if compact and compact[-1][2] == anchor[2] and (not bridge or reference_bridge.fullmatch(bridge)):
            compact[-1] = (compact[-1][0], anchor[1], anchor[2])
        else:
            compact.append(anchor)
    anchors = compact
    repeated = {domain for _, _, domain in anchors
                if sum(owner == domain for _, _, owner in anchors) > 1}
    if not repeated:
        return {}
    starts = [0]
    ends = []
    separators = []
    for previous, current in zip(anchors, anchors[1:]):
        boundaries = list(re.finditer(r"[;\n]|\b(?:and|plus|then)\b", masked[previous[1]:current[0]], re.I))
        if not boundaries:
            raise InvalidIntent("Cannot establish independent source clause bounds")
        boundary = boundaries[-1]
        ends.append(previous[1] + boundary.start())
        starts.append(previous[1] + boundary.end())
        separators.append(boundary.group().lower())
    ends.append(len(text))
    clauses = {domain: [] for domain in repeated}
    times = _requested_times(masked, now=now)
    shared_time = None
    if len(times) == 1 and not any(sep in {";", "\n", "then"} for sep in separators):
        start, end, _ = times[0]
        later_operation = re.search(r"\b(?:recap|summarize|summary|overview|digest|read|show|list|find|search|lookup|locate)\b", masked[anchors[0][1]:], re.I)
        if end <= anchors[0][0] or start >= anchors[-1][1] and not later_operation:
            # A common list suffix is shared, but a new read verb introduces
            # an independently constrained read even when joined with "and".
            shared_time = text[start:end]
    leading = text[:anchors[0][0]]
    operation = re.search(r"\b(?:recap|summarize|summary|overview|digest|read|show|list|find|search|lookup|locate)\b", leading, re.I)
    for (anchor, _, domain), start, end in zip(anchors, starts, ends):
        if domain not in clauses:
            continue
        clause = text[start:end].strip(" ;\n")
        prefix = text[start:anchor]
        if operation and not re.search(r"\b(?:recap|summarize|summary|overview|digest|read|show|list|find|search|lookup|locate)\b", prefix, re.I):
            clause = operation.group() + " " + clause
        if shared_time and not _requested_times(_instruction_text(clause, now=now), now=now):
            clause += " for " + shared_time
        clauses[domain].append(clause)
    return clauses


def _correct_occurrences(clauses, prompt: str, *, now: datetime):
    """Apply an unambiguous shared date/unread continuation to prior reads.

    A source-free change to per-read count/account/scope/duration or operation
    requires explicit clauses when multiple reads exist. Never validate the
    unchanged prior tuples while silently dropping the current correction.
    """
    if not clauses:
        return clauses
    instruction = _instruction_text(prompt, now=now)
    dates = _requested_times(instruction, now=now)
    if len(dates) > 1:
        raise InvalidIntent("Name the date for each repeated read")
    if any(_requested_count(domain, instruction) is not None for domain in clauses) or (
            _requested_minutes(instruction) or _reminder_scopes(instruction, sole=True) or
            re.search(r"\b(?:account|recap|summarize|summary|overview|digest|read|show|list|find|search|lookup|locate)\b", instruction, re.I)):
        raise InvalidIntent("Name the changed constraints for each repeated read")
    corrected = {}
    for domain, requests in clauses.items():
        corrected[domain] = []
        for clause in requests:
            if dates:
                old_dates = _requested_times(_instruction_text(clause, now=now), now=now)
                for start, end, _ in reversed(old_dates):
                    glue = re.search(r"\b(?:for|from|on|during|between)\s*$", clause[:start], re.I)
                    if glue:
                        start = glue.start()
                    clause = clause[:start] + clause[end:]
                start, end, _ = dates[0]
                clause += " for " + prompt[start:end]
            if re.search(r"\bunread\b", instruction, re.I):
                clause = "unread " + clause
            corrected[domain].append(clause)
    return corrected


def _validate_occurrences(sources, clauses, *, now: datetime):
    """Match entire clause signatures and require coverage, order independently.

    Exact duplicate model entries may still dedupe downstream. Each distinct
    authorized clause must match; no new query/date/account/count combination
    can be synthesized from individually grounded fields.
    """
    from service.utterance_shape import quoted_spans
    for domain, requests in clauses.items():
        expected = []
        for clause in requests:
            instruction = _instruction_text(clause, now=now)
            dates = {_time_identity(scope, now=now) for _, _, scope in _requested_times(instruction, now=now)}
            if len(dates) > 1:
                raise InvalidIntent("Ambiguous repeated-source date association")
            literals = {literal for owner, literal, _, _ in _unquoted_queries(clause, now=now) if owner == domain}
            accounts = []
            account_spans = []
            for match in re.finditer(r"\b(?:in|using|on)\s+(?:the\s+)?account\s+", _mask_literals(clause, keep_quotes=True), re.I):
                following = [(start, end) for start, end in quoted_spans(clause) if start >= match.end()]
                if not following or clause[match.end():following[0][0]].strip():
                    raise InvalidIntent("Use an exact quoted account for repeated reads")
                start, end = following[0]
                accounts.append(clause[start + 1:end - 1])
                account_spans.append((start, end))
            if len(accounts) > 1:
                raise InvalidIntent("Ambiguous repeated-source account")
            literals.update(clause[start + 1:end - 1] for start, end in quoted_spans(clause)
                            if (start, end) not in account_spans)
            if len(literals) > 1:
                raise InvalidIntent("Ambiguous repeated-source literal association")
            operation_match = re.search(r"\b(recap|summarize|summary|overview|digest|read|show|list|find|search|lookup|locate)\b", instruction, re.I)
            operation = None
            if domain == "calendar" and re.search(r"\b(?:free|available|availability)\b", instruction, re.I):
                operation = "free_time"
            elif operation_match:
                operation = "overview" if operation_match[1].lower() in {"recap", "summarize", "summary", "overview", "digest"} else "records"
            field = "conversation" if domain == "messages" and operation == "overview" else "query"
            scopes = _reminder_scopes(instruction, sole=True) if domain == "reminders" else set()
            minutes = _requested_minutes(instruction) if domain == "calendar" else set()
            if len(scopes) > 1 or len(minutes) > 1:
                raise InvalidIntent("Ambiguous repeated-source scope or duration")
            expected.append((operation, next(iter(dates), None), next(iter(literals), None), field,
                             next(iter(accounts), None), _requested_count(domain, instruction),
                             True if re.search(r"\bunread\b", instruction, re.I) else None,
                             next(iter(scopes), None), next(iter(minutes), None)))
        covered = set()
        for source in (s for s in sources if s.domain == domain):
            scope = source.time
            if scope is None and domain == "reminders" and source.scope in {"today", "tomorrow"}:
                scope = TimeScope(named=source.scope)
            identity = _time_identity(scope, now=now) if scope else None
            matches = []
            for index, (operation, when, literal, field, account, count, unread, reminder_scope, minutes) in enumerate(expected):
                if (operation is None or source.operation == operation) and identity == when and (
                    getattr(source, field) == literal and
                    getattr(source, "query" if field == "conversation" else "conversation") is None and
                    source.account == account and source.count == count and source.unread is unread and
                    (source.scope == reminder_scope or domain == "reminders" and reminder_scope is None
                     and source.scope in {None, "all", "today", "tomorrow"}) and source.minutes == minutes):
                    matches.append(index)
            if not matches:
                raise InvalidIntent("Read fields do not match one complete requested clause")
            covered.update(matches)
        if covered != set(range(len(expected))):
            raise InvalidIntent("Missing independently requested read clause")


def _validate_required_constraints(sources, text: str, *, now: datetime, reminder_text=None, duration_text=None, bound_domains=()):
    domains = {source.domain for source in sources}
    expected, _, unsupported_time = _source_time_requirements(text, domains, now=now)
    if unsupported_time:
        raise InvalidIntent("Requested time-of-day or range is not representable")
    for domain in domains - set(bound_domains):
        entries = [s for s in sources if s.domain == domain]
        wanted = {_time_identity(scope, now=now) for scope in expected[domain]}
        actual = []
        for source in entries:
            scope = source.time
            if domain == "reminders" and scope is None and source.scope in {"today", "tomorrow"}:
                scope = TimeScope(named=source.scope)
            identity = _time_identity(scope, now=now) if scope else None
            if (wanted and identity not in wanted) or (not wanted and identity is not None):
                raise InvalidIntent("Requested source time was dropped, changed or added")
            if identity is not None:
                actual.append(identity)
        if not wanted <= set(actual):
            raise InvalidIntent("Missing requested date range")
    instruction_text = _instruction_text(text, now=now)
    if "reminders" in domains and "reminders" not in bound_domains:
        requested = _reminder_scopes(reminder_text if reminder_text is not None else instruction_text,
                                     sole=domains == {"reminders"})
        entries = [s for s in sources if s.domain == "reminders"]
        if requested and (any(s.scope not in requested for s in entries) or not requested <= {s.scope for s in entries}):
            raise InvalidIntent("Requested reminder scope was dropped or changed")
        if not requested and any(s.scope in {"overdue", "upcoming"} for s in entries):
            raise InvalidIntent("Unrequested reminder scope")
    duration_text = duration_text if duration_text is not None else instruction_text
    minutes = _requested_minutes(duration_text)
    free_requested = bool(re.search(r"\b(?:free|available|availability)\b", instruction_text, re.I))
    entries = [s for s in sources if s.domain == "calendar" and s.domain not in bound_domains]
    if (free_requested or minutes) and any(s.operation != "free_time" for s in entries):
        raise InvalidIntent("Requested availability operation was changed")
    free_entries = [s for s in entries if s.operation == "free_time"]
    if free_entries and not (free_requested or minutes):
        raise InvalidIntent("Unrequested availability operation")
    if free_entries and minutes and (any(s.minutes not in minutes for s in free_entries) or not minutes <= {s.minutes for s in free_entries}):
        raise InvalidIntent("Requested free-slot duration was dropped or changed")


def validate_intent(value, prompt: str, *, context=(), prior_tools=(), now: datetime | None = None) -> Intent:
    now = now or datetime.now()
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
    contextual_sources = set()
    if inherit and not required:
        tool_domains = {"get_upcoming": {"calendar", "reminders"}, "find_free_time": {"calendar"},
                        "search_reminders": {"reminders"}, "view_emails": {"email"},
                        "summarize_emails": {"email"}, "view_messages": {"messages"},
                        "summarize_messages": {"messages"}, "search_notes": {"notes"}}
        contextual_sources = prior_required or set().union(
            *(tool_domains.get(name, set()) for name in prior_tools))
    filter_prompt = prompt
    if inherit and not required:
        fragment = re.match(r"^\s*(?:(?:actually|now|instead|only|just|no|make that)[, ]+)?"
                            r"(?P<cue>about|containing|contains|named|titled|called|from|with|by|for)\s+", prompt, re.I)
        if fragment:
            tail = prompt[fragment.end():]
            times = _requested_times(tail, now=now)
            is_time = fragment["cue"].lower() in {"from", "for"} and times and times[0][0] == 0
            if not is_time:
                if len(contextual_sources) != 1:
                    raise InvalidIntent("Query replacement needs one unambiguous contextual source")
                if _occurrence_clauses(prior_user, now=now):
                    raise InvalidIntent("Name which prior read the replacement changes")
                filter_prompt = next(iter(contextual_sources)) + " " + prompt[fragment.start("cue"):]
    # A current complete literal replaces the prior literal. Other inherited
    # constraints retain their separate evidence and source authority.
    current_named = bool(_unquoted_queries(filter_prompt, now=now)) or bool(re.search(r'"[^"\n]+"|“[^”\n]+”|`[^`\n]+`|\b(?:from|with|by)\s+(?!(?:today|tomorrow|yesterday|this|last|next)\b)[+\w@]', filter_prompt, re.I))
    filter_evidence = filter_prompt if current_named else evidence
    if inherit and current_named and not re.search(r"\baccount\b", _mask_literals(filter_prompt), re.I):
        # Replacing a query does not erase an independently requested account.
        # Carry only the exact quoted account phrase, never the old query.
        for match in re.finditer(r"\b(?:in|using|on)\s+(?:the\s+)?account\s+", _mask_literals(prior_user, keep_quotes=True), re.I):
            from service.utterance_shape import quoted_spans
            accounts = [(start, end) for start, end in quoted_spans(prior_user) if start >= match.end()]
            if not accounts or prior_user[match.end():accounts[0][0]].strip():
                raise InvalidIntent("Cannot establish inherited account bounds")
            filter_evidence += " " + prior_user[match.start():accounts[0][1]]
    occurrence_text = filter_prompt if current_named or not inherit else prior_user
    occurrence_clauses = _occurrence_clauses(occurrence_text, now=now)
    if inherit and not current_named:
        occurrence_clauses = _correct_occurrences(occurrence_clauses, filter_prompt, now=now)
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
        if raw["domain"] not in occurrence_clauses and requested_count is not None and raw.get("count") != requested_count and not unsupported:
            raise InvalidIntent("Requested result count was dropped or changed")
        if raw["domain"] not in occurrence_clauses and "count" in raw and raw["count"] != requested_count:
            raise InvalidIntent("Unrequested result count")
        if "minutes" in raw and raw["minutes"] not in _requested_minutes(evidence):
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
                if source.domain in unread_domains and source.domain not in occurrence_clauses and source.unread is not True and not unsupported:
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
        from service.utterance_shape import quoted_spans
        literal_filters = [filter_evidence[start + 1:end - 1] for start, end in quoted_spans(filter_evidence)]
        for literal in literal_filters:
            if not any(literal == field for source in sources for field in
                       (source.query, source.conversation, source.account)) and not unsupported:
                raise InvalidIntent("Missing exact quoted filter")
        requested_queries = _unquoted_queries(filter_evidence, now=now)
        for domain, literal, _, _ in requested_queries:
            if not any(source.domain == domain and literal in (source.query, source.conversation, source.account)
                       for source in sources) and not unsupported:
                raise InvalidIntent("Complete requested query was dropped or changed")
        domain_filters = {domain: {literal for owner, literal, _, _ in requested_queries if owner == domain}
                          for domain in represented}
        quote_anchors = sorted((m.start(), m.end(), domain) for domain, word in SOURCE_WORDS.items()
                               for m in re.finditer(word, _mask_literals(filter_evidence), re.I))
        for start, end in quoted_spans(filter_evidence):
            previous = [a for a in quote_anchors if a[1] <= start and a[2] in represented]
            following = [a for a in quote_anchors if a[0] >= end and a[2] in represented]
            owner = previous[-1][2] if previous else (following[0][2] if following else None)
            if owner is None and len(represented) == 1:
                owner = next(iter(represented))
            if owner in domain_filters:
                domain_filters[owner].add(filter_evidence[start + 1:end - 1])
        for source in sources:
            wanted = domain_filters[source.domain]
            fields = [field for field in (source.query, source.conversation, source.account) if field is not None]
            if wanted and (not fields or any(field not in wanted for field in fields)) and not unsupported:
                raise InvalidIntent("An additional read dropped or changed the requested filter")
        if filter_requested and not literal_filters and not requested_queries and not unsupported and any(
                source.operation != "free_time" and source.query for source in sources):
            raise InvalidIntent("Cannot establish complete unquoted query bounds")
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
        if not unsupported:
            current_time_text = re.sub(r"\b(?:this|the) wk\b|\bthe week\b", "this week", filter_prompt, flags=re.I)
            current_time_text = re.sub(r"\bnext wk\b", "next week", current_time_text, flags=re.I)
            if flexible_personal_agenda(prompt) and re.search(r"\b(?:my|our) (?:week|wk)\b|\bfor (?:the )?(?:week|wk)\b", prompt, re.I):
                if not _requested_times(current_time_text, now=now):
                    current_time_text += " this week"
            _, has_current_time, _ = _source_time_requirements(current_time_text, represented, now=now)
            constraint_text = current_time_text
            if inherit and not has_current_time:
                # Preserve adjacent same-source filters but prefer any current
                # date correction over old temporal context.
                constraint_text += "\n" + prior_user
            current_instruction = _instruction_text(filter_prompt, now=now)
            current_reminder_scopes = _reminder_scopes(current_instruction, sole=represented == {"reminders"})
            current_minutes = _requested_minutes(current_instruction)
            _validate_occurrences(sources, occurrence_clauses, now=now)
            _validate_required_constraints(sources, constraint_text, now=now, bound_domains=occurrence_clauses,
                reminder_text=current_instruction if current_reminder_scopes else None,
                duration_text=current_instruction if current_minutes else None)
    elif sources:
        raise InvalidIntent("Non-read intent cannot carry source calls")
    elif value["kind"] in {"none", "inline"} and (required or contextual_sources or personal_agenda_period(prompt) or flexible_personal_agenda(prompt)):
        raise InvalidIntent("A requested source read was incorrectly dropped")
    return Intent(1, value["kind"], tuple(sources), frozenset(excluded), tuple(unsupported))

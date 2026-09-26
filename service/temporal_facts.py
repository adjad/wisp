"""Conservative, attributed extraction from captured text (A08a, no integrations).

``extract_temporal_facts`` never reads a source, the clock, host timezone, or
application state. It uses only caller inputs and the standard timezone database.
This is an English *evidence extractor*, not a task-instruction resolver or an
exhaustive natural-language parser. It supports ISO/calendar dates, English month
names, numeric slash dates, today/tomorrow/yesterday, weekdays, ``in N days/weeks``
and ``in N hours/minutes``, clocks, and explicit clock/date ranges. Unsupported
recognized expressions remain unknown; arbitrary prose need not yield a fact.

Dates without years stay yearless. Slash order is never selected when ambiguous.
Bare/next weekdays remain ambiguous. Date-only values never become midnight or
end-of-day. Clock ranges never infer meridiem or roll an end into the next day.
An aware capture time anchors relative expressions but does not supply a source
timezone. Explicit source zones take precedence over the caller's fallback zone.

Offsets are half-open Python character offsets into the untouched input. A fact's
span covers its temporal expression; its context span preserves the entire local
clause, including kind cues, negation, conditions, and alternative mentions.
``resolved`` means the supported expression has a unique interpretation at its
stated precision, not that the source's claim is true. Consumers must retain the
uncertainties, provenance, precision, and range boundary semantics. This private
slice deliberately does not establish the A01 public contract.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone as dt_timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXTRACTOR_VERSION = "a08a-1"
MAX_TEXT_LENGTH = 100_000
MAX_FACTS = 256


@dataclass(frozen=True)
class EvidenceContext:
    source_type: str
    source_id: str
    source_reference: str | None = None


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class Provenance:
    evidence: EvidenceContext | None
    captured_at: datetime | None
    timezone: str | None
    extractor_version: str = EXTRACTOR_VERSION


@dataclass(frozen=True)
class TemporalValue:
    year: int | None = None
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None
    timezone: str | None = None
    precision: str = "unknown"  # date, minute, instant (elapsed duration), unknown
    # UTC ISO strings; two candidates on a DST fold, none on a gap/unknown.
    instants: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemporalFact:
    kind: str  # due, event, availability, unknown
    span: SourceSpan
    context_span: SourceSpan
    start: TemporalValue
    end: TemporalValue | None
    relation: str  # on, by, before, after, until, range, alternatives
    end_boundary: str | None  # inclusive, exclusive, unspecified (ranges only)
    status: str  # resolved, partial, unknown
    uncertainties: tuple[str, ...]
    provenance: Provenance


@dataclass(frozen=True)
class ExtractionResult:
    facts: tuple[TemporalFact, ...]
    status: str  # resolved, partial, unknown
    issues: tuple[str, ...] = ()


_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)
_MONTHS = {name: index for index, name in enumerate(_MONTH_NAMES, 1)}
_MONTHS.update({name[:3]: index for index, name in enumerate(_MONTH_NAMES, 1)})
_MONTH = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday".split()
_WEEKDAY = "(?:" + "|".join(_WEEKDAYS) + ")"
_DATE = (
    rf"(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|"
    rf"{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{2,4}})?|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}(?:\s+\d{{2,4}})?|"
    rf"today|tomorrow|yesterday|(?:(?:next|this)\s+)?{_WEEKDAY}|"
    rf"in\s+\d+\s+(?:days?|weeks?|hours?|minutes?))"
)
_CLOCK = r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2}|noon|midnight|\d{1,2})"
# Consume an entire zone-shaped token, including malformed offsets, so no
# supported prefix (e.g. UTC in UTC+2) can silently replace the source evidence.
# Abbreviations are lexically ambiguous regardless of case or whether they are
# familiar. Reserve grammar connectors/qualifiers, not a dictionary of zones.
_ZONE = (
    r"(?!(?i:TO|AT|ON|OR|BY|IF|IN|AND|FROM|UNTIL|THROUGH|SO|ISH|APPROX)\b)"
    r"(?:(?i:UTC|GMT)[A-Za-z0-9_+:\-]*|[A-Za-z_]+/[A-Za-z0-9_+/:\-]*|"
    r"[+-][0-9:]+|(?i:Z|[A-Z]{2,5}))(?![\w/+:\-])"
)
_TIME_PART = (
    rf"(?P<clock>{_CLOCK})(?:\s*(?P<zone>(?-i:{_ZONE})))?"
    rf"(?:\s*(?P<time_connector>-|–|—|to|until)\s*(?P<end_clock>{_CLOCK})"
    rf"(?:\s*(?P<end_zone>(?-i:{_ZONE})))?)?"
)
_TIME_TAIL = rf"(?:\s*(?-i:{_ZONE}))?(?:\s*(?:-|–|—|to|until)\s*{_CLOCK}(?:\s*(?-i:{_ZONE}))?)?"
_TIME_PREFIX = r"(?:at|from|after|before|by|until)"
_EXPR = re.compile(
    rf"(?<![\w/:-])(?:"
    rf"(?P<date>{_DATE})(?:\s*(?P<date_connector>to|through|until|–|—)\s*(?P<end_date>{_DATE}))?"
    rf"(?:(?:\s+(?:at\s+|from\s+)?|T){_TIME_PART})?"
    rf"(?:\s+(?P<date_zone>(?-i:{_ZONE})))?"
    rf"|(?P<time_only>{_TIME_PREFIX}\s+{_CLOCK}{_TIME_TAIL})"
    rf"|(?P<standalone>(?:\d{{1,2}}:\d{{2}}(?:\s*(?:am|pm))?|\d{{1,2}}\s*(?:am|pm)|noon|midnight){_TIME_TAIL})"
    rf"|(?P<vague>tonight|(?:this |next )?(?:morning|afternoon|evening|week|month|year)|"
    rf"soon|later|end of (?:day|week|month)|in\s+\w+\s+(?:days?|weeks?|hours?|minutes?)))"
    rf"(?![\w/:]|-\d)", re.I,
)
_TIME = re.compile(rf"(?:{_TIME_PREFIX}\s+)?{_TIME_PART}$", re.I)
_CLAUSE = re.compile(r"[^;\n.!?]+[.!?]?")
_CUE = re.compile(
    r"\b(?P<due>due|deadline|submit|payable|deliver by)\b|"
    r"\b(?P<availability>available|availability|free|unavailable)\b|"
    r"\b(?P<event>meeting|meet|appointment|event|call|interview|starts?|begins?|conference)\b",
    re.I,
)


def _codes(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for group in groups for code in group))


def _zone(name: str | None):
    if name is None:
        return None, ("missing_timezone",)
    if not isinstance(name, str) or not name.strip():
        return None, ("invalid_timezone",)
    if name.upper() in {"UTC", "GMT", "Z"}:
        return dt_timezone.utc, ()
    offset = re.fullmatch(r"(?:UTC)?([+-])(\d{2}):?(\d{2})", name, re.I)
    if offset:
        hours, minutes = int(offset[2]), int(offset[3])
        if hours > 23 or minutes > 59:
            return None, ("invalid_timezone",)
        delta = timedelta(hours=hours, minutes=minutes)
        return dt_timezone(delta if offset[1] == "+" else -delta), ()
    if "/" not in name:
        return None, ("ambiguous_timezone",)
    try:
        return ZoneInfo(name), ()
    except (ZoneInfoNotFoundError, ValueError):
        return None, ("invalid_timezone",)


def _date_value(raw: str, captured_at: datetime | None, zone_name: str | None) -> TemporalValue:
    value = raw.lower()
    year = month = day = None
    issues: tuple[str, ...] = ()
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
        year, month, day = map(int, value.split("-"))
    elif "/" in value:
        parts = value.split("/")
        first, second = map(int, parts[:2])
        if len(parts) == 3:
            if len(parts[2]) != 4:
                issues = ("ambiguous_year",)
            else:
                year = int(parts[2])
        if first <= 12 and second <= 12 and first != second and first > 0 and second > 0:
            return TemporalValue(year=year, precision="date", uncertainties=_codes(
                issues, ("missing_year",) if year is None else (), ("ambiguous_date_order",)))
        month, day = (second, first) if first > 12 else (first, second)
    elif match := re.fullmatch(rf"({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{2,4}}))?", value):
        month, day = _MONTHS[match[1]], int(match[2])
        if match[3]:
            if len(match[3]) == 4:
                year = int(match[3])
            else:
                issues = ("ambiguous_year",)
    elif match := re.fullmatch(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})(?:\s+(\d{{2,4}}))?", value):
        day, month = int(match[1]), _MONTHS[match[2]]
        if match[3]:
            if len(match[3]) == 4:
                year = int(match[3])
            else:
                issues = ("ambiguous_year",)
    else:
        if any(day_name in value for day_name in _WEEKDAYS):
            return TemporalValue(precision="date", uncertainties=("ambiguous_weekday",))
        zone, zone_issues = _zone(zone_name)
        if captured_at is None or zone is None:
            return TemporalValue(uncertainties=_codes(
                ("missing_capture_anchor",) if captured_at is None else (), zone_issues))
        try:
            anchor = captured_at.astimezone(zone)
            if value in {"today", "tomorrow", "yesterday"}:
                target = anchor.date() + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[value])
            else:
                match = re.fullmatch(r"in\s+(\d+)\s+(days?|weeks?|hours?|minutes?)", value)
                if match is None:
                    return TemporalValue(uncertainties=("unsupported_expression",))
                count, unit = int(match[1]), match[2].rstrip("s")
                if unit in {"hour", "minute"}:
                    instant = captured_at.astimezone(dt_timezone.utc) + timedelta(**{unit + "s": count})
                    local = instant.astimezone(zone)
                    return TemporalValue(local.year, local.month, local.day, local.hour, local.minute,
                                         zone_name, "instant", (instant.isoformat(),))
                target = anchor.date() + timedelta(days=count * (7 if unit == "week" else 1))
            year, month, day = target.year, target.month, target.day
        except (OverflowError, ValueError):
            return TemporalValue(uncertainties=("out_of_range",))
    if year is None:
        issues = _codes(issues, ("missing_year",))
    try:
        # Leap year 2000 validates yearless Feb 29 without assuming its year.
        date(year if year is not None else 2000, month, day)
    except (ValueError, TypeError):
        issues = _codes(issues, ("invalid_date",))
    return TemporalValue(year, month, day, precision="date", uncertainties=issues)


def _clock_value(raw: str, base: TemporalValue, zone_name: str | None) -> TemporalValue:
    value = raw.lower().replace(" ", "")
    issues = base.uncertainties
    if base.hour is not None:
        issues = _codes(issues, ("conflicting_time_components",))
    hour = minute = None
    if value in {"noon", "midnight"}:
        hour, minute = (12 if value == "noon" else 0), 0
    else:
        match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", value)
        if match:
            hour, minute = int(match[1]), int(match[2] or 0)
            if minute > 59 or (match[3] and not 1 <= hour <= 12) or hour > 23:
                issues = _codes(issues, ("invalid_time",))
            elif match[3]:
                hour = hour % 12 + (12 if match[3] == "pm" else 0)
            elif not match[2]:
                issues = _codes(issues, ("ambiguous_meridiem",))
        else:
            issues = _codes(issues, ("invalid_time",))
    if base.year is None or base.month is None or base.day is None:
        issues = _codes(issues, ("missing_date",))
    zone, zone_issues = _zone(zone_name)
    issues = _codes(issues, zone_issues)
    instants: tuple[str, ...] = ()
    if not issues:
        try:
            wall = datetime(base.year, base.month, base.day, hour, minute)
            candidates = set()
            for fold in (0, 1):
                instant = wall.replace(tzinfo=zone, fold=fold).astimezone(dt_timezone.utc)
                if instant.astimezone(zone).replace(tzinfo=None) == wall:
                    candidates.add(instant.isoformat())
            instants = tuple(sorted(candidates))
            if not instants:
                issues = ("nonexistent_local_time",)
            elif len(instants) > 1:
                issues = ("ambiguous_local_time",)
        except (ValueError, OverflowError):
            issues = ("out_of_range",)
    return replace(base, hour=hour, minute=minute, timezone=zone_name, precision="minute",
                   instants=instants, uncertainties=issues)


def _span(text: str, start: int, end: int) -> SourceSpan:
    return SourceSpan(start, end, text[start:end])


def _status(start: TemporalValue, end: TemporalValue | None, issues: tuple[str, ...]) -> str:
    if not issues:
        return "resolved"
    values = (start,) if end is None else (start, end)
    return "partial" if any(v.month is not None or v.hour is not None for v in values) else "unknown"


def extract_temporal_facts(
    text: str, *, captured_at: datetime, timezone: str | None,
    evidence: EvidenceContext,
) -> ExtractionResult:
    """Extract attributed candidates without selecting an ambiguous interpretation.

    Invalid text/oversized input returns an unknown result. Invalid metadata is
    explicitly reported; otherwise supported absolute expressions are retained.
    ``captured_at`` must be an aware datetime; ``timezone`` is an explicit caller
    fallback (IANA name/UTC/offset), never implicitly taken from the timestamp.
    Relative hours/minutes are elapsed UTC durations; days/weeks are local dates.
    Facts in one clause linked by correction/alternative language retain both
    mentions and carry ``conflicting_mentions``; no winner is inferred. Different
    sentences/subjects are not reconciled. The parser does not assert truth.
    """
    if not isinstance(text, str):
        return ExtractionResult((), "unknown", ("invalid_text",))
    if len(text) > MAX_TEXT_LENGTH:
        return ExtractionResult((), "unknown", ("text_limit_exceeded",))
    issues: tuple[str, ...] = ()
    if not isinstance(captured_at, datetime) or captured_at.tzinfo is None or captured_at.utcoffset() is None:
        captured_at = None
        issues = ("invalid_captured_at",)
    if (not isinstance(evidence, EvidenceContext)
            or not isinstance(evidence.source_type, str) or not evidence.source_type.strip()
            or not isinstance(evidence.source_id, str) or not evidence.source_id.strip()
            or (evidence.source_reference is not None and not isinstance(evidence.source_reference, str))):
        evidence = None
        issues = _codes(issues, ("invalid_evidence",))
    if timezone is not None and (not isinstance(timezone, str) or _zone(timezone)[0] is None):
        issues = _codes(issues, ("invalid_timezone_context",))
        timezone = None
    provenance = Provenance(evidence, captured_at, timezone)
    facts: list[TemporalFact] = []
    for clause in _CLAUSE.finditer(text):
        clause_text = clause.group()
        cues = list(_CUE.finditer(clause_text))
        clause_facts: list[TemporalFact] = []
        for match in _EXPR.finditer(clause_text):
            if len(facts) + len(clause_facts) >= MAX_FACTS:
                return ExtractionResult(tuple(facts + clause_facts), "partial", _codes(issues, ("fact_limit_exceeded",)))
            prior_cues = [cue for cue in cues if cue.end() <= match.start()]
            kind = prior_cues[-1].lastgroup if prior_cues else "unknown"
            local_issues = () if kind != "unknown" else ("unknown_kind",)
            if re.search(r"\b(?:not|never|unavailable|cancelled|canceled)\b|\bno longer\b|\b(?:can't|won't|isn't)\b", clause_text, re.I):
                local_issues = _codes(local_issues, ("negated_or_cancelled",))
            if re.search(r"\b(?:if|maybe|might|tentative|possibly|could)\b|\?", clause_text, re.I):
                local_issues = _codes(local_issues, ("conditional_or_tentative",))
            if re.search(r"\b(?:about|around|approximately|roughly)\b", clause_text, re.I):
                local_issues = _codes(local_issues, ("approximate_expression",))
            zone_name = match["zone"] or match["end_zone"] or match["date_zone"] or timezone
            base = _date_value(match["date"], captured_at, zone_name) if match["date"] else TemporalValue()
            end = _date_value(match["end_date"], captured_at, zone_name) if match["end_date"] else None
            clock, end_clock = match["clock"], match["end_clock"]
            end_zone = match["end_zone"] or zone_name
            connector = match["date_connector"] or match["time_connector"]
            if match["time_only"] or match["standalone"]:
                time_match = _TIME.fullmatch(match["time_only"] or match["standalone"])
                clock, end_clock = time_match["clock"], time_match["end_clock"]
                zone_name = time_match["zone"] or time_match["end_zone"] or timezone
                end_zone = time_match["end_zone"] or zone_name
                connector = time_match["time_connector"]
            if match["date_zone"] and not clock:
                base = replace(base, timezone=zone_name, uncertainties=_codes(base.uncertainties, _zone(zone_name)[1]))
                if end is not None:
                    end = replace(end, timezone=zone_name, uncertainties=_codes(end.uncertainties, _zone(zone_name)[1]))
            if match["vague"]:
                base = TemporalValue(uncertainties=("unsupported_expression",))
            if clock:
                end_base = end if end is not None else base
                start = _clock_value(clock, base, zone_name)
                if end_clock:
                    end = _clock_value(end_clock, end_base, end_zone)
                elif end is not None:
                    local_issues = _codes(local_issues, ("ambiguous_range_attachment",))
            else:
                start = base
            if clock and match["date_zone"]:
                local_issues = _codes(local_issues, ("conflicting_timezones",))
                start = replace(start, instants=())
                if end is not None:
                    end = replace(end, instants=())
            # A hyphen followed by HH:MM can be either a numeric zone offset
            # or a range endpoint. Only an ISO dateTclock fixes that reading.
            iso_datetime = bool(match["date"] and re.match(
                r"\d{4}-\d{1,2}-\d{1,2}T", match.group(), re.I))
            if clock and not iso_datetime and any(
                    zone and re.fullmatch(r"-\d{2}:\d{2}", zone)
                    for zone in (zone_name, end_zone)):
                local_issues = _codes(local_issues, ("ambiguous_offset_or_range",))
                start = replace(start, instants=())
                if end is not None:
                    end = replace(end, instants=())
            # Do not downgrade malformed/unsupported adjacent temporal syntax
            # to a confident date or a supported prefix of a clock/zone.
            tail = clause_text[match.end():]
            unsupported = re.match(r"\s*(?:(?:at|from)\s+\S+|[+:]\S+|\d[\d:]+\S*)", tail, re.I)
            span_end = match.end()
            approximate_suffix = re.match(
                r"\s*(?:[-–—]ish\b|or\s+so\b|approx(?:imately)?\b\.?)", tail, re.I)
            if approximate_suffix:
                local_issues = _codes(local_issues, ("approximate_expression",))
                span_end += approximate_suffix.end()
            if "approximate_expression" in local_issues:
                # Preserve the stated wall components, but never advertise a
                # single exact instant for a qualified/approximate mention.
                start = replace(start, instants=())
                if end is not None:
                    end = replace(end, instants=())
            if unsupported:
                local_issues = _codes(local_issues, ("unsupported_time_expression",))
                start = replace(start, instants=())
                if end is not None:
                    end = replace(end, instants=())
                span_end += unsupported.end()
            prefix = clause_text[:match.start()].rstrip()
            relation_match = re.search(r"\b(by|before|after|until)\s*$", prefix, re.I)
            time_relation = re.match(r"(by|before|after|until)\b", match.group(), re.I)
            relation = "range" if end is not None else (
                (relation_match or time_relation)[1].lower() if relation_match or time_relation else "on")
            end_boundary = None if end is None else {
                "until": "exclusive", "through": "inclusive",
            }.get((connector or "").lower(), "unspecified")
            if (kind == "due" and end is not None
                    and re.search(r"\bfrom\s*$", prefix, re.I)):
                # A deadline moving from one value to another is not a valid
                # availability window. The wording between the due cue and
                # 'from' need not be a verb this bounded parser recognizes.
                local_issues = _codes(local_issues, ("ambiguous_due_range",))
                relation, end_boundary = "alternatives", None
                start = replace(start, instants=())
                end = replace(end, instants=())
            if end is not None:
                try:
                    a = (start.year, start.month, start.day, start.hour or 0, start.minute or 0)
                    b = (end.year, end.month, end.day, end.hour or 0, end.minute or 0)
                    # Compare instants when both endpoints are unambiguous:
                    # different source zones can reverse wall-clock ordering.
                    reversed_range = (end.instants[0] < start.instants[0]
                                      if len(start.instants) == len(end.instants) == 1
                                      else all(v is not None for v in (*a, *b)) and b < a)
                    if reversed_range:
                        local_issues = _codes(local_issues, ("reversed_range",))
                except TypeError:
                    pass
            fact_issues = _codes(issues, local_issues, start.uncertainties, end.uncertainties if end else ())
            clause_facts.append(TemporalFact(
                kind, _span(text, clause.start() + match.start(), clause.start() + span_end),
                _span(text, clause.start(), clause.end()), start, end, relation, end_boundary,
                _status(start, end, fact_issues), fact_issues, provenance,
            ))
        # Full datetime ranges outside the bounded clock/date range grammar
        # remain separate attributed endpoints, explicitly marked partial.
        for index in range(1, len(clause_facts)):
            left, right = clause_facts[index - 1:index + 1]
            between = text[left.span.end:right.span.start]
            if re.fullmatch(r"\s*(?:to|until|through|[-–—])\s*", between, re.I):
                for target in (index - 1, index):
                    fact = clause_facts[target]
                    clause_facts[target] = replace(fact, status="partial", uncertainties=_codes(
                        fact.uncertainties, ("unsupported_range",)))
        alternatives = re.search(
            r"\b(?:or(?!\s+so\b)|instead|changed|correction|rather|moved|"
            r"rescheduled|postponed|corrected|revised)\b", clause_text, re.I)
        if alternatives and (len(clause_facts) > 1 or any(f.end is not None for f in clause_facts)):
            clause_facts = [replace(fact, status="partial", relation="alternatives", end_boundary=None,
                                   uncertainties=_codes(fact.uncertainties, ("conflicting_mentions",)))
                            for fact in clause_facts]
        facts.extend(clause_facts)
    if not facts:
        return ExtractionResult((), "unknown", _codes(issues, ("no_temporal_expression",)))
    status = "resolved" if all(f.status == "resolved" for f in facts) else (
        "unknown" if all(f.status == "unknown" for f in facts) else "partial")
    return ExtractionResult(tuple(facts), status, issues)

"""Compile supported reads deterministically, or explain the missing capability."""
from __future__ import annotations
from datetime import datetime, timedelta
from .schema import Intent, SourceIntent, TimeScope
from .validation import InvalidIntent


class UnsupportedRead(InvalidIntent):
    """A valid interpretation contains a filter the current read tool cannot do."""


def _reject(source: SourceIntent, supported: set[str]):
    for key in ("query", "conversation", "account", "unread", "count", "minutes", "scope"):
        if getattr(source, key) is not None and key not in supported:
            raise UnsupportedRead(f"The {source.domain} read cannot apply the requested {key} filter.")


def canonical_period(scope: TimeScope | None, *, now: datetime | None = None) -> str | None:
    if not scope:
        return None
    if scope.named == "tonight":
        raise UnsupportedRead("The read tools cannot apply a night-only time filter. Should I check today's upcoming agenda?")
    if scope.named in {"this weekend", "next weekend"}:
        anchor = (now or datetime.now()).date()
        saturday = anchor + timedelta(days=5 - anchor.weekday())
        if scope.named == "next weekend":
            saturday += timedelta(days=7)
        return f"{saturday.isoformat()} to {(saturday + timedelta(days=1)).isoformat()}"
    return scope.period()


def compile_intent(intent: Intent, *, now: datetime | None = None):
    """Returns the existing execute_read contract ([(tool, arguments)], response).

    No model-produced tool names enter this compiler. Every call is validated
    against the current registry before any execution. No tools run here.
    """
    if intent.kind != "read":
        return [], ""
    if intent.unsupported_constraints:
        raise UnsupportedRead("I cannot apply these read constraints: " + "; ".join(intent.unsupported_constraints) + ".")
    calls = []
    from service.tools.timeranges import resolve_span, BadPeriod
    from service.tools.registry import get_tool, _validate_args
    # get_upcoming natively combines calendar/reminders. Coalesce ONLY matching
    # declared scopes; it is never authority to add an unrequested source.
    combined = {}
    consumed = set()
    for index, source in enumerate(intent.sources):
        if source.domain != "calendar" or source.operation == "free_time" or source.account:
            continue
        for other_index, other in enumerate(intent.sources):
            if other_index in consumed or other.domain != "reminders":
                continue
            if (other.time == source.time and other.query == source.query and
                    other.operation == source.operation and other.scope is None and
                    all(getattr(other, key) is None for key in
                        ("account", "conversation", "unread", "count", "minutes"))):
                combined[index] = other_index
                consumed.add(other_index)
                break
    for index, source in enumerate(intent.sources):
        if index in consumed:
            continue
        args = {}
        period = canonical_period(source.time, now=now)
        rolling = source.time.rolling_days if source.time else None
        if period:
            try:
                start, end, _ = resolve_span(period, now=now)
            except (BadPeriod, ValueError, OverflowError) as exc:
                raise InvalidIntent("Unsupported or invalid date scope") from exc
        if source.operation == "free_time":
            if source.domain != "calendar":
                raise UnsupportedRead("Free-time lookup is supported only for the calendar.")
            _reject(source, {"minutes"})
            # Existing exact-day free-time tool supports a day, not an arbitrary
            # named week/month. Do not turn a week into a rolling days=7 window.
            if period and (not source.time or not (source.time.date or source.time.named in {"today", "tomorrow"})):
                raise UnsupportedRead("Free-time lookup currently supports one exact day; which day should I check?")
            if rolling and rolling > 14:
                raise UnsupportedRead("Free-time lookup supports a rolling horizon of at most 14 days.")
            if source.minutes is not None and not period and source.minutes < 5:
                raise UnsupportedRead("A rolling free-time lookup supports slots of at least five minutes; please name an exact day.")
            name = "find_free_time"
            if period:
                args["period"] = period
            if rolling:
                args["days"] = rolling
            if source.minutes:
                args["minutes"] = source.minutes
        elif source.domain == "calendar":
            _reject(source, {"query", "account"})
            if period and end <= (now or datetime.now()).timestamp():
                raise UnsupportedRead("Calendar reads currently show active upcoming items; they cannot retrieve an elapsed day or historical range.")
            name = "get_upcoming"
            if period:
                args["period"] = period
            if rolling:
                args["days"] = rolling
            # A source entry is authority for that domain only. General agenda
            # extraction lists reminders explicitly when they are requested.
            args["calendar_only"] = index not in combined
            if source.query is not None:
                args["query"] = source.query
            if source.account is not None:
                args["account"] = source.account
        elif source.domain == "reminders":
            _reject(source, {"query", "scope"})
            native_scope = source.scope or "all"
            if source.time:
                anchor = (now or datetime.now()).date()
                if source.time.named in {"today", "tomorrow"}:
                    native_scope = source.time.named
                elif source.time.date in {anchor.isoformat(), (anchor + timedelta(days=1)).isoformat()}:
                    native_scope = "today" if source.time.date == anchor.isoformat() else "tomorrow"
                else:
                    raise UnsupportedRead("Reminder search cannot apply that date range; ask for today, tomorrow, overdue, or upcoming reminders.")
                if source.scope and source.scope != native_scope:
                    raise UnsupportedRead("The reminder scope conflicts with the requested date.")
            name = "search_reminders"
            args = {"query": source.query or "", "scope": "past_due" if native_scope == "overdue" else native_scope}
        elif source.domain == "email":
            _reject(source, {"account", "unread", "count"} | ({"query"} if source.operation == "records" else set()))
            if rolling:
                raise UnsupportedRead("Email reads need a named or exact date range rather than a rolling future horizon.")
            if source.operation == "overview" and (
                    source.unread is True and (period or source.count is not None)
                    or period and source.count is not None):
                raise UnsupportedRead("Email summaries cannot combine the requested date, unread, and result-count filters. Ask for matching email records instead.")
            name = "summarize_emails" if source.operation == "overview" else "view_emails"
            for field in ("account", "unread", "count", "query"):
                if getattr(source, field) is not None:
                    args[field] = getattr(source, field)
            if source.query is not None:
                args["strict_match"] = True
            if period:
                args["period"] = period
        elif source.domain == "messages":
            _reject(source, {"count", "conversation"} if source.operation == "overview" else {"count", "query"})
            if rolling:
                raise UnsupportedRead("Message reads need a named or exact date range rather than a rolling future horizon.")
            if source.operation == "overview" and period and source.count is not None and source.conversation is None:
                raise UnsupportedRead("A message overview cannot combine a date range with an explicit result limit unless a conversation is selected.")
            name = "summarize_messages" if source.operation == "overview" else "view_messages"
            if source.operation == "records" and source.query is not None:
                registered = get_tool(name)
                if not registered or "strict_match" not in registered.parameters.get("properties", {}):
                    raise UnsupportedRead("Exact filtered message reads are unavailable; I will not substitute unrelated messages.")
                args["strict_match"] = True
            for field in ("count", "query", "conversation"):
                if getattr(source, field) is not None:
                    args[field] = getattr(source, field)
            if period:
                args["period"] = period
        elif source.domain == "notes":
            _reject(source, {"query", "count"})
            if rolling:
                raise UnsupportedRead("Note reads cannot apply a rolling future horizon.")
            name = "search_notes"
            for field in ("count", "query"):
                if getattr(source, field) is not None:
                    args[field] = getattr(source, field)
            if period:
                args["period"] = period
        else:
            raise InvalidIntent("Unsupported read domain")
        tool = get_tool(name)
        if tool is None:
            raise UnsupportedRead(f"The required {source.domain} read tool is unavailable.")
        if error := _validate_args(tool, args):
            raise InvalidIntent("Compiled arguments do not match the registered read tool")
        # Only identical calls are redundant; same tool/different scope remains.
        if (name, args) not in calls:
            calls.append((name, args))
    return calls, ""

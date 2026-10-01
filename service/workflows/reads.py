"""Narrow, deterministic read intents from the replay; no effect tools."""
from __future__ import annotations

import re
from datetime import date, datetime

from service.router.router import _reminder_is_excluded, calendar_is_excluded
from service.utterance_shape import deliberate
from service.safety.policy import Tier, decide
from service.tools.registry import DisplayOnlyToolResult, get_tool, run_tool, classify_tool_outcome
from service.tasks.models import TaskExecution
from service.workflows.compiler import (
    _normalize, _date_range, _source_args, _OUTBOUND, _INLINE_EMAIL_SUMMARY,
    _STOCK_IDENTIFIER, _KNOWN_STOCK_TICKERS,
    extract_stock_symbols,
)


# A negative stock clause must be removed before symbol extraction. If its
# positive half is incomplete, the structured read asks instead of fetching
# an instrument the user explicitly excluded.
_STOCK_EXCLUSION = re.compile(
    r"\b(?:but\s+not|all\s+but|exclud(?:e|es|ed|ing)|omit(?:ting)?|"
    r"except(?:\s+for)?|other\s+than|without|not)\b",
    re.I,
)
_EXCLUDED_STOCK_IDENTIFIER = (
    r"(?i:[A-Za-z][A-Za-z0-9.'’&-]*(?:\s+(?!(?:and|or|email|text|send)\b)"
    r"(?:[A-Za-z][A-Za-z0-9.'’&-]*|&)){0,3})(?=\s+(?:stocks?|shares?)\b)"
    r"|"
    # A lowercase unknown name can be part of a list whose stock noun appears
    # only on the last item: "not palantir or tesla shares". Stop before a
    # connector, but never reinterpret common source/action words as tickers.
    r"(?i:(?!(?:and|or|email|text|send|from|my|the|notes?|using|without)\b)"
    r"[a-z][a-z0-9.'’&-]*(?:\s+(?!(?:and|or|email|text|send|from|my|the|notes?|"
    r"using|without)\b)[a-z][a-z0-9.'’&-]*){0,3})(?=\s*(?:,|and|or)\s+)"
    r"|"
    r"(?-i:\$?[A-Z][A-Za-z0-9]*(?:[.'’&-][A-Za-z0-9]+)*"
    r"(?:\s+[A-Z][A-Za-z0-9]*(?:[.'’&-][A-Za-z0-9]+)*){0,3})"
    rf"|(?:{_STOCK_IDENTIFIER})(?![A-Za-z])"
)
_EXCLUDED_STOCK_LIST = (
    rf"(?:the\s+)?(?:{_EXCLUDED_STOCK_IDENTIFIER})(?:\s+(?:stocks?|shares?))?"
    rf"(?:\s*(?:,|and|or)\s*(?:the\s+)?(?:{_EXCLUDED_STOCK_IDENTIFIER})"
    rf"(?:\s+(?:stocks?|shares?))?)*"
)
_BARE_STOCK_NAME = (
    r"(?!(?:from|for|with|using|my|the|notes?|today|yesterday|now|news|"
    r"market|email|text|send|and|or)\b)[a-z][a-z0-9.'’&-]*"
    r"(?:\s+(?!(?:and|or|email|text|send|from|for|with|using|notes?)\b)"
    r"[a-z][a-z0-9.'’&-]*){0,3}"
)
_BARE_STOCK_EXCLUSION = re.compile(
    rf"(?:the\s+)?{_BARE_STOCK_NAME}"
    rf"(?:\s*(?:,|and|or)\s*(?:the\s+)?{_BARE_STOCK_NAME})*"
    r"(?=\s*(?:$|[,.;!?]|\band\s+(?:email|text|send)\b))",
    re.I,
)


def stock_symbol_key(value: str) -> str:
    """Compare known aliases, tickers and free-form company names consistently."""
    name = re.sub(r"\s+(?:stocks?|shares?)$", "", value.strip(), flags=re.I)
    name = re.sub(r"^the\s+", "", name, flags=re.I).strip(" ,")
    name = re.sub(r"\s*,?\s+(?:inc\.?|incorporated|corp\.?|corporation|co\.?|"
                  r"company|ltd\.?|limited)$", "", name, flags=re.I)
    resolved = extract_stock_symbols(name, standalone=True)
    return (resolved[0] if len(resolved) == 1 else name).casefold()


def stock_exclusion_clauses(text: str) -> list[tuple[int, frozenset[str]]]:
    """Locate named stock exclusions without treating unrelated negation as stock scope."""
    clauses = []
    for marker in _STOCK_EXCLUSION.finditer(text):
        tail = text[marker.end():].lstrip(" ,")
        match = re.match(_EXCLUDED_STOCK_LIST, tail, re.I)
        if re.search(
                r"\b(?:stocks?|shares?|portfolio|prices?|quotes?|equities)\b",
                text[:marker.start()], re.I):
            bare = _BARE_STOCK_EXCLUSION.match(tail)
            if bare and (not match or bare.end() > match.end()):
                match = bare
        if match:
            names = frozenset(stock_symbol_key(part) for part in re.split(
                r"\s*(?:,|\band\b|\bor\b)\s*", match.group(), flags=re.I) if part)
            if names:
                clauses.append((marker.start(), names))
    return clauses


def excluded_stock_symbols(text: str) -> frozenset[str]:
    """Resolve named negative stock clauses even when another action follows.

    This is also used immediately before stock tool execution, because mixed
    delivery requests bypass the standalone structured-read compiler.
    """
    return frozenset(symbol for _, names in stock_exclusion_clauses(text)
                     for symbol in names)


def permitted_stock_symbols(text: str, symbols: list[str]) -> list[str]:
    """Fail closed on excluded name/ticker aliases before any stock fetch."""
    clauses = stock_exclusion_clauses(text)
    if not clauses:
        return symbols
    excluded = {symbol for _, names in clauses for symbol in names}
    included = {stock_symbol_key(symbol) for symbol in
                extract_stock_symbols(text[:clauses[0][0]])}
    known_tickers = {symbol.casefold() for symbol in _KNOWN_STOCK_TICKERS}
    unresolved = any(symbol not in known_tickers for symbol in excluded)
    return [symbol for symbol in symbols
            if (key := stock_symbol_key(str(symbol))) not in excluded
            and (not included or key in included)
            and not (unresolved and (not included or key not in known_tickers))]


def _stock_request_without_exclusions(text: str, period: str) -> tuple[str, list[str]] | None:
    exclusion = _STOCK_EXCLUSION.search(text)
    if exclusion is None:
        return text, []
    excluded_text = text[exclusion.end():].strip()
    if period:
        excluded_text = re.sub(
            r"\s+(?:(?:for|during|in)\s+)?(?:the\s+)?" + re.escape(period) + r"$",
            "", excluded_text, flags=re.I).strip()
    if not re.fullmatch(_EXCLUDED_STOCK_LIST, excluded_text, re.I):
        return None
    excluded_symbols = list(excluded_stock_symbols(text))
    if not excluded_symbols:
        return None
    return text[:exclusion.start()].strip(" ,"), excluded_symbols


def adjacent_stock_response(last_assistant: str, last_tools: str) -> str:
    """Expose stock symbols only from the immediately preceding stock reply."""
    tools = {name.strip() for name in last_tools.split(",") if name.strip()}
    return last_assistant if "get_stock_price" in tools else ""


def _stock_read_request(text: str, period: str) -> bool:
    """Recognize complete quote/performance asks, not financial topic words."""
    body = re.sub(r"^(?:and\s+)?(?:(?:please|can\s+you|could\s+you)\s+)?", "", text, flags=re.I)
    if period:
        body = re.sub(r"\s+(?:(?:for|from|over|in|during|as\s+of)\s+)?(?:the\s+)?"
                      + re.escape(period) + r"$", "", body, flags=re.I)
    subject = r"(?:[\w.,&'-]+\s+){0,8}(?:stocks?|shares?|portfolio)"
    names = r"[\w.,&'-]+(?:\s+[\w.,&'-]+){0,8}"
    quote = (
        r"(?:what(?:'s|\s+is|\s+are|\s+was|\s+were)|show(?:\s+me)?|check|get|fetch)\s+"
        r"(?:(?:my|the|current|latest)\s+)*(?:"
        + subject + r"\s+(?:prices?|quotes?)|"
        r"(?:stocks?|shares?)\s+(?:prices?|quotes?)\s+(?:of|for)\s+" + names + r"|"
        r"(?:prices?|quotes?)\s+(?:of|for)\s+" + subject + r")"
    )
    performance_verb = (
        r"(?:doing|do|done|perform(?:ing|ed)?|trend(?:ing|ed)?|"
        r"mov(?:e[ds]?|ing)|chang(?:e[ds]?|ing))"
    )
    performance = (
        r"(?:how\s+(?:are|is|did|has|have)\s+" + subject + r"\s+" + performance_verb
        + r"|what\s+(?:did|has|have)\s+" + subject + r"\s+" + performance_verb + r")"
    )
    if not (re.fullmatch(quote, body, re.I) or re.fullmatch(performance, body, re.I)):
        return False
    # A market-wide question has no portfolio identity to inherit. Only a
    # named equity or an explicit personal/demonstrative reference may do so.
    return bool(
        extract_stock_symbols(body)
        or re.search(
            r"\b(?:(?:my|our|these|those)\s+(?:stocks?|shares?|portfolio)|"
            r"(?:this|that)\s+(?:stock|share|portfolio))\b",
            body,
            re.I,
        )
    )


# A structured read may answer ONLY the request it fully understands. These
# helpers decide that; anything they decline goes on to the router and the
# model, which see every clause. Declining is always safe (the request is still
# answered, one model call slower); answering a different request than the one
# asked is not.
_MONTHS = ("january february march april may june july august september october "
           "november december").split()
_MONTH_DAY = re.compile(
    r"\b(?:on\s+|for\s+)?(?P<month>" + "|".join(m[:3] + r"[a-z]*" for m in _MONTHS) + r")\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?::|am\b|pm\b|o'clock))", re.I)
_DAY_MONTH = re.compile(
    r"\b(?:on\s+|for\s+)?(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
    r"(?P<month>" + "|".join(m[:3] + r"[a-z]*" for m in _MONTHS) + r")\b", re.I)
# Date words the period grammar cannot express. If one is present and was not
# resolved to an exact date, the shortcut must not guess a window.
_UNRESOLVED_DATE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d+|"
    r"\d{1,2}(?:st|nd|rd|th)\b|\d{1,2}/\d{1,2}|\d{4}-\d{2}-\d{2}|"
    r"in\s+(?:\d+|a|an|a\s+couple\s+of|a\s+few)\s+(?:days?|weeks?|months?)|"
    r"(?:the\s+)?day\s+after\s+tomorrow|a\s+week\s+from)\b", re.I)
_OTHER_SOURCE = re.compile(
    r"\b(?:messages?|texts?|texted|imessages?|e-?mails?|e-?mailed|inbox|mail|notes?|weather|news|"
    r"stocks?|forecast|contacts?|files?|who|whom)\b", re.I)
_SECOND_REQUEST = re.compile(
    r"\b(?:and|then|also|plus|but|after\s+that)\s+(?:please\s+)?"
    r"(?:what|show|check|list|who|when|where|how|tell|send|email|text|message|remind|"
    r"delete|remove|cancel|add|set|create|move|reschedule|archive|mark|forward|reply|"
    r"summari[sz]e|read|open|call)\b", re.I)
_EXCLUSION_CUE = re.compile(
    r"\b(?:do\s*n[o']?t|don[’']?t|without|except|excluding|exclude|skip|ignore|"
    r"leave\s+out|no)\b", re.I)


def _named_date(text: str, today: date) -> str | None:
    """'October 12' / '12th of Oct' as an exact YYYY-MM-DD, or None. A date that
    has already passed this year is not guessed into next year."""
    match = _MONTH_DAY.search(text) or _DAY_MONTH.search(text)
    if not match:
        return None
    month = next((i + 1 for i, name in enumerate(_MONTHS)
                  if name.startswith(match.group("month")[:3].lower())), None)
    try:
        found = date(today.year, month, int(match.group("day")))
    except (TypeError, ValueError):
        return None
    return found.isoformat() if found >= today else None


def _calendar_read_args(text: str, period: str, today: date | None = None) -> dict | None:
    """get_upcoming arguments for a calendar read the shortcut FULLY understands,
    or None to hand the request on. Handles one source, one clause, one time scope
    (including 'today and tomorrow' and an exact date), and a reminder exclusion."""
    today = today or datetime.now().date()
    if _OTHER_SOURCE.search(text) or _SECOND_REQUEST.search(text):
        return None
    reminders_excluded = _reminder_is_excluded(text)
    if _EXCLUSION_CUE.search(text) and not reminders_excluded:
        return None
    scope = text
    if re.search(r"\btoday\b.*\btomorrow\b|\btomorrow\b.*\btoday\b", scope, re.I):
        args: dict = {"period": "today and tomorrow"}
    elif (exact := _named_date(scope, today)) is not None:
        args = {"period": exact}
    elif _UNRESOLVED_DATE.search(scope):
        return None
    else:
        args = _source_args("calendar", text, period)
    if reminders_excluded:
        args = {**args, "calendar_only": True}
    return args


_NOT_A_SENDER = re.compile(
    r"\b(?:and|or|then|also|but|yesterday|today|tomorrow|tonight|last|this|next|since|before|after|"
    r"between|during|from|week|weeks|month|months|year|years|day|days|hour|hours|ago|"
    r"delete|remove|trash|archive|mark|move|forward|reply|send|unsubscribe|flag|read|unread|"
    r"summari[sz]e|that|which|who|where|with|about|over|under|only|just)\b", re.I)


def _sender_phrase(raw: str) -> str | None:
    """The tail of 'purchases from X' when it is a plain sender name; otherwise
    None. Anything carrying a time word, a conjunction or another action means the
    request has more in it than a sender, and must not be flattened into one."""
    candidate = raw.strip()
    if (not candidate or len(candidate.split()) > 4 or _NOT_A_SENDER.search(candidate)
            or re.search(r"\d{1,2}[/-]\d{1,2}|\d{4}", candidate)):
        return None
    return candidate


def compile_read(prompt: str, *, last_user: str = "", last_tools: str = "",
                 last_stock_response: str = ""):
    if deliberate(prompt) is not None:
        return None
    text = _normalize(prompt).strip(" *_.?!")
    read_prefix = re.match(r"(?:can you |could you |please )?(?:what|show|check|list|compare)\b", text, re.I)
    if _OUTBOUND.search(text) and not _INLINE_EMAIL_SUMMARY.match(text) and not read_prefix:
        return None
    if re.search(r"\b(?:create|set|add|remove|delete|cancel|update|remind)\b", text, re.I):
        return None
    period = _date_range(text)
    if re.fullmatch(r"(?:my\s+)?daily\s+(?:summary|brief|digest)", text, re.I):
        return [("daily_brief", {})], ""
    if re.fullmatch(r"(?:show|put|keep)?\s*(?:it|this|that)?\s*(?:here\s+)?on\s+wisp", text, re.I):
        if any(name in last_tools for name in ("summarize_emails", "summarize_messages", "daily_brief")):
            return [], "The summary above is already displayed here in Wisp; nothing was sent elsewhere."
    if (re.search(r"\b(?:calendar|my schedule)\b", text, re.I)
            and re.search(r"\b(?:what|show|check|list)\b", text, re.I)
            and not calendar_is_excluded(text)):
        # Only a calendar read the shortcut fully understands runs here. A second
        # source ("and messages"), a named date it cannot resolve, a second request
        # or an exclusion it cannot honour all continue to the router and the model.
        args = _calendar_read_args(text, period)
        if args is None:
            return None
        return [("get_upcoming", args)], ""
    if re.search(r"\bstock\s+markets?\b", text, re.I):
        # A market question is not a request for the preceding portfolio's
        # tickers. Preserve only the exact news-topic substitution; other
        # market intents (movement, explanation, forecast, or a new time span)
        # must reach the router/model with their original wording intact.
        if (re.fullmatch(r"(?:and\s+)?in\s+the\s+stock\s+market", text, re.I)
                and "web_search" in {name.strip() for name in last_tools.split(",")}
                and re.search(r"\bnews\b", last_user, re.I)):
            return [("web_search", {"query": "stock market news today"})], ""
        return None
    stock_request = _stock_request_without_exclusions(text, period)
    stock_text, _ = stock_request or (text, [])
    compare_context = (re.match(r"compare\s+(?:this|that|it)\b", stock_text, re.I)
                       and ("get_stock_price" in last_tools or "stock" in last_user.lower()))
    stock_context = (stock_request is not None and (
        _stock_read_request(stock_text, period)
        or compare_context
        or (bool(re.fullmatch(r"(?:all|both|these|those)\s+(?:of\s+)?them", stock_text, re.I))
            and bool(last_stock_response))))
    if not stock_context and _STOCK_EXCLUSION.search(text) and _stock_read_request(text, period):
        return [], "Which stock symbols or company names should I include?"
    if stock_context and not re.search(r"\bnews\b", text, re.I):
        args = _source_args("stock", stock_text, period)
        prior_tools = {name.strip() for name in last_tools.split(",") if name.strip()}
        prior_symbols = (
            extract_stock_symbols(last_user)
            if "get_stock_price" in prior_tools or compare_context else []
        ) or extract_stock_symbols(last_stock_response)
        if (not args.get("symbols")
                and re.search(r"\b(?:this|that)\s+(?:stock|share)\b", stock_text, re.I)
                and len(prior_symbols) != 1):
            return [], "Which stock symbol or company name do you mean?"
        args["symbols"] = permitted_stock_symbols(
            text, args.get("symbols") or prior_symbols)
        if (not args.get("period")
                and not re.search(r"\b(?:current|latest|live|now)\b", stock_text, re.I)
                and (prior_period := _date_range(last_user))):
            args["period"] = prior_period
        if not args["symbols"]:
            return [], "Which stock symbols or company names should I include?"
        return [("get_stock_price", args)], ""
    if re.search(r"\b(?:email|inbox)\b", text, re.I) and re.search(r"\b(?:summaries|summary|digest)\b", text, re.I):
        return [("summarize_emails", _source_args("email", text, period))], ""
    if re.search(r"\b(?:email|inbox)\b", text, re.I) and re.search(r"\bpurchases?\s+from\b", text, re.I):
        match = re.search(r"\bpurchases?\s+from\s+([\w -]+)$", text, re.I)
        sender = _sender_phrase(match.group(1)) if match else None
        if sender:
            return [("view_emails", {"query": sender, "strict_match": True})], ""
        return None
    return None


async def execute_read(compiled, emit, *, test_mode=False):
    planned, response = compiled
    calls, results, displays = [], [], []
    if response:
        return TaskExecution("needs_input", response)
    has_display_only = False
    for name, args in planned:
        tool = get_tool(name)
        if not tool:
            return TaskExecution("failed", f"Required read tool {name} is unavailable.")
        policy = decide(tool.category, args, tool=name)
        call = {"id": f"read_{len(calls)}", "name": name, "args": args,
                "decision": policy.tier.value, "reason": policy.reason}
        calls.append(call)
        await emit({"type": "tool_call", **call})
        if test_mode:
            raw = "Dry run only — no data read and no tool executed."
        elif policy.tier is not Tier.ALLOW:
            raw = f"Read blocked by policy: {policy.reason}"
        else:
            raw = await run_tool(tool, args)
        displays.append(str(raw))
        if isinstance(raw, DisplayOnlyToolResult):
            raw = raw.model_text
            has_display_only = True
        status = ("planned" if test_mode else "denied" if policy.tier is not Tier.ALLOW
                  else classify_tool_outcome(name, raw).status)
        item = {"id": call["id"], "name": name, "result": raw, "status": status}
        results.append(item)
        await emit({"type": "tool_result", **item})
    failed = any(r["status"] not in {"succeeded", "no_match", "planned"} for r in results)
    response = "\n\n".join(r["result"] for r in results)
    if has_display_only:
        response = DisplayOnlyToolResult("\n\n".join(displays), model_text=response,
                                         artifact_kind="news" if len(results) == 1 else "mixed")
    return TaskExecution("planned" if test_mode else "failed" if failed else "completed",
                         response, calls, results)

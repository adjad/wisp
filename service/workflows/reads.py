"""Narrow, deterministic read intents from the replay; no effect tools."""
from __future__ import annotations

import re

from service.router.router import calendar_is_excluded
from service.safety.policy import Tier, decide
from service.tools.registry import DisplayOnlyToolResult, get_tool, run_tool, classify_tool_outcome
from service.tasks.models import TaskExecution
from service.workflows.compiler import (
    _normalize, _date_range, _source_args, _OUTBOUND, _INLINE_EMAIL_SUMMARY,
    _STOCK_IDENTIFIER,
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
    r"(?-i:\$?[A-Z][A-Za-z0-9]*(?:[.'’&-][A-Za-z0-9]+)*"
    r"(?:\s+[A-Z][A-Za-z0-9]*(?:[.'’&-][A-Za-z0-9]+)*){0,3})"
    rf"|(?:{_STOCK_IDENTIFIER})(?![A-Za-z])"
)
_EXCLUDED_STOCK_LIST = (
    rf"(?:the\s+)?(?:{_EXCLUDED_STOCK_IDENTIFIER})(?:\s+(?:stocks?|shares?))?"
    rf"(?:\s*(?:,|and|or)\s*(?:the\s+)?(?:{_EXCLUDED_STOCK_IDENTIFIER})"
    rf"(?:\s+(?:stocks?|shares?))?)*"
)


def stock_symbol_key(value: str) -> str:
    """Compare known aliases, tickers and free-form company names consistently."""
    name = re.sub(r"\s+(?:stocks?|shares?)$", "", value.strip(), flags=re.I)
    name = re.sub(r"^the\s+", "", name, flags=re.I).strip(" ,")
    resolved = extract_stock_symbols(name, standalone=True)
    return (resolved[0] if len(resolved) == 1 else name).casefold()


def stock_exclusion_clauses(text: str) -> list[tuple[int, frozenset[str]]]:
    """Locate named stock exclusions without treating unrelated negation as stock scope."""
    clauses = []
    for marker in _STOCK_EXCLUSION.finditer(text):
        tail = text[marker.end():].lstrip(" ,")
        match = re.match(_EXCLUDED_STOCK_LIST, tail, re.I)
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
    excluded_symbols = extract_stock_symbols(excluded_text)
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


def compile_read(prompt: str, *, last_user: str = "", last_tools: str = "",
                 last_stock_response: str = ""):
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
        return [("get_upcoming", _source_args("calendar", text, period))], ""
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
    stock_text, excluded_symbols = stock_request or (text, [])
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
        excluded = {symbol.upper() for symbol in excluded_symbols}
        args["symbols"] = [symbol for symbol in (args.get("symbols") or prior_symbols)
                           if symbol.upper() not in excluded]
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
        if match:
            return [("view_emails", {"query": match.group(1).strip(), "strict_match": True})], ""
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

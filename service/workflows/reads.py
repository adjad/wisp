"""Narrow, deterministic read intents from the replay; no effect tools."""
from __future__ import annotations

import re

from service.safety.policy import Tier, decide
from service.tools.registry import get_tool, run_tool, classify_tool_outcome
from service.tasks.models import TaskExecution
from service.workflows.compiler import (
    _normalize, _date_range, _source_args, _OUTBOUND, _INLINE_EMAIL_SUMMARY,
    extract_stock_symbols,
)


def compile_read(prompt: str, *, last_user: str = "", last_tools: str = ""):
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
    if re.search(r"\b(?:calendar|my schedule)\b", text, re.I) and re.search(
            r"\b(?:what|show|check|list)\b", text, re.I):
        return [("get_upcoming", _source_args("calendar", text, period))], ""
    if (re.search(r"\bstock market\b", text, re.I)
            and "web_search" in last_tools and re.search(r"\bnews\b", last_user, re.I)):
        return [("web_search", {"query": "stock market news today"})], ""
    stock_context = (re.search(r"\b(?:stocks?|share prices?|portfolio)\b", text, re.I)
                     or (re.match(r"compare\s+(?:this|that|it)\b", text, re.I)
                         and ("get_stock_price" in last_tools or "stock" in last_user.lower())))
    if stock_context and not re.search(r"\bnews\b", text, re.I):
        args = _source_args("stock", text, period)
        args["symbols"] = args.get("symbols") or extract_stock_symbols(last_user)
        if not args["symbols"]:
            return [], "Which stock symbols or company names should I include?"
        return [("get_stock_price", args)], ""
    if re.search(r"\b(?:news|headlines)\b", text, re.I):
        return [("web_search", {"query": text})], ""
    if re.search(r"\b(?:email|inbox)\b", text, re.I) and re.search(r"\b(?:summaries|summary|digest)\b", text, re.I):
        return [("summarize_emails", _source_args("email", text, period))], ""
    if re.search(r"\b(?:email|inbox)\b", text, re.I) and re.search(r"\bpurchases?\s+from\b", text, re.I):
        match = re.search(r"\bpurchases?\s+from\s+([\w -]+)$", text, re.I)
        if match:
            return [("view_emails", {"query": match.group(1).strip(), "strict_match": True})], ""
    return None


async def execute_read(compiled, emit, *, test_mode=False):
    planned, response = compiled
    calls, results = [], []
    if response:
        return TaskExecution("needs_input", response)
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
        status = ("planned" if test_mode else "denied" if policy.tier is not Tier.ALLOW
                  else classify_tool_outcome(name, raw).status)
        item = {"id": call["id"], "name": name, "result": raw, "status": status}
        results.append(item)
        await emit({"type": "tool_result", **item})
    failed = any(r["status"] not in {"succeeded", "no_match", "planned"} for r in results)
    return TaskExecution("planned" if test_mode else "failed" if failed else "completed",
                         "\n\n".join(r["result"] for r in results), calls, results)

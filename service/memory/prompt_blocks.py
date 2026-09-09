"""Small system-prompt fragments shared by every text path — main.py's plain
chat branches AND the agent loop (service/agent/loop.py) — so the same fact is
computed once instead of drifting between two near-identical copies.
"""
from __future__ import annotations

from datetime import datetime


def now_line(*, resolve_hint: bool = False) -> str:
    """Current date/time, appended to the system prompt so trivial questions
    like "what's the time" or "what day is it" can be answered directly from
    context — no tool call needed, and no more wrongly claiming no clock
    access.

    `resolve_hint=True` appends "Resolve relative dates/times against this." —
    needed by the agent loop, whose tools can SCHEDULE things ("remind me
    tomorrow", "in 2 hours") and must resolve those phrases against a real
    clock rather than guessing. main.py's plain chat branches never call a
    scheduling tool, so they get the plain line — the extra sentence there
    would be dead instruction, not wrong, just unearned prompt tokens on every
    non-agent turn.

    Full minute precision, never rounded: the system prompt tells the model to
    answer "what time is it" directly from this line, so rounding buys a
    little prefill and pays for it with an answer that's wrong by minutes. The
    agent loop places this LAST among its system blocks specifically because
    it's the one piece that changes within a session — see run_agent's
    sys_content assembly for the prefix-cache reasoning.
    """
    line = ("\nThe current date and time is "
            + datetime.now().strftime("%A, %B %-d, %Y at %-I:%M %p") + ".")
    if resolve_hint:
        line += " Resolve relative dates/times against this."
    return line


def memory_block(query: str = "") -> str:
    """Explicit-memory context (service/memory/facts.py), safe to call from
    any text path. Best-effort — a missing or unreadable memory store must
    never break a turn. Travels everywhere this is injected, so a fact the
    user asked Wisp to remember is just as visible on a plain chat turn as on
    a tool-using one — otherwise "what's my sister's name" would answer
    correctly only when the router happened to pick a tool route.
    """
    try:
        from service.memory.facts import memory_context_block
        return memory_context_block(query=query)
    except Exception:  # noqa: BLE001
        return ""

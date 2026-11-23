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


def memory_block(query: str = "", *, mail_facts: bool = False) -> str:
    """Explicit-memory context (service/memory/facts.py), safe to call from
    any text path. Best-effort — a missing or unreadable memory store must
    never break a turn. Travels everywhere this is injected, so a fact the
    user asked Wisp to remember is just as visible on a plain chat turn as on
    a tool-using one — otherwise "what's my sister's name" would answer
    correctly only when the router happened to pick a tool route.

    `mail_facts`: whether facts learned only from mail may ride along. Off unless
    the caller passes profile.mail_facts_may_go(endpoint): Wisp's own on-Mac
    model or the user's own Mac, never a cloud model, even under Cloud "Everything".
    """
    try:
        from service.memory.facts import memory_context_block
        block = memory_context_block(query=query, mail_facts=mail_facts)
    except Exception:  # noqa: BLE001
        block = ""
    return block + profile_block()


PROFILE_HEADER = ("\nWhat Wisp has learned about the user's routines (statistical patterns from "
                  "their own activity; may be wrong, never state them as certain facts):\n")


def profile_block() -> str:
    """The behavior engine's compact profile card (<= 700 bytes), or "".

    Rides along with memory_block on every path that already includes the
    user's remembered facts. Cached for 60 s by the runtime and never
    blocks the event loop; any failure is simply no card.
    """
    from service.behavior_switch import enabled
    if not enabled():
        return ""
    try:
        from service.assistant.behavior_runtime import profile_card
        card = profile_card().strip()
    except Exception:  # noqa: BLE001
        return ""
    return PROFILE_HEADER + card if card else ""


STYLE_HEADER = "\n"


def style_block(endpoint=None, *, managed: bool = False) -> str:
    """How the user writes (about 300 bytes of measured numbers), for DRAFTING only.

    Returned only when learning and the writing-profile switch are on AND the model that
    will read it runs on this Mac or the user's own node (profile.mail_facts_may_go): it is
    derived from sent mail and messages, so a cloud model never sees it, not even under
    Cloud "Everything", and never while remote inference is pinned."""
    from service.behavior_switch import enabled, remote_active
    if not enabled() or remote_active():
        return ""
    try:
        from service.memory.profile import mail_facts_may_go
        if not mail_facts_may_go(endpoint, managed=managed):
            return ""
        from service.assistant.behavior_runtime import style_card
        card = style_card().strip()
    except Exception:  # noqa: BLE001
        return ""
    return "\n\n" + card if card else ""

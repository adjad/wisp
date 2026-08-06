"""Explicit memory tools — remember / recall / forget.

The store behind these (service/memory/facts.py) is read into EVERY turn's
system prompt, so `remember` is what makes a fact stick across sessions rather
than dying with the conversation that produced it. `recall` exists for the
cases the always-on block can't cover: it's capped at ~2000 chars, so anything
older or bulkier than that is only reachable by searching.

All three are `assistant_write`/`assistant_read` — they touch Wisp's own
database under ~/.moe and nothing else, so they stay usable in view-only mode
for the same reason the commitment store does.
"""
from __future__ import annotations

from service.memory.facts import CATEGORIES, store
from service.tools.registry import register


@register(
    "remember",
    "Save a durable fact about the user so it's still known in future "
    "conversations. Use whenever the user says 'remember that…', 'don't forget…', "
    "'from now on…', or states something durable about themselves, the people in "
    "their life, their projects, their routines, or how they want you to behave. "
    "Also use it proactively when the user corrects a standing assumption. Write "
    "the fact as a short standalone sentence that will still make sense months "
    "later with no conversation around it (say 'Adi's sister Priya lives in "
    "Boston', not 'she lives there'). Do NOT use it for one-off task details or "
    "anything only relevant to the current request.",
    {"type": "object",
     "properties": {
         "fact": {"type": "string",
                  "description": "the fact, as a short standalone sentence"},
         "category": {"type": "string",
                      "description": "one of: " + ", ".join(CATEGORIES)},
     },
     "required": ["fact"]},
    category="assistant_write",
)
async def remember(fact: str, category: str = "fact") -> str:
    category = (category or "fact").strip().lower()
    if category not in CATEGORIES:
        category = "fact"
    res = store.add(fact, category=category)
    verb = "Updated what I remember" if res["updated"] else "Saved to memory"
    return f"{verb}: {res['text']}"


@register(
    "recall",
    "Search everything the user has asked you to remember. The most relevant "
    "memories are already in your context automatically, so only call this when "
    "you need something that isn't there — an older fact, or the full list on a "
    "topic. Use when the user asks 'what do you remember about X', or when a "
    "request depends on a detail you were told before but can't see.",
    {"type": "object",
     "properties": {
         "query": {"type": "string",
                   "description": "what to look for; omit to list everything"},
     }},
    category="assistant_read",
)
async def recall(query: str | None = None) -> str:
    rows = store.search(query or "", limit=30)
    if not rows:
        if store.count() == 0:
            return ("(Nothing in memory yet. Ask me to remember something and "
                    "it'll be here in every future conversation.)")
        return f"(Nothing in memory matches '{query}'.)"
    store.touch([r["id"] for r in rows])
    lines = "\n".join(f"- [{r['category']}] {r['text']}" for r in rows)
    return f"From memory:\n{lines}"


@register(
    "forget",
    "Delete something from memory. Use when the user says 'forget that', "
    "'that's no longer true', or asks you to stop remembering something. Pass "
    "enough of the fact to identify it unambiguously — if nothing clearly "
    "matches, nothing is deleted and you should ask the user which one they mean "
    "rather than guessing.",
    {"type": "object",
     "properties": {
         "query": {"type": "string",
                   "description": "text identifying the fact to delete"},
     },
     "required": ["query"]},
    category="assistant_write",
)
async def forget(query: str) -> str:
    n = store.delete_matching(query)
    if not n:
        near = store.search(query, limit=5)
        if near:
            listed = "\n".join(f"- {r['text']}" for r in near)
            return (f"(Nothing matched '{query}' closely enough to delete safely. "
                    f"Closest memories — ask the user which one to forget:\n{listed})")
        return f"(Nothing in memory matches '{query}'.)"
    return f"Forgotten ({n} {'memory' if n == 1 else 'memories'} removed)."

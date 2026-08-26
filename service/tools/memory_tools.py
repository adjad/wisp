"""Explicit memory tools — remember / recall / forget, and searching/clearing
PAST CONVERSATIONS (distinct from the facts store: those are things the user
explicitly asked to be remembered; this is the raw transcript history).

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
                      "description":
                          "one of: " + ", ".join(CATEGORIES) + ". Use 'fact' "
                          "for what they own/hold/are (car, investments, where "
                          "they live), 'preference' ONLY for tastes and how "
                          "you should behave, 'person' for people in their "
                          "life, 'routine' for recurring commitments, "
                          "'project' for ongoing work."},
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


@register(
    "search_conversations",
    "Search PAST conversation transcripts for a keyword — 'what did we talk "
    "about last week', 'did I ask you about X before'. Different from "
    "`recall`: this searches what was actually SAID, not facts you asked to "
    "be remembered.",
    {"type": "object",
     "properties": {
         "query": {"type": "string", "description": "Keyword to search for."},
         "limit": {"type": "integer", "description": "Max sessions to search back through. Default 20."},
     },
     "required": ["query"]},
    category="assistant_read",
    aliases=["did I ask you about this before", "what did we talk about last week",
             "search my chat history for", "have we discussed this already"],
)
async def search_conversations(query: str, limit: int = 20) -> str:
    from service.memory.store import store as session_store

    q = (query or "").strip().lower()
    if not q:
        return "(error: search_conversations needs a `query`.)"
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 20

    import datetime

    hits = []
    for sess in session_store.list_sessions(limit=n):
        for turn in session_store.turns_from(sess["id"], 0):
            if q in (turn.get("content") or "").lower():
                when = datetime.datetime.fromtimestamp(
                    turn["created_at"]).strftime("%b %-d")
                snippet = turn["content"].strip().replace("\n", " ")[:140]
                hits.append((turn["created_at"], turn["role"], when, snippet))
    if not hits:
        return f"Nothing in the last {n} conversations mentions {query!r}."
    hits.sort(key=lambda h: h[0], reverse=True)
    lines = [f"  [{when}] {role}: {snippet}" for _, role, when, snippet in hits[:15]]
    more = f"\n  (+{len(hits) - 15} more matches)" if len(hits) > 15 else ""
    return f"Found {len(hits)} mention(s) of {query!r}:\n" + "\n".join(lines) + more


@register(
    "clear_memory",
    "Delete SEVERAL memories at once matching a keyword — 'forget everything "
    "about my old job'. Use `forget` instead for a single specific fact; this "
    "is for bulk cleanup and shows what it's about to delete first unless "
    "`confirm=true`.",
    {"type": "object",
     "properties": {
         "query": {"type": "string", "description": "Keyword — every fact containing it is a candidate."},
         "confirm": {"type": "boolean", "description": "Set true to actually delete. Default false (preview only)."},
     },
     "required": ["query"]},
    category="assistant_write",
    aliases=["forget everything about my old job", "clear out old memories about this",
             "wipe what you know about that project"],
)
async def clear_memory(query: str, confirm: bool = False) -> str:
    q = (query or "").strip()
    if not q:
        return "(error: clear_memory needs a `query`.)"
    matches = store.search(q, limit=50)
    if not matches:
        return f"Nothing in memory matches {q!r}."
    if not confirm:
        listed = "\n".join(f"  - {r['text']}" for r in matches[:15])
        more = f"\n  (+{len(matches) - 15} more)" if len(matches) > 15 else ""
        return (f"Would delete {len(matches)} memor{'y' if len(matches) == 1 else 'ies'} "
                f"matching {q!r}:\n{listed}{more}\n\nCall again with confirm=true to actually delete them.")
    n = 0
    for r in matches:
        if store.delete(r["id"]):
            n += 1
    return f"Deleted {n} memor{'y' if n == 1 else 'ies'} matching {q!r}."

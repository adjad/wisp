"""The planner's tool menu: retrieval plus each relevant family's core tools.

Replaces the regex router's POSITIVE steering for planner turns — no forced
first tool, no router-direct calls, no narrow subsets that keep one half of a
compound request — while the router's negative contract (forbidden tools,
argument bindings) still applies on top. Measured on the 439-case routing
corpus (2026-10-13, origin/main 8af31bda): the regex router's menus missed a
needed tool in ~9% of cases, fell back to the generic lexical menu in 29%, and
62% of offered tools were not needed (precision 0.38). In a production-loop
mirror on LFM2.5-24B, 11 of 19 failures were the menu, e.g. "do i have anything
on sunday" offered `stopwatch` but not `get_upcoming`, and "let dad know i
landed" offered `forward_email` but not `send_message`.

Menu = lexical retrieval (the existing BM25 ranker) ∪ the core tools of every
family the request touches. A family is touched when a retrieved tool belongs
to it or a broad family hint matches. Hints only ADD tools; they never force,
order or remove one, so a wrong hint costs a few menu slots, not a turn.

Core tools are listed by name. Names that are not registered are skipped, so a
renamed or merged tool simply drops out of its family until this table is
updated — the registry, not this file, decides what exists.
"""
from __future__ import annotations

import re

MAX_MENU = 20
LEXICAL_K = 8
LEXICAL_K_HINTED = 4

CORE: dict[str, tuple[str, ...]] = {
    # Order matters: the first CORE_HEAD of each touched family survive the cap.
    "calendar": ("get_upcoming", "add_reminder", "add_calendar_event", "search_reminders",
                 "update_reminder", "complete_reminder", "cancel_event", "update_event",
                 "find_free_time", "get_past_events", "clear_reminders"),
    "mail": ("view_emails", "summarize_emails", "send_email", "reply_to_email", "draft_email",
             "forward_email", "schedule_send"),
    "messages_contacts": ("lookup_contact", "send_message", "view_messages", "summarize_messages",
                          "draft_message", "search_conversations"),
    "notes": ("search_notes", "create_note", "append_note"),
    "memory": ("remember", "recall", "search_conversations", "forget"),
    "files": ("find_files", "list_dir", "read_file", "move_path", "delete_path"),
    "public_web": ("web_search", "web_fetch", "get_weather", "travel_time", "find_place"),
    "apps_media": ("open_app", "music"),
    "device": ("set_volume", "system_status", "toggle_setting"),
    "automation": ("set_timer", "manage_timers", "run_shell"),
    "compute": ("calculate", "convert_units", "world_time"),
}
CORE_HEAD = 4

_DAYS = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?s?"
HINTS: tuple[tuple[re.Pattern, tuple[str, ...]], ...] = tuple(
    (re.compile(p, re.I), fams) for p, fams in (
        (rf"\b(?:{_DAYS}|today|tonight|tomorrow|tmrw|this (?:week|weekend|morning|afternoon|evening)|"
         r"next (?:week|month)|calendar|schedule|agenda|meeting|appointment|event|busy|free\b|"
         r"plans?|to-?do|todo|task|due|deadline|remind|reminder|ping me|nudge me|at \d{1,2}(?::\d\d)?\s*(?:am|pm)?)\b",
         ("calendar",)),
        (r"\b(?:e-?mail\w*|emials?|emals?|inbox|g?mail|repl(?:y|ied)|respond|forward|newsletter|unsubscribe)\b",
         ("mail",)),
        (r"\b(?:texts?|txts?|messages?|imessages?|sms|tell\s+(?!me\b)\w+|let\s+(?!me\b)\S+(?:\s+\S+)?\s+know|"
         r"ping\s+(?!me\b)\w+|contact|phone|call\s+(?!me\b)\w+|what did \w+ (?:say|send)|wish \w+|"
         r"(?:get back|write) to \w+)\b", ("messages_contacts",)),
        (r"\b(?:notes?|wrote down|jotted|code|password|combo|combination|membership|member id|"
         r"account number|frequent flyer|locker|parked|where did i put|name of)\b", ("notes", "memory")),
        (r"\b(?:remember|remeber|forget|stop remembering|memor(?:y|ies)|what you know|keep in mind|fyi|did i mention|did we talk|talk(?:ed)? about|"
         r"you recommended|i said|you know about me|my preferences?)\b", ("memory",)),
        (r"\b(?:file|files|folder|downloads|desktop|documents|pdf|docx?|resume|cv|photos?)\b", ("files",)),
        (r"\b(?:weather|forecast|rain|news|look up|search the web|google|price|stock|hours|"
         r"open(?:s|ing)? (?:at|time)|directions|drive|traffic|how far|near(?:by| me)|restaurant|"
         r"who is|ceo|right now|latest)\b", ("public_web",)),
        (r"\b(?:open|launch|play|pause|music|song|spotify|podcast)\b", ("apps_media",)),
        (r"\b(?:volume|louder|quieter|mute|crank it|turn it (?:up|down)|wi-?fi|bluetooth|battery|"
         r"brightness|dark mode|settings?)\b", ("device",)),
        (r"\b(?:timer|alarm|stopwatch|countdown|shortcut)\b", ("automation",)),
        (r"\b(?:calculate|convert|what time is it in|percent|\d+\s*[x*/+-]\s*\d+)\b", ("compute",)),
    ))


# `forget` is offered only for a message that OPENS with an instruction to
# forget: "forget my gate code", "please stop remembering ...", "delete what you
# know about my ex". Questions ("did you forget ...?"), statements ("I forget
# my gate code") and negations ("don't forget ...") never open that way.
# Three independent review rounds on #211 each found phrasings that slipped
# past "forget appears anywhere, minus negations".
_FORGET_INTENT_RE = re.compile(
    r"^\s*(?:(?:hey\s+)?wisp[,:]?\s+)?(?:(?:please|pls|just|can\s+you|could\s+you|would\s+you|"
    r"will\s+you|i\s+want\s+you\s+to|i\s+need\s+you\s+to)\s+)*"
    r"(?:forget|stop\s+remembering|unlearn|"
    # A generic deletion verb counts only with memory as its DIRECT object,
    # or "X from your memory" inside one clause with no but/keep/not:
    # "delete my notes, but keep your memory of my gate code" must not match
    # (fourth independent review of #211).
    r"(?:delete|remove|erase|wipe|clear)\s+(?:all\s+)?(?:of\s+)?(?:your\s+|the\s+|my\s+)?"
    r"(?:memor(?:y|ies)(?!\s+(?:card|stick|usage|pressure|leaks?|chips?|slots?))|"
    r"what\s+you\s+(?:know|remember))|"
    r"(?:delete|remove|erase|wipe|clear)\s+(?:(?!but\b|keep\b|not\b|and\b)[^\s,.;:?!]+\s+){0,6}"
    r"from\s+(?:your\s+)?memory)\b", re.I)


def family_of(name: str) -> str:
    from service.router.model_led import family_for, registry_specs
    from service.tools.registry import REGISTRY
    tool = REGISTRY.get(name)
    return family_for(registry_specs({name: tool})[0]) if tool is not None else "other"


def hinted_families(text: str) -> list[str]:
    seen: list[str] = []
    for pattern, fams in HINTS:
        if pattern.search(text or ""):
            seen.extend(f for f in fams if f not in seen)
    return seen


def _admissible(name: str, text: str, *, context_open: bool, hinted: bool = False) -> bool:
    """Registered, routable and available, and through the same write/outbound
    gates the retrievers already apply (sends are offered only when the words
    can ask for one; file deletion only with file-action words).

    `hinted`: the tool's family was named by the request. That opens the
    FILE-mutation gate for non-file families — `remember` is registered as
    fs_write, so the file gate otherwise hides it from "remember that my
    wife's birthday is March 3". Delete-category tools (`forget`) are offered
    only when the request itself has deletion words, and the outbound and
    bulk-delete intent gates are never opened by a hint."""
    from service.tools.registry import REGISTRY, is_tool_routable
    from service.router.semantic import gated_tool_allowed, _allowed, _gate_open, _DELETE_INTENT_RE
    from service.router.router import has_write_intent
    tool = REGISTRY.get(name)
    if tool is None or tool.unavailable_reason or not is_tool_routable(name):
        return False
    if name == "forget":
        # `forget` deletes memories and their correction history at once, so
        # it needs an affirmative, un-negated request to forget something in
        # THIS message; neither a family hint nor the previous turn opens it.
        # Independent review of #211: "what is my gate code", "do not forget
        # my gate code" and "delete the old file and recall my gate code" had
        # all put it on the menu.
        if not _FORGET_INTENT_RE.search(text):
            return False
    elif tool.category.endswith("_delete") and not (context_open or _DELETE_INTENT_RE.search(text)):
        return False
    open_gate = (_gate_open(text, writing=has_write_intent(text)) or context_open
                 # A delete tool reaches here only with deletion words (above).
                 or (hinted and family_of(name) != "files"))
    return _allowed(name, writing=open_gate) and gated_tool_allowed(name, text, context_open=context_open)


def build_menu(text: str, *, last_assistant: str | None = None,
               forbidden: frozenset[str] = frozenset(), context_open: bool = False,
               max_tools: int = MAX_MENU) -> list[str]:
    """Tool names to offer a planner turn, most relevant first.

    A short follow-up ("yes please", "tell her i'll be there") borrows family
    hints from the previous assistant turn, which is where its subject lives.
    The previous turn opens the outbound/delete gates only when it offered such
    an action, mirroring the retrievers' `context_open`.
    """
    from service.router.reranker import ranked_lexical_candidates
    from service.router.router import has_write_intent
    from service.utterance_shape import mask_quoted

    masked = mask_quoted(text or "")
    followup = bool(last_assistant) and len(masked.split()) <= 6
    if followup and not context_open:
        from service.router.semantic import outbound_intent
        context_open = outbound_intent(last_assistant or "") or bool(
            re.search(r"\b(?:send|reply|delete|clear|cancel|remove)\b", last_assistant or "", re.I))
    ranked = ranked_lexical_candidates(masked, writing=has_write_intent(masked), k=LEXICAL_K,
                                       context_open=context_open)
    families = hinted_families(masked)
    if followup:
        families += [f for f in hinted_families(last_assistant or "") if f not in families]
    if not {"messages_contacts", "mail"} & set(families):
        # Addressed to someone but naming no channel: "holler at Sam", "ask
        # Sam if he's free", "give dad a ring". The outbound gate already
        # opened for these; what was missing was the family, so the menu
        # carried no messaging tool at all (12 of the 16 corpus misses).
        from service.router.semantic import outbound_intent
        if outbound_intent(masked):
            families.append("messages_contacts")
    if families:
        # The request named its domains: retrieval keeps its top two (typos
        # and synonyms the hints miss: "chek my texts", "wether") plus a few
        # tools from those families, but imports no whole family. Measured on "whats on my todo list": the lexical
        # top-3 pulled in the automation and device families, and the menu
        # carried list_shortcuts, wisp_mcp and set_display next to the
        # calendar tools; the model then planned cannot_do.
        ranked = list(dict.fromkeys(ranked[:2] + [n for n in ranked if family_of(n) in families][:LEXICAL_K_HINTED]))
    else:
        for name in ranked[:2]:
            fam = family_of(name)
            if fam in CORE and fam not in families:
                families.append(fam)

    menu: list[str] = []

    hinted = set(hinted_families(masked))

    def add(name: str, fam: str | None = None) -> None:
        if (name not in menu and name not in forbidden
                and _admissible(name, masked, context_open=context_open, hinted=fam in hinted)):
            menu.append(name)

    # Interleave so every touched family keeps its first core tools even when
    # the cap bites: core heads first, then retrieval, then the rest of cores.
    for fam in families:
        for name in CORE.get(fam, ())[:CORE_HEAD]:
            add(name, fam)
    for name in ranked:
        add(name)
    for fam in families:
        for name in CORE.get(fam, ())[CORE_HEAD:]:
            add(name, fam)
    return menu[:max_tools]

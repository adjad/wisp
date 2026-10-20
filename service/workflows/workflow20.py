"""Pure personal-source read constraints shared by entry points.

This module gathers no data and makes no action decision. Complex constraints
remain with model selection; only complete bounded unread requests run directly.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from service.utterance_shape import mask_quoted, is_complete_explanation, explanation_remainder


_SOURCE = {
    "email": r"\b(?:e-?mails?|mail|inbox)\b",
    "messages": r"\b(?:messages?|texts?|imessages?|mesages)\b",
    "calendar": r"\b(?:calendar|calender|schedule|meetings?|events?|commitments?)\b",
    "notes": r"\bnotes?\b",
    "reminders": r"\breminders?\b",
}
_TOOLS = {"email": ("view_emails", "summarize_emails"),
          "messages": ("view_messages", "summarize_messages"),
          "calendar": ("get_upcoming",), "notes": ("search_notes",),
          "reminders": ("search_reminders",)}
_ACTION = re.compile(r"\b(?:send|text|email|draft|reply|forward|schedule|create|add|delete|remove|mark|archive|unsubscribe|cancel|move|update|remind|save|write)\b", re.I)
_PUBLIC = re.compile(r"\b(?:news|web|internet|public|product|app|apps|settings|tutorial|install|download|features?|versus|vs|API|security|encryption)\b", re.I)
_EXCLUSION = re.compile(r"\b(?:without|skip|except|exclude|excluding|not|never|don['’]t|do not)\b", re.I)
_READ = re.compile(r"\b(?:what|which|show|check|read|list|tell\s+me|give\s+me|summari[sz]e|summary|summaries|overview|recap|brief|briefing|digest|catch|happening|priorities|focus|work)\b", re.I)


def _complete_unread(text: str):
    """Consume one shared state and date, never flatten source-specific clauses."""
    channel = r"(?:e-?mails?|mail|inbox|messages?|texts?|imessages?|mesages)"
    channels = rf"{channel}(?:\s+(?:and|across)\s+{channel})?"
    prefix = r"(?:(?:please|can you|could you)\s+)?"
    verb = r"(?P<verb>show(?: me)?|check|read|list|summari[sz]e|catch me up on)"
    date = r"(?:\s+(?:from\s+)?(?P<day>today|yesterday))?"
    patterns = [
        rf"{prefix}{verb}\s+(?:(?:my|our|all|the|only)\s+)*unread\s+{channels}{date}",
        rf"{prefix}what\s+unread\s+{channels}\s+do\s+I\s+have{date}",
        rf"{prefix}which\s+unread\s+{channels}\s+(?:arrived|came\s+in){date}",
    ]
    for pattern in patterns:
        if match := re.fullmatch(pattern, text.strip(" .!?"), re.I):
            args = {"unread": True, **({"day": match["day"].casefold()} if match["day"] else {})}
            verb = (match.groupdict().get("verb") or "").casefold()
            return args, verb in {"summarize", "summarise", "catch me up on"}
    return None


@dataclass(frozen=True)
class PersonalRead:
    sources: tuple[str, ...]
    tools: tuple[str, ...]
    groups: tuple[frozenset[str], ...]
    bindings: dict[str, dict]
    direct_calls: tuple[tuple[str, dict], ...] = ()


def _evidence_sources(included: str | None, excluded: str | None,
                      allowed: tuple[str, ...]) -> tuple[str, ...] | None:
    def parse(value):
        found = []
        for part in re.split(r"\s*(?:,|/|\band\b|\bor\b)\s*", value, flags=re.I):
            part = re.sub(r"^(?:my|our|the)\s+", "", part.strip(), flags=re.I)
            names = [name for name in allowed if re.fullmatch(_SOURCE[name], part, re.I)]
            if len(names) != 1:
                return None
            found.append(names[0])
        return tuple(dict.fromkeys(found))
    selected = parse(included) if included else allowed
    removed = parse(excluded) if excluded else ()
    if selected is None or removed is None:
        return None
    return tuple(name for name in selected if name not in removed)


def priority_read(text: str) -> PersonalRead | None:
    """Complete priority scope; the period describes tasks, never mail receipt."""
    match = re.fullmatch(
        r"\s*(?:(?:please|can you|could you)\s+)?(?:what\s+(?:should\s+I\s+"
        r"(?:work\s+on|focus\s+on|do|prioriti[sz]e|tackle)|do\s+I\s+need\s+to\s+do)|"
        r"help\s+me\s+prioriti[sz]e(?:\s+my\s+(?:work|tasks))?|"
        r"what\s+are\s+my\s+priorities|what\s+needs\s+(?:me|my\s+attention))"
        r"(?:\s+(?P<period>now|today|tomorrow|tommorow|tommorrow|tomorow|tommrow|tmrow|tmrw|(?:this|next)\s+(?:week|month)))?"
        r"(?:\s+(?:using|from)\s+(?P<include>[^.!?;\n]+?)(?:\s+only)?)?"
        r"(?:\s*,?\s*(?:without|excluding|skip)\s+(?P<exclude>[^.!?;\n]+))?[.!?\s]*", text, re.I)
    if not match:
        return None
    sources = _evidence_sources(match["include"], match["exclude"], tuple(_SOURCE))
    if sources is None:
        return None
    period = (match["period"] or "today").casefold()
    period = {"now": "today", **dict.fromkeys(
        ("tommorow", "tommorrow", "tomorow", "tommrow", "tmrow", "tmrw"), "tomorrow")}.get(period, period)
    args = {"period": period, "include_overdue": True, "include_undated": True,
            **{f"include_{source}": source in sources for source in _SOURCE}}
    return PersonalRead(sources, ("collect_task_evidence",), (frozenset({"collect_task_evidence"}),),
                        {"collect_task_evidence": args})


def attention_read(text: str) -> PersonalRead | None:
    """Importance questions need current bodies, not an unread-header ranking.

    Only complete read-only questions are consumed. A request to compose/send,
    a source-specific date, or an additional clause keeps its existing owner.
    """
    prefix = r"\s*(?:(?:please|can you|could you)\s+)?"
    patterns = (
        r"what\s+(?:needs|requires)\s+(?:my\s+)?attention\s+(?:in|on|from|across)\s+(?P<sources>.+?)",
        r"(?:which|what)\s+(?P<sources>.+?)\s+(?:are|is)\s+(?:important|urgent)",
        r"which\s+(?P<sources>.+?)\s+should\s+I\s+(?:reply|respond)\s+to(?:\s+first)?",
    )
    sources = None
    for pattern in patterns:
        if match := re.fullmatch(prefix + pattern + r"[.!?\s]*", text, re.I):
            sources = _evidence_sources(match["sources"], None, ("email", "messages"))
            break
    if re.fullmatch(prefix + r"what\s+should\s+I\s+(?:reply|respond)\s+to\s+first[.!?\s]*", text, re.I):
        sources = ("email", "messages")
    if not sources:
        return None
    args = {"period": "today", "include_overdue": False, "include_undated": False,
            **{f"include_{source}": source in sources for source in _SOURCE}}
    return PersonalRead(sources, ("collect_task_evidence",), (frozenset({"collect_task_evidence"}),),
                        {"collect_task_evidence": args})


def mentioned_sources(text: str) -> tuple[str, ...]:
    source = mask_quoted(text)
    return tuple(name for name, pattern in _SOURCE.items() if re.search(pattern, source, re.I))


def weather_read(text: str) -> PersonalRead | None:
    """A complete city forecast uses the bounded provider adapter."""
    match = re.fullmatch(
        r"\s*(?:(?:please|can you|could you)\s+)?"
        r"(?:what(?:'s| is)\s+(?:the\s+)?weather|weather|(?:show|check|give)\s+(?:me\s+)?(?:the\s+)?weather)"
        r"\s+(?:in|for)\s+(?P<location>[A-Za-z][A-Za-z .,’'-]{0,100}?)"
        r"(?:\s+(?:right\s+now|now|currently|at\s+the\s+moment))?"
        r"(?:\s+(?P<period>today|tomorrow|tonight|(?:this|next)\s+(?:week|month)))?"
        r"(?:\s+(?:right\s+now|now|currently|at\s+the\s+moment))?[.!?\s]*", text, re.I)
    if not match or re.search(r"\b(?:and|then|also|without|web|internet|search|news|email|text|send|if|unless|not)\b", match["location"], re.I):
        return None
    args = {"location":match["location"].strip(" ."), "period":(match["period"] or "").casefold()}
    return PersonalRead(("weather",),("get_weather",),(frozenset({"get_weather"}),),
                        {"get_weather":args}, (("get_weather",args),))


def calendar_question(text: str) -> PersonalRead | None:
    """Bind only complete, explicitly personal named-time requests."""
    from service.calendar_time_intent import named_calendar_time
    intent = named_calendar_time(text)
    if intent is None or not intent.supported or not intent.personal or intent.no_calendar:
        return None
    args = intent.calendar_args()
    return PersonalRead(("calendar",),("get_upcoming",),(frozenset({"get_upcoming"}),),
                        {"get_upcoming":args}, (("get_upcoming",args),))


def daily_read(text: str) -> PersonalRead | None:
    """One fully consumed daily envelope; unknown constraints fall through."""
    text = re.sub(r',?\s+please[.!?\s]*$', '', text, flags=re.I)
    calendar = re.fullmatch(
        r"\s*(?:please\s+)?brief\s+me\s+on\s+(?P<period>today|tomorrow|(?:this|next)\s+(?:week|month))"
        r"(?:'s|’s)?\s+(?:my\s+)?calendar\s+only[.!?\s]*", text, re.I)
    if calendar:
        args={'period':calendar['period'].casefold(),'calendar_only':True}
        return PersonalRead(('calendar',),('get_upcoming',),(frozenset({'get_upcoming'}),),
                            {'get_upcoming':args}, (('get_upcoming',args),))
    match = re.fullmatch(
        r"\s*(?:(?:please|show(?:\s+me)?|give(?:\s+me)?|can\s+you\s+give\s+me|could\s+you\s+give\s+me)\s+)?"
        r"(?:my\s+|a\s+|the\s+)?(?:daily\s+(?:summary|brief|briefing|digest|rundown)"
        r"(?:\s+(?:for\s+)?(?P<period>today|tomorrow|(?:this|next)\s+(?:week|month)))?|"
        r"(?P<first>today|tomorrow)(?:'s|’s)?\s+(?:summary|brief|briefing|digest|rundown))"
        r"(?:\s*,?\s*(?:without|excluding|skip|no)\s+(?P<exclude>"
        r"(?:my\s+)?(?:e-?mails?|mail|messages?|texts?|calendar|calender|reminders?|weather)"
        r"(?:\s*(?:,|and|or)\s*(?:my\s+)?(?:e-?mails?|mail|messages?|texts?|calendar|calender|reminders?|weather))*))?"
        r"[.!?\s]*", text, re.I)
    if not match:
        return None
    period=match['period'] or match['first']
    args = {"period": period.casefold()} if period else {}
    excluded = match["exclude"] or ""
    flags = {"email": "include_email", "messages": "include_messages",
             "calendar": "include_calendar", "reminders": "include_reminders"}
    for source in mentioned_sources(excluded):
        if source in flags:
            args[flags[source]] = False
    if re.search(r"\bweather\b", excluded, re.I):
        args["include_weather"] = False
    sources = tuple(source for source, flag in flags.items() if args.get(flag) is not False)
    return PersonalRead(sources, ("daily_brief",), (frozenset({"daily_brief"}),),
                        {"daily_brief": args}, (("daily_brief", args),))


def conversation_read(text: str) -> PersonalRead | None:
    """One explicitly named chat, with bounded history and no sender inference."""
    match=re.fullmatch(
        r"\s*(?:please\s+)?(?:summari[sz]e|recap|catch\s+me\s+up\s+on)\s+"
        r"(?:the\s+)?(?P<label>[A-Za-z][A-Za-z0-9 ’'_-]{0,60}?)\s+"
        r"(?:group(?:\s+chat)?|chat|conversation)"
        r"(?:\s+(?:from|for|during)\s+(?P<period>today|yesterday|(?:this|last)\s+(?:week|month)))?"
        r"[.!?\s]*",text,re.I)
    if not match or re.search(
            r"\b(?:and|or|then|also|without|except|not|only|all|whole|entire|full|complete|every|"
            r"my|email|messages?|texts?|calendar|notes?|reminders?|web|news|public|"
            r"today|yesterday|this|last|from|about|containing|with)\b",match['label'],re.I):
        return None
    args={'conversation':match['label'].strip()}
    period=match['period']
    if period:
        args['day' if period.casefold() in {'today','yesterday'} else 'period']=period.casefold()
    return PersonalRead(('messages',),('summarize_messages',),(frozenset({'summarize_messages'}),),
                        {'summarize_messages':args}, (('summarize_messages',args),))


def email_summary_excluding_messages(text: str) -> PersonalRead | None:
    """A complete source exclusion, never a content redaction instruction."""
    match=re.fullmatch(
        r"\s*(?:(?:please|can you|could you)\s+)?(?:show(?:\s+me)?|give(?:\s+me)?|check|list)\s+"
        r"(?:my\s+|the\s+)?(?:e-?mail|inbox)\s+(?:summaries|summary|digest|recap)"
        r"(?:\s+(?:from\s+|for\s+)?(?P<period>today|yesterday|(?:this|last)\s+(?:week|month)))?"
        r"\s*,?\s*(?:but\s+not|without|excluding|do\s+not\s+include)\s+"
        r"(?:my\s+)?(?:messages?|texts?|iMessages)[.!?\s]*",text,re.I)
    if not match:
        return None
    period=match['period']
    args={} if not period else {('day' if period.casefold() in {'today','yesterday'} else 'period'):period.casefold()}
    return PersonalRead(('email',),('summarize_emails',),(frozenset({'summarize_emails'}),),
                        {'summarize_emails':args},(('summarize_emails',args),))


def personal_read(text: str) -> PersonalRead | None:
    if is_complete_explanation(text):
        return None
    if (remainder := explanation_remainder(text)) is not None:
        return personal_read(remainder) if remainder else None
    if (typed := priority_read(text) or attention_read(text) or weather_read(text) or calendar_question(text) or conversation_read(text)
            or email_summary_excluding_messages(text)) is not None:
        return typed
    if (daily := daily_read(text)) is not None:
        return daily
    source = mask_quoted(text).strip()
    sources = mentioned_sources(source)
    if not sources or not _READ.search(source) or _PUBLIC.search(source) or _EXCLUSION.search(source):
        return None
    # A source word can be a search topic rather than another reader. Leave
    # qualified cross-domain phrases to semantic selection with the full text.
    if len(sources) > 1:
        spans = sorted(re.search(_SOURCE[name], source, re.I).span() for name in sources)
        for (_, end), (begin, _) in zip(spans, spans[1:]):
            bridge = source[end:begin]
            if not re.search(r"\b(?:and|plus|across|then|also)\b|[,/]", bridge, re.I):
                return None
            if bridge.strip(" ,/:") and not re.fullmatch(
                    r"[\s,/:]*(?:(?:and|plus|across|then|also|my|our|the|all|only|unread|"
                    r"today|tomorrow|yesterday|this|last|week|month|from|for|recent|current|show|check|read|list|"
                    r"summari[sz]e|recap|tell|give|me|what|is|are|on|in|next)\b[\s,/:]*)+",
                    bridge, re.I):
                return None
        first_end = min(re.search(_SOURCE[name], source, re.I).end() for name in sources)
        if re.search(r"\b(?:about|containing|with\s+the\s+word|named|titled)\b", source[first_end:], re.I):
            return None
    # Nouns such as email/schedule/text may appear in a read; action verb
    # positions (or independent effect verbs anywhere) must reach normal policy.
    actions = set(m.group().casefold() for m in _ACTION.finditer(source))
    if actions - {"email", "text", "schedule"}:
        return None
    if re.match(r"(?:please\s+)?(?:email|text|schedule)\b", source, re.I):
        return None
    if re.search(r"(?:\b(?:and|then|also|while)\b|[;,\n.!?])\s*"
                 r"(?:(?:then|also|please)\s+)*(?:email|text|schedule)\s+(?![.!?]*$)\S", source, re.I):
        return None
    if re.search(r"\b(?:tell|inform)\s+(?!me\b|us\b)|\bput\s+(?:it|this|that)\b", source, re.I):
        return None
    # A separate imperative must be understood as another read. Unknown
    # clauses belong to normal interpretation, including device/app actions.
    for clause in re.split(r"\b(?:and|then|also|while|before|after)\b|[,;.!?]", source, flags=re.I)[1:]:
        if not clause.strip():
            continue
        if not mentioned_sources(clause):
            return None
        if not re.match(r"\s*(?:my|our|the|all|only|unread|today|yesterday|tomorrow|this|last|next|recent|current|e-?mails?|mail|inbox|messages?|texts?|imessages?|calendar|calender|schedule|meetings?|events?|notes?|reminders?|show|check|read|list|summari[sz]e|recap|tell\s+me|give\s+me)\b", clause, re.I):
            return None
    unread = bool(re.search(r"\bunread\b|\bhaven['’]t\s+(?:read|opened)\b", source, re.I))
    if not unread and len(sources) < 2:
        return None
    personal = bool(re.search(r"\b(?:my|me|our|I)\b", source, re.I))
    across = bool(re.search(r"\b(?:across|overview|recap|summary|summaries|summari[sz]e|briefing|brief|rundown)\b", source, re.I))
    if not (personal or unread or (len(sources) > 1 and across)):
        return None
    if unread and len(sources) > 1 and re.search(r"\b(?:all|read)\s+(?:my\s+)?(?:messages?|texts?|e-?mails?|mail)\b", source, re.I):
        return None
    tools = tuple(tool for name in sources for tool in _TOOLS[name])
    bindings = {}
    if unread and len(sources) == 1:
        for name in sources:
            if name in {"email", "messages"}:
                bindings.update({tool: {"unread": True} for tool in _TOOLS[name]})
    # Only exact, fully consumed source-only unread shapes can bypass selection.
    # Named people, accounts, topics, exclusions, ranges and numeric dates keep
    # their full request and reach model/identity resolution.
    direct = ()
    if unread and set(sources) <= {"email", "messages"} and (complete := _complete_unread(text)):
        args, summary = complete
        # Structured reads return tool output directly, without narration.
        # Preserve the requested presentation instead of exposing raw bodies
        # and identifiers when the user asked for a bounded digest.
        prefix = "summarize" if summary else "view"
        direct = tuple((f"{prefix}_{'emails' if name == 'email' else 'messages'}", dict(args)) for name in sources)
        bindings.update({tool: dict(args) for tool, _ in direct})
    return PersonalRead(sources, tools, tuple(frozenset(_TOOLS[name]) for name in sources), bindings, direct)

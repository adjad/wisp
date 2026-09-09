"""Deterministic compiler for grounded summary-delivery workflows."""
from __future__ import annotations

import re

from service.config import role_to_model
from service.reminder_intent import REMINDER_CREATE_RE
from service.router.router import RouteDecision
from service.workflows.models import WorkflowPlan


_EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{6,}\d)(?!\w)")
_CANCEL = re.compile(r"^\s*(?:no|nope|never\s*mind|nevermind|cancel(?:\s+it)?|stop|forget\s+it)\s*[.!]?\s*$", re.I)
_ASSENT = re.compile(r"^\s*(?:(?:ok(?:ay)?|yes)\s+)?(?:yes|yep|yeah|ok(?:ay)?|sure|go ahead|do it|send it|try again|retry)\s*[.!]?\s*$", re.I)
_RELATION = r"mom|mother|dad|father|sister|brother|wife|husband|partner|boss"
_NAME = r"[A-Z][A-Za-z'\-]*(?:\s+[A-Z][A-Za-z'\-]*){0,2}"

SOURCE_TO_TOOL = {
    "calendar": "get_upcoming",
    "reminder": "search_reminders",
    "email": "summarize_emails",
    "stock": "get_stock_price",
    "news": "web_search",
    "daily_brief": "daily_brief",
    "weather": "get_weather",
}

_OUTBOUND = re.compile(
    r"\b(?:send|text|message|e-?mail|share|forward|draft|compose|write)\b", re.I)
_SUMMARY = re.compile(
    r"\b(?:summary|summaries|digest|report|brief|briefing|rundown|update|recap|"
    r"what(?:'s| is) (?:on|in)|updates|comparing|comparison|upcoming|schedule|agenda|movements?|prices?|headlines?)\b",
    re.I,
)

_MESSAGES_CHANNEL = re.compile(
    r"\b(?:via|through|using|on)\s+(?:i\s*message|imessages?|messages?|texts?)\b|"
    r"\b(?:i\s*message|imessages?)\b|"
    r"\b(?:text|message)\s+(?:it\s+)?(?:to\s+)?", re.I)
_EMAIL_CHANNEL = re.compile(
    r"\b(?:via|through|using|on)\s+(?:e-?mail|mail)\b|"
    r"\b(?:send\s+(?:an?\s+)?e-?mail|e-?mail)\s+(?:to\s+)?(?:me\b|myself\b|"
    rf"{_RELATION}\b|[A-Z0-9._%+\-]+@)", re.I)
_EMAIL_PROPER_NAME = re.compile(
    r"(?i:\b(?:send\s+(?:an?\s+)?e-?mail|e-?mail)\s+(?:to\s+)?)"
    r"[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+){0,2}\b")

_DRAFT = re.compile(r"\b(?:draft|compose|write)\b|\b(?:do not|don't|dont)\s+send\b|\bunsent\b", re.I)
_SCHEDULE = re.compile(
    r"\bschedule\b[^.?!]{0,80}\b(?:send|text|message|e-?mail)\b|"
    r"\b(?:send|text|message|e-?mail)\b[^.?!]{0,100}\b(?:"
    r"at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|tonight\s+at|"
    r"tomorrow\s+at|next\s+\w+\s+at)\b", re.I)

_RANGE_PATTERNS = [
    re.compile(r"\b(?:today|tomorrow|yesterday)\b", re.I),
    re.compile(r"\b(?:this|last|next|past)\s+(?:day|week|month|year|weekend)\b", re.I),
    re.compile(r"\b(?:past|last|next)\s+\d+\s+(?:days?|weeks?|months?)\b", re.I),
]
_WHEN = re.compile(
    r"\b(?:(?:today|tomorrow|tonight|next\s+\w+)\s+)?at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b(?:\s+(?:today|tomorrow))?|"
    r"\b(?:tomorrow|tonight)\s+(?:morning|afternoon|evening)\b", re.I)

_INLINE_EMAIL_SUMMARY = re.compile(
    r"^(?:can\s+you\s+|could\s+you\s+|please\s+)?(?:"
    r"(?:send|show|give|tell)\s+me\s+(?:my\s+|the\s+)?"
    r"(?:e-?mail|inbox)\s+(?:summary|summaries|digest|recap)|"
    r"what\s+are\s+(?:my\s+|the\s+)?(?:e-?mail|inbox)\s+"
    r"(?:summary|summaries|digest|recap))"
    r"(?:\s+(?:for|from)\s+(?:today|yesterday|"
    r"(?:this|last|past)\s+(?:day|week|month|year)|"
    r"(?:last|past)\s+\d+\s+(?:days?|weeks?|months?)|"
    r"\d{4}-\d{2}(?:-\d{2})?))?[\s.!?]*$", re.I)


def _normalize(text: str) -> str:
    replacements = {
        r"\bcalender\b": "calendar",
        r"\bscedule\b": "schedule",
        r"\bschedual\b": "schedule",
        r"\b(?:tmrow|tmrw)\b": "tomorrow",
        r"\bimsg\b": "Messages",
        r"\btexxt\b": "text",
        r"\bportfoliio\b": "portfolio",
    }
    value = text
    for pattern, replacement in replacements.items():
        value = re.sub(pattern, replacement, value, flags=re.I)
    return value

def is_cancel(text: str) -> bool:
    return bool(_CANCEL.match(text))


def is_assent(text: str) -> bool:
    return bool(_ASSENT.match(text))


def extract_channel(text: str) -> str:
    value = _normalize(text.strip())
    if re.fullmatch(r"(?:i\s*message|imessages?|messages?|texts?)(?:\s+message)?\.?", value, re.I):
        return "messages"
    if re.search(r"\ba\s+Messages\s+(?:update|summary|report)\b", value, re.I):
        return "messages"
    if re.fullmatch(r"(?:e-?mail|mail)\.?", value, re.I):
        return "email"
    if _MESSAGES_CHANNEL.search(value):
        return "messages"
    if (re.search(r"\bsend\b[^.?!]{0,50}\ban?\s+e-?mail\b", value, re.I)
            or re.search(r"\bto\s+my\s+(?:e-?mail|inbox)\b", value, re.I)
            or _EMAIL.search(value) or _EMAIL_CHANNEL.search(value)
            or _EMAIL_PROPER_NAME.search(value)):
        return "email"
    return ""


# "send/text/share <anything> to <name>" — the artifact-first phrasing.
_DELIVER_TO = re.compile(
    r"\b(?:send|text|message|e-?mail|share|forward)\b[^.?!]{0,80}?"
    r"\b(?:to|with)\s+(?:my\s+)?"
    r"(?P<name>[A-Za-z][A-Za-z'\-]{0,30}(?:\s+[A-Za-z][A-Za-z'\-]{0,30}){0,2})",
    re.I)
# A name ends where the rest of the sentence begins.
_NAME_STOP = frozenset({
    "for", "with", "about", "saying", "that", "via", "through", "using",
    "on", "in", "at", "and", "or", "by", "from", "the", "a", "an", "as",
    "this", "next", "last", "today", "tomorrow", "tonight", "please", "asap",
    "now", "again", "only", "instead", "later", "immediately", "right",
    "then", "my", "our", "your", "their", "his", "her",
})
# Things that are never a person: the payload, the channel, or the user
# themselves. Without this, "send my calendar to my email" would text "email".
_NOT_A_PERSON = frozenset({
    "message", "messages", "text", "texts", "imessage", "email", "emails",
    "mail", "e-mail", "inbox", "calendar", "schedule", "agenda", "summary",
    "summaries", "report", "reports", "digest", "brief", "briefing", "news",
    "reminder", "reminders", "notes", "everyone", "them", "him", "her", "it",
    "me", "myself", "us", "group", "work", "number", "phone", "contact",
    "contacts", "trash", "archive",
})


def _trim_name(raw: str) -> str:
    """The person out of a loose match, or "" if it isn't one."""
    words: list[str] = []
    for word in raw.split():
        if word.lower() in _NAME_STOP:
            break
        words.append(word)
    if not 1 <= len(words) <= 3:
        return ""
    if any(word.lower().strip(".,'") in _NOT_A_PERSON for word in words):
        return ""
    return " ".join(words)


def extract_recipient(text: str, channel: str = "") -> str:
    text = _normalize(text)
    if match := _EMAIL.search(text):
        return match.group(0)
    if match := _PHONE.search(text):
        return match.group(0).strip()
    if re.search(r"\b(?:myself|to me|to my (?:e-?mail|inbox)|email me|message me|text me)\b", text, re.I):
        return "me"

    relation_patterns = [
        rf"\bto\s+(?:my\s+)?(?P<name>{_RELATION})\b",
        rf"\b(?:text|message|e-?mail)\s+(?:my\s+)?(?P<name>{_RELATION})\b",
        rf"\bsend\s+(?:my\s+)?(?P<name>{_RELATION})\b",
    ]
    for pattern in relation_patterns:
        if match := re.search(pattern, text, re.I):
            return match.group("name").strip()

    # Lowercase contact names are normal user input. The older proper-name
    # branch intentionally required capitals and therefore lost "trishe" in
    # the exact phrase "send a message to trishe with ...". Keep lowercase
    # extraction bounded by compose grammar so payload nouns cannot become a
    # recipient.
    bounded_name_patterns = [
        r"\b(?:send|text|message|email)\s+(?:this|that|it)\s+(?:to\s+)?"
        r"(?P<name>[A-Za-z][A-Za-z'\-]{0,30}(?:\s+[A-Za-z][A-Za-z'\-]{0,30}){0,2})"
        r"(?=\s+(?:via|through|using)\b|[,.!?]|$)",
        r"\b(?:send|draft|compose|write)\s+(?:an?\s+)?(?:i\s*message|message|text|e-?mail)"
        r"\s+to\s+(?:my\s+)?(?P<name>[A-Za-z][A-Za-z'\- ]{0,60}?)"
        r"(?=\s+(?:with|about|saying|that)\b|[,.!?]|$)",
        r"\b(?:send|text|message|e-?mail)\s+(?:my\s+)?"
        r"(?P<name>[A-Za-z][A-Za-z'\- ]{0,60}?)\s+(?:an?\s+)?"
        r"(?:i\s*message|message|text|e-?mail)\b",
        r"\be-?mail\s+(?:to\s+)?(?P<name>[A-Za-z][A-Za-z'\-]{0,30})"
        r"(?=\s+(?:my|the|a|an|with|about)\b|$)",
    ]
    for pattern in bounded_name_patterns:
        if match := re.search(pattern, text, re.I):
            # Trimmed by the same rule as the artifact-first pattern below: a
            # greedy three-word capture otherwise swallows the tail of the
            # sentence, and "send this to trishe via messages" addressed a
            # contact named "trishe via messages".
            if name := _trim_name(match.group("name")):
                return name

    # EVERY pattern above assumes the sentence's direct object is the word
    # "message"/"text"/"email" — "send a message to trishe with my calendar".
    # MEASURED FAILURE (2026-09-08, live): "send my calender to trishe(the next
    # two weeks)" names the ARTIFACT as the object instead, so none of them fit,
    # the case-sensitive proper-name branch below skipped a lowercase "trishe",
    # and Wisp asked "Who should I message it to?" about a person the user had
    # already named in their very first sentence.
    #
    # So: any delivery verb, then "to"/"with", then a name — case-insensitive,
    # with the payload nouns and the sentence's continuation stripped off by
    # _trim_name rather than by a lookahead. A lookahead was what lost this one
    # in the first place: "trishe(the" has no space before its terminator.
    if match := _DELIVER_TO.search(text):
        if name := _trim_name(match.group("name")):
            return name

    # Keep the proper-name portion case-sensitive. Compiling the whole pattern
    # with re.I makes ordinary payload words ("email summaries") look like a
    # capitalized contact and was one of the ambiguities this compiler exists
    # to remove.
    proper_patterns = [
        rf"(?i:\bto\s+(?:my\s+)?)(?P<name>{_NAME})\b",
        rf"(?i:\b(?:text|message|e-?mail)\s+(?:my\s+)?)(?P<name>{_NAME})\b",
        rf"(?i:\bsend\s+(?:my\s+)?)(?P<name>{_NAME})\s+(?:a\s+)?"
        rf"(?:(?i:stock|calendar|e-?mail|news)\s+)?"
        rf"(?i:message|text|e-?mail|summary|report|digest|brief|my\b|the\b)",
    ]
    banned = {"message", "messages", "email", "emails", "mail", "calendar", "schedule",
              "summary", "summaries", "report", "reports"}
    for pattern in proper_patterns:
        if match := re.search(pattern, text):
            name = match.group("name").strip()
            if name.lower() not in banned:
                return name
    return ""


def _date_range(text: str) -> str:
    for pattern in _RANGE_PATTERNS:
        if match := pattern.search(text):
            return match.group(0).lower()
    return ""


def extract_location(text: str, *, standalone: bool = False) -> str:
    value = text.strip().strip(".,")
    value = re.sub(r"^(?:(?:it(?:'s| is)\s+)?(?:for|in)\s+)", "", value, flags=re.I)
    if is_temporal_location(value):
        return ""
    if standalone and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,60}(?:,\s*[A-Z]{2})?", value):
        return value
    match = re.search(
        r"\b(?:weather|forecast)\b[^.?!]{0,50}\b(?:in|for)\s+"
        r"(?P<place>[A-Za-z][A-Za-z .'-]{1,50}(?:,\s*[A-Z]{2})?)"
        r"(?=\s+(?:today|tomorrow|this|next|last|past|and\b)|[,.?!]|$)",
        text, re.I)
    if not match:
        return ""
    place = match.group("place").strip()
    place = re.split(r"\s+(?:via|through|using|and|today|tomorrow)\b", place,
                     maxsplit=1, flags=re.I)[0].strip()
    if is_temporal_location(place):
        return ""
    return place


def is_temporal_location(value: str) -> bool:
    """Reject a time slot used as a place, at compilation and tool dispatch."""
    return bool(re.fullmatch(
        r"(?:(?:for|in)\s+)?(?:today|tomorrow|yesterday|tonight|now|"
        r"(?:this|next|last|past|coming)(?:\s+\d+)?\s+"
        r"(?:days?|weeks?|months?|years?|weekend|morning|afternoon|evening)|"
        r"(?:the\s+)?(?:next|last)\s+\w+day)", value.strip(), re.I))


def extract_stock_symbols(text: str, *, standalone: bool = False) -> list[str]:
    for company, ticker in {"nvidia": "NVDA", "apple": "AAPL", "microsoft": "MSFT",
                            "amazon": "AMZN", "tesla": "TSLA", "google": "GOOGL",
                            "amd": "AMD", "micron": "MU"}.items():
        text = re.sub(rf"\b{company}\b", ticker, text, flags=re.I)
    tickers = re.findall(r"(?<![A-Za-z])\$?([A-Z]{1,5})(?![A-Za-z])", text)
    tickers = [s for s in tickers if s not in {"I", "A", "THE", "SEND", "EMAIL"}]
    value = text.strip().strip(".,")
    if not standalone:
        match = re.search(
            r"\b(?:share|stock)\s+prices?\s+(?:of|for)\s+(?P<names>.+?)"
            r"(?=\s+(?:from|over|during|today|yesterday|tomorrow|this|last|past|"
            r"via|through|using)\b|[.?!]|$)", text, re.I)
        if not match:
            return list(dict.fromkeys(tickers))
        value = match.group("names")
    if len(value) > 100:
        return []
    parts = [part.strip(" ,") for part in re.split(r"\s*(?:,|\band\b)\s*", value, flags=re.I)]
    banned = {"", "my stock", "my stocks", "stock", "stocks", "share", "shares",
              "today", "tomorrow", "yesterday", "now", "current", "this week",
              "last week", "this month", "last month"}
    return list(dict.fromkeys(part for part in parts
                              if part.lower() not in banned
                              and re.fullmatch(r"[A-Za-z][A-Za-z .&'-]{0,35}", part)))[:10]


def _source_args(source: str, text: str, date_range: str) -> dict:
    if source == "calendar":
        if re.search(r"\bmove[ -]in\s+date\b", text, re.I):
            return {"days": 60, "calendar_only": True, "query": "move in"}
        if re.search(r"\bthis\s+week\s+and\s+next\s+week\b", text, re.I):
            return {"period": "this week and next week"}
        if date_range in {"today", "tomorrow", "yesterday", "this week", "next week",
                          "last week", "this month", "last month", "next month"}:
            return {"period": date_range}
        days = 7
        lower = text.lower()
        if re.search(r"\b(?:this\s+week\s+and\s+next\s+week|next\s+(?:two|2)\s+weeks?)\b", lower):
            days = 14
        elif re.search(r"\b(?:next|this)\s+month\b", lower):
            days = 31
        elif re.search(r"\b(?:next|this|past|last)\s+(?:few|three|3)\s+weeks?\b", lower):
            days = 21
        elif date_range in {"today"}:
            days = 1
        elif date_range == "tomorrow":
            days = 2
        elif match := re.search(r"(?:past|last|next)\s+(\d+)\s+days?", date_range):
            days = min(60, max(1, int(match.group(1))))
        return {"days": days}
    if source == "reminder":
        match = re.search(
            r"\bwith\s+(?:my\s+)?(?P<query>[A-Za-z0-9][A-Za-z0-9 '\-]{0,50}?)"
            r"\s+information\s+from\s+(?:my\s+)?(?:apple\s+)?reminders?\b",
            text, re.I)
        if not match:
            match = re.search(
                r"\b(?P<query>(?:[A-Za-z0-9][A-Za-z0-9'\-]*\s+)"
                r"{0,3}[A-Za-z0-9][A-Za-z0-9'\-]*)\s+"
                r"(?:information\s+)?from\s+(?:my\s+)?(?:apple\s+)?reminders?\b",
                text, re.I)
        query = " ".join(match.group("query").split()) if match else ""
        while re.match(r"^(?:with|about|the|my)\s+", query, re.I):
            query = re.sub(r"^(?:with|about|the|my)\s+", "", query,
                           count=1, flags=re.I)
        return {"query": query, "scope": "all"}
    if source == "email":
        if date_range in {"today", "yesterday"}:
            return {"day": date_range}
        if date_range:
            return {"period": date_range}
        return {}
    if source == "stock":
        # Preserve either explicit tickers or the company-name phrase the user
        # supplied; get_stock_price resolves both forms.
        symbols = extract_stock_symbols(text)
        spans = re.findall(
            r"\b(?:exactly\s+)?(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|"
            r"\d+)\s+(?:days?|weeks?|months?)\s+ago|(?:last|past|over\s+the)\s+"
            r"(?:few\s+)?(?:days?|weeks?|months?|year))\b", text, re.I)
        period = spans[-1] if spans else date_range
        return {"symbols": list(dict.fromkeys(symbols)), **({"period": period} if period else {})}
    if source == "news":
        topic = "stock market news" if re.search(r"\b(?:stocks?|market)\b", text, re.I) else "world news"
        return {"query": topic + " today"}
    if source == "daily_brief":
        return {"days": 2 if date_range == "tomorrow" else 1}
    if source == "weather":
        return {"location": extract_location(text), **({"period": date_range} if date_range else {})}
    return {}


def extract_sources(text: str) -> list[str]:
    sources: list[str] = []
    if re.search(r"\b(?:daily\s+(?:summary|digest|recap|brief|briefing)|morning\s+brief|full\s+briefing)\b", text, re.I):
        sources.append("daily_brief")
    if re.search(
            r"\b(?:from|with|using)\s+(?:my\s+)?(?:apple\s+)?reminders?(?:\.app)?\b|"
            r"\bmy\s+reminders?\s+(?:summary|list|schedule|information)\b", text, re.I):
        sources.append("reminder")
    if re.search(r"\b(?:cal[ae]ndar|agenda|meetings?|events?|appointments?|move[ -]in\s+date|"
                 r"my\s+schedule|schedule\s+(?:summary|report|digest|recap))\b", text, re.I):
        sources.append("calendar")
    # Remove explicit destination phrases before looking for an inbox source;
    # "send a news report to my email" names email as the channel, not data to
    # summarize.
    email_payload = re.sub(
        r"\b(?:to|via|through|using|on)\s+my\s+(?:e-?mail|inbox)\b", "", text,
        flags=re.I)
    if (re.search(r"\b(?:my\s+e-?mails?|my\s+inbox|inbox|unread\s+e-?mails?|"
                  r"e-?mail\s+(?:summary|summaries|digest|report))\b", email_payload, re.I)
            and _SUMMARY.search(text)):
        sources.append("email")
    if (re.search(r"\b(?:stocks?|shares?|portfolio|tickers?|market|\$[A-Z]{1,5})\b", text, re.I)
            and _SUMMARY.search(text)):
        sources.append("stock")
    if re.search(r"\b(?:news|headlines?)\b", text, re.I):
        sources.append("news")
    if re.search(r"\b(?:weather|forecast)\b", text, re.I):
        sources.append("weather")
    return list(dict.fromkeys(sources))


def compile_new(text: str, *, last_user: str = "", last_assistant: str = "") -> WorkflowPlan | None:
    original = text
    text = _normalize(text)
    if REMINDER_CREATE_RE.search(text):
        return None
    if _INLINE_EMAIL_SUMMARY.match(text.strip()):
        return None
    if not _OUTBOUND.search(text):
        return None
    sources = extract_sources(text)
    # Bind a referent before tool retrieval can reinterpret it as an email or
    # note. Named reports can refer to the answer just produced, too.
    refers_back = bool(re.search(r"\b(?:send|text|email|share|forward)\s+(?:this|that|it)\b", text, re.I))
    prior_sources = extract_sources(last_user)
    named_report = bool(sources and sources == prior_sources and _SUMMARY.search(text)
                        and not _OUTBOUND.search(last_user)
                        and last_assistant and not last_assistant.rstrip().endswith('?'))
    # Clarification/denial prose is not a report. Pending plans are continued
    # by the state machine, never by copying its last question into a message.
    safe_prior = (last_assistant and not last_assistant.rstrip().endswith('?')
                  and not re.search(r"\b(?:denied|cancelled|nothing sent|nothing has been sent)\b",
                                    last_assistant, re.I))
    artifact = last_assistant if safe_prior and (refers_back or named_report) else ""
    if not sources and not artifact:
        return None
    if artifact:
        sources = []
    date_range = _date_range(text)
    args = {source: _source_args(source, text, date_range) for source in sources}
    recipient = extract_recipient(text)
    channel = extract_channel(text)
    delivery = "draft" if _DRAFT.search(text) else ("scheduled" if _SCHEDULE.search(text) else "send")
    # Immediate self-delivery uses the reviewable draft path, matching the
    # existing outbound safety rule. Explicit future sends still use the
    # scheduled queue and its confirmation.
    if recipient == "me" and delivery == "send":
        delivery = "draft"
    plan = WorkflowPlan(
        sources=sources,
        source_args=args,
        recipient=recipient,
        channel=channel,
        delivery=delivery,
        when=(_WHEN.search(text).group(0) if delivery == "scheduled" and _WHEN.search(text) else ""),
        location=(args.get("weather") or {}).get("location", ""),
        stock_symbols=(args.get("stock") or {}).get("symbols", []),
        date_range=date_range,
        original_request=original,
        artifact_text=artifact,
    )
    plan.recompute_status()
    return plan


def compile_decision(plan: WorkflowPlan) -> RouteDecision:
    source_tools = [SOURCE_TO_TOOL[source] for source in plan.sources]
    named_recipient = plan.recipient not in {"", "me"} and not (
        _EMAIL.fullmatch(plan.recipient) or _PHONE.fullmatch(plan.recipient))
    contact_tools = ["lookup_contact"] if named_recipient else []
    if plan.delivery == "scheduled":
        effect = "schedule_send"
    elif plan.delivery == "draft":
        effect = "draft_message" if plan.channel == "messages" else "draft_email"
    else:
        effect = "send_message" if plan.channel == "messages" else "send_email"

    direct_calls = [(SOURCE_TO_TOOL[source], plan.source_args.get(source, {}))
                    for source in plan.sources]
    if named_recipient:
        direct_calls.append(("lookup_contact", {"name": plan.recipient}))
    offered = list(dict.fromkeys(source_tools + contact_tools + [effect]))
    groups = tuple(frozenset({name}) for name in offered)
    all_effects = {"send_message", "send_email", "draft_message", "draft_email",
                   "schedule_send", "forward_email", "reply_to_email"}
    # Source arguments are workflow-owned too. A failed stock/news lookup may
    # be retried, but the model cannot silently narrow four requested symbols
    # to the one that happened to succeed or widen a date range on retry.
    bindings: dict[str, dict] = {
        SOURCE_TO_TOOL[source]: dict(plan.source_args.get(source, {}))
        for source in plan.sources
    }
    bindings[effect] = {"to": plan.recipient}
    if plan.artifact_text:
        # Copy exactly; the model must not rewrite, fabricate, or execute text
        # from a referenced answer while composing the delivery.
        payload_key = "text" if effect in {"send_message", "draft_message"} else "body"
        bindings[effect][payload_key] = plan.artifact_text
    if effect == "schedule_send":
        bindings[effect].update({
            "channel": "message" if plan.channel == "messages" else "email",
            "when": plan.when,
        })
    return RouteDecision(
        role="agent",
        model=role_to_model("agent"),
        needs_tools=True,
        source="workflow",
        reason=("typed grounded delivery workflow: "
                + " -> ".join(source_tools + contact_tools + [effect])),
        route_source="workflow",
        expect_tool_first=False,
        tool_subset=offered,
        light_read=False,
        multi_round=True,
        narration_after=frozenset(offered),
        direct_calls=direct_calls,
        required_tool_groups=groups,
        forbidden_tools=frozenset(all_effects - {effect}),
        tool_argument_bindings=bindings,
    )

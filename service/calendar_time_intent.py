"""Pure, whole-envelope admission for named event time questions.

A definite article identifies a title, not a private source. Quoted titles are
opaque bytes; all source/time/action syntax is interpreted outside them.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_QUOTE = re.compile(r'''"[^"\n]+"|“[^”\n]+”|`[^`\n]+`|(?<!\w)'[^'\n]+'(?!\w)|(?<!\w)‘[^’\n]+’(?!\w)''')
_HEAD = re.compile(r"\s*(?:please\s+)?(?:when\s+(?:is|does)|what\s+time\s+(?:is|does))\s+", re.I)
_PERIOD = r"today|tomorrow|(?:this|next)\s+(?:week|month)"
_KIND = re.compile(r"\b(?:tests?|exams?|examinations?|assessments?|midterms?|finals?|quiz|quizzes|review|standup|dentist|appointments?|meetings?|events?|classes|class|lectures?|workshops?|interviews?)\b", re.I)
# These are unconsumed grammatical operators, not a list of public event nouns.
_RESIDUE = re.compile(r"\b(?:and|or|then|also|but|without|except|not|never|only|using|unless|if|from|on|in|at|to|for|with|before|after|during|between|next|last|this|today|tomorrow|yesterday|tonight|now|week|month|year|hours?|minutes?|calendar|schedule|start|starts|begin|begins|happen|happens|end|ends|finish|finishes|over|done|send|email|text|remind|create|delete)\b", re.I)
_TEMPORAL = re.compile(
    r"\b(?:(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"morning|afternoon|evening|midnight|noon|later|soon|ago|daily|weekly|monthly|"
    r"tmrw|tmrow|tommorow|tommorrow|tomorow|tommrow)\b|"
    r"\b\d{1,2}(?::\d{2}|\s*(?:am|pm))\b|\b\d{4}[-/]\d", re.I)
_POLICY = re.compile(r"(?:but\s+)?(?:do\s+not|don['’]t|never)\s+(?:"
    r"(?P<browse>browse(?:\s+(?:the\s+)?(?:web|internet))?)|"
    r"(?P<verb>use|read|search|check|access|open)\s+(?:my\s+|the\s+)?"
    r"(?P<source>calendar|schedule|web|internet|notes|reminders|mail|email|messages))", re.I)


@dataclass(frozen=True)
class CalendarTimeIntent:
    title: str
    personal: bool
    boundary: str = 'start'
    period: str = ''
    supported: bool = True
    no_web: bool = False
    no_calendar: bool = False
    query: str = ''

    def calendar_args(self) -> dict:
        args = {'calendar_only': True, 'query': self.title}
        args.update({'period': self.period} if self.period else {'days': 60})
        if self.boundary == 'end':
            args['boundary'] = 'end'
        return args

    def calendar_bindings(self) -> dict:
        # Model-supplied optional arguments cannot widen the typed window,
        # replace the endpoint or silently choose another linked account.
        return {'days': 60, 'period': '', 'boundary': 'start',
                'account': '', 'include_holidays': False, **self.calendar_args()}


def named_calendar_time(text: str) -> CalendarTimeIntent | None:
    """Return None for other owners; unsupported named envelopes clarify only."""
    quotes = []
    def hide(match):
        quotes.append(match.group()[1:-1])
        return f'QUOTEDTITLE{len(quotes)-1}TOKEN'
    shape = _QUOTE.sub(hide, text)
    head = _HEAD.match(shape)
    if not head:
        return None
    tail = shape[head.end():].strip(' .!?')
    # Split only sentence/source-policy boundaries, never quoted punctuation.
    parts = re.split(r'\s*[;.!?]\s*|\s*,\s*(?=(?:but\s+)?(?:do\s+not|don[’\']t|never)\b)', tail)
    subject, policies = parts[0], parts[1:]
    no_web = no_calendar = False
    supported = True
    for policy in policies:
        if not policy.strip():
            continue
        match = _POLICY.fullmatch(policy.strip())
        if not match:
            supported = False
            continue
        source = (match['source'] or '').casefold()
        no_web |= bool(match['browse']) or source in {'web', 'internet'}
        no_calendar |= source in {'calendar', 'schedule'}
    query = subject.strip()
    period = ''
    if match := re.search(r'\s+(?P<period>' + _PERIOD + r')$', subject, re.I):
        period = match['period'].casefold()
        subject = subject[:match.start()]
    explicit_source = False
    if match := re.search(r'\s+(?:on|in|from)\s+(?:my|our|the)\s+calendar$', subject, re.I):
        explicit_source = True
        subject = subject[:match.start()]
    boundary = 'start'
    if match := re.search(r'\s+(?P<boundary>starts?|begins?|happens?|ends?|finish(?:es)?|over|done)$', subject, re.I):
        boundary = 'end' if re.fullmatch(r'ends?|finish(?:es)?|over|done', match['boundary'], re.I) else 'start'
        subject = subject[:match.start()]
    elif re.search(r'\bdoes\s*$', head.group(), re.I):
        supported = False  # "does" requires a fully understood event relation.
    # Also accept the natural ordering "class on my calendar ends tomorrow".
    if match := re.search(r'\s+(?:on|in|from)\s+(?:my|our|the)\s+calendar$', subject, re.I):
        explicit_source = True
        subject = subject[:match.start()]
    owned = re.match(r'(my|our|the)\s+', subject, re.I)
    personal = explicit_source or bool(owned and owned[1].casefold() in {'my', 'our'})
    if owned:
        subject = subject[owned.end():]
    if match := re.match(r'calendar\s+event\s+(?:named|titled|called)\s+', subject, re.I):
        explicit_source = personal = True
        subject = subject[match.end():]
    # Contextual pronouns remain owned by the existing continuation contract.
    if re.fullmatch(r'(?:it|this|that|they|them|these|those)(?:\s+.*)?', subject, re.I):
        return None
    quoted = re.fullmatch(r'QUOTEDTITLE(\d+)TOKEN', subject)
    if quoted and int(quoted[1]) < len(quotes):
        title = quotes[int(quoted[1])]
    else:
        title = subject.strip()
        supported &= bool(re.fullmatch(r"[\w][\w ’'/-]{0,100}", title)) and not bool(_RESIDUE.search(title) or _TEMPORAL.search(title))
        supported &= not bool(re.search(r'QUOTEDTITLE\d+TOKEN', title))
    if not title:
        supported = False
    # Possessive alone cannot identify an arbitrary private domain as Calendar.
    if personal and not explicit_source and not _KIND.search(title):
        supported = False
    # Public query bytes contain only the fully consumed question, no policies.
    for index, value in enumerate(quotes):
        query = query.replace(f'QUOTEDTITLE{index}TOKEN', '"' + value + '"')
    return CalendarTimeIntent(title, personal, boundary, period, bool(supported),
                              no_web, no_calendar, text[:head.end()] + query + '?')

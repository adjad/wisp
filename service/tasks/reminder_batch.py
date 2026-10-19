"""Bounded dated reminder requests, with one independently verified item per step."""
from __future__ import annotations

from datetime import datetime
import re

from service.tasks.models import SlotValue, TaskPlan, TemporalValue
from service.tasks.temporal import local_timezone_name, resolve_named_time, unambiguous_local_time
from service.utterance_shape import quoted_spans


MAX_ITEMS = 10
_DAY = (r'(?:\d{4}-\d{2}-\d{2}|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|'
        r'jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|'
        r'nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|'
        r'today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)')
_CLOCK = r'(?:\d{1,2}(?::\d{2})?\s*[ap]m|noon|midnight)'
_ALERT = re.compile(_DAY + rf'(?:\s+(?:at\s+{_CLOCK}|morning|afternoon|evening|night))?', re.I)
_HEAD = re.compile(
    r'^\s*(?:(?:please|can you|could you|would you)\s+)?'
    r'(?:set(?:\s+up)?|add|create|make|schedule)\s+(?:me\s+)?(?:an?\s+)?'
    r'(?P<all_day>all[ -]day\s+)?reminders?\s+(?:on\s+)?(?P<rest>[\s\S]+)$', re.I)
_NEXT = re.compile(
    r'\s+(?:and\s+|;\s*)(?:(?:another|one more)(?:\s+reminder)?\s+on\s+|'
    r'(?:set|add|create|make|schedule)\s+(?:an?\s+)?reminder\s+on\s+)', re.I)
# Bounded subject grammar: unquoted clause punctuation/connectors require
# clarification, including unknown verbs. Literal titles may be quoted.
_EXTRA = re.compile(r'[;,\n.!?:]|\b(?:if|unless|otherwise|or|then|but|also)\b', re.I)


def all_day_answer(text: str) -> bool:
    return bool(re.fullmatch(
        r'\s*(?:(?:please|just)\s+)?(?:(?:keep|make)\s+(?:it|them|both)\s+)?'
        r'all[ -]day(?:\s+long)?[.!?\s]*', text, re.I))


def batch_time_answer(text: str) -> bool:
    return all_day_answer(text) or bool(re.fullmatch(
        r'\s*(?:(?:actually|like)\s+)?(?:\d{1,2}(?::\d{2})?\s*[ap]m|noon|midnight)'
        r'(?:\s+(?:today|tomorrow))?[.!?\s]*', text, re.I))


def is_batch(plan: TaskPlan) -> bool:
    return plan.intent == 'reminder.create' and 'items' in plan.parameters


def validated_items(plan: TaskPlan, *, now: datetime | None = None) -> list[dict]:
    """Validate the complete stored envelope before constructing/executing any step."""
    value = plan.parameters['items'].value
    if (plan.intent != 'reminder.create' or plan.channel.value != 'reminders'
            or plan.recipient is not None or not isinstance(value, list)
            or not 1 <= len(value) <= MAX_ITEMS):
        raise ValueError('invalid reminder items')
    ids, identities = set(), set()
    result = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {'id', 'title', 'when_iso', 'all_day'}
                or not isinstance(item['id'], str) or not re.fullmatch(r'reminder_[1-9][0-9]?', item['id'])
                or item['id'] in ids or not isinstance(item['title'], str)
                or not item['title'].strip() or len(item['title']) > 1000
                or type(item['all_day']) is not bool or not isinstance(item['when_iso'], str)):
            raise ValueError('invalid reminder item')
        if item['all_day']:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', item['when_iso']):
                raise ValueError('all-day reminders require a calendar date')
        elif not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?', item['when_iso']):
            raise ValueError('timed reminders require a canonical time')
        due = datetime.fromisoformat(item['when_iso'])
        if not item['all_day'] and due.tzinfo is None and not unambiguous_local_time(due):
            raise ValueError('ambiguous reminder time')
        if now is not None:
            if item['all_day']:
                past = due.date() < now.date()
            else:
                past = due.timestamp() <= now.timestamp()
            if past:
                raise ValueError('a reminder date or time has passed')
        identity = (' '.join(item['title'].split()).casefold(),
                    due.date() if item['all_day'] else due.timestamp(), item['all_day'])
        if identity in identities:
            raise ValueError('duplicate reminder items')
        identities.add(identity)
        ids.add(item['id'])
        result.append(dict(item))
    return result


def compile_dated_reminders(text: str, *, now: datetime, turn: int = 0) -> TaskPlan | None:
    """Claim complete date-before-subject commands, never an arbitrary conjunction."""
    match = _HEAD.fullmatch(text)
    if not match:
        return None
    rest = match['rest'].strip().rstrip('.!?')
    if not (re.match(r'^(?:' + _DAY + r'|(?:the\s+)?\d{1,2}(?:st|nd|rd|th))\b', rest, re.I)
            and re.search(r'\s+for\s+', rest, re.I)):
        return None  # Other subject/clock grammars retain their existing owner.
    def result(items):
        plan = TaskPlan(original_request=text,
            subject=SlotValue(items[0]['title'] if items else '', 'explicit', turn=turn),
            temporal=TemporalValue(original=text, timezone=local_timezone_name(now)),
            parameters={'items': SlotValue(items, 'explicit', turn=turn, original=text)})
        plan.recompute_status()
        return plan
    all_day = bool(match['all_day'])
    if tail := re.search(r'\s+all[ -]day(?:\s+long)?$', rest, re.I):
        all_day, rest = True, rest[:tail.start()]
    spans = quoted_spans(rest)
    cuts = [m for m in _NEXT.finditer(rest)
            if not any(a <= m.start() < b for a, b in spans)]
    pieces, start = [], 0
    for cut in cuts:
        pieces.append(rest[start:cut.start()]); start = cut.end()
    pieces.append(rest[start:])
    if len(pieces) > MAX_ITEMS:
        return result([])
    items, reference = [], None
    for index, piece in enumerate(pieces):
        part = re.fullmatch(r'(?P<when>[^\n]+?)\s+for\s+(?P<title>[\s\S]+)', piece.strip(), re.I)
        if not part:
            return result([])
        title = part['title'].strip()
        title_spans = quoted_spans(title)
        unquoted = ''.join(' ' if any(a <= i < b for a, b in title_spans) else char
                           for i, char in enumerate(title))
        # Only the bounded shared earnings-report noun form is accepted as an
        # unquoted conjunction. Arbitrary companion clauses need clarification;
        # quoting a literal title remains the general escape hatch.
        conjunctions = re.sub(r"\b[a-z0-9]+\s+and\s+[a-z0-9]+\s+earnings\s+report\b", "", unquoted, flags=re.I)
        if _EXTRA.search(unquoted) or re.search(r'\band\b', conjunctions, re.I):
            return result([])
        if len(title_spans) == 1 and title_spans[0] == (0, len(title)):
            title = title[1:-1]
        when = part['when'].strip()
        ordinal = re.fullmatch(r'(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?', when, re.I)
        if ordinal and reference is not None:
            try:
                resolved = reference.replace(day=int(ordinal[1]), hour=9, minute=0, second=0, microsecond=0)
            except ValueError:
                return result([])
            defaulted = 'morning'
        elif _ALERT.fullmatch(when):
            resolved, defaulted = resolve_named_time(when, now=now)
        else:
            return result([])
        if resolved is None:
            return result([])
        # An explicit date in this request supplies the month/year for a
        # following ordinal. Never obtain them from memory or another task.
        reference = resolved
        if all_day and not defaulted:
            return result([])  # A date-only instruction cannot also specify a clock.
        items.append({'id': f'reminder_{index + 1}', 'title': title,
                      'when_iso': resolved.date().isoformat() if all_day else resolved.isoformat(timespec='minutes'),
                      'all_day': all_day})
    return result(items)

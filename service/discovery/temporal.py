"""A08 adapter from deterministic temporal syntax to captured-revision evidence.

The parser resolves expressions, never whether a source claim is true. Keep all
mentions and uncertainty; only an exact, unqualified due instant can be offered
as a candidate deadline by the extraction layer.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import re

from service.temporal_facts import (EvidenceContext, ExtractionResult,
                                    extract_temporal_facts)

_ESTIMATE_CUE = re.compile(r"\b(?:estimate|estimated duration|takes?|allow)\b", re.I | re.ASCII)
_DURATION = re.compile(r"\b(?P<number>\d{1,4})\s*(?P<unit>minutes?|hours?)\b", re.I)
_LABEL = re.compile(r"^\s*(?P<role>due|deadline|event|exam time|available|availability|"
                    r"estimate|estimated duration)\s*:", re.I | re.ASCII)
_ROLE = {'deadline': 'due', 'exam time': 'event', 'available': 'availability',
         'estimated duration': 'estimate'}
# A hedge qualifies the whole due line; its instant is kept only as a mention.
_HEDGE = re.compile(r"\b(?:subject\s+to\s+change|tentative(?:ly)?|provisional(?:ly)?|"
                    r"probably|likely|possibly|perhaps|maybe|approximately|approx|"
                    r"roughly|(?:around|about)(?=\s+\d)|estimated|expected|tbc|tbd|"
                    r"to\s+be\s+(?:confirmed|determined)|unconfirmed|may\s+change|"
                    r"might\s+change|could\s+change|or\s+so)\b|\?", re.I | re.ASCII)
# Approximation and footnote marks ("~2026-10-05", "17:00 UTC*") qualify a due
# line just as a word would. They are symbols, not English, so this is closed.
_HEDGE_MARK = re.compile('[~\u223c\u2248\u2243*\u2020\u2021\u00b1]')
_CUE = re.compile(r"\b(?:due|deadline|tomorrow|today|tonight|yesterday|next week|"
                  r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                  r"january|february|march|april|may|june|july|august|september|"
                  r"october|november|december|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}|"
                  r"\d{1,2}:\d{2}|\d+\s*(?:hours?|minutes?))\b", re.I)


def normalize(source: dict, evidence, *, timezone_name: str | None = None,
              max_facts: int = 64, max_quote: int = 8192) -> tuple[list[dict], bool]:
    """Return one grounded fact per temporal line, retaining every parsed mention.

    ``timezone_name`` is trusted caller metadata. The capture timestamp anchors
    relative expressions, but its UTC representation is never used as a source
    timezone or as evidence for a deadline.
    """
    text = source['text']
    try:
        captured_at = datetime.fromtimestamp(source['observed_at_ms'] / 1000,
                                             timezone.utc)
    except (OverflowError, OSError, ValueError):
        captured_at = None
    try:
        parsed = extract_temporal_facts(
            text, captured_at=captured_at,
            timezone=timezone_name,
            evidence=EvidenceContext(source['source_kind'], source['id'],
                                     source['revision']))
    except (ValueError, OverflowError):
        parsed = ExtractionResult((), 'unknown', ('temporal_parse_error',))
    output: list[dict] = []
    offset = 0
    limited = any(issue in parsed.issues for issue in
                  ('fact_limit_exceeded', 'temporal_parse_error'))
    for line in text.splitlines(keepends=True):
        start, end = offset, offset + len(line)
        offset = end
        label = _LABEL.match(line)
        mentions = [fact for fact in parsed.facts
                    if start <= fact.span.start < end]
        if not label and not mentions and not _CUE.search(line):
            continue
        if end - start > max_quote or len(output) == max_facts:
            limited = True
            continue
        role = _ROLE.get(label['role'].lower(), label['role'].lower()) if label else 'unknown'
        if role == 'unknown' and _ESTIMATE_CUE.search(line) and _DURATION.search(line):
            role = 'estimate'
        if role == 'unknown' and mentions:
            kinds = {fact.kind for fact in mentions}
            if len(kinds) == 1 and 'unknown' not in kinds:
                role = kinds.pop()
        duration = None
        if role == 'estimate':
            match = _DURATION.search(line)
            if match:
                amount = int(match['number'])
                duration = amount * (60 if match['unit'].lower().startswith('hour') else 1)
        values = []
        for fact in mentions:
            values.append({'kind': fact.kind, 'start': fact.span.start,
                           'end': fact.span.end, 'quote': fact.span.quote,
                           'evidence': evidence({'start': fact.span.start,
                                                 'end': fact.span.end,
                                                 'quote': fact.span.quote}),
                           'relation': fact.relation, 'status': fact.status,
                           'uncertainties': list(fact.uncertainties),
                           'start_value': asdict(fact.start),
                           'end_value': asdict(fact.end) if fact.end else None})
        # ISO 8601 seconds with a numeric offset or Z are fully specified even
        # though the shared English parser does not currently lex seconds.
        iso = re.fullmatch(r'\s*(?:Due|Deadline)\s*:\s*'
                           r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}'
                           r'(?:Z|[+-]\d{2}:\d{2}))\s*', line, re.I | re.ASCII)
        # The shared parser can emit a spurious partial mention for a numeric
        # offset ("+05:30"). Mentions wholly inside the ISO value are syntax
        # of that value, so the fully specified instant replaces them.
        if iso and all(start + iso.start(1) <= fact.span.start and
                       fact.span.end <= start + iso.end(1) for fact in mentions):
            values = []
            try:
                stamp = datetime.fromisoformat(iso[1].replace('Z', '+00:00'))
                instant = stamp.astimezone(timezone.utc).isoformat()
                position = start + iso.start(1)
                values.append({'kind': 'due', 'start': position,
                               'end': position + len(iso[1]), 'quote': iso[1],
                               'evidence': evidence({'start': position,
                                                     'end': position + len(iso[1]),
                                                     'quote': iso[1]}),
                               'relation': 'on', 'status': 'resolved',
                               'uncertainties': [], 'start_value': {
                                   'year': stamp.year, 'month': stamp.month,
                                   'day': stamp.day, 'hour': stamp.hour,
                                   'minute': stamp.minute,
                                   'timezone': 'UTC' if stamp.utcoffset().total_seconds() == 0
                                   else stamp.strftime('%z'),
                                   'precision': 'minute', 'instants': (instant,),
                                   'uncertainties': ()}, 'end_value': None})
            except (ValueError, OverflowError):
                pass
        hedged = role == 'due' and bool(_HEDGE.search(line) or _HEDGE_MARK.search(line))
        exact = (role == 'due' and not hedged and len(values) == 1 and
                 values[0]['kind'] == 'due' and values[0]['status'] == 'resolved' and
                 values[0]['relation'] in ('on', 'by') and
                 values[0]['end_value'] is None and
                 not values[0]['uncertainties'] and
                 len(values[0]['start_value']['instants']) == 1)
        interpreted = (not hedged and len(values) == 1 and
                       values[0]['status'] == 'resolved' and
                       not values[0]['uncertainties'])
        output.append({'role': role,
                       'resolution': 'resolved' if interpreted or
                       (role == 'estimate' and duration is not None and not values)
                       else 'unresolved',
                       'evidence': evidence({'start': start, 'end': end,
                                             'quote': line}),
                       'mentions': values, 'estimated_minutes': duration,
                       'due_instant': values[0]['start_value']['instants'][0]
                       if exact else None,
                       'line_start': start, 'line_end': end})
    return output, limited

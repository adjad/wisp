"""Pure event previews and an injected, durable paired capture transaction.

No source reads or native writes live here. Each pair is claimed before either effect;
independent receipts survive partial completion and uncertain writes are never retried.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

from service.attention.contacts import Contacts
from service.attention.corpus import AFFIRM, DECLINE, Item, Snapshot
from service.attention.detectors import _OBLIGATION, _WITHDRAW
from service.attention.exclusions import _PROMO, _SCAM_PHRASES, screen
from service.attention.ledger import Ledger
from service.attention.live import LOOKBACK_S, Settings, _clause, items_from_records, local_midnight, plan_reminder
from service.attention.matching import find_on_file, shares_words
from service.attention.resolve import _CANCEL, _TENTATIVE, resolve

HORIZON_DAYS = 31
_EVENT = re.compile(r"\b(?:exam|quiz|midterm|final|appointment|interview|meeting|coffee(?:\s+chat)?|hangout|hang\s+out|lunch|dinner|breakfast|brunch|party|practice|rehearsal|concert|visit|reservation|flight|workshop|class|seminar|session|catch\s+up|meet(?:\s+up)?|see\s+you|come\s+over)\b", re.I)
_GENERIC_EVENT = re.compile(r"\b(?:we(?:['’]re|\s+are|\s+will)|let'?s|see\s+you|i(?:['’]ll|\s+will)\s+(?:join|meet|see|bring|pick\s+you))\b", re.I)
_GENERIC_UNCERTAIN = re.compile(r"\b(?:not|never|no|wondering|hoping|considering|thinking|maybe|might)\b", re.I)
_COMMITTED = re.compile(r"\b(?:confirmed|scheduled|booked|we(?:'re|\s+are|\s+will)|i(?:'ll|\s+will)|see\s+you|let'?s|our|your|reminder|have|is|on\s+for)\b", re.I)
_QUOTED = re.compile(r"(?im)^\s*(?:on .+wrote:|from:|>+|[-_]{3,}\s*(?:original|forwarded)|begin forwarded message:|forwarded message:)")
_TYPOS = {"wenesday": "wednesday", "wensday": "wednesday", "thirsday": "thursday", "thurday": "thursday"}
_NEGATED = re.compile(r"\b(?:no|not)\b[^.!?\n]{0,30}\b(?:exam|quiz|meeting|coffee|hangout|event|class|plans?)\b|\b(?:already|previously)\s+(?:attended|finished|happened)\b", re.I)
_OTHER_OWNER = re.compile(r"\b(?:my|his|her|their)\b|\b(?:he|she|they)\s+(?:has|have|is|are|will)\b|\bi\s+(?:have|am|will|need)\b|\bi['’]m\b|\b[A-Za-z]+['’]s\s+(?:exam|appointment|meeting|interview)\b", re.I)
_THIRD_OWNER = re.compile(r"\b(?:my|your|our|his|her|their)\s+(?:brother|sister|friend|partner|mother|father|son|daughter|roommate|boss|colleague)\b|\b(?:he|she|they)\s+(?:has|have|is|are|will)\b|\b[A-Za-z]+['’]s\s+(?:exam|appointment|meeting|interview)\b", re.I)
_DIRECT_JOINT = re.compile(r"\bi\s+have\s+our\b|\bi(?:['’]ll|\s+will|['’]m)\s+(?:meet(?:ing)?|see(?:ing)?|join(?:ing)?)\s+you\b", re.I)
_USER_PARTICIPATES = re.compile(r"\b(?:you|your|our|we)\b|\b(?:let'?s|see\s+you)\b", re.I)
_RANGE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*(?:to|[-–—])\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", re.I)
_DURATION = re.compile(r"\b(?:for|lasting)\s+(\d{1,3})\s*(minutes?|mins?|hours?|hrs?)\b", re.I)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Candidate:
    source_id: str
    source_ts: float
    source_fingerprint: str
    capture_key: str
    title: str
    quote: str
    day: str
    timezone: str
    start_ts: float | None
    duration_min: int | None
    due_ts: float
    when: str
    inferred: tuple[str, ...]
    source_kind: str
    scope: str
    automatic_eligible: bool

    @property
    def review_fingerprint(self) -> str:
        """Bind Add to the displayed schedule, including reminder defaults."""
        return digest(json.dumps({key: value for key, value in asdict(self).items()
                                  if key != 'inferred'}, sort_keys=True, allow_nan=False))

    def calendar_plan(self, *, automatic: bool) -> dict:
        return {"capture_key": self.capture_key, "source_id": self.source_id,
                "title": self.title, "day": self.day, "timezone": self.timezone,
                "start_ts": self.start_ts, "duration_min": self.duration_min,
                "all_day": self.start_ts is None, "automatic": automatic}


def mail_items(rows: list[dict]) -> tuple[list[Item], set[str]]:
    """Adapt *admitted* current rows. Identity includes native account + Message-ID.

    No body history, sender display-name trust, HTML interpretation or new source reads.
    School eligibility requires an institutional address plus an actual class/event cue.
    """
    items, institutional = [], set()
    for row in rows:
        try:
            ts = float(row['ts'])
            account, mid = str(row['account_id']), str(row['message_id'])
            if not account or not mid or not math.isfinite(ts):
                continue
            sender = str(row.get('sender') or '')
            address = parseaddr(str(row.get('sender_address') or sender))[1].lower()
            domain = address.rpartition('@')[2]
            subject = str(row.get('subject') or '')[:2000]
            body = _QUOTED.split(str(row.get('body') or '')[:16000], maxsplit=1)[0]
            body = re.split(r'(?im)^\s*(?:unsubscribe|manage (?:your )?notifications)\b', body, maxsplit=1)[0]
            # Keep current plain text; footer calls to unsubscribe are screened upstream.
            text = subject + '\n' + body
            sid = 'mail:' + digest(account + '\0' + mid)
            item = Item(sid, 'mail', ts, 'incoming', sender, address, text)
            items.append(item)
            school = domain.endswith('.edu') or domain in {'edstem.org', 'us.edstem.org', 'edstem.com', 'instructure.com'}
            course = re.search(r"\b(?:[A-Z]{2,8}[- ]?\d{1,4}|exam|quiz|midterm|class|course)\b", subject, re.I)
            if school and (course or _EVENT.search(subject)) and _EVENT.search(text) and not (_PROMO.search(subject) or _SCAM_PHRASES.search(text)):
                institutional.add(sid)
        except (KeyError, TypeError, ValueError):
            continue
    return items, institutional


def candidates(records: list[dict], rows: list[dict], *, now: float, tz: str,
               settings: Settings) -> list[Candidate]:
    items = items_from_records(records)
    mail, institutional = mail_items(rows)
    items += mail
    contacts = Contacts(items)
    snapshot = Snapshot(Path('.'), {}, sorted(items, key=lambda i: i.ts), [])
    out = []
    for item in items:
        if item.direction != 'incoming' or not now - LOOKBACK_S <= item.ts <= now:
            continue
        trusted_school = item.id in institutional
        if trusted_school:
            # An individual course announcement may be machine-sent. Other exclusion
            # signals retain their force; no global relaxation of the Messages screen.
            if _PROMO.search(item.text) or _SCAM_PHRASES.search(item.text):
                continue
        elif screen(item) or not contacts.importance(item).eligible:
            continue
        text = re.sub(r'\b(' + '|'.join(_TYPOS) + r')\b', lambda m: _TYPOS[m[0].lower()], item.text, flags=re.I)
        event_cue = _EVENT.search(text) or (_GENERIC_EVENT.search(text) and not _GENERIC_UNCERTAIN.search(text))
        if not event_cue or _CANCEL.search(text) or _WITHDRAW.search(text) or _NEGATED.search(text):
            continue
        stated_range = _RANGE.search(text)
        start_text = text[:stated_range.start()] + stated_range[1] + text[stated_range.end():] if stated_range else text
        r = resolve(start_text, datetime.fromtimestamp(item.ts, timezone.utc), tz, item.id)
        range_minutes = None
        if stated_range:
            end_text = text[:stated_range.start()] + stated_range[2] + text[stated_range.end():]
            end = resolve(end_text, datetime.fromtimestamp(item.ts, timezone.utc), tz, item.id)
            if r.start is None or end.start is None or end.blocked or end.guessed:
                continue
            gap = (end.start.timestamp() - r.start.timestamp()) / 60
            if not 1 <= gap <= 10080 or gap != int(gap):
                continue
            range_minutes = int(gap)
        if r.blocked or r.day is None or r.guessed:
            continue
        if item.source == 'messages':
            # Participation must belong to the clause that supplied this event,
            # never an unrelated 'your notes look good' in the next sentence.
            at = r.at or 0
            before = max((text.rfind(mark, 0, at) for mark in '.!?\n;'), default=-1) + 1
            ends = [index for mark in '.!?\n;' if (index := text.find(mark, at)) >= 0]
            event_clause = text[before:min(ends) if ends else len(text)]
            if _THIRD_OWNER.search(event_clause):
                continue
            if _OTHER_OWNER.search(event_clause) and not _DIRECT_JOINT.search(event_clause):
                continue
            if not _EVENT.search(event_clause) and not (_GENERIC_EVENT.search(event_clause) and not _GENERIC_UNCERTAIN.search(event_clause)):
                continue
        # A reply must actually affirm; an unrelated outgoing reply is not consent.
        later = [i for i in items if i.source == item.source and i.conversation == item.conversation
                 and i.direction == 'outgoing' and item.ts < i.ts <= min(now, item.ts + 6 * 3600)]
        confirmed = any(AFFIRM.search(i.text) and not DECLINE.search(i.text) for i in later)
        if any(_CANCEL.search(i.text) or _WITHDRAW.search(i.text) or DECLINE.search(i.text)
               for i in snapshot.items if i.conversation == item.conversation and item.ts < i.ts <= now):
            continue
        if (r.tentative or _TENTATIVE.search(text)) and not confirmed:
            continue
        if not (trusted_school or _COMMITTED.search(text) or confirmed or _OBLIGATION.search(text)):
            # A concise declarative plan ("coffee chat next Thursday") is a statement;
            # tentative invitations/questions were already refused above.
            if len(text.split()) > 16:
                continue
        day_gap = (r.day - datetime.fromtimestamp(now, ZoneInfo(tz)).date()).days
        if not 0 <= day_gap <= HORIZON_DAYS:
            continue
        plan = plan_reminder(item, r, now, settings, tz, who='Email' if item.source == 'mail' else None)
        if plan is None:
            continue
        quote = _clause(item.text, r.at, 160)
        title = ' '.join(quote.split())[:110]
        duration = _DURATION.search(text)
        minutes = int(duration[1]) * (60 if duration[2].lower().startswith(('hour', 'hr')) else 1) if duration else None
        if range_minutes is not None:
            if minutes is not None and minutes != range_minutes:
                continue
            minutes = range_minutes
        if minutes is not None and not 1 <= minutes <= 10080:
            minutes = None
        when = r.day.isoformat() + (' · time not stated; all-day Calendar; reminder at ' + datetime.fromtimestamp(plan.due_ts, ZoneInfo(tz)).strftime('%H:%M') if not r.has_clock else ' · ' + plan.when_label)
        if r.has_clock and minutes is None:
            when += ' · Calendar needs an end time or duration'
        terms = re.sub(r'\b(?:next|this|reminder|monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow)\b', '', text.split('\n')[0], flags=re.I)
        semantic = ' '.join(re.findall(r'[a-z0-9]+', terms.lower()))
        course = re.search(r'\b((?!(?:EXAM|QUIZ|FINAL|COURSE)\b)[A-Z]{2,8})[- ]?(\d{1,4})\b', item.text.split('\n')[0])
        scope = 'course:' + digest(course[1].lower() + course[2]) if course else 'thread:' + digest(item.conversation.casefold())
        key = 'capture:' + digest(f'{scope}|{r.day}|{r.start.timestamp() if r.start else "date"}|{semantic}')
        automatic_eligible = item.source == 'messages' or bool(course and re.search(r'\b(?:exam|quiz|midterm|final|class)\b', text, re.I))
        if not automatic_eligible:
            when += ' · review participation before adding'
        when += f' · using {tz}'
        out.append(Candidate(item.id, item.ts, digest(f'{item.ts}|{item.sender}|{item.conversation}|{item.text}|{tz}|{settings.default_hour}|{settings.lead_minutes}'), key, title, quote,
                             r.day.isoformat(), tz, r.start.timestamp() if r.start else None,
                             minutes, plan.due_ts, when, r.inferred + ('timezone:local_at_first_preview',), item.source, scope, automatic_eligible))
    return out


def preview(candidate: Candidate, ledger: Ledger, now: float) -> dict:
    """Upsert a disposable preview separately from the durable effect claim."""
    sid = 'preview:' + candidate.source_id
    detail = {**asdict(candidate), 'paired': True, 'when': candidate.when, 'quote': candidate.quote,
              'review_fingerprint': candidate.review_fingerprint}
    return ledger.save_preview(sid, title=candidate.title, due_ts=candidate.due_ts,
                               event_ts=candidate.start_ts, now=now, detail=detail)


def _resolved(c: Candidate):
    from datetime import date
    from service.attention.resolve import Resolved
    return Resolved(datetime.fromtimestamp(c.start_ts, ZoneInfo(c.timezone)) if c.start_ts else None,
                    date.fromisoformat(c.day), c.start_ts is not None, c.quote)


async def capture(c: Candidate, *, ledger: Ledger, now: float, settings: Settings,
                  fresh_commitments, create_calendar, create_reminder, automatic: bool,
                  still_current=lambda: True) -> dict:
    """Claim once, save each effect, never replay a completed/uncertain pair."""
    existing = ledger.get(c.capture_key)
    if existing:
        ledger.finish('preview:' + c.source_id, existing['state'], existing['detail'], now)
        return existing
    # Cross-message dedup is intentionally conservative: same event day and shared
    # event words. It prevents announcement followups from making a second pair.
    for row in ledger.rows_relevant(now - LOOKBACK_S, now, ('created', 'partial', 'unknown', 'claimed', 'undone')):
        if row['source_id'].startswith('capture:') and row['detail'].get('day') == c.day and row['detail'].get('scope') == c.scope and row['event_ts'] == c.start_ts and shares_words(c.quote, row):
            ledger.finish('preview:' + c.source_id, row['state'], row['detail'], now)
            return row
    if not still_current():
        raise ValueError('The source changed or is no longer fresh. Refresh the preview.')
    if ledger.capture_count_since(local_midnight(now, c.timezone)) >= settings.daily_cap:
        raise ValueError('Daily capture limit reached.')
    detail = {**asdict(c), 'paired': True, 'effects': {}, 'automatic': automatic, 'when': c.when}
    if not ledger.claim(c.capture_key, title=c.title, due_ts=c.due_ts, event_ts=c.start_ts, now=now, detail=detail):
        return ledger.get(c.capture_key)
    for kind, effect in (('calendar', create_calendar), ('reminder', create_reminder)):
        if not still_current():
            detail['effects'][kind] = {'status': 'blocked', 'error': 'Source changed; refresh required.'}
            ledger.finish(c.capture_key, 'claimed', detail, now)
            continue
        try:
            commitments = fresh_commitments()
            subset = [r for r in commitments if (r.get('source') == 'calendar') == (kind == 'calendar')]
            if find_on_file(_resolved(c), c.quote, subset, c.timezone):
                detail['effects'][kind] = {'ok': True, 'status': 'already_on_file'}
            elif kind == 'calendar' and c.start_ts is not None and c.duration_min is None:
                detail['effects'][kind] = {'status': 'needs_duration', 'error': 'An end time or duration is required.'}
            else:
                detail['effects'][kind] = {'status': 'claimed'}
                ledger.finish(c.capture_key, 'claimed', detail, now)
                result = await effect(c, automatic=automatic)
                status = 'succeeded' if result.get('ok') is True else result.get('status', 'unknown')
                detail['effects'][kind] = {**{k: result[k] for k in ('ok', 'source_id', 'action_id', 'created', 'where', 'error') if k in result}, 'status': status}
        except Exception as exc:  # a dispatch may already have happened
            detail['effects'][kind] = {'status': 'unknown', 'error': type(exc).__name__}
        ledger.finish(c.capture_key, 'claimed', detail, now)
    states = [v['status'] for v in detail['effects'].values()]
    success = {'succeeded', 'already_on_file'}
    state = 'created' if all(s in success for s in states) else ('unknown' if 'unknown' in states else ('partial' if any(s in success for s in states) else 'failed'))
    ledger.finish(c.capture_key, state, detail, now)
    ledger.finish('preview:' + c.source_id, state, detail, now)
    return ledger.get(c.capture_key)

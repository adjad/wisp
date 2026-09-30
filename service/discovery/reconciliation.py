"""A09 local reconciliation of grounded captures; no external effects.

Source identity narrows the search before title similarity. A changed claim is
durable uncertainty until a caller explicitly confirms it. A capture's absence
of an item, including a complete capture, is never a deletion or completion.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
from urllib.parse import urlsplit, urlunsplit

from service.browser.contracts import ContractViolation, require, validate
from service.discovery.store import DiscoveryStore, RevisionConflict, encode, integer
from service.discovery.temporal import normalize as normalize_temporal


def _url(value: str | None) -> str | None:
    if value is None:
        return None
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip('/') or '/', parts.query, ''))


def _tokens(value: str) -> set[str]:
    return set(re.findall(r'\w+', value.casefold()))


def _similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _valid_claims(value) -> bool:
    return (type(value) is dict and {'due_at_ms', 'due_timezone'} <= value.keys() and
            set(value) <= {'due_at_ms', 'due_timezone', 'location', 'requirements'} and
            (value['due_at_ms'] is None or type(value['due_at_ms']) is int) and
            (value['due_timezone'] is None or type(value['due_timezone']) is str) and
            (value['due_at_ms'] is None) == (value['due_timezone'] is None) and
            all(type(value[key]) is str for key in ('location', 'requirements')
                if key in value))


def _row(row) -> dict:
    try:
        claims = json.loads(row['claims'])
        pending = json.loads(row['pending']) if row['pending'] is not None else None
    except (TypeError, ValueError) as exc:
        raise ContractViolation('Corrupt reconciliation state') from exc
    require(_valid_claims(claims), 'Corrupt reconciliation claims')
    if row['pending'] is not None:
        require(type(pending) is dict and set(pending) == {
            'candidate', 'claims', 'source_revision', 'changes'} and
            _valid_claims(pending['claims']) and
            type(pending['source_revision']) is str and
            type(pending['changes']) is dict,
            'Corrupt pending reconciliation')
        candidate = validate('ActionableItem', pending['candidate'])
        require(all(evidence['source_revision'] == pending['source_revision'] and
                    evidence['observation_id'] == candidate['evidence'][0]['observation_id']
                    for evidence in candidate['evidence']) and
                pending['claims']['due_at_ms'] == candidate['due_at_ms'] and
                pending['claims']['due_timezone'] == candidate['due_timezone'],
                'Corrupt pending source relationship')
    return {'item_id': row['item_id'], 'source_kind': row['source_kind'],
            'source_record_id': row['source_record_id'], 'source_url': row['source_url'],
            'observation_id': row['observation_id'], 'source_revision': row['source_revision'],
            'claims': claims, 'pending': pending}


class Reconciler:
    def __init__(self, store: DiscoveryStore):
        self.store = store

    def get(self, item_id: str) -> dict | None:
        with self.store.assistant.transaction(write=False) as db:
            row = db.execute('SELECT * FROM discovery_reconciliation_links WHERE item_id=?',
                             (item_id,)).fetchone()
            link = _row(row) if row else None
            if link:
                latest_change = db.execute(
                    'SELECT id,observation_id,details FROM discovery_reconciliation_events '
                    'WHERE item_id=? AND event IN '
                    "('conflict','additional_revision') ORDER BY id DESC LIMIT 1",
                    (item_id,)).fetchone()
                latest_confirm = db.execute(
                    'SELECT id FROM discovery_reconciliation_events WHERE item_id=? '
                    "AND event='confirmed' ORDER BY id DESC LIMIT 1",
                    (item_id,)).fetchone()
                unresolved = (latest_change is not None and
                              (latest_confirm is None or
                               latest_change['id'] > latest_confirm['id']))
                require((link['pending'] is not None) == unresolved,
                        'Corrupt pending reconciliation state')
            if link and link['pending']:
                pending = link['pending']
                observation_id = pending['candidate']['evidence'][0]['observation_id']
                require(latest_change['observation_id'] == observation_id,
                        'Corrupt pending reconciliation event')
                try:
                    details = json.loads(latest_change['details'])
                except (TypeError, ValueError) as exc:
                    raise ContractViolation('Corrupt pending reconciliation event') from exc
                require(type(details) is dict and
                        details.get('source_revision', pending['source_revision']) ==
                        pending['source_revision'] and
                        details.get('changes', details) == pending['changes'] and
                        (details.get('claims', pending['claims']) == pending['claims']),
                        'Corrupt pending reconciliation event')
                omitted_due = (pending['claims']['due_at_ms'] is None and
                               pending['claims']['due_timezone'] is None and
                               not {'due_at_ms', 'due_timezone'} & pending['changes'].keys())
                for field, value in pending['claims'].items():
                    if omitted_due and field in ('due_at_ms', 'due_timezone'):
                        continue
                    change = pending['changes'].get(field)
                    if change is not None:
                        require(type(change) is dict and change.get('proposed') == value,
                                'Corrupt pending claim change')
                    elif field != 'due_timezone' or 'due_at_ms' not in pending['changes']:
                        require(value == link['claims'].get(field),
                                'Corrupt pending claim change')
                if 'title' in pending['changes']:
                    require(type(pending['changes']['title']) is dict and
                            pending['changes']['title'].get('proposed') ==
                            pending['candidate']['title'],
                            'Corrupt pending title change')
                else:
                    current = self.store.get('ActionableItem', item_id)
                    require(current is not None and
                            pending['candidate']['title'] == current['payload']['title'],
                            'Corrupt pending title change')
            return link

    def effective_claims(self, item_id: str) -> dict | None:
        """Project accepted source claims with explicit local overrides on top."""
        link = self.get(item_id)
        item = self.store.get('ActionableItem', item_id)
        if link is None or item is None:
            return None
        allowed = {'due_at_ms', 'due_timezone', 'location', 'requirements'}
        return {**link['claims'], **{key: value for key, value in
                                     item['overrides'].items() if key in allowed}}

    def events(self, item_id: str) -> list[dict]:
        with self.store.assistant.transaction(write=False) as db:
            return [dict(id=row['id'], item_id=row['item_id'],
                         observation_id=row['observation_id'], event=row['event'],
                         details=json.loads(row['details']))
                    for row in db.execute('SELECT * FROM discovery_reconciliation_events '
                                          'WHERE item_id=? ORDER BY id', (item_id,))]

    @staticmethod
    def _event(db, item_id: str, observation_id: str, event: str, details: dict) -> None:
        db.execute('INSERT INTO discovery_reconciliation_events'
                   '(item_id,observation_id,event,details) VALUES (?,?,?,?)',
                   (item_id, observation_id, event, encode(details)))

    def _mark_uncertain(self, item_id: str, current: dict) -> None:
        value = current['payload']
        if value['state'] in ('completed', 'dismissed'):
            return
        message = 'Conflicting captures reuse one source revision; explicit confirmation required.'
        if value['state'] == 'needs_clarification' and value['ambiguity'] == message:
            return
        self.store.save('ActionableItem', {**value, 'state': 'needs_clarification',
            'ambiguity': message, 'revision': value['revision'] + 1,
            'supersedes_revision': value['revision']},
            expected_revision=current['revision'])

    @staticmethod
    def _validate_input(observation: dict, extraction: dict,
                        fact_claims: dict | None) -> tuple[dict, list[tuple[dict, dict]]]:
        source = validate('SourceObservation', observation)
        if (type(extraction) is not dict or
                extraction.get('coverage') not in ('complete', 'partial', 'unknown') or
                type(extraction.get('processing_complete')) is not bool or
                type(extraction.get('items')) is not list or
                type(fact_claims) not in (dict, type(None))):
            raise ValueError('Invalid reconciliation input')
        provided = fact_claims or {}
        if any(type(k) is not str or type(v) is not dict for k, v in provided.items()):
            raise ValueError('Invalid fact claims')
        raw_spans = extraction.get('spans', [])
        require(type(raw_spans) is list, 'Invalid extraction spans')
        spans = {}
        for span in raw_spans:
            require(type(span) is dict and set(span) == {'evidence_id', 'start', 'end'} and
                    type(span['evidence_id']) is str and span['evidence_id'] not in spans and
                    type(span['start']) is int and type(span['end']) is int and
                    0 <= span['start'] < span['end'] <= len(source['text']),
                    'Invalid extraction span')
            spans[span['evidence_id']] = span
        entries = []
        seen = set()
        headings = list(re.finditer(
            r'(?im)^[ \t]*(?:assignment|homework|exam|quiz|scheduling|follow[ -]up)[ \t]*:',
            source['text']))
        items = [validate('ActionableItem', raw) for raw in extraction['items']]

        def title_position(candidate: dict) -> int:
            anchors = []
            for entry in candidate['evidence']:
                span = spans.get(entry['id'])
                if span is None or candidate['title'] not in entry['quote']:
                    continue
                positions = [match.start() for match in
                             re.compile(re.escape(candidate['title'])).finditer(
                                 source['text'], span['start'], span['end'])]
                if len(positions) == 1:
                    anchors.append((span['end'] - span['start'], positions[0]))
            if anchors:
                shortest = min(length for length, _ in anchors)
                positions = {position for length, position in anchors
                             if length == shortest}
                return next(iter(positions)) if len(positions) == 1 else -1
            matches = list(re.finditer(re.escape(candidate['title']), source['text']))
            return matches[0].start() if len(matches) == 1 else -1

        title_positions = {item['id']: title_position(item) for item in items}
        region_lines = sorted({source['text'].rfind('\n', 0, position) + 1
                               for position in title_positions.values() if position >= 0})
        for item in items:
            require(item['id'] not in seen and item['revision'] == 1 and
                    item['supersedes_revision'] is None and
                    item['state'] == 'needs_clarification' and
                    item['completion_receipt_id'] is None and
                    not item['external_record_ids'], 'Invalid extracted candidate')
            seen.add(item['id'])
            evidence = {e['id']: e for e in item['evidence']}
            require(len(evidence) == len(item['evidence']), 'Duplicate candidate evidence')
            require(any(item['title'] in e['quote'] for e in evidence.values()),
                    'Ungrounded candidate title')
            for entry in evidence.values():
                validate('Evidence', entry)
                require(entry['observation_id'] == source['id'] and
                        entry['source_revision'] == source['revision'] and
                        entry['captured_at_ms'] == source['observed_at_ms'] and
                        entry['quote'] in source['text'], 'Candidate evidence mismatch')
                if entry['id'] in spans:
                    span = spans[entry['id']]
                    require(source['text'][span['start']:span['end']] == entry['quote'],
                            'Candidate span mismatch')
            title_at = title_positions[item['id']]
            preceding = [heading for heading in headings if heading.start() <= title_at]
            if preceding:
                block_start = preceding[-1].start()
                block_end = next((heading.start() for heading in headings
                                  if heading.start() > title_at), len(source['text']))
            elif title_at >= 0 and region_lines:
                line = source['text'].rfind('\n', 0, title_at) + 1
                block_start = 0 if line == region_lines[0] else line
                block_end = next((other for other in region_lines
                                  if other > line), len(source['text']))
                block_end = min(block_end, headings[0].start()) if headings else block_end
            else:
                block_start, block_end = 0, len(source['text'])
            block = source['text'][block_start:block_end]
            if item['due_at_ms'] is not None:
                require(title_at >= 0 and
                        (len(items) == 1 or preceding or
                         len(region_lines) == len(items)), 'Unscoped due instant')
                if preceding:
                    heading_line_end = source['text'].find('\n', preceding[-1].start())
                    require(title_at < (heading_line_end if heading_line_end >= 0
                                        else len(source['text'])),
                            'Unscoped due instant')
                facts = extraction.get('temporal_facts')
                require(type(facts) is list, 'Ungrounded due instant')
                verified, limited = normalize_temporal(
                    source, lambda span: {'quote': span['quote']},
                    timezone_name=item['due_timezone'])
                require(not limited, 'Incomplete temporal grounding')
                expected = {(fact['line_start'], fact['line_end'],
                             fact['due_instant'], fact['evidence']['quote'])
                            for fact in verified if fact['role'] == 'due' and
                            fact['due_instant'] is not None}
                grounded_due = False
                for fact in facts:
                    if type(fact) is not dict or fact.get('due_instant') is None:
                        continue
                    fact_evidence = fact.get('evidence')
                    require(fact.get('role') == 'due' and
                            type(fact_evidence) is dict,
                            'Ungrounded temporal fact')
                    validate('Evidence', fact_evidence)
                    require(fact_evidence['observation_id'] == source['id'] and
                            fact_evidence['source_revision'] == source['revision'] and
                            fact_evidence['captured_at_ms'] == source['observed_at_ms'] and
                            (fact.get('line_start'), fact.get('line_end'),
                             fact['due_instant'], fact_evidence['quote']) in expected,
                            'Ungrounded temporal fact')
                    if (block_start <= fact['line_start'] < fact['line_end'] <= block_end and
                            int(datetime.fromisoformat(fact['due_instant']).timestamp() * 1000)
                            == item['due_at_ms']):
                        grounded_due = True
                require(grounded_due, 'Ungrounded due instant')
            claims = {'due_at_ms': item['due_at_ms'],
                      'due_timezone': item['due_timezone']}
            extra = provided.get(item['id'], {})
            require(set(extra) <= {'location', 'requirements'}, 'Unsupported fact claim')
            if extra and (title_at < 0 or (len(items) > 1 and not preceding and
                                         len(region_lines) != len(items))):
                raise ContractViolation('Unscoped fact claim')
            for field, fact in extra.items():
                label = 'Location' if field == 'location' else 'Requirements?'
                require(type(fact) is dict and set(fact) == {'value', 'evidence_id'} and
                        type(fact['value']) is str and bool(fact['value'].strip()) and
                        len(fact['value']) <= 2048 and fact['evidence_id'] in evidence and
                        fact['value'] in evidence[fact['evidence_id']]['quote'] and
                        re.search(r'(?im)^[ \t]*' + label + r'[ \t]*:[ \t]*' +
                                  re.escape(fact['value']) + r'(?:[ \t]*$|\r?$)',
                                  block) is not None,
                        'Ungrounded fact claim')
                claims[field] = fact['value']
            entries.append((item, claims))
        require(set(provided) <= seen, 'Unknown fact claim candidate')
        return source, entries

    def _match(self, db, source: dict, item: dict, claims: dict,
               used: set[str]) -> tuple[dict | None, bool]:
        rows = [_row(row) for row in db.execute('SELECT * FROM discovery_reconciliation_links '
                                               'WHERE source_kind=?', (source['source_kind'],))]
        url = _url(source['source_url'])
        ranked = []
        weak_identity = False
        for link in rows:
            if link['item_id'] in used:
                continue
            old = self.store.get('ActionableItem', link['item_id'])
            if old is None or old['payload']['kind'] != item['kind']:
                continue
            score = _similarity(old['payload']['title'], item['title'])
            same_host = (url is None or link['source_url'] is None or
                         urlsplit(url).netloc == urlsplit(link['source_url']).netloc)
            exact_id = (source['source_record_id'] is not None and same_host and
                        source['source_record_id'] == link['source_record_id'])
            exact_url = (url is not None and url == link['source_url'] and
                         (source['source_record_id'] is None or
                          link['source_record_id'] is None or
                          source['source_record_id'] == link['source_record_id']))
            if exact_id and score >= (0.85 if url != link['source_url'] else 0.6):
                priority = 3
            elif exact_id:
                weak_identity = True
                continue
            elif exact_url and score >= 0.6:
                priority = 2
            elif (not source['source_record_id'] and not url and
                  score >= 0.85 and claims['due_at_ms'] is not None and
                  claims['due_at_ms'] == link['claims'].get('due_at_ms')):
                # Similarity alone cannot join unrelated source records.
                priority = 1
            else:
                continue
            ranked.append((priority, score, link))
        if not ranked:
            return None, weak_identity
        ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        best = ranked[0]
        if (len(ranked) > 1 and best[0] == ranked[1][0] and
                best[1] - ranked[1][1] < 0.2):
            return None, True
        return best[2], False

    def apply(self, observation: dict, extraction: dict,
              *, fact_claims: dict | None = None) -> list[dict]:
        """Persist candidates and source changes atomically; never infer absence.

        Failed extraction yields no reconciliation writes. Partial extraction may
        add or corroborate candidates but cannot erase an existing claim.
        """
        source, entries = self._validate_input(observation, extraction, fact_claims)
        if not extraction['processing_complete'] and not entries:
            return []
        results = []
        used = set()
        with self.store.assistant.transaction() as db:
            self.store.save('SourceObservation', source)
            for item, claims in entries:
                for evidence in item['evidence']:
                    self.store.save('Evidence', evidence)
                # Capture-scoped IDs make an exact retry recognizable even if
                # this candidate was retained after an ambiguous match.
                known = self.get(item['id'])
                if known is not None:
                    if known['observation_id'] != source['id']:
                        seen_capture = db.execute(
                            'SELECT 1 FROM discovery_reconciliation_events WHERE '
                            'item_id=? AND observation_id=? LIMIT 1',
                            (item['id'], source['id'])).fetchone()
                        require(seen_capture is not None, 'Candidate ID collision')
                        used.add(item['id'])
                        results.append({'candidate_id': item['id'], 'item_id': item['id'],
                                        'status': 'stale_capture'})
                        continue
                    require(known['source_revision'] == source['revision'],
                            'Candidate ID collision')
                    was_ambiguous = db.execute(
                        'SELECT 1 FROM discovery_reconciliation_events WHERE '
                        "item_id=? AND event='ambiguous_match' LIMIT 1",
                        (item['id'],)).fetchone() is not None
                    used.add(item['id'])
                    results.append({'candidate_id': item['id'], 'item_id': item['id'],
                                    'status': 'ambiguous_match' if was_ambiguous
                                    else 'pending' if known['pending'] else 'unchanged'})
                    continue
                link, ambiguous = self._match(db, source, item, claims, used)
                if ambiguous:
                    # Preserve the uncertainty as a separate candidate rather
                    # than guessing an existing obligation or dropping it.
                    self.store.save('ActionableItem', item)
                    db.execute('INSERT INTO discovery_reconciliation_links VALUES (?,?,?,?,?,?,?,?)',
                               (item['id'], source['source_kind'], source['source_record_id'],
                                _url(source['source_url']), source['id'], source['revision'],
                                encode(claims), None))
                    self._event(db, item['id'], source['id'], 'ambiguous_match', claims)
                    used.add(item['id'])
                    results.append({'candidate_id': item['id'], 'item_id': item['id'],
                                    'status': 'ambiguous_match'})
                    continue
                if link is None:
                    # The candidate ID is capture-scoped and becomes the stable
                    # canonical ID only for this first observation.
                    self.store.save('ActionableItem', item)
                    db.execute('INSERT INTO discovery_reconciliation_links VALUES (?,?,?,?,?,?,?,?)',
                               (item['id'], source['source_kind'], source['source_record_id'],
                                _url(source['source_url']), source['id'], source['revision'],
                                encode(claims), None))
                    self._event(db, item['id'], source['id'], 'candidate', claims)
                    used.add(item['id'])
                    results.append({'candidate_id': item['id'], 'item_id': item['id'],
                                    'status': 'needs_confirmation'})
                    continue
                item_id = link['item_id']
                used.add(item_id)
                old = self.store.get('ActionableItem', item_id)
                pending = link['pending']
                if (link['observation_id'] != source['id'] and
                        (pending is None or pending['candidate']['evidence'][0]['observation_id']
                         != source['id']) and
                        db.execute('SELECT 1 FROM discovery_reconciliation_events WHERE '
                                   'item_id=? AND observation_id=? LIMIT 1',
                                   (item_id, source['id'])).fetchone() is not None):
                    results.append({'candidate_id': item['id'], 'item_id': item_id,
                                    'status': 'stale_capture'})
                    continue
                if pending and pending['source_revision'] == source['revision']:
                    pending_observation = pending['candidate']['evidence'][0]['observation_id']
                    if pending_observation == source['id']:
                        results.append({'candidate_id': item['id'], 'item_id': item_id,
                                        'status': 'pending'})
                        continue
                    exists = db.execute('SELECT 1 FROM discovery_reconciliation_events '
                                        'WHERE item_id=? AND observation_id=? AND '
                                        "event='same_revision_conflict'",
                                        (item_id, source['id'])).fetchone()
                    if not exists:
                        self._mark_uncertain(item_id, old)
                        self._event(db, item_id, source['id'], 'same_revision_conflict', claims)
                    results.append({'candidate_id': item['id'], 'item_id': item_id,
                                    'status': 'source_revision_conflict'})
                    continue
                if link['observation_id'] == source['id']:
                    require(link['source_revision'] == source['revision'],
                            'Reused observation ID')
                    results.append({'candidate_id': item['id'], 'item_id': item_id,
                                    'status': 'pending' if link['pending'] else 'unchanged'})
                    continue
                if link['source_revision'] == source['revision']:
                    exists = db.execute('SELECT 1 FROM discovery_reconciliation_events '
                                        'WHERE item_id=? AND observation_id=? AND '
                                        "event='same_revision_conflict'",
                                        (item_id, source['id'])).fetchone()
                    if not exists:
                        self._mark_uncertain(item_id, old)
                        self._event(db, item_id, source['id'], 'same_revision_conflict', claims)
                    results.append({'candidate_id': item['id'], 'item_id': item_id,
                                    'status': 'source_revision_conflict'})
                    continue
                changed = {field: {'previous': link['claims'].get(field), 'proposed': value}
                           for field, value in claims.items()
                           if (value is not None or
                               (field == 'due_at_ms' and
                                extraction['coverage'] == 'complete' and
                                extraction['processing_complete'])) and
                           value != link['claims'].get(field)}
                if item['title'] != old['payload']['title']:
                    changed['title'] = {'previous': old['payload']['title'],
                                        'proposed': item['title']}
                if link['pending'] is not None:
                    if (extraction['coverage'] != 'complete' or
                            not extraction['processing_complete']):
                        # An incomplete later read cannot supersede any earlier
                        # pending claim, even when it reports some concrete facts.
                        self._event(db, item_id, source['id'], 'incomplete_revision',
                                    {'source_revision': source['revision'],
                                     'claims': claims})
                        results.append({'candidate_id': item['id'], 'item_id': item_id,
                                        'status': 'pending'})
                        continue
                    pending = {'candidate': item, 'claims': claims,
                               'source_revision': source['revision'], 'changes': changed}
                    db.execute('UPDATE discovery_reconciliation_links SET pending=? WHERE item_id=?',
                               (encode(pending), item_id))
                    self._event(db, item_id, source['id'], 'additional_revision',
                                {'source_revision': source['revision'], 'claims': claims,
                                 'changes': changed})
                    results.append({'candidate_id': item['id'], 'item_id': item_id,
                                    'status': 'pending'})
                    continue
                if changed:
                    pending = {'candidate': item, 'claims': claims,
                               'source_revision': source['revision'], 'changes': changed}
                    db.execute('UPDATE discovery_reconciliation_links SET pending=? WHERE item_id=?',
                               (encode(pending), item_id))
                    if old['payload']['state'] not in ('completed', 'dismissed'):
                        current = old['payload']
                        updated = {**current, 'state': 'needs_clarification',
                                   'ambiguity': 'Source claims changed; explicit confirmation required.',
                                   'revision': current['revision'] + 1,
                                   'supersedes_revision': current['revision']}
                        self.store.save('ActionableItem', updated,
                                        expected_revision=old['revision'])
                    self._event(db, item_id, source['id'], 'conflict', changed)
                    status = 'pending'
                else:
                    # A new capture can corroborate facts. Unknown/omitted fields
                    # never clear a previous claim, even for complete coverage.
                    merged = {**link['claims'], **{k: v for k, v in claims.items()
                                                  if v is not None}}
                    db.execute('UPDATE discovery_reconciliation_links SET '
                               'observation_id=?,source_revision=?,claims=? WHERE item_id=?',
                               (source['id'], source['revision'], encode(merged), item_id))
                    self._event(db, item_id, source['id'], 'corroborated', merged)
                    status = 'unchanged'
                results.append({'candidate_id': item['id'], 'item_id': item_id,
                                'status': status})
        return results

    def confirm(self, item_id: str, *, expected_revision: int,
                source_revision: str, resolution: str = 'accept_source') -> dict:
        """Explicitly resolve a candidate/change with CAS; preserve overrides.

        Confirmation is a local obligation decision, never an approval for an
        external action. A caller must separately establish the user's intent.
        """
        integer(expected_revision, 'expected_revision', 1)
        if resolution not in ('accept_source', 'keep_current', 'keep_separate'):
            raise ValueError('Invalid resolution')
        with self.store.assistant.transaction() as db:
            link = self.get(item_id)
            current = self.store.get('ActionableItem', item_id)
            if link is None or current is None:
                raise ValueError('Unknown reconciliation item')
            if current['revision'] != expected_revision:
                raise RevisionConflict('Discovery record changed')
            pending = link['pending']
            expected_source = pending['source_revision'] if pending else link['source_revision']
            if source_revision != expected_source:
                raise RevisionConflict('Source revision changed')
            if pending:
                pending_observation = pending['candidate']['evidence'][0]['observation_id']
                observed = self.store.get('SourceObservation', pending_observation)
                require(observed is not None and
                        observed['payload']['revision'] == pending['source_revision'],
                        'Corrupt pending source observation')
            ambiguous = db.execute('SELECT 1 FROM discovery_reconciliation_events '
                                   "WHERE item_id=? AND event='ambiguous_match' LIMIT 1",
                                   (item_id,)).fetchone() is not None
            if ambiguous and resolution != 'keep_separate':
                raise ValueError('Ambiguous match needs explicit separate-item confirmation')
            if not ambiguous and resolution == 'keep_separate':
                raise ValueError('Separate-item confirmation requires ambiguous match')
            if current['payload']['state'] in ('completed', 'dismissed'):
                raise ValueError('Terminal obligation needs separate reopening')
            if pending is None and resolution != 'keep_current':
                latest = db.execute('SELECT event FROM discovery_reconciliation_events '
                                    'WHERE item_id=? ORDER BY id DESC LIMIT 1',
                                    (item_id,)).fetchone()
                if latest and latest['event'] == 'same_revision_conflict':
                    raise ValueError('Conflicting source revision needs keep-current confirmation')
            value = current['payload']
            claims = link['claims']
            observation_id = link['observation_id']
            if pending:
                observation_id = pending['candidate']['evidence'][0]['observation_id']
                if resolution == 'accept_source':
                    claims = {**claims, **{k: v for k, v in pending['claims'].items()
                                          if v is not None}}
                    if {'due_at_ms', 'due_timezone'} & pending['changes'].keys():
                        claims['due_at_ms'] = pending['claims']['due_at_ms']
                        claims['due_timezone'] = pending['claims']['due_timezone']
                    candidate = pending['candidate']
                    value = {**value, 'title': candidate['title'],
                             'evidence': candidate['evidence']}
                    if {'due_at_ms', 'due_timezone'} & pending['changes'].keys():
                        value['due_at_ms'] = pending['claims']['due_at_ms']
                        value['due_timezone'] = pending['claims']['due_timezone']
                # Keep the user's explicit override as a separate local layer.
            value = {**value, 'state': 'tracked', 'ambiguity': None}
            if any(value[k] != current['payload'][k] for k in
                   ('title', 'due_at_ms', 'due_timezone', 'ambiguity', 'evidence')):
                value = {**value, 'revision': current['payload']['revision'] + 1,
                         'supersedes_revision': current['payload']['revision']}
            saved = self.store.save('ActionableItem', value,
                                    expected_revision=current['revision'])
            if pending:
                db.execute('UPDATE discovery_reconciliation_links SET observation_id=?, '
                           'source_revision=?,claims=?,pending=NULL WHERE item_id=?',
                           (observation_id, source_revision, encode(claims), item_id))
            self._event(db, item_id, observation_id, 'confirmed',
                        {'resolution': resolution, 'source_revision': source_revision})
            return saved

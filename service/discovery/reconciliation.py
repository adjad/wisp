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


def _row(row) -> dict:
    return {'item_id': row['item_id'], 'source_kind': row['source_kind'],
            'source_record_id': row['source_record_id'], 'source_url': row['source_url'],
            'observation_id': row['observation_id'], 'source_revision': row['source_revision'],
            'claims': json.loads(row['claims']),
            'pending': json.loads(row['pending']) if row['pending'] else None}


class Reconciler:
    def __init__(self, store: DiscoveryStore):
        self.store = store

    def get(self, item_id: str) -> dict | None:
        with self.store.assistant.transaction(write=False) as db:
            row = db.execute('SELECT * FROM discovery_reconciliation_links WHERE item_id=?',
                             (item_id,)).fetchone()
            return _row(row) if row else None

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
        entries = []
        seen = set()
        headings = list(re.finditer(
            r'(?im)^[ \t]*(?:assignment|homework|exam|quiz|scheduling|follow[ -]up)[ \t]*:',
            source['text']))
        for raw in extraction['items']:
            item = validate('ActionableItem', raw)
            require(item['id'] not in seen and item['revision'] == 1 and
                    item['supersedes_revision'] is None and
                    item['state'] == 'needs_clarification' and
                    item['completion_receipt_id'] is None and
                    not item['external_record_ids'], 'Invalid extracted candidate')
            seen.add(item['id'])
            evidence = {e['id']: e for e in item['evidence']}
            require(any(item['title'] in e['quote'] for e in evidence.values()),
                    'Ungrounded candidate title')
            for entry in evidence.values():
                validate('Evidence', entry)
                require(entry['observation_id'] == source['id'] and
                        entry['source_revision'] == source['revision'] and
                        entry['captured_at_ms'] == source['observed_at_ms'] and
                        entry['quote'] in source['text'], 'Candidate evidence mismatch')
            if item['due_at_ms'] is not None:
                facts = extraction.get('temporal_facts')
                require(type(facts) is list and any(
                    type(f) is dict and f.get('role') == 'due' and
                    type(f.get('due_instant')) is str and
                    int(datetime.fromisoformat(f['due_instant']).timestamp() * 1000)
                    == item['due_at_ms'] for f in facts), 'Ungrounded due instant')
            claims = {'due_at_ms': item['due_at_ms'],
                      'due_timezone': item['due_timezone']}
            extra = provided.get(item['id'], {})
            require(set(extra) <= {'location', 'requirements'}, 'Unsupported fact claim')
            title_at = source['text'].find(item['title'])
            preceding = [heading for heading in headings if heading.start() <= title_at]
            if extra and (title_at < 0 or (len(extraction['items']) > 1 and not preceding)):
                raise ContractViolation('Unscoped fact claim')
            block_start = preceding[-1].start() if preceding else 0
            block_end = next((heading.start() for heading in headings
                              if heading.start() > title_at), len(source['text']))
            block = source['text'][block_start:block_end]
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
                    require(known['observation_id'] == source['id'] and
                            known['source_revision'] == source['revision'],
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

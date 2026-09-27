"""Synthetic A08 integration tests; no live model, browser, or user state."""
import asyncio
import json

from service.discovery.extraction import extract_observation
from service.discovery.local_model import extract_observation_local


def observation(text, **changes):
    return {'schema_version': '1.0', 'id': 'obs.a08', 'source_kind': 'browser',
            'source_url': 'https://course.invalid/a08', 'source_record_id': 'a08',
            'revision': 'source.r7', 'observed_at_ms': 1790352000000,
            'title': 'Synthetic source', 'text': text, 'private_context': False,
            **changes}


def codes(result):
    return {issue['code'] for issue in result['clarifications']}


def test_exact_due_is_attributed_and_distinct_from_other_temporal_roles():
    source = observation('Scheduling: Meet team\nAvailable: 2026-10-01 09:00 UTC\n'
                         'Event: 2026-10-02 09:00 UTC\nEstimate: 30 minutes\n'
                         'Due: 2026-10-03 17:00 UTC\n')
    result = extract_observation(source)
    assert [f['role'] for f in result['temporal_facts']] == [
        'availability', 'event', 'estimate', 'due']
    assert result['temporal_facts'][2]['estimated_minutes'] == 30
    assert all(f['due_instant'] is None for f in result['temporal_facts'][:3])
    assert result['items'][0]['due_at_ms'] == 1791046800000
    assert result['items'][0]['due_timezone'] == 'UTC'
    for fact in result['temporal_facts']:
        for mention in fact['mentions']:
            evidence = mention['evidence']
            assert evidence['observation_id'] == source['id']
            assert evidence['source_revision'] == source['revision']
            assert evidence['quote'] == source['text'][mention['start']:mention['end']]


def test_iso_seconds_and_relative_time_resolve_from_source_and_capture():
    iso = extract_observation(observation(
        'Assignment: Report\nDue: 2026-10-02T17:00:00Z\n'))
    assert iso['items'][0]['due_at_ms'] == 1790960400000
    relative = extract_observation(observation(
        'Assignment: Report\nDue: tomorrow at 5pm UTC\n'))
    assert relative['items'][0]['due_at_ms'] == 1790442000000
    assert relative['temporal_facts'][0]['mentions'][0]['quote'] == 'tomorrow at 5pm UTC'


def test_unknown_timezone_and_competing_deadlines_do_not_choose_a_due():
    unknown = extract_observation(observation(
        'Assignment: Report\nDue: 2026-10-02 17:00\n'))
    assert unknown['items'][0]['due_at_ms'] is None
    assert 'unresolved_temporal_facts' in codes(unknown)
    conflict = extract_observation(observation(
        'Assignment: Report\nDue: 2026-10-02 17:00 UTC\n'
        'Update: now due 2026-10-03 17:00 UTC\n'))
    assert conflict['items'][0]['due_at_ms'] is None
    assert 'conflicting_temporal_facts' in codes(conflict)
    assert not conflict['processing_complete']
    assert len(conflict['temporal_facts']) == 2


def test_unknown_revision_and_dst_fold_do_not_choose_a_due():
    revised = extract_observation(observation(
        'Assignment: Report\nDue: 2026-10-02 17:00 UTC\n'
        'Update: moved to 2026-10-03; details pending.\n'))
    assert revised['items'][0]['due_at_ms'] is None
    assert 'possible_deadline_revision' in codes(revised)
    folded = extract_observation(observation(
        'Assignment: Report\nDue: 2026-11-01 01:30 America/Los_Angeles\n'))
    assert folded['items'][0]['due_at_ms'] is None
    assert folded['temporal_facts'][0]['mentions'][0]['status'] == 'partial'


def test_out_of_contract_historic_timestamp_stays_uncertain():
    result = extract_observation(observation(
        'Assignment: Historic\nDue: 1900-01-01 12:00 UTC\n'))
    assert result['items'][0]['due_at_ms'] is None
    assert 'unrepresentable_due_at' in codes(result)


def test_two_labeled_blocks_attach_their_own_due_times():
    result = extract_observation(observation(
        'Assignment: One\nDue: 2026-10-02 17:00 UTC\n'
        'Assignment: Two\nDue: 2026-10-03 18:00 UTC\n'))
    assert [item['due_at_ms'] for item in result['items']] == [
        1790960400000, 1791050400000]


def test_due_line_before_only_labeled_item_is_not_attached():
    result = extract_observation(observation(
        'Due: 2026-10-02 17:00 UTC\nAssignment: Report\n'))
    assert result['items'][0]['due_at_ms'] is None
    assert result['temporal_facts'][0]['role'] == 'due'


def test_no_date_cancellation_suppresses_prior_due_claim():
    for update in ('Update: assignment cancelled.',
                   'Actually no submission required.',
                   'These instructions are obsolete.'):
        result = extract_observation(observation(
            'Assignment: Report\nDue: 2026-10-02 17:00 UTC\n' + update))
        assert result['items'][0]['due_at_ms'] is None
        assert 'possible_deadline_revision' in codes(result)
        assert not result['processing_complete']


class FakeLocalClient:
    managed = True
    base_url = 'http://127.0.0.1:8000'
    model = 'synthetic-local'

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def chat(self, model, messages, **options):
        self.calls.append((model, messages, options))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def response(content, finish_reason='stop'):
    return {'choices': [{'finish_reason': finish_reason,
                         'message': {'content': content}}]}


def test_local_model_uses_closed_schema_and_grounded_spans():
    text = 'Please write the report.'
    title = 'write the report'
    start = text.index(title)
    content = json.dumps({'candidates': [{'kind': 'assignment',
        'title': {'start': start, 'end': start + len(title), 'quote': title},
        'evidence': [{'start': 0, 'end': len(text), 'quote': text}]}]})
    client = FakeLocalClient(response(content))
    result = asyncio.run(extract_observation_local(observation(text), client=client))
    assert [item['title'] for item in result['items']] == [title]
    assert result['items'][0]['state'] == 'needs_clarification'
    assert result['items'][0]['completion_receipt_id'] is None
    options = client.calls[0][2]
    assert options['response_format']['type'] == 'json_schema'
    schema = options['response_format']['json_schema']['schema']
    assert schema['additionalProperties'] is False
    assert client.calls[0][1][1]['content'] == text


def test_bad_local_model_output_recovers_labeled_candidate_without_leaking_content():
    source = observation('Assignment: Write report\nDue: 2026-10-02 17:00 UTC\n')
    for malformed in (response('PRIVATE BAD OUTPUT'),
                      response('{"candidates":[]}', finish_reason='length'),
                      RuntimeError('PRIVATE MODEL ERROR')):
        client = FakeLocalClient(malformed)
        result = asyncio.run(extract_observation_local(source, client=client))
        assert [item['title'] for item in result['items']] == ['Write report']
        assert 'invalid_model_output' in codes(result)
        assert not result['processing_complete']
        assert 'PRIVATE' not in json.dumps(result)


def test_remote_client_and_invalid_capture_never_invoke_model():
    remote = FakeLocalClient(response('{"candidates":[]}'))
    remote.base_url = 'https://provider.invalid'
    result = asyncio.run(extract_observation_local(
        observation('Assignment: Report'), client=remote))
    assert not remote.calls
    assert 'local_model_required' in codes(result)
    local = FakeLocalClient(response('{"candidates":[]}'))
    invalid = asyncio.run(extract_observation_local(
        observation('secret', private_context=True), client=local))
    assert not local.calls
    assert codes(invalid) == {'invalid_observation'}


def test_large_capture_does_not_silently_truncate_model_input():
    client = FakeLocalClient(response('{"candidates":[]}'))
    source = observation('Assignment: Report\n' + 'x' * 5000)
    result = asyncio.run(extract_observation_local(source, client=client))
    assert not client.calls
    assert 'model_input_limit' in codes(result)
    assert not result['processing_complete']

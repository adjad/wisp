"""A08 pure synthetic captures only; no model, native app, network or user state."""
import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from service.browser.contracts import ContractViolation, validate
from service.discovery.extraction import (
    MAX_CANDIDATES, MAX_FACTS, MAX_OBSERVATIONS, MAX_QUOTE,
    build_model_request, extract_observation, extract_observations,
)

CASES = json.loads((Path(__file__).resolve().parents[1] /
                   'test_fixtures/discovery/extraction/cases.json').read_text())


def observation(text='Assignment: Write report\nDue: Friday at 17:00\n', **changes):
    return {'schema_version': '1.0', 'id': 'obs.synthetic', 'source_kind': 'browser',
            'source_url': 'https://course.invalid/assignment', 'source_record_id': 'assignment.1',
            'revision': 'source.r1', 'observed_at_ms': 1790352000000, 'title': 'Synthetic course',
            'text': text, 'private_context': False, **changes}


def span(text, quote, start=0):
    offset = text.index(quote, start)
    return {'start': offset, 'end': offset + len(quote), 'quote': quote}


def model_response(source):
    return {'candidates': [{'kind': 'assignment',
            'title': span(source['text'], 'Write report'),
            'evidence': [span(source['text'], source['text'])]}]}


def codes(result):
    return {issue['code'] for issue in result['clarifications']}


def assert_grounded(result, source):
    locations = {s['evidence_id']: s for s in result['spans']}
    evidence = [e for item in result['items'] for e in item['evidence']]
    evidence += [fact['evidence'] for fact in result['temporal_facts']]
    for entry in evidence:
        assert validate('Evidence', entry) == entry
        assert entry['observation_id'] == source['id']
        assert entry['source_revision'] == source['revision']
        assert entry['captured_at_ms'] == source['observed_at_ms']
        location = locations[entry['id']]
        assert entry['quote'] == source['text'][location['start']:location['end']]
    for item in result['items']:
        assert validate('ActionableItem', item) == item
        assert item['state'] == 'needs_clarification'
        assert item['ambiguity']
        assert item['due_at_ms'] is None and item['due_timezone'] is None
        assert item['completion_receipt_id'] is None and item['external_record_ids'] == []
        assert any(item['title'] in e['quote'] for e in item['evidence'])
        assert item['revision'] == 1 and item['supersedes_revision'] is None


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['name'])
def test_synthetic_extraction_cases(case):
    source = observation(case['text'])
    before = deepcopy(source)
    result = extract_observation(source)
    assert [i['kind'] for i in result['items']] == case['kinds']
    assert [i['title'] for i in result['items']] == case['titles']
    assert [f['role'] for f in result['temporal_facts']] == case['roles']
    assert all(f['resolution'] == 'unresolved' for f in result['temporal_facts'])
    assert result['processing_complete'] is True
    assert_grounded(result, source)
    assert source == before
    assert result == extract_observation(source)


@pytest.mark.parametrize('kind', ['browser', 'mail', 'calendar', 'reminder', 'manual'])
def test_supported_captured_sources_without_acquisition(kind):
    source = observation(source_kind=kind)
    result = extract_observation(source)
    assert len(result['items']) == 1
    assert_grounded(result, source)


@pytest.mark.parametrize('coverage', ['complete', 'partial', 'unknown'])
def test_coverage_survives_but_never_authorizes_tracking(coverage):
    source = observation('Assignment: Write report')
    result = extract_observation(source, coverage=coverage)
    assert result['coverage'] == coverage
    assert result['scope'] == 'captured_text_only'
    assert_grounded(result, source)
    if coverage != 'complete':
        assert coverage in result['items'][0]['ambiguity']
    empty = extract_observation(observation(''), coverage=coverage)
    assert not empty['items'] and 'unresolved_text' in codes(empty)


@pytest.mark.parametrize('changes', [
    {'private_context': True}, {'source_kind': 'messages'}, {'source_kind': 'pdf'},
    {'schema_version': '2.0'}, {'observed_at_ms': True}, {'observed_at_ms': -1},
    {'observed_at_ms': float('nan')}, {'revision': ''}, {'id': 'bad id'},
    {'source_url': None}, {'text': 12}, {'text': '\ud800'}, {'text': 'x' * 32769},
    {'unexpected': 'ignored?'}, {'title': ''},
])
def test_invalid_capture_rejected_without_reflecting_sensitive_text(changes):
    result = extract_observation({**observation('SECRET_PRIVATE_CAPTURE'), **changes})
    assert codes(result) == {'invalid_observation'}
    assert result['items'] == result['temporal_facts'] == result['spans'] == []
    assert 'SECRET_PRIVATE_CAPTURE' not in json.dumps(result)
    assert result['processing_complete'] is False


@pytest.mark.parametrize('value', [None, [], 'text', 5, {'text': 'private'}, False])
def test_malformed_top_level(value):
    assert codes(extract_observation(value)) == {'invalid_observation'}


@pytest.mark.parametrize('coverage', [None, [], True, 'entire_account', 'COMPLETE'])
def test_invalid_coverage(coverage):
    assert codes(extract_observation(observation(), coverage=coverage)) == {'invalid_coverage'}


def test_model_request_is_closed_data_and_isolated():
    source = observation()
    request = build_model_request(source)
    assert request['source'] == {'text': source['text']}
    assert request['output_schema']['additionalProperties'] is False
    assert set(request['output_schema']['properties']) == {'candidates'}
    request['output_schema']['properties'].clear()
    assert build_model_request(source)['output_schema']['properties']
    with pytest.raises(ContractViolation):
        build_model_request(observation(private_context=True))


def test_general_language_local_model_spans_remain_unverified():
    source = observation('Please write the report and send it when ready.')
    output = {'candidates': [{'kind': 'follow_up',
        'title': span(source['text'], 'write the report'),
        'evidence': [span(source['text'], source['text'])]}]}
    before = deepcopy(output)
    result = extract_observation(source, model_output=output)
    assert result['items'][0]['title'] == 'write the report'
    assert 'classification is unverified' in result['items'][0]['ambiguity']
    assert_grounded(result, source)
    assert output == before


@pytest.mark.parametrize('field,value', [
    ('state', 'completed'), ('due_at_ms', 1), ('due_timezone', 'UTC'),
    ('completion_receipt_id', 'verified'), ('approval', True), ('tool_calls', []),
    ('observation_id', 'forged'), ('source_revision', 'forged'),
])
def test_model_cannot_supply_authority_or_provenance(field, value):
    source = observation()
    output = model_response(source)
    output['candidates'][0][field] = value
    result = extract_observation(source, model_output=output)
    assert codes(result) == {'invalid_model_output'} and not result['items']


@pytest.mark.parametrize('field,value', [
    ('start', -1), ('start', True), ('start', 12.0), ('end', 999), ('end', 0),
    ('quote', 'Invented title'), ('quote', ''), ('quote', 'write report'),
])
def test_model_cannot_fabricate_or_normalize_spans(field, value):
    source = observation()
    output = model_response(source)
    output['candidates'][0]['title'][field] = value
    assert codes(extract_observation(source, model_output=output)) == {'invalid_model_output'}


def test_title_must_be_inside_supplied_evidence():
    source = observation()
    output = model_response(source)
    output['candidates'][0]['evidence'] = [span(source['text'], 'Due: Friday at 17:00')]
    assert codes(extract_observation(source, model_output=output)) == {'invalid_model_output'}


@pytest.mark.parametrize('output', [
    '', '{"candidates": []}', [], {'candidates': [], 'approved': True},
    {'candidates': {}}, {'candidates': [None]}, {'candidates': [{}]},
])
def test_invalid_model_output_is_recoverable(output):
    source = observation()
    assert codes(extract_observation(source, model_output=output)) == {'invalid_model_output'}
    assert extract_observation(source, model_output=model_response(source))['items']


def test_invalid_candidate_rejects_whole_model_response():
    source = observation()
    output = model_response(source)
    output['candidates'].append({'kind': 'assignment'})
    result = extract_observation(source, model_output=output)
    assert not result['items'] and not result['spans']


def test_model_omitted_competing_update_remains_visible_as_temporal_fact():
    source = observation('Assignment: Write report\nDue: Friday\nUpdate: now due Monday?\n')
    output = model_response(source)
    output['candidates'][0]['evidence'] = [span(source['text'], 'Assignment: Write report')]
    result = extract_observation(source, model_output=output)
    assert [f['evidence']['quote'] for f in result['temporal_facts']] == [
        'Due: Friday\n', 'Update: now due Monday?\n']
    assert_grounded(result, source)


def test_duplicate_proposals_and_evidence_are_idempotent():
    source = observation()
    output = model_response(source)
    output['candidates'][0]['evidence'] *= 2
    output['candidates'] *= 3
    result = extract_observation(source, model_output=output)
    assert len(result['items']) == 1 and len(result['items'][0]['evidence']) == 1
    assert result == extract_observation(source, model_output=output)


def test_same_text_at_distinct_offsets_retains_distinct_provenance():
    source = observation('Assignment: Write report\nAssignment: Write report\n')
    result = extract_observation(source)
    assert len(result['items']) == 2
    assert result['items'][0]['id'] != result['items'][1]['id']
    assert {e['id'] for e in result['items'][0]['evidence']} != {
        e['id'] for e in result['items'][1]['evidence']}
    assert_grounded(result, source)


def test_crlf_unicode_and_whitespace_not_normalized():
    source = observation('  Assignment: Résumé 🚀  \r\nDue: vendredi\r\n')
    result = extract_observation(source)
    assert result['items'][0]['title'] == 'Résumé 🚀  '
    assert result['items'][0]['evidence'][0]['quote'] == source['text']
    assert_grounded(result, source)


def test_revision_and_changed_capture_are_not_overwritten_or_auto_completed():
    old = observation()
    new = observation('Assignment: Write report\nDue: Monday\n', id='obs.new', revision='source.r2')
    empty = observation('', id='obs.partial', revision='source.r3')
    batch = extract_observations([old, deepcopy(old), new, empty], coverage='partial')
    assert len(batch['results']) == 3
    assert codes(batch) == {'competing_source_captures'}
    results = [e['extraction'] for e in batch['results']]
    assert results[0]['items'][0]['id'] != results[1]['items'][0]['id']
    assert not results[2]['items']
    for result, source in zip(results, [old, new, empty]):
        assert_grounded(result, source)
        assert 'competing_source_captures' in codes(result)


def test_reused_immutable_observation_id_rejects_entire_batch():
    batch = extract_observations([observation(), observation(revision='source.r2')])
    assert batch == {'results': [], 'clarifications': [
        {'code': 'invalid_batch', 'requires_clarification': True}]}


def test_source_kinds_and_records_are_not_fuzzy_merged():
    batch = extract_observations([observation(), observation(id='obs.mail', source_kind='mail'),
                                 observation(id='obs.other', source_record_id='other')])
    assert len(batch['results']) == 3 and not batch['clarifications']


@pytest.mark.parametrize('value', [None, {}, [None], [observation(private_context=True)]])
def test_invalid_batches(value):
    assert codes(extract_observations(value)) == {'invalid_batch'}


def test_observation_budget_rejects_batch_without_partial_success():
    assert codes(extract_observations([observation()] * (MAX_OBSERVATIONS + 1))) == {'invalid_batch'}
    assert len(extract_observations([observation()] * MAX_OBSERVATIONS)['results']) == 1


def test_candidate_limit_is_explicit_and_deterministic():
    source = observation(''.join(f'Assignment: Task {i}\n' for i in range(MAX_CANDIDATES + 1)))
    result = extract_observation(source)
    assert len(result['items']) == MAX_CANDIDATES
    assert 'extraction_limit' in codes(result) and not result['processing_complete']
    assert 'limits' in result['items'][0]['ambiguity']
    assert result == extract_observation(source)
    output = model_response(observation())
    output['candidates'] *= MAX_CANDIDATES + 1
    assert codes(extract_observation(observation(), model_output=output)) == {'invalid_model_output'}


def test_temporal_limit_preserves_uncertainty():
    source = observation('Assignment: Write report\n' + 'Due: Monday or Tuesday\n' * (MAX_FACTS + 1))
    result = extract_observation(source)
    assert len(result['temporal_facts']) == MAX_FACTS
    assert 'extraction_limit' in codes(result) and not result['processing_complete']
    assert_grounded(result, source)


def test_oversize_block_is_not_silently_clipped():
    source = observation('Assignment: Write report\n' + 'x' * MAX_QUOTE)
    result = extract_observation(source)
    assert not result['items'] and 'extraction_limit' in codes(result)
    assert not result['processing_complete']


def test_model_byte_budget_and_evidence_limit():
    source = observation('Assignment: Write report\n' + 'x' * 4000)
    output = model_response(source)
    output['candidates'] *= 9
    assert codes(extract_observation(source, model_output=output)) == {'invalid_model_output'}
    output = model_response(source)
    output['candidates'][0]['evidence'] *= 9
    assert codes(extract_observation(source, model_output=output)) == {'invalid_model_output'}


def test_results_do_not_alias_input_or_other_calls():
    source = observation()
    result = extract_observation(source)
    result['items'][0]['evidence'][0]['quote'] = 'changed'
    assert extract_observation(source)['items'][0]['evidence'][0]['quote'] == source['text']


def test_pure_module_has_no_effect_or_inference_dependencies():
    # Protect the A08 architecture boundary against accidental runtime wiring.
    path = Path(__file__).resolve().parents[1] / 'service/discovery/extraction.py'
    tree = ast.parse(path.read_text())
    allowed = {'__future__', 'copy', 'hashlib', 'json', 're', 'service.browser.contracts'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {a.name for a in node.names} <= allowed
        if isinstance(node, ast.ImportFrom):
            assert node.module in allowed
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {'open', 'eval', 'exec', '__import__', 'compile'}


@pytest.mark.parametrize('qualifier', [
    'Correction: this assignment is cancelled.',
    'Do not submit this assignment.',
    'Optional practice only; no submission required.',
    'Update: the instructions above are obsolete.',
])
def test_model_cannot_drop_cancellation_or_qualifiers(qualifier):
    source = observation('Assignment: Write report\n' + qualifier)
    output = model_response(source)
    output['candidates'][0]['evidence'] = [span(source['text'], 'Assignment: Write report')]
    result = extract_observation(source, model_output=output)
    assert any(qualifier in e['quote'] for e in result['items'][0]['evidence'])
    assert_grounded(result, source)


def test_context_before_heading_is_retained():
    source = observation('All following tasks are optional.\nAssignment: Write report\n')
    result = extract_observation(source)
    assert any(e['quote'] == source['text'] for e in result['items'][0]['evidence'])


def test_full_context_is_bounded_exact_chunks_without_clipping():
    text = 'Assignment: Write report\n' + 'x' * 16000 + '\nCancelled.'
    source = observation(text)
    output = model_response(observation())
    output['candidates'][0]['evidence'] = [span(text, 'Assignment: Write report')]
    result = extract_observation(source, model_output=output)
    item = result['items'][0]
    locations = {s['evidence_id']: s for s in result['spans']}
    context = [e for e in item['evidence'] if locations[e['id']]['start'] % MAX_QUOTE == 0
               and locations[e['id']]['end'] == min(locations[e['id']]['start'] + MAX_QUOTE, len(text))]
    assert ''.join(e['quote'] for e in context) == text
    assert all(len(e['quote']) <= MAX_QUOTE for e in item['evidence'])
    assert_grounded(result, source)


@pytest.mark.parametrize('text', [
    'Aſsignment: Foo\n', 'Assıgnment: Foo\n', 'Assignment: Foo\nEstımate: 30 minutes\n',
])
def test_unicode_casefold_lookalikes_cannot_crash_or_become_trusted_labels(text):
    result = extract_observation(observation(text))
    assert result['processing_complete']
    if text.startswith('Assignment:'):
        assert result['temporal_facts'][0]['role'] == 'unknown'
    else:
        assert not result['items'] and 'unresolved_text' in codes(result)


def test_model_title_schema_matches_the_wire_bound():
    schema = build_model_request(observation())['output_schema']
    title = schema['properties']['candidates']['items']['properties']['title']
    assert title['properties']['quote']['maxLength'] == 512

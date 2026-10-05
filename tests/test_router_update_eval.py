"""Scoring and isolated subprocess checks; never read the sealed prompt contents."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/eval_router_update.py'
spec = importlib.util.spec_from_file_location('router_update_eval', SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evaluation
spec.loader.exec_module(evaluation)


def case(**expected):
    return {'expected': {'calls': [], 'sources': [], **expected}}


def run(calls=(), **kwargs):
    return {'executed_calls': list(calls), 'events': [{'type': 'done'}], 'answer': '', **kwargs}


def test_exact_arguments_do_not_accept_substrings_wrong_types_or_additional_scope():
    expected = case(calls=[{'name': 'view_emails', 'args': {'query': 'AB-42', 'count': 3}}], sources=['email'])
    for args in ({'query': 'prefix AB-42 suffix', 'count': 3}, {'query': 'AB-42', 'count': '3'},
                 {'query': 'AB-42', 'count': 3, 'period': 'today'}):
        assert not evaluation.score(expected, run([{'name': 'view_emails', 'args': args}]))['metrics']['exact_arguments']


def test_defaults_are_normalized_only_when_runtime_default_matches_type():
    schemas = {'view_emails': {'properties': {'count': {'default': 5}}}}
    assert evaluation.canonical_args('view_emails', {'count': 5}, schemas) == {}
    assert evaluation.canonical_args('view_emails', {'count': '5'}, schemas) == {'count': '5'}
    assert evaluation.canonical_args('view_emails', {'count': False}, schemas) == {'count': False}


def test_fact_scoring_requires_exact_labeled_value():
    expected = case(answer_facts={'Count': '3'})
    for answer in ['There are 3 somewhere in this sample.', 'Count: 30', 'Count: 3 additional', 'Count: 30\nCount: 3', 'Count: 3\nCount: 3']:
        assert evaluation.score(expected, run(answer=answer))['metrics']['answer_facts'] is False
    assert evaluation.score(expected, run(answer='Count: 3'))['metrics']['answer_facts'] is True


def test_literal_tokens_and_excluded_sources_are_not_substring_matches():
    expected = case(answer_literals=['AB-42'], excluded_sources=['email'])
    result = evaluation.score(expected, run([{'name': 'view_emails', 'args': {}}], answer='Reference: AB-420'))
    assert result['metrics']['literal_fidelity'] is False
    assert result['metrics']['excluded_source'] is False


def test_default_calendar_tool_reads_reminders_and_calendar_only_does_not():
    expected = case(sources=['calendar'], calls=[{'name': 'get_upcoming', 'args': {'calendar_only': True}}])
    assert not evaluation.score(expected, run([{'name': 'get_upcoming', 'args': {}}]))['metrics']['source']
    assert evaluation.score(expected, run([{'name': 'get_upcoming', 'args': {'calendar_only': True}}]))['metrics']['source']


def test_duplicate_recovery_and_failure_receipts_affect_end_to_end():
    call = {'name': 'summarize_emails', 'args': {}}
    expected = case(calls=[call], sources=['email'])
    events = [{'type': 'tool_result', 'id': 'a', 'result': '(error: fixture unavailable)'}, {'type': 'done'}]
    result = evaluation.score(expected, run([call, call], events=events, answer='Everything is fine.'))
    assert not result['metrics']['duplicate']
    assert not result['metrics']['failure_honesty']
    assert not result['metrics']['end_to_end']
    assert evaluation.score(expected, run([call], events=events, answer='Email was unavailable.'))['metrics']['failure_honesty']


def test_denominators_preserve_not_applicable_and_zero_values():
    rows = [{'score': {'metrics': {'source': True, 'menu': None}}}, {'score': {'metrics': {'source': False, 'menu': False}}}]
    assert evaluation.aggregate(rows) == {'menu': {'numerator': 0, 'denominator': 1}, 'source': {'numerator': 1, 'denominator': 2}}


def test_seal_metadata_family_splits_and_dev_integrity():
    seal = json.loads((evaluation.DATA / 'seal.json').read_text())
    assert seal['splits']['test']['count'] == 320
    assert seal['splits']['test']['families'] == 40
    dev = evaluation.read_cases('dev')
    assert len(dev) == 80 and len({c['family'] for c in dev}) == 10
    # Only hash test bytes; do not parse or output any heldout prompt.
    assert evaluation.digest_bytes((evaluation.DATA / 'test.jsonl').read_bytes()) == seal['splits']['test']['sha256']


def test_cli_refuses_unfrozen_heldout_and_missing_candidate(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), '--split', 'test', '--output', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0 and 'sealed test requires' in result.stderr


def test_production_endpoint_uses_fixtures_not_original_callable(tmp_path):
    code = r'''
import asyncio, json, pathlib, tempfile
from scripts import eval_router_update as e
state = pathlib.Path(tempfile.mkdtemp(prefix='wisp-isolation-test-'))
e.install_guard(state, state)
from service.tools import registry
from service import main
original = registry.REGISTRY['summarize_messages']
def forbidden(**kwargs):
    raise AssertionError('production callable ran')
original.func = forbidden
case = {'id': 'fixture-success', 'prompt': 'summarize my messages', 'clock': '2026-10-05T12:00:00-07:00',
        'expected': {'calls': [{'name':'summarize_messages','args':{}}], 'sources':['messages']},
        'fixture_answer': 'Verified synthetic digest.'}
result = asyncio.run(e.run_case(case, state))
assert result['answer'] == 'Verified synthetic digest.', result['events']
assert result['executed_calls'] == [{'name': 'summarize_messages', 'args': {}, 'source': 'messages',
                                    'fixture': True, 'execution_stage': 'service/agent/loop.py'}]
assert not result['raw_model_io'], 'digest unnecessarily invoked fake model'
assert pathlib.Path.home() == state
assert main.store is not None
print(json.dumps({'ok': True}))
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ok']


@pytest.mark.parametrize('action', [
    "__import__('socket').create_connection(('127.0.0.1', 1))",
    "__import__('subprocess').run(['true'])",
    "__import__('os').system('true')",
    "pathlib.Path('/private/tmp/wisp-eval-forbidden-outside').write_text('no')",
    "__import__('sqlite3').connect('/private/tmp/wisp-eval-forbidden-outside.db')",
])
def test_guard_denies_network_process_and_external_writes(action):
    code = f'''
import pathlib, tempfile
from scripts import eval_router_update as e
state = pathlib.Path(tempfile.mkdtemp(prefix='wisp-guard-test-'))
e.install_guard(state, state)
try:
    {action}
except PermissionError:
    print('DENIED')
else:
    raise AssertionError('guard accepted forbidden effect')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'DENIED'


def test_context_list_of_lists_is_rejected_without_dropping_history():
    import asyncio
    bad = {'context': [['user', 'prior request'], ['assistant', 'prior response']]}
    with pytest.raises(ValueError, match='context must be explicit'):
        asyncio.run(evaluation.run_case(bad, Path('/unused')))


def test_runtime_date_equivalence_preserves_dst_boundaries_and_scope():
    clock = '2026-03-08T12:00:00-07:00'
    day = evaluation.canonical_args('view_emails', {'day': '2026-03-08'}, {}, clock=clock)
    period = evaluation.canonical_args('view_emails', {'period': 'today'}, {}, clock=clock)
    assert day == period
    start, end = day['_resolved_span']
    assert end - start == 23 * 3600
    assert day != evaluation.canonical_args('view_emails', {'period': 'this week'}, {}, clock=clock)


def test_injected_inference_is_disabled_before_any_client_operation():
    import asyncio
    class Never:
        async def status(self):
            raise AssertionError('disabled adapter touched inference')
    grant = evaluation.InferenceGrant('Ling-fixture', 'fixture-revision', 'a' * 64, 'synthetic-receipt', ('127.0.0.1', 1))
    adapter = evaluation.ResidentInferenceAdapter(Never(), grant)
    with pytest.raises(PermissionError, match='disabled'):
        asyncio.run(adapter.status())
    with pytest.raises(PermissionError, match='disabled'):
        asyncio.run(adapter.chat('Ling-fixture', []))


def test_injected_adapter_never_loads_wrong_or_nonresident_model():
    import asyncio
    class Fake:
        async def status(self):
            return {'models': [{'id': 'Ling-fixture', 'loaded': False}]}
        async def ensure_only(self, *args, **kwargs):
            raise AssertionError('adapter called model load')
    grant = evaluation.InferenceGrant('Ling-fixture', 'fixture-revision', 'a' * 64, 'synthetic-receipt', ('127.0.0.1', 1), enabled=True)
    adapter = evaluation.ResidentInferenceAdapter(Fake(), grant)
    with pytest.raises(PermissionError, match='not already resident'):
        asyncio.run(adapter.ensure_only('Ling-fixture'))
    with pytest.raises(PermissionError, match='differs'):
        asyncio.run(adapter.ensure_only('Other-model'))


def test_matching_gold_arguments_cannot_reward_runtime_ignored_filters():
    call = {'name': 'summarize_emails', 'args': {'unread': True, 'period': 'last week', 'count': 3}}
    expected = case(calls=[call], sources=['email'])
    result = evaluation.score(expected, run([call]))
    assert result['metrics']['exact_arguments']
    assert not result['metrics']['runtime_constraints']
    assert not result['metrics']['end_to_end']
    assert result['runtime_constraint_issues'] == ['email-unread-count-clamped-to-at-least-50', 'email-unread-ignores-date-scope']


def test_scripted_general_agenda_preserves_both_source_domains():
    row = case(calls=[{'name': 'get_upcoming', 'args': {'period': 'next week'}}], sources=['calendar', 'reminders'])
    intent = evaluation.scripted_intent(row)
    assert [source['domain'] for source in intent['sources']] == ['calendar', 'reminders']
    assert intent['sources'][0]['time'] == intent['sources'][1]['time']


@pytest.mark.skipif(not (ROOT / 'service/router/intent/planner.py').exists(), reason='Requires integrated core candidate; base control has no intent API')
@pytest.mark.parametrize('clock', ['2026-03-08T12:00:00-07:00', '2026-12-31T23:30:00-08:00'])
def test_integrated_candidate_planner_sees_frozen_clock(clock):
    code = f"""
import asyncio, pathlib, tempfile
from scripts import eval_router_update as e
state = pathlib.Path(tempfile.mkdtemp(prefix='wisp-candidate-clock-'))
e.install_guard(state, state)
row = {{'id':'clock', 'prompt':'Find notes about clock-fixture and show four results.', 'clock':{clock!r},
       'expected':{{'calls':[{{'name':'search_notes','args':{{'query':'clock-fixture','count':4}}}}], 'sources':['notes']}},
       'fixture_answer':'Synthetic clock result.'}}
result = asyncio.run(e.run_case(row, state, candidate=True))
planner = [r for r in result['raw_model_io'] if r['kind']=='scripted_intent']
assert planner, result['events']
assert 'Local clock: ' + {clock!r} in planner[0]['messages'][0]['content']
print('FROZEN')
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'FROZEN'


def test_workflow_renders_fixture_calendar_and_denies_synthetic_delivery():
    code = r'''
import asyncio, pathlib, tempfile
from scripts import eval_router_update as e
state = pathlib.Path(tempfile.mkdtemp(prefix='wisp-workflow-render-'))
e.install_guard(state, state)
raw = ('Today is Monday, October 5, 2026. Upcoming (tomorrow, 1 item(s)) — each row is tagged relative to today:\n'
       'Calendar events: 1; Wisp/Apple reminders: 0. A calendar event alone is not a reminder.\n'
       '- TOMORROW (Tue Oct 6) 9:00 AM (in 1 d) meeting: Fixture Review [Calendar event]')
row = {'id':'workflow-render', 'prompt':'text +15550100101 a summary of my calendar tomorrow',
       'clock':'2026-10-05T12:00:00-07:00', 'approve':False,
       'expected':{'calls':[], 'sources':[]}, 'tool_results':{'get_upcoming':[raw]}}
result = asyncio.run(e.run_case(row, state))
assert any(event['type']=='workflow' for event in result['events']), result['events']
confirm = next(event for event in result['events'] if event['type']=='confirm')
assert 'Fixture Review' in confirm['args']['text'], confirm
assert 'each row is tagged' not in confirm['args']['text'], confirm
assert all(call['name']!='send_message' for call in result['executed_calls'])
assert result['executed_calls'][0]['execution_stage']=='service/workflows/executor.py'
print('RENDERED_SYNTHETIC')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'RENDERED_SYNTHETIC'

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



@pytest.mark.parametrize('host_timezone', ['UTC', 'America/Los_Angeles'])
@pytest.mark.parametrize('clock,day,start,end,hours', [
    ('2026-03-08T12:00:00-07:00', '2026-03-08', '2026-03-08T00:00:00-08:00', '2026-03-09T00:00:00-07:00', 23),
    ('2026-11-01T12:00:00-08:00', '2026-11-01', '2026-11-01T00:00:00-07:00', '2026-11-02T00:00:00-08:00', 25),
])
def test_date_scoring_is_exact_across_host_timezones_and_both_dst_transitions(host_timezone, clock, day, start, end, hours):
    code = f"""
import os, pathlib, tempfile, time
from datetime import datetime
from scripts import eval_router_update as e
state = pathlib.Path(tempfile.mkdtemp(prefix='wisp-date-host-contract-'))
e.install_guard(state, state)
os.environ['TZ'] = {host_timezone!r}
time.tzset()
original = (os.environ['TZ'], time.localtime(datetime.fromisoformat({clock!r}).timestamp()))
expected_span = [datetime.fromisoformat({start!r}).timestamp(), datetime.fromisoformat({end!r}).timestamp()]
for name in ('view_emails', 'summarize_emails', 'view_messages', 'summarize_messages', 'search_notes'):
    exact_day = e.canonical_args(name, {{'day': {day!r}}}, {{}}, clock={clock!r})
    today = e.canonical_args(name, {{'period': 'today'}}, {{}}, clock={clock!r})
    assert exact_day == today == {{'_resolved_span': expected_span}}
    assert e.canonical_args(name, {{'day': {day!r}}}, {{}}, clock={clock!r}[:19]) == today
    assert today['_resolved_span'][1] - today['_resolved_span'][0] == {hours} * 3600
    assert today != e.canonical_args(name, {{'period': 'this week'}}, {{}}, clock={clock!r})
    assert today != e.canonical_args(name, {{'period': 'tomorrow'}}, {{}}, clock={clock!r})
    assert e.canonical_args(name, {{'period': 'invalid synthetic scope'}}, {{}}, clock={clock!r}) == {{'period': 'invalid synthetic scope'}}
    assert (os.environ['TZ'], time.localtime(datetime.fromisoformat({clock!r}).timestamp())) == original
row = {{'clock': {clock!r}, 'expected': {{'calls': [{{'name':'view_emails', 'args':{{'day':{day!r}}}}}], 'sources':['email']}}}}
run = {{'executed_calls':[{{'name':'view_emails', 'args':{{'period':'today'}}}}], 'events':[{{'type':'done'}}], 'answer':''}}
assert e.score(row, run)['metrics']['end_to_end']
run['executed_calls'][0]['name'] = 'view_messages'
wrong_source = e.score(row, run)['metrics']
assert not wrong_source['source'] and not wrong_source['exact_arguments'] and not wrong_source['end_to_end']
run['executed_calls'][0] = {{'name':'view_emails', 'args':{{'period':'tomorrow'}}}}
assert not e.score(row, run)['metrics']['exact_arguments']
late_clock = {clock!r}.replace('T12:00:00', 'T23:30:00')
agenda = {{'clock':late_clock, 'expected':{{'calls':[{{'name':'get_upcoming', 'args':{{'period':'today'}}}}]}}}}
assert e.runtime_constraint_issues(agenda, agenda['expected']['calls']) == []
agenda['clock'] = late_clock[:19]
assert e.runtime_constraint_issues(agenda, agenda['expected']['calls']) == []
assert e.runtime_constraint_issues(agenda, [{{'name':'get_upcoming', 'args':{{'period':'yesterday'}}}}]) == ['upcoming-tool-cannot-read-elapsed-calendar-window']
assert (os.environ['TZ'], time.localtime(datetime.fromisoformat({clock!r}).timestamp())) == original
print('EXACT_DST_SCOPE_AND_SOURCE')
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'EXACT_DST_SCOPE_AND_SOURCE'


@pytest.mark.parametrize('host_timezone', ['UTC', 'America/Los_Angeles', None])
def test_evaluation_timezone_restores_environment_and_local_clock_after_failure(host_timezone):
    code = f"""
import os, time
from scripts import eval_router_update as e
if {host_timezone!r} is None:
    os.environ.pop('TZ', None)
else:
    os.environ['TZ'] = {host_timezone!r}
time.tzset()
original = (os.environ.get('TZ'), time.localtime(1772956800))
try:
    with e.evaluation_timezone():
        assert os.environ['TZ'] == 'America/Los_Angeles'
        assert time.localtime(1772956800).tm_hour == 0
        raise ValueError('synthetic resolver failure')
except ValueError:
    pass
assert (os.environ.get('TZ'), time.localtime(1772956800)) == original
print('RESTORED')
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'RESTORED'


def test_timezone_contract_fails_closed_without_tzset(monkeypatch):
    import os
    previous = os.environ.get('TZ')
    monkeypatch.delattr(evaluation.time, 'tzset')
    with pytest.raises(RuntimeError, match='requires time.tzset'):
        with evaluation.evaluation_timezone():
            raise AssertionError('unsupported timezone contract was accepted')
    assert os.environ.get('TZ') == previous


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


def test_enabled_adapter_rejects_unisolated_caller_before_underlying_status():
    import asyncio
    class Never:
        async def status(self):
            raise AssertionError('adapter touched underlying I/O without isolation')
    grant = evaluation.InferenceGrant('Ling-fixture', 'fixture-revision', 'a' * 64, 'synthetic-receipt', ('127.0.0.1', 1), enabled=True)
    adapter = evaluation.ResidentInferenceAdapter(Never(), grant)
    with pytest.raises(PermissionError, match='isolated evaluation guard'):
        asyncio.run(adapter.status())
    with pytest.raises(PermissionError, match='differs'):
        asyncio.run(adapter.ensure_only('Other-model'))



def test_adapter_has_only_named_readonly_metadata_and_disabled_access():
    metadata = {'target', 'base_url', 'endpoint_name', 'provider', 'api_prefix', '_credential_transport'}
    properties = {name for name, value in vars(evaluation.ResidentInferenceAdapter).items() if isinstance(value, property)}
    assert properties == metadata
    assert '__getattr__' not in vars(evaluation.ResidentInferenceAdapter)
    grant = evaluation.InferenceGrant('Ling-fixture', 'fixture-revision', 'a' * 64, 'synthetic-receipt', ('127.0.0.1', 1))
    adapter = evaluation.ResidentInferenceAdapter(object(), grant)
    for name in metadata:
        with pytest.raises(PermissionError, match='disabled'):
            getattr(adapter, name)
        with pytest.raises(AttributeError):
            setattr(adapter, name, object())
    for name in ('api_key', 'start', 'load', 'unload', 'swap'):
        with pytest.raises(AttributeError):
            getattr(adapter, name)


@pytest.mark.skipif((ROOT / 'service/router/intent/planner.py').exists(), reason='Absence check applies to original control only')
def test_adapter_fails_closed_when_strict_core_is_unavailable():
    code = r'''
import asyncio, pathlib, tempfile
from scripts import eval_router_update as e
state = pathlib.Path(tempfile.mkdtemp(prefix='wisp-adapter-missing-core-'))
e.install_guard(state, state)
class Never:
    async def status(self):
        raise AssertionError('adapter reached I/O without strict identity verifier')
grant = e.InferenceGrant('Ling-fixture', 'fixture-revision', 'a' * 64, 'synthetic-receipt', ('127.0.0.1', 1), enabled=True)
try:
    asyncio.run(e.ResidentInferenceAdapter(Never(), grant).status())
except PermissionError as exc:
    assert str(exc) == 'Strict isolated router identity cannot be verified'
else:
    raise AssertionError('unavailable core identity verifier was accepted')
print('CLOSED')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'CLOSED'


# Every operation below is a fake callback, including successful chat/stream.
# install_guard receives no inference grant, so sockets remain entirely denied.
ADAPTER_IDENTITY_CHECKS = r'''
import asyncio, dataclasses, pathlib, tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit
from scripts import eval_router_update as e
state = pathlib.Path(tempfile.mkdtemp(prefix='wisp-adapter-identity-'))
e.install_guard(state, state)
from service.config import endpoints
from service.router.intent.planner import _client_matches_target
base = e.ScriptedClient({'expected': {'calls': []}}).target
original_resolver = endpoints.role_target
current = base
endpoints.role_target = lambda role: current if role == 'router' else original_resolver(role)
origin = urlsplit(base.endpoint.base_url)
grant = e.InferenceGrant(base.model, 'synthetic-revision', 'a' * 64, 'synthetic-resource-receipt', (origin.hostname, origin.port), enabled=True)
seen = []
def fixture():
    client = e.ScriptedClient({'expected': {'calls': []}})
    async def status():
        seen.append('status')
        return {'models': [{'id': client.target.model, 'loaded': True}]}
    async def chat(model, messages, **kwargs):
        seen.append('chat')
        return {'synthetic': True}
    async def stream(model, messages, **kwargs):
        seen.append('stream')
        yield {'synthetic': True}
    async def forbidden(*args, **kwargs):
        raise AssertionError('underlying load/readiness method ran')
    client.status, client.chat, client.stream_events = status, chat, stream
    client.ensure_only = forbidden
    return client
async def exercise():
    global current
    client = fixture()
    adapter = e.ResidentInferenceAdapter(client, grant)
    for name in ('target', 'base_url', 'endpoint_name', 'provider', 'api_prefix', '_credential_transport'):
        assert getattr(adapter, name) is getattr(client, name), name
    assert _client_matches_target(adapter, current)
    assert not seen, 'metadata qualification invoked I/O'
    assert await adapter.chat(grant.model, []) == {'synthetic': True}
    assert [event async for event in adapter.stream_events(grant.model, [])] == [{'synthetic': True}]
    assert seen == ['status', 'chat', 'status', 'stream'], seen
    # Original authority is borrowed, never constructed or substituted.
    assert adapter._credential_transport is client._credential_transport
    assert all('credential' not in row for row in adapter.raw)
    for scenario in ('disabled', 'incomplete', 'remote', 'grant_model', 'grant_origin', 'grant_revision',
                     'client_model', 'client_role', 'client_context', 'client_provider', 'client_prefix',
                     'client_endpoint', 'client_origin', 'client_managed', 'missing_transport',
                     'transport_origin', 'transport_backend', 'configuration_model', 'configuration_role',
                     'configuration_origin', 'configuration_unavailable'):
        current = base
        client = fixture()
        selected = grant
        if scenario == 'disabled': selected = dataclasses.replace(grant, enabled=False)
        if scenario == 'incomplete': selected = dataclasses.replace(grant, approval_receipt='')
        if scenario == 'remote': selected = dataclasses.replace(grant, endpoint=('example.invalid', origin.port))
        if scenario == 'grant_model': selected = dataclasses.replace(grant, model='Ling-other')
        if scenario == 'grant_origin': selected = dataclasses.replace(grant, endpoint=(origin.hostname, origin.port + 1))
        if scenario == 'grant_revision':
            current = dataclasses.replace(base, revision='configured-revision')
            client.target = current
        if scenario == 'client_model': client.target = dataclasses.replace(base, model='Ling-other')
        if scenario == 'client_role': client.target = dataclasses.replace(base, role='actor')
        if scenario == 'client_context': client.target = dataclasses.replace(base, context_window=base.context_window + 1)
        if scenario == 'client_provider': client.provider = SimpleNamespace(name='openai-compatible')
        if scenario == 'client_prefix': client.api_prefix = '/v2'
        if scenario == 'client_endpoint': client.endpoint_name = 'remote'
        if scenario == 'client_origin': client.base_url = 'http://127.0.0.1:2'
        if scenario == 'client_managed': client.managed = False
        if scenario == 'missing_transport': del client._credential_transport
        if scenario == 'transport_origin': client._credential_transport.origin = 'http://127.0.0.1:2'
        if scenario == 'transport_backend': client._credential_transport.backend = None
        if scenario == 'configuration_model': current = dataclasses.replace(base, model='Ling-other')
        if scenario == 'configuration_role': current = dataclasses.replace(base, role='actor')
        if scenario == 'configuration_origin': current = dataclasses.replace(base, endpoint=dataclasses.replace(base.endpoint, base_url='http://127.0.0.1:2'))
        if scenario == 'configuration_unavailable': current = None
        seen.clear()
        adapter = e.ResidentInferenceAdapter(client, selected)
        for operation in (adapter.status, lambda: adapter.chat(selected.model, [])):
            try:
                await operation()
            except PermissionError:
                pass
            else:
                raise AssertionError('accepted identity mismatch: ' + scenario)
            assert seen == [], (scenario, seen)
    current = base
    client = fixture()
    async def absent():
        seen.append('status')
        return {'models': [{'id': grant.model, 'loaded': False}]}
    client.status = absent
    seen.clear()
    try:
        await e.ResidentInferenceAdapter(client, grant).chat(grant.model, [])
    except PermissionError as exc:
        assert 'not already resident' in str(exc)
    else:
        raise AssertionError('nonresident model was accepted')
    assert seen == ['status']
    for streaming in (False, True):
        client = fixture()
        async def mutating():
            seen.append('status')
            client.base_url = 'http://127.0.0.1:2'
            return {'models': [{'id': grant.model, 'loaded': True}]}
        client.status = mutating
        seen.clear()
        adapter = e.ResidentInferenceAdapter(client, grant)
        try:
            if streaming:
                _ = [event async for event in adapter.stream_events(grant.model, [])]
            else:
                await adapter.chat(grant.model, [])
        except PermissionError:
            pass
        else:
            raise AssertionError('identity change after status reached generation')
        assert seen == ['status']
    # Changing state/config to a real home is rejected before any client I/O.
    import os
    seen.clear()
    os.environ['WISP_HOME'] = str(state / 'unapproved')
    try:
        await e.ResidentInferenceAdapter(fixture(), grant).status()
    except PermissionError:
        pass
    else:
        raise AssertionError('escaped isolated configuration')
    assert not seen
asyncio.run(exercise())
print('IDENTITY_CHECKS_PASSED')
'''


@pytest.mark.skipif(not (ROOT / 'service/router/intent/planner.py').exists(), reason='Requires integrated strict core; unavailable core fails closed separately')
def test_adapter_borrows_existing_identity_and_rejects_mismatches_before_io():
    result = subprocess.run([sys.executable, '-c', ADAPTER_IDENTITY_CHECKS], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'IDENTITY_CHECKS_PASSED'


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


def message_guard_schema():
    return {'view_messages': {'properties': {'query': {'type': 'string'},
        'count': {'type': 'integer'}, 'strict_match': {'type': 'boolean', 'default': False}}}}


def test_added_registered_message_guard_keeps_original_metrics_and_adds_guarded_credit():
    gold = {'name': 'view_messages', 'args': {'query': 'LITERAL-42', 'count': 3}}
    actual = {'name': 'view_messages', 'args': {'query': 'LITERAL-42', 'count': 3, 'strict_match': True}}
    result = evaluation.score(case(calls=[gold], sources=['messages']), run([actual], schemas=message_guard_schema()))
    metrics = result['metrics']
    assert metrics['exact_arguments'] is False
    assert metrics['first_call'] is False
    assert metrics['end_to_end'] is False
    assert metrics['request_argument'] is True
    assert metrics['query_scope_guard'] is True
    assert metrics['guarded_end_to_end'] is True


@pytest.mark.parametrize('args', [
    {'query': 'prefix LITERAL-42 suffix', 'count': 3, 'strict_match': True},
    {'query': 'LITERAL-42', 'count': '3', 'strict_match': True},
    {'query': 'LITERAL-42', 'count': True, 'strict_match': True},
    {'query': 'LITERAL-42', 'count': 3.0, 'strict_match': True},
    {'query': 'LITERAL-42', 'count': 3, 'day': 'today', 'strict_match': True},
    {'query': 'LITERAL-42', 'count': 3, 'account': 'extra-account', 'strict_match': True},
    {'query': ['LITERAL-42'], 'count': 3, 'strict_match': True},
    {'query': 'LITERAL-42', 'count': 3, 'strict_match': 1},
    {'query': 'LITERAL-42', 'count': 3, 'strict_match': 'true'},
])
def test_message_guard_addition_never_relaxes_other_arguments_or_types(args):
    expected = case(calls=[{'name': 'view_messages', 'args': {'query': 'LITERAL-42', 'count': 3}}], sources=['messages'])
    metrics = evaluation.score(expected, run([{'name': 'view_messages', 'args': args}], schemas=message_guard_schema()))['metrics']
    assert metrics['request_argument'] is False
    assert metrics['guarded_end_to_end'] is False


@pytest.mark.parametrize('properties', [{}, {'strict_match': {'default': False}},
    {'strict_match': {'type': 'string'}}, {'strict_match': {'type': 'boolean', 'enum': [False]}},
    {'strict_match': {'type': 'boolean', 'const': False}}])
def test_message_guard_needs_actual_registered_boolean_capability(properties):
    gold = {'name': 'view_messages', 'args': {'query': 'LITERAL-42'}}
    actual = {'name': 'view_messages', 'args': {'query': 'LITERAL-42', 'strict_match': True}}
    metrics = evaluation.score(case(calls=[gold], sources=['messages']), run([actual], schemas={'view_messages': {'properties': properties}}))['metrics']
    assert metrics['request_argument'] is False
    assert metrics['query_scope_guard'] is False
    assert metrics['guarded_end_to_end'] is False


@pytest.mark.parametrize('flag', [None, False, 0, 1, 'true'])
def test_omitted_or_invalid_message_guard_is_not_guarded_success(flag):
    args = {'query': 'LITERAL-42'}
    if flag is not None:
        args['strict_match'] = flag
    gold = {'name': 'view_messages', 'args': {'query': 'LITERAL-42'}}
    metrics = evaluation.score(case(calls=[gold], sources=['messages']), run([{'name': 'view_messages', 'args': args}], schemas=message_guard_schema()))['metrics']
    assert metrics['query_scope_guard'] is False
    assert metrics['guarded_end_to_end'] is False


@pytest.mark.parametrize('gold_flag', [True, False])
def test_explicit_gold_message_guard_remains_authoritative(gold_flag):
    gold = {'name': 'view_messages', 'args': {'query': 'LITERAL-42', 'strict_match': gold_flag}}
    same = evaluation.score(case(calls=[gold], sources=['messages']), run([gold], schemas=message_guard_schema()))['metrics']
    assert same['request_argument'] is True
    assert same['query_scope_guard'] is (True if gold_flag else None)
    opposite = {'name': 'view_messages', 'args': {'query': 'LITERAL-42', 'strict_match': not gold_flag}}
    wrong = evaluation.score(case(calls=[gold], sources=['messages']), run([opposite], schemas=message_guard_schema()))['metrics']
    assert wrong['request_argument'] is False
    assert wrong['guarded_end_to_end'] is False


def test_request_argument_amendment_does_not_include_email_or_no_query_calls():
    for name, args in [('view_emails', {'query': 'LITERAL-42'}), ('view_messages', {})]:
        gold = {'name': name, 'args': args}
        actual = {'name': name, 'args': {**args, 'strict_match': True}}
        schemas = {name: {'properties': {'strict_match': {'type': 'boolean', 'default': False}}}}
        metrics = evaluation.score(case(calls=[gold], sources=[evaluation.SOURCES[name]]), run([actual], schemas=schemas))['metrics']
        assert metrics['request_argument'] is False
        assert metrics['query_scope_guard'] is None
        assert metrics['guarded_end_to_end'] is False


def test_guarded_end_to_end_preserves_all_other_failure_gates():
    gold = {'name': 'view_messages', 'args': {'query': 'LITERAL-42'}}
    actual = {'name': 'view_messages', 'args': {'query': 'LITERAL-42', 'strict_match': True}}
    expected = case(calls=[gold], sources=['messages'], excluded_sources=['messages'])
    metrics = evaluation.score(expected, run([actual], schemas=message_guard_schema()))['metrics']
    assert metrics['request_argument'] and metrics['query_scope_guard']
    assert not metrics['excluded_source'] and not metrics['guarded_end_to_end']


@pytest.mark.parametrize('disposition', ['compiled', 'clarify', 'declined'])
def test_finite_intent_disposition_derives_path_without_rewriting_raw_route(disposition):
    event = {'type': 'routed', 'route_source': 'model', 'intent_disposition': disposition}
    assert evaluation.evaluation_path([event]) == 'intent_' + disposition
    assert event['route_source'] == 'model'


@pytest.mark.parametrize('disposition', ['invented-private-text', '', None, ['compiled']])
def test_unknown_or_wrong_type_intent_disposition_never_enters_path(disposition):
    event = {'type': 'routed', 'route_source': 'model', 'intent_disposition': disposition}
    assert evaluation.evaluation_path([event]) == 'model'


def test_scripted_client_exposes_coherent_synthetic_router_identity_without_transport():
    code = r'''
import asyncio, pathlib, tempfile
from types import SimpleNamespace
from scripts import eval_router_update as e
state = pathlib.Path(tempfile.mkdtemp(prefix='wisp-fake-identity-'))
e.install_guard(state, state)
from service.config.endpoints import role_target, Target
client = e.ScriptedClient({'expected': {'calls': [], 'sources': []}})
target = role_target('router')
assert isinstance(client.target, Target) and client.target == target
assert client.base_url == target.endpoint.base_url.rstrip('/')
assert client.endpoint_name == target.endpoint.name
assert client.managed is True
assert client.provider.name == target.endpoint.provider == 'omlx'
assert client.api_prefix == target.endpoint.api_prefix == '/v1'
assert isinstance(client._credential_transport, SimpleNamespace)
assert str(client._credential_transport.origin).rstrip('/') == client.base_url
assert client._credential_transport.backend is not None
assert asyncio.run(client.status())['models'] == [{'id': target.model, 'loaded': True}]
assert not client.raw
print('SYNTHETIC_IDENTITY')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'SYNTHETIC_IDENTITY'


@pytest.fixture
def runtime_capability(monkeypatch, tmp_path):
    import os
    home = tmp_path / 'original-fixture-home'
    home.mkdir(mode=0o700)
    (home / '.moe').mkdir(mode=0o700)
    lock = home / '.moe/.provisioning.lock'
    lock.write_bytes(b'fixture lock contents')
    lock.chmod(0o600)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    grant = evaluation.InferenceGrant('Ling-Fixture', 'fixture-revision', 'a' * 64,
                                      'synthetic-only-execution-receipt', ('127.0.0.1', 8000), True)
    cap = evaluation.RuntimeCapability(grant, end_utc='2099-01-01T00:00:00+00:00',
                                       execution_receipt='synthetic-only', enabled=True)
    cap._original_open = os.open
    return cap


def test_runtime_capability_default_disabled_and_expiry_uses_retained_clock(runtime_capability, monkeypatch):
    import time
    cap = runtime_capability
    cap.enabled = False
    with pytest.raises(PermissionError): cap.check()
    cap.enabled = True
    monkeypatch.setattr(time, 'time', lambda: 0)  # Scenario clock must never extend admission.
    cap.stop_wall = evaluation._REAL_WALL() - 1
    with pytest.raises(PermissionError): cap.check()
    cap.stop_wall = 10**12
    cap.stop_monotonic = evaluation._REAL_MONOTONIC() - 1
    with pytest.raises(PermissionError): cap.check()


def test_runtime_phase_does_not_leak_and_cannot_grant_a_personal_read(runtime_capability):
    cap = runtime_capability
    assert not cap._real_home()
    assert not cap._read_allowed(cap.home / '.omlx/settings.json')
    with cap._phase('construct'):
        assert cap._real_home()
        assert cap._read_allowed(cap.home / '.omlx/settings.json')
        assert not cap._read_allowed(cap.home / 'Documents/private.txt')
    assert not cap._real_home()
    assert not cap._read_allowed(cap.home / '.omlx/settings.json')


@pytest.mark.parametrize('argv', [
    ['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8765', '-sTCP:LISTEN', '-Fpufn'],
    ['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000', '-sTCP:LISTEN', '-Fpufn', '-r'],
    ['/bin/ps', '-ww', '-p', '0', '-o', 'ppid=,uid=,comm='],
    ['/bin/ps', '-ww', '-p', '1;true', '-o', 'ppid=,uid=,comm='],
    ['/bin/ps', '-ww', '-p', '2147483648', '-o', 'ppid=,uid=,comm='],
    ['/bin/launchctl', 'kickstart', 'anything'], ['/bin/sh', '-c', 'true'],
])
def test_runtime_process_capability_rejects_altered_argv(argv):
    assert not evaluation.RuntimeCapability._argv_allowed(argv)


def test_exact_inspection_argv_still_needs_trusted_callsite_and_phase(runtime_capability):
    cap = runtime_capability
    argv = ['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000', '-sTCP:LISTEN', '-Fpufn']
    args = (argv[0], argv, None, {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
    assert cap._argv_allowed(argv)
    assert not cap._process_allowed(args)
    with cap._phase('peer'):
        assert not cap._process_allowed(args)  # Never allow arbitrary direct Popen.


@pytest.mark.parametrize('url,method,body', [
    ('http://127.0.0.1:8765/v1/models/status', 'GET', None),
    ('http://localhost:8000/v1/models/status', 'GET', None),
    ('http://127.0.0.1:8000/v1/models/status?x=1', 'GET', None),
    ('http://127.0.0.1:8000/v1/models/load', 'POST', {}),
    ('http://127.0.0.1:8000/health', 'GET', None),
    ('http://127.0.0.1:8000/v1/chat/completions', 'GET', None),
    ('http://127.0.0.1:8000/v1/chat/completions', 'POST', {'model': 'Other'}),
])
def test_runtime_http_capability_is_exact(runtime_capability, url, method, body):
    import httpx
    cap = runtime_capability
    with cap._phase('peer'), pytest.raises(PermissionError):
        cap._request_allowed(httpx.Request(method, url, json=body))


def test_runtime_http_status_and_completion_require_private_phase(runtime_capability):
    import httpx
    cap = runtime_capability
    request = httpx.Request('POST', 'http://127.0.0.1:8000/v1/chat/completions', json={'model': cap.grant.model})
    with pytest.raises(PermissionError): cap._request_allowed(request)
    with cap._phase('peer'):
        cap._request_allowed(request)
        cap._request_allowed(httpx.Request('GET', 'http://127.0.0.1:8000/v1/models/status'))


@pytest.mark.parametrize('damage', ['missing', 'mode', 'symlink', 'content', 'same_content', 'replacement'])
def test_existing_credential_lock_cannot_be_created_unqualified_or_changed(runtime_capability, damage):
    cap = runtime_capability
    lock = cap.home / '.moe/.provisioning.lock'
    cap._check_lock()
    if damage == 'missing': lock.unlink()
    elif damage == 'mode': lock.chmod(0o644)
    elif damage == 'symlink':
        lock.unlink()
        lock.symlink_to(cap.home / 'missing')
    elif damage == 'content': lock.write_bytes(b'changed')
    elif damage == 'same_content': lock.write_bytes(lock.read_bytes())
    elif damage == 'replacement':
        saved = lock.with_suffix('.saved')
        lock.rename(saved)
        lock.write_bytes(saved.read_bytes())
        lock.chmod(0o600)
    with pytest.raises((PermissionError, FileNotFoundError)):
        cap._check_lock()


def test_runtime_phase_propagates_to_thread_without_leaking(runtime_capability):
    import asyncio
    cap = runtime_capability
    async def check():
        with cap._phase('peer'):
            assert await asyncio.to_thread(cap._current_phase) == 'peer'
        assert await asyncio.to_thread(cap._current_phase) is None
    asyncio.run(check())


RUNTIME_FACTORY_FIXTURE = r'''
import asyncio, json, os, pathlib, tempfile
from types import SimpleNamespace
from scripts import eval_router_update as e
import httpx
base = pathlib.Path(tempfile.mkdtemp(prefix='wisp-runtime-offline-')).resolve()
home, state = base / 'original', base / 'state'
home.mkdir(mode=0o700); state.mkdir(mode=0o700)
(home / '.moe').mkdir(mode=0o700); (home / '.omlx').mkdir(mode=0o700)
lock = home / '.moe/.provisioning.lock'
lock.write_bytes(b'existing synthetic lock'); lock.chmod(0o600)
roles = ('router','fast','agent','general','coding','reasoning')
model = 'Ling-Fixture'
overlay = {'roles':dict.fromkeys(roles, model), 'inference':{'bindings':{
    role:{'endpoint':'local','model_id':model,'revision':'synthetic-revision','context_window':4096}
    for role in roles}}}
import yaml
(home / '.moe/config.yaml').write_text(yaml.safe_dump(overlay))
(home / '.omlx/settings.json').write_text(json.dumps({'auth':{'api_key':'synthetic-key'},'server':{'host':'127.0.0.1','port':8000}}))
(home / '.omlx/model_settings.json').write_text(json.dumps({'models':{model:{'max_context_window':4096}}}))
pathlib.Path.home = classmethod(lambda cls: home)
grant=e.InferenceGrant(model,'synthetic-revision','a'*64,'fixture-only',('127.0.0.1',8000),True)
cap=e.RuntimeCapability(grant,end_utc='2099-01-01T00:00:00+00:00',execution_receipt='fixture-only',enabled=True)
e.install_guard(state,base,inference_grant=grant,runtime_capability=cap)
from service import config as cfg
from service.config import quarantine
from service.inference import omlx_client
from service.config.endpoints import role_target
before=(cfg.USER_CONFIG,cfg.OMLX_SETTINGS,cfg.OMLX_MODEL_SETTINGS)
seen=[]
class FakeSupportedClient:
    def __init__(self, *, target):
        assert target.endpoint.api_key() == 'synthetic-key'
        assert quarantine._gate.directory == home / '.moe'
        self.target=target; self.base_url=target.endpoint.base_url
        self.endpoint_name=target.endpoint.name; self.managed=True
        self.provider=SimpleNamespace(name='omlx'); self.api_prefix='/v1'
        self._credential_transport=SimpleNamespace(origin=httpx.URL(self.base_url),backend=object())
        def fixture_http(request):
            seen.append((request.method,request.url.path))
            return httpx.Response(200,json={'models':[{'id':model,'loaded':True}]})
        self._client=quarantine.guard_client(httpx.AsyncClient(base_url=self.base_url,transport=httpx.MockTransport(fixture_http)))
    async def status(self): return (await self._client.get('/v1/models/status')).json()
    async def chat(self, name, messages, **kwargs):
        await self._client.post('/v1/chat/completions',json={'model':name,'messages':messages})
        return {'choices':[{'finish_reason':'stop','message':{'content':'synthetic only'}}]}
    async def stream_events(self, name, messages, **kwargs):
        await self._client.post('/v1/chat/completions',json={'model':name,'messages':messages})
        yield {'kind':'content','text':'synthetic only'}
    async def aclose(self):
        seen.append(('close','local'))
        await self._client.aclose()
omlx_client.OMLXClient=FakeSupportedClient
'''


def test_supported_factory_delegates_normal_auth_gate_and_restores_configuration(tmp_path):
    code = RUNTIME_FACTORY_FIXTURE + r'''
async def exercise():
    with cap._phase('configure'):
        inode=lock.stat().st_ino; contents=lock.read_bytes()
    async with e.supported_resident_client(cap) as supplied:
        assert pathlib.Path.home() == state
        assert (cfg.USER_CONFIG,cfg.OMLX_SETTINGS,cfg.OMLX_MODEL_SETTINGS) == before
        assert role_target('router') == supplied.target
        assert 'synthetic-key' not in (state / '.moe/config.yaml').read_text()
        assert 'synthetic-key' not in (state / '.omlx/settings.json').read_text()
        assert not hasattr(supplied, 'ensure_only') and not hasattr(supplied, 'start')
        adapter=e.ResidentInferenceAdapter(supplied,grant)
        assert (await adapter.status())['models'][0]['loaded'] is True
        assert await adapter.chat(model,[]) == {'choices':[{'finish_reason':'stop','message':{'content':'synthetic only'}}]}
        assert [x async for x in adapter.stream_events(model,[])] == [{'kind':'content','text':'synthetic only'}]
        assert cap.raw[0]['outcome'] == cap.raw[1]['outcome'] == 'completed'
        with cap._phase('peer'):
            assert lock.stat().st_ino == inode and lock.read_bytes() == contents
        marker=base / 'external-fixture-marker'
        marker.write_text('{}')
        marker.rename(home / '.moe/.helper-transaction.json')
        prior=list(seen)
        try: await adapter.status()
        except quarantine.CredentialQuarantined: pass
        else: raise AssertionError('real recovery marker was ignored')
        assert seen == prior
    assert cap.revoked and seen[-1] == ('close','local')
    assert (cfg.USER_CONFIG,cfg.OMLX_SETTINGS,cfg.OMLX_MODEL_SETTINGS) == before
    assert not (state / '.moe/config.yaml').exists()
    assert not (state / '.omlx/model_settings.json').exists()
asyncio.run(exercise())
print('GENUINE_OFFLINE_GATE_AND_RESTORATION')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    (tmp_path / 'factory.stdout').write_text(result.stdout)
    (tmp_path / 'factory.stderr').write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'GENUINE_OFFLINE_GATE_AND_RESTORATION'


@pytest.mark.parametrize('failure', ['constructor','wrong_identity','managed_manifest','invalid_config'])
def test_supported_factory_failure_restores_constants_and_closes_owned_client(failure):
    setup = ''
    if failure == 'managed_manifest': setup = "(home / '.moe/omlx-runtime-authorization.json').write_text('{}')\n"
    if failure == 'invalid_config': setup = "(home / '.moe/config.yaml').write_text('[')\n"
    fixture = RUNTIME_FACTORY_FIXTURE.replace('e.install_guard(state,base,', setup + 'e.install_guard(state,base,')
    code = fixture + f"failure={failure!r}\n" + r'''
if failure in {'constructor','wrong_identity'}:
    class FaultyClient(FakeSupportedClient):
        def __init__(self, *, target):
            if failure == 'constructor': raise ValueError('synthetic constructor failure')
            super().__init__(target=target)
            self.base_url='http://127.0.0.1:8765'
    omlx_client.OMLXClient=FaultyClient
async def exercise():
    try:
        async with e.supported_resident_client(cap): raise AssertionError('factory accepted failure')
    except (PermissionError, ValueError, yaml.YAMLError): pass
    assert (cfg.USER_CONFIG,cfg.OMLX_SETTINGS,cfg.OMLX_MODEL_SETTINGS) == before
    assert pathlib.Path.home() == state and cap.revoked
    assert not any(method in {'GET','POST'} for method,path in seen)
    assert not (state / '.moe/config.yaml').exists()
    if failure=='wrong_identity': assert seen == [('close','local')]
asyncio.run(exercise())
print('TRANSACTION_RESTORED')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'TRANSACTION_RESTORED'


def test_lock_replacement_between_snapshot_and_open_is_rejected_before_request():
    code = RUNTIME_FACTORY_FIXTURE + r'''
async def exercise():
    try:
        async with e.supported_resident_client(cap) as supplied:
            original=cap._original_open; opens=0
            replacement=base / 'external-fixture-replacement'
            replacement.write_bytes(b'existing synthetic lock'); replacement.chmod(0o600)
            def racing(path,flags,*args,**kwargs):
                nonlocal opens
                if pathlib.Path(path)==lock:
                    opens+=1
                    if opens==2:
                        lock.rename(lock.with_suffix('.old'))
                        replacement.rename(lock)
                return original(path,flags,*args,**kwargs)
            cap._original_open=racing
            try:
                with cap._phase('peer'):
                    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            except PermissionError: pass
            else:
                os.close(fd)
                raise AssertionError('replacement fd escaped before request')
            assert not seen and opens==2
            # Restore the inode; changed ctime must still fail cleanup honestly.
            lock.unlink(); lock.with_suffix('.old').rename(lock)
    except PermissionError as error:
        assert str(error)=='Existing credential lock changed'
    else: raise AssertionError('changed lock falsely passed cleanup')
    assert cap.revoked and seen==[('close','local')]
asyncio.run(exercise())
print('RACE_REJECTED_BEFORE_BYTES')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'RACE_REJECTED_BEFORE_BYTES'


@pytest.mark.parametrize('outcome', ['cancel', 'timeout', 'revoke', 'concurrent'])
def test_owned_request_cleanup_and_raw_failure_receipts(runtime_capability, outcome):
    import asyncio
    from types import SimpleNamespace
    cap = runtime_capability
    cap._check_lock()
    started = asyncio.Event()
    released = asyncio.Event()
    class WaitingClient:
        _client = SimpleNamespace(event_hooks={})
        async def chat(self, *args, **kwargs):
            started.set()
            await released.wait()
            return {'fixture': True}
        async def status(self): return {'fixture': True}
        async def aclose(self): self.closed = True
    client = WaitingClient()
    supplied = evaluation._ScopedResidentClient(client, cap)
    async def exercise():
        if outcome == 'timeout': cap.stop_monotonic = evaluation._REAL_MONOTONIC() + 0.05
        task = asyncio.create_task(supplied.chat(cap.grant.model, ['synthetic']))
        await started.wait()
        if outcome == 'cancel': task.cancel()
        elif outcome == 'revoke':
            cap.revoked = True
            released.set()
        elif outcome == 'concurrent':
            with pytest.raises(PermissionError, match='one owned'):
                await supplied.status()
            released.set()
        if outcome == 'concurrent': assert await task == {'fixture': True}
        else:
            expected = {'cancel': asyncio.CancelledError, 'timeout': TimeoutError, 'revoke': PermissionError}[outcome]
            with pytest.raises(expected): await task
        assert not cap._busy and evaluation._RUNTIME_PHASE.get() is None
        assert cap.raw[0]['messages'] == ['synthetic']
        assert cap.raw[0]['outcome'] != 'pending'
        await supplied.aclose()
        assert client.closed
    asyncio.run(exercise())


def test_guard_runtime_scope_never_allows_public_calls_symlink_reads_or_real_writes():
    code = RUNTIME_FACTORY_FIXTURE + r'''
import sys
sentinel=base / 'outside-synthetic-file'
sentinel.write_text('synthetic sentinel')
link=home / 'Documents-link'
link.symlink_to(sentinel)
for protected in ('.omlx/settings.json','.moe/.credential-generation','.moe/.helper-transaction.json'):
    path=home / protected
    staged=base / ('link-' + path.name)
    staged.symlink_to(sentinel)
    if path.exists(): path.rename(base / ('saved-' + path.name))
    staged.rename(path)
    with cap._phase('construct'):
        try: path.read_bytes()
        except PermissionError: pass
        else: raise AssertionError('exact symlink capability escaped HOME')
with cap._phase('construct'):
    for path in (link,home / '.moe/config.yaml'):
        try:
            if path==link: path.read_bytes()
            else: path.write_text('forbidden mutation')
        except PermissionError: pass
        else: raise AssertionError('private capability allowed personal read/write')
try: sys.audit('socket.connect',object(),('127.0.0.1',8000))
except PermissionError: pass
else: raise AssertionError('public socket call gained capability')
with cap._phase('peer'):
    sys.audit('socket.connect',object(),('127.0.0.1',8000))  # Policy event only, no socket/probe.
    try: sys.audit('socket.connect',object(),('127.0.0.1',8765))
    except PermissionError: pass
    else: raise AssertionError('native origin admitted')
    argv=['/usr/sbin/lsof','-nP','-a','-iTCP:8000','-sTCP:LISTEN','-Fpufn']
    try: sys.audit('subprocess.Popen',argv[0],argv,None,{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LC_ALL':'C'})
    except PermissionError: pass
    else: raise AssertionError('public subprocess call gained capability')
print('SCOPE_STAYS_CLOSED')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'SCOPE_STAYS_CLOSED'


def test_existing_lock_lease_fd_is_read_only_and_missing_directory_is_not_created():
    code = RUNTIME_FACTORY_FIXTURE + r'''
with cap._phase('peer'):
    cap._check_lock()
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try: os.write(fd,b'forbidden content')
        except OSError: pass
        else: raise AssertionError('lease fd could mutate lock')
    finally: os.close(fd)
    directory=home / '.moe'
    original=cap._qualified_directory
    moved=base / 'external-moved-gate'
    def disappeared():
        answer=original()
        directory.rename(moved)
        return answer
    cap._qualified_directory=disappeared
    os.mkdir(directory,0o700)
    assert not directory.exists(), 'real mkdir happened after directory disappeared'
    cap._qualified_directory=original
    try: os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    except FileNotFoundError: pass
    else: raise AssertionError('missing lock was created')
    assert not directory.exists()
print('READONLY_LEASE_NO_CREATION')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'READONLY_LEASE_NO_CREATION'


@pytest.mark.parametrize('cause', ['generation', 'exclusive_lock'])
def test_genuine_recovery_generation_and_lock_contention_stop_before_bytes(cause):
    code = RUNTIME_FACTORY_FIXTURE + r'''
import fcntl
async def exercise():
    async with e.supported_resident_client(cap) as supplied:
        if CAUSE == 'generation':
            staged=base / 'external-generation'
            staged.write_text('b'*64); staged.chmod(0o600)
            staged.rename(home / '.moe/.credential-generation')
        else:
            with cap._phase('peer'):
                fd=cap._original_open(lock,os.O_RDONLY|os.O_NOFOLLOW)
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            try: await supplied.status()
            except quarantine.CredentialQuarantined: pass
            else: raise AssertionError('recovery/lock boundary did not block')
            assert not seen, 'HTTP bytes passed the genuine gate'
        finally:
            if CAUSE == 'exclusive_lock':
                fcntl.flock(fd,fcntl.LOCK_UN); os.close(fd)
        if CAUSE == 'generation':
            assert supplied._client._client.headers.get('Authorization') is None
            assert supplied._client._client.is_closed is False
asyncio.run(exercise())
assert cap.revoked and seen==[('close','local')]
print('GENUINE_GATE_DENIED_BEFORE_BYTES')
'''.replace('CAUSE', repr(cause))
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'GENUINE_GATE_DENIED_BEFORE_BYTES'


@pytest.mark.parametrize('field,value', [('role','agent'),('model','Other-Ling'),('revision','wrong')])
def test_captured_target_must_match_router_grant(runtime_capability, field, value):
    from types import SimpleNamespace
    cap = runtime_capability
    target = SimpleNamespace(role='router', model=cap.grant.model, revision=cap.grant.revision,
        endpoint=SimpleNamespace(name='local',base_url='http://127.0.0.1:8000',managed=True,
                                 provider='omlx',api_prefix='/v1',credential_ref='local_omlx'))
    cap._validate_target(target)
    setattr(target, field, value)
    with pytest.raises(PermissionError): cap._validate_target(target)


@pytest.mark.parametrize('field,value', [('name','other'),('base_url','http://127.0.0.1:8000/v1'),
    ('managed',False),('provider','other'),('api_prefix','/v2'),('credential_ref','keychain:other')])
def test_captured_endpoint_cannot_select_other_authority(runtime_capability, field, value):
    from types import SimpleNamespace
    cap = runtime_capability
    endpoint = SimpleNamespace(name='local',base_url='http://127.0.0.1:8000',managed=True,
                               provider='omlx',api_prefix='/v1',credential_ref='local_omlx')
    target = SimpleNamespace(role='router',model=cap.grant.model,revision=cap.grant.revision,endpoint=endpoint)
    setattr(endpoint,field,value)
    with pytest.raises(PermissionError): cap._validate_target(target)


def test_peer_inspection_policy_uses_actual_callsite_without_executing_a_process():
    code = RUNTIME_FACTORY_FIXTURE + r'''
from service.inference import attributed_transport
import sys
argv=['/usr/sbin/lsof','-nP','-a','-iTCP:8000','-sTCP:LISTEN','-Fpufn']
def fake_run(args, **kwargs):
    assert kwargs['timeout']==10 and kwargs['env']=={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LC_ALL':'C'}
    sys.audit('subprocess.Popen',args[0],args,None,kwargs['env'])  # Audit event only; no native process.
    return SimpleNamespace(returncode=0,stderr=b'',stdout=b'synthetic inventory')
attributed_transport.subprocess.run=fake_run
try: attributed_transport.inspect_command(argv)
except PermissionError: pass
else: raise AssertionError('inspection escaped private phase')
with cap._phase('peer'):
    assert attributed_transport.inspect_command(argv)==b'synthetic inventory'
    try: attributed_transport.inspect_command(argv+['-r'])
    except PermissionError: pass
    else: raise AssertionError('altered argv gained callsite authority')
print('ACTUAL_CALLSITE_POLICY_ONLY')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ACTUAL_CALLSITE_POLICY_ONLY'


def test_r11_copied_phase_is_revoked_on_exit_even_during_a_new_phase(runtime_capability):
    from contextvars import copy_context
    cap = runtime_capability
    with cap._phase('peer'):
        copied = copy_context()
        assert copied.run(cap._current_phase) == 'peer'
    assert copied.run(cap._current_phase) is None
    assert not copied.run(cap._real_home)
    with cap._phase('peer'):
        assert cap._current_phase() == 'peer'
        assert copied.run(cap._current_phase) is None


@pytest.mark.parametrize('finish', ['exhaust', 'early_close', 'cancel', 'expiry', 'timeout'])
def test_r11_supported_stream_consumer_and_inherited_task_have_no_io_authority(finish):
    code = RUNTIME_FACTORY_FIXTURE + r'''
import sys
async def exercise():
    async with e.supported_resident_client(cap) as supplied:
        assert not cap.revoked
        started=asyncio.Event(); closed=[]; children=[]; releases=[]; io=[]
        def policy():
            try: sys.audit('socket.connect',object(),('127.0.0.1',8000))
            except PermissionError: allowed=False
            else: allowed=True
            return (cap._current_phase(),pathlib.Path.home(),allowed)
        async def inherited(release):
            await release.wait()
            return policy()
        async def events(name,messages,**kwargs):
            try:
                for number in range(2):
                    assert cap._current_phase()=='peer'
                    assert policy()==('peer',home,True)
                    assert await asyncio.to_thread(policy)==('peer',home,True)
                    release=asyncio.Event(); releases.append(release)
                    children.append(asyncio.create_task(inherited(release)))
                    io.append(number)
                    await supplied._client._client.post('/v1/chat/completions',json={'model':name,'messages':messages})
                    if FINISH in {'cancel','timeout'}:
                        started.set()
                        await asyncio.Event().wait()
                    yield {'kind':'content','text':'synthetic '+str(number)}
            finally:
                closed.append(cap._current_phase())
        supplied._client.stream_events=events
        adapter=e.ResidentInferenceAdapter(supplied,grant)
        stream=adapter.stream_events(model,[{'role':'user','content':'synthetic only'}])
        if FINISH in {'expiry','timeout'}:
            cap.stop_monotonic=e._REAL_MONOTONIC()+0.3
        try:
            if FINISH in {'cancel','timeout'}:
                task=asyncio.create_task(anext(stream))
                await started.wait()
                if FINISH=='cancel': task.cancel()
                try: await asyncio.wait_for(task,2)
                except (asyncio.CancelledError if FINISH=='cancel' else TimeoutError): pass
                else: raise AssertionError('owned I/O cancellation/timeout was swallowed')
            else:
                assert (await anext(stream))['text']=='synthetic 0'
                assert policy()==(None,state,False), 'caller retained peer authority after yield'
                with_guard=False
                try: cap._request_allowed(httpx.Request('GET','http://127.0.0.1:8000/v1/models/status'))
                except PermissionError: with_guard=True
                assert with_guard and not cap._read_allowed(home / '.omlx/settings.json')
                try: await supplied.status()
                except PermissionError: pass
                else: raise AssertionError('concurrent request admitted while stream open')
                # Release a copied owned-I/O child while the stream remains suspended.
                releases[0].set()
                assert await children[0]==(None,state,False), 'copied phase outlived owned I/O'
                consumer_release=asyncio.Event(); releases.append(consumer_release)
                children.append(asyncio.create_task(inherited(consumer_release)))
                if FINISH=='expiry':
                    # A suspended consumer must not inherit the owned I/O timeout.
                    await asyncio.sleep(0.4)
                    assert policy()==(None,state,False)
                    try: await anext(stream)
                    except PermissionError: pass
                    else: raise AssertionError('expired stream resumed I/O')
                if FINISH=='exhaust':
                    assert [event['text'] async for event in stream]==['synthetic 1']
        finally:
            await stream.aclose()
            for release in releases: release.set()
        assert all(result==(None,state,False) for result in await asyncio.gather(*children))
        assert children and io==([0,1] if FINISH=='exhaust' else [0])
        # EOF/cancellation finalize inside owned __anext__; early close uses cleanup.
        assert closed==(['cleanup'] if FINISH in {'early_close','expiry'} else ['peer'])
        assert policy()==(None,state,False) and not cap._busy and not cap.revoked
        assert cap.raw[-1]['outcome']=={'exhaust':'completed','early_close':'GeneratorExit','cancel':'CancelledError','expiry':'PermissionError','timeout':'TimeoutError'}[FINISH]
        # Fresh legitimate I/O remains admitted; no stale token is revived.
        if FINISH in {'expiry','timeout'}:
            try: await adapter.status()
            except PermissionError: pass
            else: raise AssertionError('expired capability dispatched new work')
        else:
            assert (await adapter.status())['models'][0]['loaded'] is True
    assert cap.revoked and seen[-1]==('close','local')
    assert e._RUNTIME_PHASE.get() is None
asyncio.run(exercise())
print('R11_STREAM_AUTHORITY_AND_CLEANUP_VERIFIED')
'''.replace('FINISH', repr(finish))
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'R11_STREAM_AUTHORITY_AND_CLEANUP_VERIFIED'

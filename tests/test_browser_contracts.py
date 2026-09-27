"""Synthetic boundary checks; no browser, user state or credentials."""
import copy
import math
import pytest
from scripts.check_browser_contracts import assert_expected, cases, check_mirrors, run_case
from service.browser.contracts import ContractViolation, negotiate, validate

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import select
import secrets
import sys
from service.assistant.store import AssistantStore
from service.browser.bridge import BrowserBridge, BridgeIdentity, BridgeRuntimeContext, BridgeSessionClosed
from service.browser.bridge_protocol import MAX_FRAME, dumps, inspect, loads, open_frame, seal
from service.discovery.approvals import AppApprovalContext, ApprovalStore
from service.safety import policy

BROWSER = [c for c in cases() if c.get('contract') not in
           ('ActionableItem', 'ActionProposal', 'ActionReceipt', 'ScheduledBlock', 'ExternalRecord')
           and c['operation'] not in ('proposal', 'completion')]


@pytest.mark.parametrize('case', BROWSER, ids=lambda c: c['name'])
def test_shared_browser_cases(case):
    assert_expected([case], [run_case(case)])


def test_generated_schema_and_typed_records_are_current():
    check_mirrors()


def test_validated_values_are_isolated_from_caller_mutation():
    source = next(c['payload'] for c in BROWSER if c['name'] == 'assignment_snapshot')
    result = validate('BrowserSnapshot', source)
    result['elements'][0]['target_id'] = 'mutated'
    assert source['elements'][0]['target_id'] == 'document1.button1'


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf, -1, 0.25, True, '123', 9007199254740992])
def test_invalid_capture_times(value):
    payload = copy.deepcopy(next(c['payload'] for c in BROWSER if c['name'] == 'assignment_source'))
    payload['observed_at_ms'] = value
    with pytest.raises(ContractViolation):
        validate('SourceObservation', payload)


def test_wire_integral_number_accepts_equivalent_json_numeric_representations():
    payload = copy.deepcopy(next(c['payload'] for c in BROWSER if c['name'] == 'assignment_source'))
    payload['observed_at_ms'] = 1.0
    assert validate('SourceObservation', payload)['observed_at_ms'] == 1


def test_negotiation_is_symmetric_but_does_not_grant_authority():
    c = next(c for c in cases() if c['name'] == 'negotiate_common')
    assert negotiate(c['local'], c['remote']) == negotiate(c['remote'], c['local'])
    assert set(negotiate(c['local'], c['remote'])) == {'schema_version', 'capabilities'}


def test_unpaired_surrogate_is_not_a_unicode_scalar():
    payload = copy.deepcopy(next(c['payload'] for c in BROWSER if c['name'] == 'assignment_source'))
    payload['title'] = '\ud800'
    with pytest.raises(ContractViolation):
        validate('SourceObservation', payload)


# A04 authenticated bridge: synthetic keys, native context and disposable SQLite.
ROOT = Path(__file__).resolve().parents[1]
ADAPTER_KEY = bytes([7]) * 32
APP_KEY = bytes([9]) * 32


class Peer:
    def __init__(self, bridge, credential='adapter', key=ADAPTER_KEY, handshake=None):
        self.bridge, self.credential, self.key = bridge, credential, key
        challenge = bridge.challenge(credential)
        frame, server = open_frame(challenge, key, 'to_peer')
        self.sid = frame['session_id']
        self.seq = 0
        h = dict(server, peer='extension' if credential == 'adapter' else 'app',
                 credential_role='native_bridge' if credential == 'adapter' else 'app_approval')
        self.registered = bridge.receive(self.frame('register', dict(handshake=handshake or h,
                                                                    client_nonce=secrets.token_hex(32))))

    def frame(self, kind, payload):
        raw = seal(self.key, self.credential, self.sid, self.seq, kind, payload, 'to_service')
        self.seq += 1
        return raw

    def send(self, kind, payload):
        return self.bridge.receive(self.frame(kind, payload))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, '_READ_ONLY', False)
    monkeypatch.setattr(policy, '_FULL_ACCESS', False)
    records = json.loads((ROOT / 'test_fixtures/discovery_storage/lifecycle.json').read_text())
    records['ActionProposal']['state'] = 'proposed'
    now = [1790388000000]
    clock = [100.0]
    authority = AppApprovalContext()
    assistant = AssistantStore(tmp_path / 'bridge.db')
    approvals = ApprovalStore(assistant, app_context=authority, clock=lambda: now[0])
    for name in ('SourceObservation', 'ActionableItem', 'ActionProposal'):
        if name == 'ActionableItem':
            approvals.records.save('Evidence', records[name]['evidence'][0])
        approvals.records.save(name, records[name])
    context = [BridgeRuntimeContext('profile.1', True, False, True,
        frozenset({'https://school.example.invalid'}), records['SourceObservation']['source_url'], 'task.1', 'snapshot.1')]
    bridge = BrowserBridge(runtime_context=lambda _: context[0], approvals=approvals,
                           app_context=authority, clock=lambda: clock[0])
    bridge.provision(BridgeIdentity('adapter', 'extension', 'native_bridge', 'profile.1'), ADAPTER_KEY)
    bridge.provision(BridgeIdentity('approval', 'app', 'app_approval', 'profile.1'), APP_KEY)
    yield bridge, approvals, records, context, now, clock
    assistant._db.close()


def decision(env):
    _, _, r, _, now, _ = env
    p = r['ActionProposal']
    return dict(proposal_id=p['id'], decision='approved', expected_proposal_revision=1,
        expected_item_storage_revision=1, intent=p['intent'], evidence_ids=p['evidence_ids'], expires_at_ms=now[0] + 1000)


def approved_action(env):
    bridge, approvals, r, _, _, _ = env
    Peer(bridge, 'approval', APP_KEY).send('decision', decision(env))
    d = approvals.get('proposal.1')
    return dict(schema_version='1.0', intent=r['ActionProposal']['intent'], approval=dict(
        proposal_id='proposal.1', authority='app', approved_at_ms=d['decided_at_ms'],
        expires_at_ms=d['expires_at_ms'], intent=r['ActionProposal']['intent']))


def read_action(env, **intent):
    return dict(schema_version='1.0', intent=dict(env[2]['ActionProposal']['intent'], command='snapshot',
        target_id=None, consequential=False, **intent), approval=None)


def result(env, **changes):
    return dict(env[2]['ActionReceipt'], completes_obligation=False, **changes)


def test_observation_is_scoped_transient_and_does_not_persist(env):
    peer = Peer(env[0])
    observation = dict(env[2]['SourceObservation'], id='obs.new')
    event = peer.send('observation', observation)
    assert event['identity'].profile_id == 'profile.1'
    assert event['payload'] == observation
    assert env[1].records.get('SourceObservation', 'obs.new') is None


@pytest.mark.parametrize('changes', [dict(peer='app'), dict(credential_role='app_approval'),
    dict(capabilities=['dom_text', 'verified_receipts', 'exact_app_approval']),
    dict(required_capabilities=['exact_app_approval']), dict(supported_versions=['9.0'])])
def test_handshake_cannot_promote_adapter(env, changes):
    bridge = env[0]
    h = dict(bridge._handshake(bridge._credentials['adapter'].identity), peer='extension', credential_role='native_bridge')
    with pytest.raises(ContractViolation):
        Peer(bridge, handshake=dict(h, **changes))
    assert env[1].get('proposal.1') is None


def test_adapter_cannot_decide_or_send_commands(env):
    for kind, payload in [('decision', decision(env)), ('command', read_action(env))]:
        with pytest.raises(ContractViolation):
            Peer(env[0]).send(kind, payload)
    assert env[1].get('proposal.1') is None


def test_app_approval_credential_cannot_capture(env):
    with pytest.raises(ContractViolation):
        Peer(env[0], 'approval', APP_KEY).send('observation', env[2]['SourceObservation'])


def test_authenticated_decision_uses_a03_and_claims_once(env):
    action = approved_action(env)
    peer = Peer(env[0])
    raw = env[0].dispatch(peer.sid, action, evidence_ids=['e.assignment'])
    f, p = open_frame(raw, ADAPTER_KEY, 'to_peer')
    assert f['kind'] == 'command' and p == action
    assert env[1].get('proposal.1')['consumed_at_ms'] is not None
    with pytest.raises(ContractViolation):
        env[0].dispatch(peer.sid, action, evidence_ids=['e.assignment'])
    assert peer.send('result', result(env))['kind'] == 'result'
    assert env[1].records.get('ActionReceipt', 'receipt.1') is None
    with pytest.raises(ContractViolation):
        peer.send('result', result(env))


def test_forged_exact_approval_never_consumes(env):
    p = decision(env)
    action = dict(schema_version='1.0', intent=p['intent'], approval=dict(proposal_id='proposal.1', authority='app',
        approved_at_ms=env[4][0], expires_at_ms=p['expires_at_ms'], intent=p['intent']))
    with pytest.raises(ContractViolation):
        env[0].dispatch(Peer(env[0]).sid, action, evidence_ids=['e.assignment'])
    assert env[1].get('proposal.1') is None


@pytest.mark.parametrize('change', [dict(private_context=True), dict(private_context=None), dict(enabled=False),
    dict(background=False), dict(profile_id='profile.other'), dict(allowed_origins=frozenset()),
    dict(url='https://other.example.invalid/')])
@pytest.mark.parametrize('operation', ['observation', 'command'])
def test_independent_native_context_gates_data_and_commands(env, change, operation):
    peer = Peer(env[0])
    env[3][0] = replace(env[3][0], **change)
    with pytest.raises(ContractViolation):
        if operation == 'observation':
            peer.send('observation', env[2]['SourceObservation'])
        else:
            env[0].dispatch(peer.sid, read_action(env), evidence_ids=[])


def test_unknown_context_denies(env):
    env[3][0] = None
    with pytest.raises(ContractViolation):
        Peer(env[0]).send('observation', env[2]['SourceObservation'])


@pytest.mark.parametrize('changes', [dict(private_context=True), dict(source_kind='mail'),
    dict(source_url='https://other.example.invalid/'), dict(unknown='secret')])
def test_hostile_observation_denied(env, changes):
    with pytest.raises(ContractViolation):
        Peer(env[0]).send('observation', dict(env[2]['SourceObservation'], **changes))


def test_snapshot_scope_and_stale_command(env):
    peer = Peer(env[0])
    snap = dict(schema_version='1.0', id='snapshot.1', task_id='task.1', observation_id='obs.assignment',
        url=env[3][0].url, origin='https://school.example.invalid', captured_at_ms=env[4][0], enabled=True,
        site_permission='granted', private_context=False, tab_role='background', content_mode='dom_text', elements=[])
    assert peer.send('snapshot', snap)['payload'] == snap
    with pytest.raises(ContractViolation):
        env[0].dispatch(peer.sid, read_action(env, snapshot_id='old'), evidence_ids=[])
    assert not env[0]._sessions[peer.sid].pending
    with pytest.raises(ContractViolation):
        peer.send('snapshot', dict(snap, id='other'))


def test_replay_and_concurrent_duplicate_are_rejected(env):
    peer = Peer(env[0])
    raw = peer.frame('observation', env[2]['SourceObservation'])
    def submit():
        try:
            env[0].receive(raw)
            return True
        except ContractViolation:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: submit(), range(2))) == [False, True]


def test_bad_mac_cannot_close_legitimate_channel(env):
    peer = Peer(env[0])
    raw = peer.frame('observation', env[2]['SourceObservation'])
    frame = loads(raw)
    frame['mac'] = '0' * 64
    with pytest.raises(ContractViolation):
        env[0].receive(dumps(frame))
    assert env[0].receive(raw)['kind'] == 'observation'


def test_revocation_invalidates_session_and_forbids_key_reuse(env):
    peer = Peer(env[0])
    env[0].dispatch(peer.sid, read_action(env), evidence_ids=[])
    assert env[0].revoke('adapter') == ['action.1']
    with pytest.raises(ContractViolation):
        peer.send('observation', env[2]['SourceObservation'])
    with pytest.raises(ContractViolation):
        env[0].challenge('adapter')
    with pytest.raises(ContractViolation):
        env[0].provision(BridgeIdentity('adapter', 'extension', 'native_bridge', 'profile.1'), bytes([8]) * 32)
    with pytest.raises(ContractViolation):
        env[0].provision(BridgeIdentity('other', 'app', 'app_approval', 'profile.1'), ADAPTER_KEY)


def test_reconnect_and_disconnect_report_uncertainty_no_replay(env):
    old = Peer(env[0])
    env[0].dispatch(old.sid, read_action(env), evidence_ids=[])
    new = Peer(env[0])
    assert new.registered['uncertain_actions'] == ['action.1']
    assert not env[0]._sessions[new.sid].pending
    with pytest.raises(ContractViolation):
        old.send('result', result(env, proposal_id=None))
    env[0].dispatch(new.sid, read_action(env, action_id='action.2'), evidence_ids=[])
    assert new.send('disconnect', {})['uncertain_actions'] == ['action.2']


def test_expired_challenge_and_unauthenticated_registration_do_not_evict(env):
    peer = Peer(env[0])
    challenge = env[0].challenge('adapter')
    f, h = open_frame(challenge, ADAPTER_KEY, 'to_peer')
    env[5][0] += 31
    with pytest.raises(ContractViolation):
        env[0].receive(seal(ADAPTER_KEY, 'adapter', f['session_id'], 0, 'register',
            dict(handshake=dict(h, peer='extension', credential_role='native_bridge'),
                 client_nonce=secrets.token_hex(32)), 'to_service'))
    assert peer.send('observation', env[2]['SourceObservation'])['kind'] == 'observation'


@pytest.mark.parametrize('change', [dict(task_id='other'), dict(action_id='other'), dict(proposal_id='other'),
                                   dict(completes_obligation=True)])
def test_result_must_match_pending_peer_action_and_never_complete(env, change):
    peer = Peer(env[0])
    env[0].dispatch(peer.sid, approved_action(env), evidence_ids=['e.assignment'])
    payload = result(env)
    payload.update(change)
    with pytest.raises(ContractViolation):
        peer.send('result', payload)
    assert env[1].records.get('ActionReceipt', 'receipt.1') is None


def test_other_peer_cannot_supply_result(env):
    bridge = env[0]
    peer = Peer(bridge)
    bridge.dispatch(peer.sid, read_action(env), evidence_ids=[])
    identity = BridgeIdentity('other', 'extension', 'native_bridge', 'profile.1')
    bridge.provision(identity, bytes([11]) * 32)
    handshake = dict(bridge._handshake(identity), peer='extension', credential_role='native_bridge')
    with pytest.raises(ContractViolation):
        Peer(bridge, 'other', bytes([11]) * 32, handshake).send('result', result(env, proposal_id=None))
    assert 'action.1' in bridge._sessions[peer.sid].pending


def signed_payload(credential, sid, sequence, kind, payload, direction='to_service', key=ADAPTER_KEY):
    frame = loads(seal(key, credential, sid, sequence, kind, {}, direction))
    frame['payload'] = base64.b64encode(payload).decode()
    body = '\n'.join(['wisp-browser-bridge/1', direction, credential, sid, str(sequence), kind, frame['payload']]).encode()
    frame['mac'] = hmac.new(key, body, hashlib.sha256).hexdigest()
    return dumps(frame)


@pytest.mark.parametrize('payload', [b'{"secret":"synthetic-private","secret":2}', b'{"x":',
                                    b'{"x":' + b'[' * 40 + b'0' + b']' * 40 + b'}'])
def test_authenticated_bad_json_closes_with_uncertain_ids(env, payload):
    peer = Peer(env[0])
    env[0].dispatch(peer.sid, read_action(env), evidence_ids=[])
    raw = signed_payload('adapter', peer.sid, peer.seq, 'result', payload)
    with pytest.raises(BridgeSessionClosed) as error:
        env[0].receive(raw)
    assert error.value.uncertain_actions == ('action.1',)
    assert 'synthetic-private' not in str(error.value)
    assert peer.sid not in env[0]._sessions


def test_invalid_result_and_transport_eof_preserve_uncertain_ids(env):
    peer = Peer(env[0])
    env[0].dispatch(peer.sid, read_action(env), evidence_ids=[])
    with pytest.raises(BridgeSessionClosed) as error:
        peer.send('result', result(env, proposal_id=None, task_id='other'))
    assert error.value.uncertain_actions == ('action.1',)
    new = Peer(env[0])
    env[0].dispatch(new.sid, read_action(env), evidence_ids=[])
    assert env[0].close(new.sid) == ['action.1']
    assert env[0].close(new.sid) == []


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":{"x":1,"\\u0078":2}}', b'{"a":NaN}',
    b'\xff', b'[' * 2000 + b']' * 2000, b' ' * (MAX_FRAME + 1)],
    ids=['duplicate', 'escaped-duplicate', 'nan', 'utf8', 'depth', 'size'])
def test_malformed_json_fails_without_echoing_content(raw):
    with pytest.raises(ContractViolation) as error:
        loads(raw)
    assert len(str(error.value)) < 80


def test_tamper_direction_unknown_field_and_payload_bound(env):
    raw = seal(ADAPTER_KEY, 'adapter', 's', 0, 'observation', env[2]['SourceObservation'], 'to_service')
    for changed in [dict(loads(raw), kind='decision'), dict(loads(raw), extra=True),
                    dict(loads(raw), sequence=True), dict(loads(raw), payload='?')]:
        with pytest.raises(ContractViolation):
            open_frame(dumps(changed), ADAPTER_KEY, 'to_service')
    with pytest.raises(ContractViolation):
        open_frame(raw, ADAPTER_KEY, 'to_peer')
    with pytest.raises(ContractViolation):
        seal(ADAPTER_KEY, 'adapter', 's', 0, 'observation', {'text': 'x' * 140000}, 'to_service')


@pytest.fixture(scope='session')
def swift_binary(tmp_path_factory):
    if sys.platform != 'darwin':
        pytest.skip('Swift native client requires macOS')
    out = tmp_path_factory.mktemp('bridge-swift')
    subprocess.run(['swiftc', '-swift-version', '5', '-parse-as-library', '-module-cache-path', str(out / 'cache'),
        str(ROOT / 'app/Sources/WispApp/BrowserContracts.swift'), str(ROOT / 'app/Sources/WispApp/BrowserBridge.swift'),
        str(ROOT / 'app/Sources/WispApp/BrowserBridgeCredentials.swift'),
        str(ROOT / 'test_fixtures/browser_bridge/Harness.swift'), '-o', str(out / 'harness')], check=True, capture_output=True)
    return out / 'harness'


@pytest.fixture
def swift(swift_binary):
    process = subprocess.Popen([str(swift_binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def call(op, **args):
        process.stdin.write(json.dumps(dict(op=op, **args)) + '\n')
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 15)[0], 'Swift fixture response timeout'
        return json.loads(process.stdout.readline())
    try:
        yield call
    finally:
        process.stdin.close()
        try:
            assert process.wait(timeout=10) == 0
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise


def swift_register(swift, env, role='native_bridge', **options):
    assert swift('reset', role=role, **options)['ok']
    cid = 'adapter' if role == 'native_bridge' else 'approval'
    challenge = env[0].challenge(cid)
    response = swift('register', data=base64.b64encode(challenge).decode())
    assert response['ok']
    registered = env[0].receive(base64.b64decode(response['data']))
    assert swift('receive', data=base64.b64encode(registered['response']).decode()) == dict(ok=True, kind='registered')
    return inspect(challenge)['session_id']


def test_swift_keychain_queries_keep_data_protection_and_role_storage_private(swift):
    # Inspect pure query configuration only; no Security Keychain API is called.
    response = swift('credential_queries')
    assert response['ok']
    assert response['operations'] == [dict(data_protection=True, no_sync=True, account_bound=True,
                                          device_only=True, no_prompt=True)] * 3


def test_swift_backend_roundtrip_capture_command_result_disconnect(env, swift):
    sid = swift_register(swift, env)
    response = swift('publish', kind='observation', payload=env[2]['SourceObservation'])
    assert env[0].receive(base64.b64decode(response['data']))['kind'] == 'observation'
    command = env[0].dispatch(sid, approved_action(env), evidence_ids=['e.assignment'])
    assert swift('receive', data=base64.b64encode(command).decode()) == dict(ok=True, kind='command')
    response = swift('publish', kind='result', payload=result(env))
    assert env[0].receive(base64.b64decode(response['data']))['kind'] == 'result'
    response = swift('disconnect')
    assert env[0].receive(base64.b64decode(response['data']))['kind'] == 'disconnect'
    assert not swift('publish', kind='observation', payload=env[2]['SourceObservation'])['ok']


def test_swift_app_decision_and_adapter_cannot_decide(env, swift):
    swift_register(swift, env)
    assert not swift('decide', payload=decision(env))['ok']
    assert env[1].get('proposal.1') is None
    swift_register(swift, env, role='app_approval')
    response = swift('decide', payload=decision(env))
    assert env[0].receive(base64.b64decode(response['data']))['kind'] == 'decision'
    assert env[1].get('proposal.1')['decision'] == 'approved'


def test_swift_native_private_context_rejects_capture_and_closes(env, swift):
    swift_register(swift, env, **{'private': True})
    assert not swift('publish', kind='observation', payload=env[2]['SourceObservation'])['ok']
    assert not swift('disconnect')['ok']


def test_swift_rejects_replayed_command(env, swift):
    sid = swift_register(swift, env)
    raw = env[0].dispatch(sid, read_action(env), evidence_ids=[])
    encoded = base64.b64encode(raw).decode()
    assert swift('receive', data=encoded)['ok']
    assert swift('receive', data=encoded) == dict(ok=False, uncertain=['action.1'])


@pytest.mark.parametrize('op', ['disconnect', 'close'])
def test_swift_transport_close_reports_uncertainty(env, swift, op):
    sid = swift_register(swift, env)
    raw = env[0].dispatch(sid, read_action(env), evidence_ids=[])
    assert swift('receive', data=base64.b64encode(raw).decode())['ok']
    response = swift(op)
    assert response['ok'] and response['uncertain'] == ['action.1']
    if op == 'disconnect':
        assert env[0].receive(base64.b64decode(response['data']))['uncertain_actions'] == ['action.1']


def test_swift_reset_rejects_old_service_transcript(env, swift):
    assert swift('reset')['ok']
    challenge = env[0].challenge('adapter')
    registration = swift('register', data=base64.b64encode(challenge).decode())
    first = open_frame(base64.b64decode(registration['data']), ADAPTER_KEY, 'to_service')[1]
    event = env[0].receive(base64.b64decode(registration['data']))
    agreement = base64.b64encode(event['response']).decode()
    assert swift('receive', data=agreement)['ok']
    command = env[0].dispatch(inspect(challenge)['session_id'], read_action(env), evidence_ids=[])
    encoded_command = base64.b64encode(command).decode()
    assert swift('receive', data=encoded_command)['ok']
    assert swift('reset')['ok']
    registration = swift('register', data=base64.b64encode(challenge).decode())
    assert registration['ok']
    second = open_frame(base64.b64decode(registration['data']), ADAPTER_KEY, 'to_service')[1]
    assert first['client_nonce'] != second['client_nonce']
    assert not swift('receive', data=agreement)['ok']
    assert not swift('receive', data=encoded_command)['ok']


@pytest.mark.parametrize('payload', [b'{"a":1,"a":2}', b'{"a":{"x":1,"\\u0078":2}}', b'{"x":NaN}',
                                     b'{"x":' + b'[' * 40 + b'0' + b']' * 40 + b'}'])
def test_swift_rejects_signed_malformed_json(swift, payload):
    frame = loads(seal(ADAPTER_KEY, 'adapter', 's', 0, 'challenge', {}, 'to_peer'))
    frame['payload'] = base64.b64encode(payload).decode()
    body = '\n'.join(['wisp-browser-bridge/1', 'to_peer', 'adapter', 's', '0', 'challenge', frame['payload']]).encode()
    frame['mac'] = hmac.new(ADAPTER_KEY, body, hashlib.sha256).hexdigest()
    assert not swift('verify', data=base64.b64encode(dumps(frame)).decode())['ok']


@pytest.mark.parametrize('levels,accepted', [(31, True), (32, True), (33, False), (2000, False)])
def test_python_swift_depth_boundary(swift, levels, accepted):
    payload = b'{"x":' + b'[' * (levels - 1) + b'0' + b']' * (levels - 1) + b'}'
    if accepted:
        loads(payload)
    else:
        with pytest.raises(ContractViolation):
            loads(payload)
    frame = loads(seal(ADAPTER_KEY, 'adapter', 's', 0, 'challenge', {}, 'to_peer'))
    frame['payload'] = base64.b64encode(payload).decode()
    body = '\n'.join(['wisp-browser-bridge/1', 'to_peer', 'adapter', 's', '0', 'challenge', frame['payload']]).encode()
    frame['mac'] = hmac.new(ADAPTER_KEY, body, hashlib.sha256).hexdigest()
    assert swift('verify', data=base64.b64encode(dumps(frame)).decode())['ok'] is accepted


@pytest.mark.parametrize('mac', [
    'a' * 64 + '\n', 'a' * 64 + '\r', 'a' * 64 + '\r\n',
    'a' * 64 + '\u0085', 'a' * 64 + '\u2028', 'a' * 64 + '\u2029',
    '', 'a' * 63, 'a' * 65, 'a' * 128, '\n' + 'a' * 63,
    'a' * 31 + '\n' + 'a' * 32, 'a' * 63 + '\x00', 'a' * 63 + ' ',
    'A' * 64, 'g' * 64, 'ａ' * 64, 'a' * 63 + 'é', None, 64, [], {},
], ids=['trailing-lf', 'trailing-cr', 'trailing-crlf', 'trailing-nel', 'trailing-ls', 'trailing-ps',
        'empty', 'short', 'odd-long', 'double-length', 'leading-lf', 'embedded-lf', 'nul',
        'space', 'uppercase', 'nonhex', 'fullwidth', 'unicode', 'null', 'number', 'array', 'object'])
def test_python_swift_malformed_mac_rejects_without_crashing(swift, mac):
    valid = seal(ADAPTER_KEY, 'adapter', 's', 0, 'challenge', {}, 'to_peer')
    malformed = dumps(dict(inspect(valid), mac=mac))
    with pytest.raises(ContractViolation) as error:
        open_frame(malformed, ADAPTER_KEY, 'to_peer')
    assert error.value.code == 'invalid_payload'
    # invalid_payload proves shape rejection, rather than a MAC comparison.
    assert swift('verify', data=base64.b64encode(malformed).decode()) == dict(ok=False, code='invalid_payload')
    # Use the same process to prove malformed input returned instead of trapping.
    assert swift('verify', data=base64.b64encode(valid).decode()) == dict(ok=True)


def test_swift_malformed_mac_during_registration_fails_closed(env, swift):
    assert swift('reset')['ok']
    challenge = loads(env[0].challenge('adapter'))
    challenge['mac'] += '\n'
    assert swift('register', data=base64.b64encode(dumps(challenge)).decode()) == dict(ok=False, uncertain=[])
    assert not swift('disconnect')['ok']


def test_swift_malformed_mac_preserves_pending_uncertainty(env, swift):
    sid = swift_register(swift, env)
    command = env[0].dispatch(sid, read_action(env), evidence_ids=[])
    assert swift('receive', data=base64.b64encode(command).decode())['ok']
    frame = loads(command)
    frame['mac'] += '\n'
    assert swift('receive', data=base64.b64encode(dumps(frame)).decode()) == dict(ok=False, uncertain=['action.1'])
    assert swift('close') == dict(ok=True, uncertain=[])

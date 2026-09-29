"""A10 WP3 service endpoint and native activation checks.

Synthetic keys and disposable SQLite only. No browser, Keychain, installed app,
user state, network, bound socket or effect is touched, so this module runs in
the generic no-network Simulation sandbox (socketpair only). The Swift cases
compile the actual app sources with a fixture harness and answer its backend
control calls with the real BridgeHost over pipes. Real bound sockets and signed
peers are qualified separately in tests/test_browser_transport_native.py.
"""
import base64
import copy
import json
import os
from pathlib import Path
import secrets
import select
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

import pytest

from service.assistant.store import AssistantStore
from service.browser import host as bridge_host
from service.browser.bridge_protocol import dumps, open_frame, seal
from service.browser.host import BridgeHost, ControlLink, NativeContextStore, control_loads
from service.browser.contracts import ContractViolation

ROOT = Path(__file__).resolve().parents[1]
SOCKET_ROOT = os.environ.get('BROWSER_BRIDGE_SOCKET_ROOT', '/private/tmp')
OBSERVATION = json.loads((ROOT / 'test_fixtures/discovery_storage/lifecycle.json').read_text())['SourceObservation']
URL = OBSERVATION['source_url']
ORIGIN = 'https://school.example.invalid'
CHROME = ('chrome-a1', 'chrome:default', bytes([1]) * 32)
SAFARI = ('safari-b2', 'safari:default', bytes([2]) * 32)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def host(tmp_path, clock):
    assistant = AssistantStore(tmp_path / 'host.db')
    yield BridgeHost(assistant, clock=clock)
    assistant._db.close()


def provision(host, spec=CHROME, **override):
    cid, pid, key = spec
    message = dict(op='provision', credential_id=cid, peer='extension', credential_role='native_bridge',
                   profile_id=pid, key=key.hex())
    message.update(override)
    return host.handle(message)


def grant(host, spec=CHROME, *, private=False, enabled=True, trigger='user_click', ttl=30000, url=URL,
          origins=(ORIGIN,)):
    return host.handle(dict(op='context', profile_id=spec[1], enabled=enabled, private_context=private,
                            trigger=trigger, allowed_origins=list(origins), url=url, ttl_ms=ttl))


class Peer:
    """A synthetic signed-peer relay: frames are sealed with the provisioned key."""
    counter = [0]

    def __init__(self, host, spec=CHROME, register=True):
        self.host, self.cid, self.key = host, spec[0], spec[2]
        Peer.counter[0] += 1
        self.conn = Peer.counter[0]
        reply = host.handle(dict(op='peer_open', conn=self.conn, credential_id=self.cid))
        assert reply['ok'], reply
        frame, self.server = open_frame(base64.b64decode(reply['frames'][0]), self.key, 'to_peer')
        self.sid, self.seq = frame['session_id'], 0
        if register:
            self.registered = self.send('register', dict(handshake=dict(self.server, peer='extension',
                credential_role='native_bridge'), client_nonce=secrets.token_hex(32)))
            assert self.registered['ok'] and self.registered['event'] == 'registered', self.registered

    def send(self, kind, payload, sid=None):
        raw = seal(self.key, self.cid, sid or self.sid, self.seq, kind, payload, 'to_service')
        self.seq += 1
        return self.host.handle(dict(op='peer_frame', conn=self.conn, frame=base64.b64encode(raw).decode()))


def test_registered_peer_with_native_grant_delivers_one_observation(host):
    assert provision(host)['ok']
    peer = Peer(host)
    assert grant(host)['ok']
    reply = peer.send('observation', OBSERVATION)
    assert reply == dict(ok=True, event='observation', closed=False, frames=[], uncertain_actions=[])
    items = host.inbox.drain()
    assert [i['observation']['id'] for i in items] == [OBSERVATION['id']]
    assert items[0]['profile_id'] == CHROME[1]
    # The grant was one click. A second read on the still-open session is denied.
    reply = peer.send('observation', dict(OBSERVATION, id='obs.second'))
    assert reply['ok'] is False and reply['closed'] is True and reply['code'] == 'bridge_unauthorized'
    assert len(host.inbox) == 0


def test_unknown_or_expired_native_context_denies(host, clock):
    provision(host)
    peer = Peer(host)
    reply = peer.send('observation', OBSERVATION)
    assert (reply['ok'], reply['closed'], reply['code']) == (False, True, 'bridge_unauthorized')
    peer = Peer(host)
    grant(host, ttl=1000)
    clock.now += 1.0
    reply = peer.send('observation', OBSERVATION)
    assert (reply['ok'], reply['closed']) == (False, True)
    assert len(host.inbox) == 0


@pytest.mark.parametrize('override,code', [
    (dict(private=True), 'private_context'),
    (dict(enabled=False), 'disabled'),
    # The observation must be for the page the native side named; anything else is refused.
    (dict(url='https://other.example.invalid/x', origins=('https://other.example.invalid',)),
     'invalid_payload')])
def test_native_context_flags_deny_each_capture(host, override, code):
    provision(host)
    peer = Peer(host)
    assert grant(host, **override)['ok']
    reply = peer.send('observation', OBSERVATION)
    assert (reply['ok'], reply['closed'], reply['code']) == (False, True, code)
    assert len(host.inbox) == 0


def test_sender_private_claim_and_false_claim_never_grant(host):
    provision(host)
    peer = Peer(host)
    grant(host)
    reply = peer.send('observation', dict(OBSERVATION, private_context=True))
    assert (reply['ok'], reply['closed']) == (False, True) and len(host.inbox) == 0
    # A sender saying false does not replace a native private-context denial.
    peer = Peer(host)
    grant(host, private=True)
    assert peer.send('observation', dict(OBSERVATION, private_context=False))['ok'] is False


def test_context_cannot_be_supplied_by_a_browser_frame(host):
    provision(host)
    peer = Peer(host)
    for payload in (dict(OBSERVATION, url=URL), dict(profile_id=CHROME[1], enabled=True)):
        raw = seal(peer.key, peer.cid, peer.sid, peer.seq, 'observation', payload, 'to_service')
        reply = host.handle(dict(op='peer_frame', conn=peer.conn, frame=base64.b64encode(raw).decode()))
        assert reply['ok'] is False and reply['closed'] is True
        peer = Peer(host)
    forged = copy.deepcopy(dict(op='peer_frame', conn=peer.conn, frame=base64.b64encode(
        dumps(dict(version='1', credential_id=peer.cid, session_id=peer.sid, sequence=0, kind='context',
                   payload='e30=', mac='0' * 64))).decode()))
    assert host.handle(forged)['ok'] is False
    assert len(host.inbox) == 0


@pytest.mark.parametrize('kind', ['snapshot', 'result'])
def test_only_observations_are_accepted_from_peers(host, kind):
    provision(host)
    peer = Peer(host)
    grant(host)
    reply = peer.send(kind, dict(schema_version='1.0'))
    assert reply['ok'] is False and reply['closed'] is True
    assert len(host.inbox) == 0
    assert host.handle(dict(op='peer_frame', conn=peer.conn, frame='e30='))['ok'] is False


def test_no_approval_authority_or_second_role_can_be_provisioned(host):
    for override in (dict(peer='app', credential_role='app_approval'),
                     dict(peer='app', credential_role='native_bridge'),
                     dict(peer='extension', credential_role='app_approval')):
        assert provision(host, **override) == dict(ok=False, code='bridge_unauthorized')
    assert host.handle(dict(op='decision', proposal_id='p'))['ok'] is False
    assert host.handle(dict(op='hello'))['credentials'] == []


@pytest.mark.parametrize('override', [dict(key='0' * 63), dict(key='G' * 64), dict(key=b'x' * 32),
                                      dict(profile_id='safari:default'), dict(credential_id='chrome-a1 '),
                                      dict(profile_id='chrome:bad profile'), dict(extra='x')])
def test_provisioning_is_closed_and_browser_ids_must_agree(host, override):
    assert provision(host, **override)['ok'] is False
    assert host.handle(dict(op='hello'))['credentials'] == []


def test_chrome_and_safari_have_separate_identities(host):
    assert provision(host, CHROME)['ok'] and provision(host, SAFARI)['ok']
    assert provision(host, ('chrome-c3', CHROME[1], bytes([3]) * 32))['ok'] is False  # one credential per profile
    chrome, safari = Peer(host, CHROME), Peer(host, SAFARI)
    grant(host, CHROME)
    # Safari has no native grant of its own, so it is denied while Chrome is accepted.
    assert safari.send('observation', OBSERVATION)['ok'] is False
    assert chrome.send('observation', OBSERVATION)['ok'] is True
    assert [i['profile_id'] for i in host.inbox.drain()] == [CHROME[1]]
    # A Chrome connection cannot carry Safari's credential.
    reply = host.handle(dict(op='peer_open', conn=999, credential_id=SAFARI[0]))
    assert reply['ok'] is True
    other = Peer(host, CHROME)
    raw = seal(SAFARI[2], SAFARI[0], other.sid, 0, 'register', {}, 'to_service')
    assert host.handle(dict(op='peer_frame', conn=other.conn, frame=base64.b64encode(raw).decode()))['ok'] is False


def test_relay_cannot_steer_a_frame_into_another_session(host):
    provision(host)
    first, second = Peer(host, register=False), Peer(host, register=False)
    raw = seal(first.key, first.cid, first.sid, 0, 'register', {}, 'to_service')
    reply = host.handle(dict(op='peer_frame', conn=second.conn, frame=base64.b64encode(raw).decode()))
    assert (reply['ok'], reply['closed']) == (False, True)


def test_revoke_closes_sessions_and_clears_context(host):
    provision(host)
    peer = Peer(host)
    grant(host)
    assert host.handle(dict(op='revoke', credential_id=CHROME[0])) == dict(ok=True, uncertain_actions=[])
    assert peer.send('observation', OBSERVATION)['ok'] is False
    assert host.handle(dict(op='peer_open', conn=5000, credential_id=CHROME[0]))['ok'] is False
    assert host.handle(dict(op='revoke', credential_id='chrome-unknown'))['ok'] is True
    assert host.handle(dict(op='hello'))['credentials'] == []
    # The bridge never reissues an ID, so the app must rotate to a fresh credential.
    assert provision(host)['ok'] is False
    assert provision(host, ('chrome-d4', CHROME[1], bytes([4]) * 32))['ok'] is True
    assert len(host.inbox) == 0


def test_context_requires_a_provisioned_profile_and_is_bounded(host):
    assert grant(host)['ok'] is False
    provision(host)
    for override in (dict(ttl=0), dict(ttl=60001), dict(origins=()), dict(url='ftp://x.invalid/'), dict(trigger='auto'),
                     dict(trigger='background'),
                     dict(url=URL + 'é'), dict(origins=('https://school.example.invalid/x',)),
                     dict(url='https://school.example.invalid@evil.invalid/', origins=('https://school.example.invalid',))):
        assert grant(host, **override)['ok'] is False
    assert host.handle(dict(op='context', profile_id=CHROME[1]))['ok'] is False
    assert grant(host)['ok'] is True


def test_reset_fails_closed_without_reusing_credentials(host):
    provision(host)
    Peer(host)
    grant(host)
    host.reset()
    assert host.handle(dict(op='hello'))['credentials'] == []
    assert host.contexts.provider(type('I', (), dict(profile_id=CHROME[1]))) is None
    assert provision(host)['ok'] is False


def test_control_messages_are_closed_and_errors_are_sanitized(host):
    for raw in (b'', b'[]' * 4, b'{"op":"hello","op":"hello"}', b'{"op":NaN}', b'\xff', b'x' * (2 ** 20 + 1)):
        with pytest.raises(ContractViolation):
            control_loads(raw)
    for message in (None, [], {}, {'op': 'nope'}, {'op': 'peer_frame', 'conn': True, 'frame': ''},
                    {'op': 'peer_frame', 'conn': 1, 'frame': 'e30='}, {'op': 'hello', 'extra': 1}):
        reply = host.handle(message)
        assert set(reply) <= {'ok', 'code'} and reply['ok'] is False
    secret = 'PRIVATE-SENTINEL'
    reply = host.handle(dict(op='provision', credential_id=secret, peer='extension',
                             credential_role='native_bridge', profile_id='chrome:x', key=secret))
    assert secret not in json.dumps(reply)


def test_inbox_is_bounded_and_in_memory(host):
    provision(host)
    for i in range(bridge_host.MAX_INBOX + 4):
        peer = Peer(host)
        grant(host)
        assert peer.send('observation', dict(OBSERVATION, id=f'obs.{i}'))['ok']
        assert host.handle(dict(op='peer_close', conn=peer.conn))['ok']
    ids = [i['observation']['id'] for i in host.inbox.drain()]
    assert len(ids) == bridge_host.MAX_INBOX and ids[-1] == f'obs.{bridge_host.MAX_INBOX + 3}'


def test_native_context_store_expires_and_is_native_only(clock):
    store = NativeContextStore(clock)
    message = dict(op='context', profile_id='chrome:default', enabled=True, private_context=False,
                   trigger='user_click', allowed_origins=[ORIGIN], url=URL, ttl_ms=500)
    store.grant(message)
    identity = type('I', (), dict(profile_id='chrome:default'))
    assert store.provider(identity).url == URL
    clock.now += 0.5
    assert store.provider(identity) is None




# -- control link (socketpair: no bound socket, so it runs in the generic sandbox) ----
def call(conn, message, raw=None):
    payload = raw if raw is not None else json.dumps(message).encode()
    conn.sendall(struct.pack('>I', len(payload)) + payload)
    header = b''
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        assert chunk, 'control link closed'
        header += chunk
    size = struct.unpack('>I', header)[0]
    data = b''
    while len(data) < size:
        data += conn.recv(size - len(data))
    return json.loads(data)


def link_to(host, *, expected_pid=None):
    """A ControlLink whose connector hands out one end of a socketpair (the 'app')."""
    app, backend = socket.socketpair()
    app.settimeout(10)
    ends = [backend]
    link = ControlLink(host, '/private/tmp/a10-none/control.sock', expected_pid=expected_pid or os.getpid(),
                       poll_seconds=0.05, connector=lambda: ends.pop() if ends else None)
    link.start()
    return link, app


def wait_until(predicate, seconds=5):
    deadline = time.time() + seconds
    while not predicate() and time.time() < deadline:
        time.sleep(0.02)
    return predicate()


def test_control_link_serves_only_its_launching_app_and_fails_closed_on_loss(host):
    link, app = link_to(host)
    try:
        assert call(app, dict(op='hello')) == dict(ok=True, credentials=[])
        assert call(app, dict(op='provision', credential_id=CHROME[0], peer='extension',
                              credential_role='native_bridge', profile_id=CHROME[1], key=CHROME[2].hex()))['ok']
        assert call(app, dict(op='hello'))['credentials'] == [CHROME[0]]
        app.close()
        # Losing the app fails closed: the backend forgets every credential.
        assert wait_until(lambda: host.handle(dict(op='hello'))['credentials'] == [])
    finally:
        link.stop()


def test_control_link_refuses_a_process_that_is_not_the_launching_app(host):
    link, app = link_to(host, expected_pid=os.getpid() + 1)
    try:
        assert wait_until(lambda: select.select([app], [], [], 0.05)[0])
        assert app.recv(4) == b''
        assert host.handle(dict(op='hello'))['credentials'] == []
    finally:
        link.stop()


@pytest.mark.parametrize('size', [0, bridge_host.MAX_CONTROL + 1, 2 ** 32 - 1])
def test_control_link_closes_on_a_bad_frame_length(host, size):
    link, app = link_to(host)
    try:
        app.sendall(struct.pack('>I', size))
        assert wait_until(lambda: select.select([app], [], [], 0.05)[0])
        assert app.recv(4) == b''
    finally:
        link.stop()


def test_control_link_answers_malformed_json_without_effect(host):
    link, app = link_to(host)
    try:
        for raw in (b'{"op":"hello","op":"hello"}', b'[]', b'{"op":"provision"}', b'\xff\xfe'):
            reply = call(app, None, raw=raw)
            assert reply['ok'] is False and set(reply) == {'ok', 'code'}
        assert call(app, dict(op='hello')) == dict(ok=True, credentials=[])
    finally:
        link.stop()


def test_no_endpoint_means_no_activity_and_nothing_is_created(host):
    directory = '/private/tmp/a10-never-created'
    assert not os.path.exists(directory)
    link = ControlLink(host, directory + '/control.sock', expected_pid=os.getpid(), poll_seconds=0.02)
    link.start()
    time.sleep(0.3)
    link.stop()
    # The backend never listens, binds or creates anything: it only checks for an endpoint.
    assert not os.path.exists(directory)
    assert bridge_host.start_from_environment(None, {}) is None


def fake_stat(*, mode, uid=None):
    uid = os.getuid() if uid is None else uid
    return os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))


@pytest.mark.parametrize('directory,target,accepted', [
    (fake_stat(mode=stat.S_IFDIR | 0o700), fake_stat(mode=stat.S_IFSOCK | 0o600), True),
    (fake_stat(mode=stat.S_IFDIR | 0o755), fake_stat(mode=stat.S_IFSOCK | 0o600), False),
    (fake_stat(mode=stat.S_IFDIR | 0o700), fake_stat(mode=stat.S_IFSOCK | 0o666), False),
    (fake_stat(mode=stat.S_IFDIR | 0o700), fake_stat(mode=stat.S_IFREG | 0o600), False),
    (fake_stat(mode=stat.S_IFLNK | 0o700), fake_stat(mode=stat.S_IFSOCK | 0o600), False),
    (fake_stat(mode=stat.S_IFDIR | 0o700, uid=os.getuid() + 1), fake_stat(mode=stat.S_IFSOCK | 0o600), False),
    (fake_stat(mode=stat.S_IFDIR | 0o700), fake_stat(mode=stat.S_IFSOCK | 0o600, uid=os.getuid() + 1), False)])
def test_only_an_owner_only_endpoint_in_an_owner_only_directory_is_used(host, monkeypatch, directory, target,
                                                                        accepted):
    link = ControlLink(host, '/private/tmp/a10-none/control.sock', expected_pid=os.getpid())
    monkeypatch.setattr(os, 'lstat', lambda path: directory if path.endswith('a10-none') else target)
    assert link._socket_is_private() is accepted


def test_start_from_environment_requires_the_launching_parent(tmp_path, monkeypatch):
    assistant = AssistantStore(tmp_path / 'env.db')
    try:
        assert bridge_host.start_from_environment(assistant, {}) is None
        monkeypatch.setattr(os, 'getppid', lambda: 1)
        assert bridge_host.start_from_environment(assistant, {bridge_host.CONTROL_ENV: '/private/tmp/x/c.sock'}) is None
        monkeypatch.undo()
        for bad in ('relative/c.sock', '/private/tmp/' + 'x' * 120 + '/c.sock', ''):
            assert bridge_host.start_from_environment(assistant, {bridge_host.CONTROL_ENV: bad}) is None
        link = bridge_host.start_from_environment(assistant, {bridge_host.CONTROL_ENV: '/private/tmp/a10-none/c.sock'})
        assert link is not None and link.expected_pid == os.getppid()
        link.stop()
    finally:
        assistant._db.close()


# -- native activation: the actual Swift sources, real BridgeHost, pipes only ---------
@pytest.fixture(scope='session')
def activation_binary(tmp_path_factory):
    if sys.platform != 'darwin':
        pytest.skip('The native app sources require macOS')
    out = tmp_path_factory.mktemp('bridge-activation')
    binary = out / 'activation'
    app = ROOT / 'app/Sources/WispApp'
    subprocess.run(['swiftc', '-swift-version', '5', '-parse-as-library', '-module-cache-path', str(out / 'cache'),
                    str(app / 'BrowserContracts.swift'), str(app / 'BrowserBridge.swift'),
                    str(app / 'BrowserBridgeCredentials.swift'),
                    str(ROOT / 'test_fixtures/browser_bridge/ActivationHarness.swift'), '-o', str(binary)],
                   check=True, capture_output=True)
    return binary


class NativeApp:
    """Drives the Swift activation fixture and answers its backend calls with a real BridgeHost."""

    def __init__(self, binary, host):
        self.host = host
        self.controls = []
        self.proc = subprocess.Popen([str(binary), 'pipe'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)

    def run(self, **request):
        self.proc.stdin.write(json.dumps(request) + '\n')
        self.proc.stdin.flush()
        while True:
            assert select.select([self.proc.stdout], [], [], 30)[0], 'activation fixture stalled'
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            message = json.loads(line)
            if 'control' in message:
                self.controls.append(message['control'])
                self.proc.stdin.write(json.dumps(self.host.handle(message['control'])) + '\n')
                self.proc.stdin.flush()
                continue
            assert message['ok'] is True, message
            return message

    def setup(self, requirements=('chrome', 'safari'), enabled=()):
        return self.run(op='setup', requirements=list(requirements), enabled=list(enabled))

    def session(self, browser, *messages):
        return self.run(op='session', browser=browser, messages=list(messages))

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def native(activation_binary, host):
    app = NativeApp(activation_binary, host)
    yield app
    app.close()


PROFILE = 'chrome:default'


def hello(profile=PROFILE):
    return dict(op='hello', profile_id=profile)


def begin(url=URL, native_private=False, incognito=False):
    return dict(op='begin', url=url, native_private_context=native_private, extension_incognito=incognito)


def observe(payload=OBSERVATION):
    return dict(op='observation', payload=payload)


def credentials(host):
    return host.handle(dict(op='hello'))['credentials']


def enabled_chrome(native, *, profiles=(PROFILE,)):
    native.setup(enabled=())
    native.run(op='connect')
    for profile in profiles:
        assert 'error' not in native.run(op='enable', profile=profile)


def test_bridge_is_off_by_default_and_creates_nothing(native, host):
    native.setup(enabled=())
    status = native.run(op='begin')['status']
    assert (status['control_started'], status['listeners'], status['keyring'], status['enabled']) == (False, [], [], [])
    # No Keychain call, no backend traffic, nothing to connect to.
    assert status['store'] == dict(created=0, removed=0, sweeps=0, live=0)
    assert native.controls == [] and credentials(host) == []
    # A stray stored flag is not enough without a configured peer requirement.
    native.setup(requirements=(), enabled=[PROFILE])
    status = native.run(op='begin')['status']
    assert (status['control_started'], status['listeners'], status['backend_path']) == (False, [], None)
    assert native.run(op='enable', profile=PROFILE)['error'] == 'unavailable'
    assert native.controls == [] and credentials(host) == []


@pytest.mark.parametrize('bad', ['', 'chrome', 'chrome:', 'firefox:default', 'chrome:bad profile', 'chrome:' + 'x' * 200])
def test_only_valid_browser_profiles_can_be_enabled(native, bad):
    native.setup(enabled=[])
    response = native.run(op='enable', profile=bad)
    assert response['error'] == 'denied' and response['status']['enabled'] == []


def test_enabling_provisions_a_fresh_keychain_credential_and_starts_listeners(native, host):
    enabled_chrome(native)
    status = native.run(op='status')['status']
    assert status['control_started'] and status['listeners'] == ['chrome']
    assert status['keyring'] == [PROFILE] and status['enabled'] == [PROFILE]
    assert status['store'] == dict(created=1, removed=0, sweeps=1, live=1)
    [cid] = credentials(host)
    assert cid.startswith('chrome-')
    provision = [m for m in native.controls if m['op'] == 'provision'][0]
    assert (provision['peer'], provision['credential_role'], provision['profile_id']) == (
        'extension', 'native_bridge', PROFILE)
    # Only the extension role exists here: no app-approval credential is ever requested.
    assert {m['credential_role'] for m in native.controls if m['op'] == 'provision'} == {'native_bridge'}


def test_disabling_revokes_and_removes_the_credential_and_stops_listening(native, host):
    enabled_chrome(native)
    status = native.run(op='disable', profile=PROFILE)['status']
    assert credentials(host) == []
    assert (status['listeners'], status['keyring'], status['enabled'], status['control_started']) == ([], [], [], False)
    assert status['store']['live'] == 0
    assert any(m['op'] == 'revoke' for m in native.controls)
    assert native.session('chrome', hello())['sent'] == [dict(ok=False, code='disabled')]


def test_chrome_and_safari_are_separate_identities_with_separate_revocation(native, host):
    native.setup()
    native.run(op='connect')
    native.run(op='enable', profile='chrome:default')
    status = native.run(op='enable', profile='safari:default')['status']
    assert sorted(c.split('-')[0] for c in credentials(host)) == ['chrome', 'safari']
    assert status['listeners'] == ['chrome', 'safari']
    status = native.run(op='disable', profile='chrome:default')['status']
    assert [c.split('-')[0] for c in credentials(host)] == ['safari'] and status['listeners'] == ['safari']
    assert status['keyring'] == ['safari:default']


def test_a_browser_cannot_use_another_browsers_profile(native):
    enabled_chrome(native)
    reply = native.session('safari', hello(PROFILE))
    assert reply['sent'] == [dict(ok=False, code='bridge_unauthorized')]
    reply = native.session('chrome', hello('safari:default'))
    assert reply['sent'] == [dict(ok=False, code='bridge_unauthorized')]


def test_forget_revokes_and_reenabling_issues_a_new_credential(native, host):
    enabled_chrome(native)
    [first] = credentials(host)
    status = native.run(op='forget', profile=PROFILE)['status']
    assert credentials(host) == [] and status['enabled'] == [] and status['keyring'] == []
    assert native.session('chrome', hello())['sent'] == [dict(ok=False, code='disabled')]
    # Turning the last profile off closed the endpoint, so the backend link is new.
    host.reset()
    native.run(op='connect')
    native.run(op='enable', profile=PROFILE)
    [second] = credentials(host)
    assert second != first and second.startswith('chrome-')


def test_backend_restart_drops_every_credential_and_issues_fresh_ones(native, host):
    enabled_chrome(native)
    [first] = credentials(host)
    host.reset()
    native.run(op='disconnect')
    assert native.run(op='status')['status']['keyring'] == []
    native.run(op='connect')
    [second] = credentials(host)
    assert second != first
    assert native.run(op='status')['status']['keyring'] == [PROFILE]
    assert native.run(op='status')['status']['store']['live'] == 1


def test_only_the_backend_the_app_launched_is_recorded(native):
    native.setup()
    assert native.run(op='backend_launched', pid=4242)['pid'] == 4242


def test_a_click_delivers_one_observation_sealed_by_the_app(native, host):
    enabled_chrome(native)
    reply = native.session('chrome', hello(), begin(), observe(), dict(op='disconnect'))
    assert reply['sent'] == [dict(ok=True)] * 3 and reply['transport_closed'] is True
    [item] = host.inbox.drain()
    assert item['observation']['id'] == OBSERVATION['id'] and item['profile_id'] == PROFILE
    assert item['credential_id'].startswith('chrome-')
    # The grant was one click. A second observation without a fresh begin is denied.
    reply = native.session('chrome', hello(), observe(dict(OBSERVATION, id='obs.again')))
    assert reply['sent'][-1]['ok'] is False and len(host.inbox) == 0
    # Reconnecting replays nothing.
    assert host.inbox.drain() == []


@pytest.mark.parametrize('script,code', [
    # Private or unknown state denies before any context reaches the backend.
    ([begin(native_private=True), observe()], 'private_context'),
    ([begin(incognito=True), observe()], 'private_context'),
    ([begin(native_private=0), observe()], 'private_context'),
    ([begin(incognito='false'), observe()], 'private_context'),
    ([begin(url='ftp://school.example.invalid/x'), observe()], 'site_permission_denied'),
    ([begin(url='https://user:pw@school.example.invalid/x'), observe()], 'site_permission_denied'),
    ([begin(url='https://school.example.invalid/' + 'é'), observe()], 'site_permission_denied'),
    ([begin(url='https://school.example.invalid/' + 'a' * 2100), observe()], 'site_permission_denied'),
    ([begin(url='https://'), observe()], 'site_permission_denied'),
    # No native context at all: unknown context denies.
    ([observe()], 'bridge_unauthorized'),
    # The observation must be for the page native context named.
    ([begin(url='https://other.example.invalid/x'), observe()], 'bridge_unauthorized'),
    ([begin(), observe(dict(OBSERVATION, private_context=True))], 'bridge_unauthorized'),
    ([begin(), observe(dict(OBSERVATION, source_kind='mail'))], 'bridge_unauthorized'),
    # Context expires after 30 seconds.
    ([begin(), dict(__advance=31), observe()], 'bridge_unauthorized'),
    # The app's own enablement flag is authoritative, not the peer's.
    ([begin(), dict(__disable=PROFILE), observe()], 'bridge_unauthorized'),
    # Revocation cancels a live session before it can deliver.
    ([begin(), dict(__forget=PROFILE), observe()], None),
    # Closed messages: extra fields, context smuggling, other ops and roles.
    ([dict(begin(), allowed_origins=['https://evil.invalid'])], 'invalid_payload'),
    ([dict(observe(), profile_id='safari:default')], 'invalid_payload'),
    ([dict(observe(), enabled=True)], 'invalid_payload'),
    ([begin(), dict(op='observation', payload=[])], 'invalid_payload'),
    ([dict(op='snapshot', payload={})], 'invalid_payload'),
    ([dict(op='result', payload={})], 'invalid_payload'),
    ([dict(op='command', payload={})], 'invalid_payload'),
    ([dict(op='decision', proposal_id='p1', decision='approved')], 'invalid_payload'),
    ([dict(op='context', url=URL)], 'invalid_payload'),
])
def test_native_context_and_closed_messages_deny_every_bad_capture(native, host, script, code):
    enabled_chrome(native)
    reply = native.session('chrome', hello(), *script)
    assert len(host.inbox) == 0 and reply['transport_closed'] is True
    last = reply['sent'][-1]
    if code is None:
        assert reply['cancelled'] is True
    else:
        assert last == dict(ok=False, code=code), reply['sent']
    # Whatever happened, the session cannot leave a usable grant behind.
    assert native.run(op='context_of', profile=PROFILE)['context'] is None


def test_hello_must_be_first_and_exact(native, host):
    enabled_chrome(native)
    for first in (begin(), observe(), dict(op='hello'), dict(hello(), extra=1), dict(op='hello', profile_id=7)):
        reply = native.session('chrome', first, observe())
        assert reply['sent'][-1]['ok'] is False and len(host.inbox) == 0
    assert native.session('chrome', hello('chrome:unknown-profile'))['sent'] == [dict(ok=False, code='disabled')]


def test_a_peer_cannot_reach_a_disabled_browser_after_the_toggle_is_off(native, host):
    enabled_chrome(native)
    native.run(op='disable', profile=PROFILE)
    reply = native.session('chrome', hello(), begin(), observe())
    assert reply['sent'] == [dict(ok=False, code='disabled')] and len(host.inbox) == 0


def test_no_message_can_carry_an_approval_or_a_command(native, host):
    enabled_chrome(native)
    assert {m['op'] for m in native.controls} <= {'provision', 'revoke', 'context', 'clear_context', 'peer_open',
                                                  'peer_frame', 'peer_close'}
    native.session('chrome', hello(), begin(), observe())
    ops = {m['op'] for m in native.controls}
    assert 'decision' not in ops and 'dispatch' not in ops
    assert not any(m.get('credential_role') == 'app_approval' for m in native.controls)

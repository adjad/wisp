"""Disposable signed AF_UNIX cases; no browser or Keychain access.

The first eleven qualify the A04 transport. The rest (A10 WP3) run the actual app
activation over real private sockets against the actual Python BridgeHost: a
signed peer is accepted, an unsigned peer and a wrong backend PID are refused,
and the bridge creates no endpoint while off. Artifact CI runs this module in
browser_bridge_gate before the generic sandbox, then imports its exact-case
evidence. Direct pytest remains useful locally.
"""
import os
from pathlib import Path
import select
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
SOCKET_ROOT = os.environ.get('BROWSER_BRIDGE_SOCKET_ROOT', '/private/tmp')

@pytest.fixture(scope='session')
def signed_transport_binary(tmp_path_factory):
    if sys.platform != 'darwin':
        pytest.skip('Audit-token signed peer qualification requires macOS')
    out = tmp_path_factory.mktemp('bridge-signed')
    binary = out / 'transport'
    subprocess.run(['swiftc', '-swift-version', '5', '-parse-as-library', '-module-cache-path', str(out / 'cache'),
        str(ROOT / 'app/Sources/WispApp/BrowserContracts.swift'), str(ROOT / 'app/Sources/WispApp/BrowserBridge.swift'),
        str(ROOT / 'app/Sources/WispApp/BrowserBridgeCredentials.swift'),
        str(ROOT / 'test_fixtures/browser_bridge/TransportHarness.swift'), '-o', str(binary)],
        check=True, capture_output=True)
    subprocess.run(['codesign', '--force', '--sign', '-', '--identifier', 'invalid.wisp.a04.fixture', str(binary)],
                   check=True, capture_output=True)
    info = subprocess.run(['codesign', '-dvvv', str(binary)], check=True, capture_output=True, text=True).stderr
    import re
    digest = re.search(r'^CDHash=([0-9a-f]+)$', info, re.M).group(1)
    return binary, 'cdhash H"' + digest + '"'


@pytest.mark.parametrize('mode,expected', [('normal', 'accepted'), ('oversize', 'denied'),
                                         ('empty', 'denied'), ('truncated', 'denied'), ('header_eof', 'denied'), ('timeout', 'denied'), ('backpressure', 'denied'),
                                         ('wrong_requirement', 'denied'), ('different_executable', 'denied')])
def test_signed_connected_peer_private_transport(signed_transport_binary, mode, expected):
    import socket
    import tempfile
    import os
    binary, requirement = signed_transport_binary
    if mode == 'wrong_requirement':
        requirement = 'cdhash H"' + '0' * 40 + '"'
    # Short private path: Unix socket path limits are smaller than pytest paths.
    with tempfile.TemporaryDirectory(prefix='a04-', dir=SOCKET_ROOT) as directory:
        assert os.stat(directory).st_mode & 0o777 == 0o700
        path = directory + '/bridge'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(path)
            os.chmod(path, 0o600)
            listener.listen(1)
            server = subprocess.Popen([str(binary), 'server', str(listener.fileno()), requirement, mode],
                pass_fds=(listener.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            client = None
            try:
                if mode == 'different_executable':
                    # Python is not the exact signed fixture, regardless of UID.
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                        peer.connect(path)
                        try:
                            peer.sendall(b'\x00\x00\x00\x04wisp')
                        except BrokenPipeError:
                            pass
                else:
                    client = subprocess.Popen([str(binary), 'client', path, mode],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                stdout, stderr = server.communicate(timeout=10)
                assert server.returncode == 0 and stdout.strip() == expected, stderr
                if client:
                    output, error = client.communicate(timeout=10)
                    assert client.returncode == 0, error
                    if expected == 'accepted':
                        assert output.strip() == 'echo'
            finally:
                for process in (server, client):
                    if process and process.poll() is None:
                        process.kill()
                        process.wait()


@pytest.mark.parametrize('wrong_server', [False, True])
def test_mutual_signed_transport_rejects_wrong_server(signed_transport_binary, wrong_server):
    import tempfile
    binary, requirement = signed_transport_binary
    with tempfile.TemporaryDirectory(prefix='a04-', dir=SOCKET_ROOT) as directory:
        path = directory + '/bridge'
        server = subprocess.Popen([str(binary), 'server', path, requirement],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert select.select([server.stdout], [], [], 10)[0]
            assert server.stdout.readline().strip() == 'ready'
            expected = 'cdhash H"' + '0' * 40 + '"' if wrong_server else requirement
            client = subprocess.run([str(binary), 'client', path, 'mutual', expected],
                                    capture_output=True, text=True, timeout=10)
            assert client.returncode == 0
            assert client.stdout.strip() == ('denied' if wrong_server else 'echo'), client.stderr
            output, errors = server.communicate(timeout=10)
            assert output.strip() == ('denied' if wrong_server else 'accepted'), errors
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()


# -- A10 WP3: the actual app activation over real private sockets ------------------
@pytest.fixture(scope='session')
def signed_activation_binary(tmp_path_factory):
    if sys.platform != 'darwin':
        pytest.skip('Audit-token signed peer qualification requires macOS')
    out = tmp_path_factory.mktemp('bridge-activation-signed')
    binary = out / 'activation'
    app = ROOT / 'app/Sources/WispApp'
    subprocess.run(['swiftc', '-swift-version', '5', '-parse-as-library', '-module-cache-path', str(out / 'cache'),
        str(app / 'BrowserContracts.swift'), str(app / 'BrowserBridge.swift'), str(app / 'BrowserBridgeCredentials.swift'),
        str(ROOT / 'test_fixtures/browser_bridge/ActivationHarness.swift'), '-o', str(binary)],
        check=True, capture_output=True)
    subprocess.run(['codesign', '--force', '--sign', '-', '--identifier', 'invalid.wisp.a10.fixture', str(binary)],
                   check=True, capture_output=True)
    info = subprocess.run(['codesign', '-dvvv', str(binary)], check=True, capture_output=True, text=True).stderr
    import re
    return binary, 'cdhash H"' + re.search(r'^CDHash=([0-9a-f]+)$', info, re.M).group(1) + '"'


PROFILE = 'chrome:default'


class Rig:
    """The fixture app (real sockets) plus an in-process BridgeHost served over its control socket."""

    def __init__(self, binary, requirement, tmp_path, *, profile=PROFILE, backend_pid=None):
        import json as _json
        import tempfile
        from service.assistant.store import AssistantStore
        from service.browser.host import BridgeHost, ControlLink
        self.json, self.binary, self.requirement = _json, binary, requirement
        self.parent = tempfile.mkdtemp(prefix='a10-', dir=SOCKET_ROOT)
        os.chmod(self.parent, 0o700)
        self.directory = self.parent + '/bridge'
        self.assistant = AssistantStore(tmp_path / 'rig.db')
        self.host = BridgeHost(self.assistant)
        self.app = subprocess.Popen([str(binary), 'app', self.directory, requirement, '0', profile],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # The backend PID is an argument, so it is fixed before the app starts.
        self.app.kill()
        self.app.wait()
        pid = os.getpid() if backend_pid is None else backend_pid
        self.app = subprocess.Popen([str(binary), 'app', self.directory, requirement, str(pid), profile],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert select.select([self.app.stdout], [], [], 20)[0]
        assert self.json.loads(self.app.stdout.readline()) == {'ready': True}, self.app.stderr.read()
        self.link = ControlLink(self.host, self.directory + '/control.sock', expected_pid=self.app.pid,
                                poll_seconds=0.05)
        self.link.start()

    def run(self, **request):
        self.app.stdin.write(self.json.dumps(request) + '\n')
        self.app.stdin.flush()
        assert select.select([self.app.stdout], [], [], 20)[0]
        return self.json.loads(self.app.stdout.readline())

    def wait(self, predicate, seconds=10):
        import time
        deadline = time.time() + seconds
        while not predicate() and time.time() < deadline:
            time.sleep(0.05)
        return predicate()

    def credentials(self):
        return self.host.handle(dict(op='hello'))['credentials']

    def peer(self, *messages, requirement=None):
        done = subprocess.run([str(self.binary), 'peer', self.directory + '/chrome.sock', requirement or self.requirement,
                               self.json.dumps(list(messages))], capture_output=True, text=True, timeout=30)
        return [self.json.loads(line) for line in done.stdout.splitlines()]

    def close(self):
        self.link.stop()
        for process in (self.app,):
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assistant._db.close()
        import shutil
        shutil.rmtree(self.parent, ignore_errors=True)


@pytest.fixture
def rig_factory(signed_activation_binary, tmp_path):
    binary, requirement = signed_activation_binary
    made = []

    def make(**kwargs):
        rig = Rig(binary, requirement, tmp_path, **kwargs)
        made.append(rig)
        return rig
    yield make
    for rig in made:
        rig.close()


def _observation():
    import json
    return json.loads((ROOT / 'test_fixtures/discovery_storage/lifecycle.json').read_text())['SourceObservation']


def _messages(observation):
    return [dict(op='hello', profile_id=PROFILE),
            dict(op='begin', url=observation['source_url'], native_private_context=False, extension_incognito=False),
            dict(op='observation', payload=observation), dict(op='disconnect')]


def test_activation_serves_a_signed_peer_end_to_end(rig_factory):
    rig = rig_factory()
    assert rig.wait(lambda: rig.run(op='status')['keyring'] == [PROFILE])
    assert [c.split('-')[0] for c in rig.credentials()] == ['chrome']
    # Owner-only directory, control endpoint and peer endpoint.
    assert os.stat(rig.directory).st_mode & 0o777 == 0o700
    for name in ('control.sock', 'chrome.sock'):
        assert os.stat(rig.directory + '/' + name).st_mode & 0o777 == 0o600
    observation = _observation()
    assert rig.peer(*_messages(observation)) == [{'reply': {'ok': True}}] * 3
    [item] = rig.host.inbox.drain()
    assert item['observation']['id'] == observation['id'] and item['profile_id'] == PROFILE
    # One click: a second connection without a fresh native begin delivers nothing.
    replies = rig.peer(_messages(observation)[0], _messages(observation)[2])
    assert replies[-1]['reply']['ok'] is False and len(rig.host.inbox) == 0


def test_activation_refuses_an_unsigned_peer(rig_factory):
    import socket
    import struct
    rig = rig_factory()
    assert rig.wait(lambda: rig.run(op='status')['keyring'] == [PROFILE])
    # A same-user process that is not the pinned executable gets no reply at all.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(10)
        peer.connect(rig.directory + '/chrome.sock')
        body = b'{"op":"hello","profile_id":"chrome:default"}'
        try:
            peer.sendall(struct.pack('>I', len(body)) + body)
        except BrokenPipeError:
            pass
        assert peer.recv(4) == b''
    # A signed peer that fails the requirement is refused by the same check.
    assert rig.peer(dict(op='hello', profile_id=PROFILE), requirement='cdhash H"' + '0' * 40 + '"') == [{'denied': True}]
    assert len(rig.host.inbox) == 0 and rig.host._conns == {}


def test_activation_refuses_a_backend_that_is_not_the_launched_process(rig_factory):
    rig = rig_factory(backend_pid=os.getpid() + 1)
    # The link connects, but the app only accepts the PID it recorded for its child.
    assert not rig.wait(lambda: rig.credentials() or rig.run(op='status')['keyring'], seconds=2)
    assert rig.run(op='status')['store']['created'] == 0


def test_activation_creates_no_endpoint_while_off(rig_factory):
    import time
    rig = rig_factory(profile='')
    time.sleep(0.5)
    assert not os.path.exists(rig.directory)
    assert rig.run(op='status')['store'] == dict(created=0, removed=0, sweeps=0, live=0)
    assert rig.credentials() == []


def test_disabling_revokes_the_credential_and_removes_every_endpoint(rig_factory):
    rig = rig_factory()
    assert rig.wait(lambda: rig.run(op='status')['keyring'] == [PROFILE])
    assert rig.run(op='disable', profile=PROFILE)['keyring'] == []
    assert rig.wait(lambda: not os.path.exists(rig.directory))
    assert rig.wait(lambda: rig.credentials() == [])
    assert rig.run(op='status')['store']['live'] == 0
    assert rig.peer(dict(op='hello', profile_id=PROFILE)) == [{'denied': True}]

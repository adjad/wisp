"""Eleven disposable signed AF_UNIX cases; no browser or Keychain access.

Artifact CI runs this module in browser_bridge_gate before the generic sandbox,
then imports its exact-case evidence. Direct pytest remains useful locally.
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



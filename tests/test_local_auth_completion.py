"""Synthetic local authentication transactions; no Keychain, HTTP, or launchctl."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/mac-mini'))
import local_auth as auth
from service.inference import local_peer

SOURCE = 'a' * 40
OLD = 'b' * 64
NEW = 'c' * 64


class Prep:
    def __init__(self, root):
        self.directory = root / '.moe/provisioning'
        self.directory.mkdir(parents=True)
        self.directory.parent.chmod(0o700)
        self.settings = root / '.omlx/settings.json'
        self.settings.parent.mkdir(mode=0o700)
        self.key = OLD
        self.cas_failure = None
        self.calls = []
        self.generations = 0
        self.source_valid = True
        self.snapshot = {'binary_sha256': 'd' * 64}
        self.state_override = None
        self.write_settings({'auth': {'api_key': OLD}})

    def write_settings(self, value):
        self.settings.write_text(json.dumps(value))
        self.settings.chmod(0o600)

    def clean_source(self, source):
        if not self.source_valid or source != SOURCE:
            raise RuntimeError('unreviewed_source')

    def private_moe(self, parent):
        assert parent == self.directory.parent

    def provisioning_lock(self, parent):
        return nullcontext()

    def verify_helper(self, directory, expected_source=None):
        self.clean_source(expected_source)
        return directory / 'helper'

    def helper_snapshot(self, directory):
        return self.snapshot.copy()

    def rotate_credential_generation(self, parent):
        self.generations += 1

    def recovery_marker(self, marker, record):
        marker.write_text(json.dumps(record))
        marker.chmod(0o600)

    def sync_directory(self, parent):
        pass

    def run(self, argv, data=None):
        self.calls.append((argv, data))
        operation = argv[-1]
        if operation == 'local-export':
            return json.dumps({'local': self.key}).encode()
        if operation == 'local-state':
            return json.dumps(self.state_override or {'local': 'missing' if self.key is None else 'present'}).encode()
        payload = json.loads(data)
        if operation == 'local-check':
            if self.key != payload['expected']:
                raise RuntimeError('SYNTHETIC-SECRET-DO-NOT-PRINT')
            return b'{"local":"verified"}'
        if operation == 'local-cas':
            if self.key != payload['expected']:
                raise RuntimeError('SYNTHETIC-SECRET-DO-NOT-PRINT')
            fail = self.cas_failure
            self.cas_failure = None
            if fail == 'before':
                raise RuntimeError('SYNTHETIC-SECRET-DO-NOT-PRINT')
            self.key = payload['replacement']
            if fail == 'after':
                raise RuntimeError('SYNTHETIC-SECRET-DO-NOT-PRINT')
            return b'{"local":"verified"}'
        raise AssertionError(operation)

    @property
    def marker(self):
        return self.directory.parent / '.helper-transaction.json'


class Adapter:
    def __init__(self, prep):
        self.prep = prep
        self.restarts = 0
        self.reject_new = False
        self.fail_restart_once = False
        self.rejected = []

    def restart(self):
        self.restarts += 1
        if self.fail_restart_once:
            self.fail_restart_once = False
            raise RuntimeError('SYNTHETIC-SECRET-DO-NOT-PRINT')

    def verify(self, token, revoked=None):
        current = json.loads(self.prep.settings.read_text()).get('auth', {}).get('api_key')
        self.rejected.append(revoked)
        return bool(token and current == token and self.prep.key == token and not (token == NEW and self.reject_new))


@pytest.fixture
def world(tmp_path, monkeypatch):
    prep = Prep(tmp_path)
    monkeypatch.setattr(auth.secrets, 'token_hex', lambda _: NEW)
    return prep, Adapter(prep)


def transact(world, operation='rotate', **kwargs):
    prep, adapter = world
    return auth.transact(prep, prep.directory, SOURCE, operation, adapter, authorize_keychain=True, **kwargs)


def test_valid_rotation_is_complete_without_secret_output(world, capsys):
    prep, adapter = world
    result = transact(world)
    assert result == {'schema_version': 1, 'status': 'complete', 'local_auth': 'verified'}
    assert prep.key == NEW
    assert json.loads(prep.settings.read_text())['auth'] == {'api_key': NEW, 'skip_api_key_verification': False}
    assert OLD in adapter.rejected
    assert not prep.marker.exists()
    assert prep.generations == 1
    assert not capsys.readouterr().out
    assert OLD not in json.dumps(result) and NEW not in json.dumps(result)
    assert all(OLD not in ' '.join(argv) and NEW not in ' '.join(argv) for argv, _ in prep.calls)


@pytest.mark.parametrize('config', [{}, {'auth': {}}, {'auth': {'api_key': 'legacy-key'}}, {'auth': {'api_key': ''}}])
def test_missing_keychain_migration(world, config):
    prep, _ = world
    prep.key = None
    prep.write_settings(config)
    assert transact(world, 'migrate')['status'] == 'complete'
    assert prep.key == NEW


def test_missing_keychain_rotation_refuses(world):
    prep, _ = world
    prep.key = None
    with pytest.raises(auth.AuthRefused, match='rotation_requires_valid_auth'):
        transact(world)
    assert not prep.marker.exists()


def test_present_keychain_migration_requires_rotation(world):
    with pytest.raises(auth.AuthRefused, match='rotation_required'):
        transact(world, 'migrate')


@pytest.mark.parametrize('bad', [{'local': 'invalid'}, {'local': 'present', 'extra': True}, {}, {'local': 'unknown'}])
def test_invalid_keychain_state_refuses_without_mutation(world, bad):
    prep, _ = world
    prep.state_override = bad if bad else {'extra': 'invalid'}
    original = prep.settings.read_bytes()
    with pytest.raises(auth.AuthRefused):
        transact(world)
    assert prep.settings.read_bytes() == original and prep.key == OLD
    assert not prep.marker.exists()


@pytest.mark.parametrize('bad', [None, [], {'auth': []}, {'auth': {'api_key': 42}}, {'auth': {'api_key': 'bad\nkey'}}, {'auth': {'api_key': 'x' * 513}}])
def test_invalid_settings_refuse(world, bad):
    prep, _ = world
    prep.write_settings(bad)
    with pytest.raises(auth.AuthRefused):
        transact(world)
    assert prep.key == OLD and not prep.marker.exists()


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_cas_failure_restores_and_keeps_journal(world, failure, capsys):
    prep, adapter = world
    original = prep.settings.read_bytes()
    prep.cas_failure = failure
    with pytest.raises(auth.AuthRefused, match='^recovery_required$'):
        transact(world)
    assert prep.settings.read_bytes() == original
    assert prep.key == OLD
    assert json.loads(prep.marker.read_text())['phase'] == 'restored_unverified'
    assert adapter.restarts >= 1
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    journal = prep.marker.read_text()
    assert OLD not in journal and NEW not in journal and 'SYNTHETIC-SECRET' not in journal


@pytest.mark.parametrize('failure', ['restart', 'auth'])
def test_backend_failure_restores_and_quarantines(world, failure):
    prep, adapter = world
    if failure == 'restart':
        adapter.fail_restart_once = True
    else:
        adapter.reject_new = True
    with pytest.raises(auth.AuthRefused, match='recovery_required'):
        transact(world)
    assert prep.key == OLD
    assert json.loads(prep.settings.read_text())['auth']['api_key'] == OLD
    assert prep.marker.exists()


@pytest.mark.parametrize('kind', ['symlink', 'public', 'hardlink', 'directory'])
def test_unsafe_settings_file_refuses(world, tmp_path, kind):
    prep, _ = world
    original = prep.settings.read_bytes()
    if kind == 'public':
        prep.settings.chmod(0o644)
    else:
        prep.settings.unlink()
        if kind == 'directory':
            prep.settings.mkdir()
        else:
            target = tmp_path / 'target'
            target.write_bytes(original)
            target.chmod(0o600)
            if kind == 'symlink':
                prep.settings.symlink_to(target)
            else:
                os.link(target, prep.settings)
    with pytest.raises((auth.AuthRefused, OSError)):
        transact(world)
    assert not prep.calls


def test_unreviewed_source_before_any_helper_access(world):
    prep, _ = world
    prep.source_valid = False
    with pytest.raises(RuntimeError, match='unreviewed_source'):
        transact(world)
    assert not prep.calls and not prep.marker.exists()


def test_explicit_keychain_authorization_required(world):
    prep, adapter = world
    with pytest.raises(auth.AuthRefused, match='explicit_authorization_required'):
        auth.transact(prep, prep.directory, SOURCE, 'rotate', adapter)
    assert not prep.calls


def journal(prep):
    prep.recovery_marker(prep.marker, {'schema_version': 2, 'operation': 'local-auth', 'source_commit': SOURCE,
        'helper': prep.helper_snapshot(prep.directory), 'phase': 'restored_unverified'})


def test_recovery_accepts_current_agreement(world):
    prep, adapter = world
    journal(prep)
    assert auth.recover_local(prep, prep.directory, SOURCE, adapter, authorize_keychain=True)['status'] == 'complete'
    assert not prep.marker.exists()
    assert prep.generations == 1 and adapter.restarts == 1


@pytest.mark.parametrize('problem', ['source', 'helper', 'phase', 'keychain', 'backend', 'token'])
def test_recovery_refuses_disagreement(world, problem):
    prep, adapter = world
    journal(prep)
    if problem in ('source', 'helper', 'phase'):
        record = json.loads(prep.marker.read_text())
        record[{'source': 'source_commit', 'helper': 'helper', 'phase': 'phase'}[problem]] = 'untrusted'
        prep.recovery_marker(prep.marker, record)
    elif problem == 'keychain':
        prep.key = NEW
    elif problem == 'backend':
        adapter.verify = lambda *args, **kwargs: False
    else:
        prep.write_settings({'auth': {'api_key': 'invalid'}})
    with pytest.raises((auth.AuthRefused, RuntimeError)):
        auth.recover_local(prep, prep.directory, SOURCE, adapter, authorize_keychain=True)
    assert prep.marker.exists()


@pytest.mark.parametrize('bad_response', [200, 0, 302, 500])
@pytest.mark.parametrize('which', ['missing', 'wrong', 'revoked'])
def test_auth_acceptance_requires_missing_wrong_and_revoked_rejection(which, bad_response):
    def request(token):
        if token == OLD:
            return 200
        if (which == 'missing' and token is None or which == 'wrong' and token not in (OLD, None, NEW)
                or which == 'revoked' and token == NEW):
            return bad_response
        return 401
    assert not auth.qualifies(OLD, request=request, revoked=NEW)


def test_auth_acceptance_valid():
    assert auth.qualifies(OLD, request=lambda token: 200 if token == OLD else 401, revoked=NEW)


def test_existing_transaction_blocks_rotation(world):
    prep, _ = world
    journal(prep)
    with pytest.raises(auth.AuthRefused, match='recovery_required'):
        transact(world)
    assert not prep.calls


@pytest.mark.parametrize('parent_problem', ['symlink', 'public'])
def test_recovery_requires_private_real_settings_parent(world, tmp_path, parent_problem):
    prep, adapter = world
    journal(prep)
    original_parent = prep.settings.parent
    if parent_problem == 'public':
        original_parent.chmod(0o755)
    else:
        target = tmp_path / 'alternate-settings'
        original_parent.rename(target)
        original_parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(auth.AuthRefused):
        auth.recover_local(prep, prep.directory, SOURCE, adapter, authorize_keychain=True)
    assert prep.marker.exists()


def test_failed_migration_retains_quarantine_and_missing_keychain(world):
    prep, adapter = world
    prep.key = None
    prep.write_settings({'auth': {}})
    original = prep.settings.read_bytes()
    prep.cas_failure = 'after'
    with pytest.raises(auth.AuthRefused, match='recovery_required'):
        transact(world, 'migrate')
    assert prep.key is None
    assert prep.settings.read_bytes() == original
    assert json.loads(prep.marker.read_text())['phase'] == 'restoration_unverified'


def test_recovery_requires_explicit_keychain_authorization(world):
    prep, adapter = world
    journal(prep)
    with pytest.raises(auth.AuthRefused, match='explicit_authorization_required'):
        auth.recover_local(prep, prep.directory, SOURCE, adapter)
    assert prep.marker.exists() and not prep.calls


@pytest.mark.parametrize('settings_token,native_token', [(OLD, OLD), (NEW, OLD), (NEW, NEW), (None, None), ('legacy', None)])
def test_explicit_repair_agreement_recovers_interruption(world, settings_token, native_token, capsys):
    prep, adapter = world
    prep.key = native_token
    prep.write_settings({'auth': {'api_key': settings_token}})
    journal(prep)
    result = auth.recover_local(prep, prep.directory, SOURCE, adapter,
                                authorize_keychain=True, decision='repair-agreement')
    token = json.loads(prep.settings.read_text())['auth']['api_key']
    assert token == prep.key and token in (OLD, NEW)
    assert result == {'schema_version': 1, 'status': 'complete', 'local_auth': 'verified'}
    assert not prep.marker.exists()
    assert all(OLD not in ' '.join(argv) and NEW not in ' '.join(argv) for argv, _ in prep.calls)
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_verify_agreement_never_implicitly_repairs(world):
    prep, adapter = world
    prep.key = OLD
    prep.write_settings({'auth': {'api_key': NEW}})
    original = prep.settings.read_bytes()
    journal(prep)
    with pytest.raises(RuntimeError):
        auth.recover_local(prep, prep.directory, SOURCE, adapter, authorize_keychain=True)
    assert prep.key == OLD and prep.settings.read_bytes() == original and prep.marker.exists()
    assert not any(argv[-1] == 'local-cas' for argv, _ in prep.calls)


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_repair_cas_interruption_stays_quarantined_and_can_retry(world, failure):
    prep, adapter = world
    prep.write_settings({'auth': {'api_key': NEW}})
    journal(prep)
    prep.cas_failure = failure
    with pytest.raises(RuntimeError):
        auth.recover_local(prep, prep.directory, SOURCE, adapter, authorize_keychain=True, decision='repair-agreement')
    assert prep.marker.exists()
    result = auth.recover_local(prep, prep.directory, SOURCE, adapter,
                                authorize_keychain=True, decision='repair-agreement')
    assert result['status'] == 'complete' and prep.key == NEW and not prep.marker.exists()


@pytest.mark.parametrize('drift', ['settings', 'helper', 'keychain', 'source'])
def test_recovery_refuses_state_drift_after_backend_acceptance(world, drift):
    prep, adapter = world
    journal(prep)
    def drift_then_accept(token, revoked=None):
        if drift == 'settings':
            prep.write_settings({'auth': {'api_key': NEW}, 'other': 'concurrent edit'})
        elif drift == 'helper':
            prep.snapshot = {'binary_sha256': 'f' * 64}
        elif drift == 'keychain':
            prep.key = NEW
        else:
            prep.source_valid = False
        return True
    adapter.verify = drift_then_accept
    with pytest.raises((auth.AuthRefused, RuntimeError)):
        auth.recover_local(prep, prep.directory, SOURCE, adapter, authorize_keychain=True)
    assert prep.marker.exists() and prep.generations == 0


def test_concurrent_edit_before_transaction_write_is_preserved(world, monkeypatch):
    prep, adapter = world
    original_marker = prep.recovery_marker
    changed = {'auth': {'api_key': OLD}, 'other': 'concurrent edit'}
    def journal_then_edit(marker, record):
        original_marker(marker, record)
        prep.write_settings(changed)
    monkeypatch.setattr(prep, 'recovery_marker', journal_then_edit)
    with pytest.raises(auth.AuthRefused, match='recovery_required'):
        transact(world)
    assert json.loads(prep.settings.read_text()) == changed
    assert prep.key == OLD and adapter.restarts == 0
    assert not any(argv[-1] == 'local-cas' for argv, _ in prep.calls)
    assert prep.marker.exists()


def test_concurrent_edit_during_restart_is_preserved(world):
    prep, adapter = world
    changed = {'auth': {'api_key': OLD}, 'other': 'concurrent edit'}
    def restart():
        adapter.restarts += 1
        prep.write_settings(changed)
    adapter.restart = restart
    with pytest.raises(auth.AuthRefused, match='recovery_required'):
        transact(world)
    assert json.loads(prep.settings.read_text()) == changed
    assert prep.key == NEW and prep.marker.exists()
    assert adapter.restarts == 1


@pytest.mark.parametrize('invalid', [{'extra': None}, {'local': None, 'extra': True}, {'local': 'invalid'}])
def test_repair_export_contract_fails_closed(world, invalid):
    prep, adapter = world
    journal(prep)
    original = prep.run
    def altered(argv, data=None):
        if argv[-1] == 'local-export':
            return json.dumps(invalid).encode()
        return original(argv, data)
    prep.run = altered
    with pytest.raises(auth.AuthRefused, match='invalid_keychain_state'):
        auth.recover_local(prep, prep.directory, SOURCE, adapter,
                           authorize_keychain=True, decision='repair-agreement')
    assert prep.key == OLD and prep.marker.exists() and adapter.restarts == 0


@pytest.fixture
def managed(world, tmp_path, monkeypatch):
    import hashlib
    import plistlib
    prep, _ = world
    monkeypatch.setattr(auth.Path, 'home', lambda: tmp_path)
    executable = tmp_path / 'Library/Application Support/Wisp/omlx/1.2.3/bin/omlx'
    executable.parent.mkdir(parents=True)
    interpreter = executable.parent / 'python3'
    interpreter.write_bytes(b'\xcf\xfa\xed\xfe' + b'synthetic interpreter, never executed')
    interpreter.chmod(0o700)
    executable.write_text('#!' + str(interpreter) + ' -I\nfrom omlx.cli import main\nmain()\n')
    executable.chmod(0o700)
    plist = tmp_path / 'Library/LaunchAgents/com.wisp.omlx.plist'
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({'Label': 'com.wisp.omlx', 'ProgramArguments':
        [str(executable), 'serve', '--host', '127.0.0.1', '--port', '8000'],
        'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}))
    plist.chmod(0o600)
    authorization = tmp_path / 'authorization.json'
    authorization.write_text(json.dumps({'schema_version': 1, 'source_commit': SOURCE, 'action': 'local-auth',
        'uid': os.getuid(), 'plist_sha256': hashlib.sha256(plist.read_bytes()).hexdigest(),
        'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
        'runtime_manifest_sha256': auth.runtime_manifest(executable.parent.parent), 'native_qualified': True}))
    state = {'raw': 'pid = 1234\nstate = running\nprogram = /opt/homebrew/bin/omlx\nstdout path = /dev/null\nstderr path = /dev/null\n'
             'arguments = {\n/opt/homebrew/bin/omlx\nserve\n--host\n127.0.0.1\n--port\n8000\n}\n'
             'environment = {\nPATH => /usr/bin:/bin:/usr/sbin:/sbin\n}\n', 'calls': []}
    state['raw'] = state['raw'].replace('/opt/homebrew/bin/omlx', str(executable))
    state['listeners'] = [('127.0.0.1', 8000)]
    state['owners'] = 'p1234\nu' + str(os.getuid()) + '\nf7\nn127.0.0.1:8000\n'
    state['birth'] = 100
    monkeypatch.setattr(local_peer, 'process_identity', lambda pid, uid: (pid, uid, state['birth'], 0))
    state['connections'] = ('p1234\nu' + str(os.getuid()) + '\nf8\ntIPv4\nPTCP\n'
        'n127.0.0.1:8000->127.0.0.1:54321\nTST=ESTABLISHED\n'
        'p' + str(os.getpid()) + '\nu' + str(os.getuid()) + '\nf9\ntIPv4\nPTCP\n'
        'n127.0.0.1:54321->127.0.0.1:8000\nTST=ESTABLISHED\n')
    state['kernel'] = ('Active Internet connections (including servers)\n'
        'Proto Recv-Q Send-Q Local Address Foreign Address (state)\n'
        'tcp4 0 0 127.0.0.1.8000 127.0.0.1.54321 ESTABLISHED\n'
        'tcp4 0 0 127.0.0.1.54321 127.0.0.1.8000 ESTABLISHED\n')
    import socket_posture
    monkeypatch.setattr(local_peer, 'tcp_listeners', lambda: state['listeners'])
    def run(argv):
        state['calls'].append(argv)
        if argv[0] == '/usr/sbin/lsof':
            return state['connections' if '-sTCP:ESTABLISHED' in argv else 'owners'].encode()
        if argv[0] == '/usr/sbin/netstat': return state['kernel'].encode()
        return state['raw'].encode() if 'print' in argv else b''
    prep.run = run
    def create():
        return auth.ManagedOmlx(prep, authorization, hashlib.sha256(authorization.read_bytes()).hexdigest(), SOURCE)
    return create, state, executable


class SyntheticSocket:
    def fileno(self): return 9
    def getsockname(self): return ('127.0.0.1', 54321)
    def getpeername(self): return ('127.0.0.1', 8000)


@pytest.mark.parametrize('relative', ['bin/omlx', 'bin/python3', 'lib/site-packages/omlx/server.py'])
def test_complete_runtime_manifest_refuses_loaded_code_mutation(managed, relative):
    create, state, executable = managed
    adapter = create()
    path = executable.parent.parent / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'changed interpreter, package, or shim')
    path.chmod(0o700)
    with pytest.raises(auth.AuthRefused, match='runtime_(tree_changed|import_closure_unqualified)'):
        adapter.binding()


def test_managed_supervision_exact_synthetic_contract(managed):
    create, state, _ = managed
    adapter = create()
    adapter.restart()
    assert sum('kickstart' in argv for argv in state['calls']) == 1


@pytest.mark.parametrize('problem', ['argv', 'environment', 'program', 'output', 'executable', 'duplicate_arguments'])
def test_managed_supervision_refuses_changed_contract(managed, problem):
    create, state, executable = managed
    adapter = create()
    if problem == 'argv':
        state['raw'] = state['raw'].replace('127.0.0.1', '0.0.0.0')
    elif problem == 'environment':
        state['raw'] = state['raw'].replace('PATH =>', 'SECRET_SENTINEL =>')
    elif problem == 'program':
        state['raw'] = state['raw'].replace('program = ' + str(executable), 'program = /tmp/other')
    elif problem == 'output':
        state['raw'] = state['raw'].replace('stdout path = /dev/null', 'stdout path = /tmp/log')
    elif problem == 'executable':
        executable.write_bytes(b'changed executable')
    else:
        state['raw'] += 'arguments = {\nextra\n}\n'
    with pytest.raises(auth.AuthRefused):
        adapter.restart()
    assert not any('kickstart' in argv for argv in state['calls'])


def test_managed_supervision_accepts_indented_launchctl_blocks(managed):
    create, state, _ = managed
    state['raw'] = state['raw'].replace('\n}', '\n\t}')
    create()


@pytest.mark.parametrize('problem', ['pid', 'uid', 'extra_owner', 'missing_owner', 'unknown_field', 'ipv6', 'wildcard',
                                   'extra_kernel', 'missing_pid', 'duplicate_pid', 'not_running'])
def test_secret_probe_refuses_impersonating_or_ambiguous_listener(managed, monkeypatch, problem):
    create, state, _ = managed
    adapter = create()
    if problem == 'pid': state['owners'] = state['owners'].replace('p1234', 'p9999')
    elif problem == 'uid': state['owners'] = state['owners'].replace('u' + str(os.getuid()), 'u0')
    elif problem == 'extra_owner': state['owners'] += state['owners']
    elif problem == 'missing_owner': state['owners'] = ''
    elif problem == 'unknown_field': state['owners'] += 'cunknown\n'
    elif problem == 'ipv6': state['listeners'] = [('::1', 8000)]
    elif problem == 'wildcard': state['listeners'] = [('*', 8000)]
    elif problem == 'extra_kernel': state['listeners'].append(('127.0.0.1', 8000))
    elif problem == 'missing_pid': state['raw'] = state['raw'].replace('pid = 1234\n', '')
    elif problem == 'duplicate_pid': state['raw'] += 'pid = 1234\n'
    else: state['raw'] = state['raw'].replace('state = running', 'state = waiting')
    monkeypatch.setattr(local_peer, 'status', lambda *a, **k: pytest.fail('token sent to unqualified listener'))
    with pytest.raises(auth.AuthRefused): adapter.verify(NEW, revoked=OLD)
    with pytest.raises(auth.AuthRefused): adapter.restart()
    assert not any('kickstart' in argv for argv in state['calls'])


@pytest.mark.parametrize('phase', ['connect', 'response', 'between_probes'])
def test_listener_replacement_during_probe_never_qualifies(managed, monkeypatch, phase):
    create, state, _ = managed
    adapter = create()
    sent = []
    class Connection:
        def __init__(self, *args, **kwargs): self.sock = SyntheticSocket()
        def connect(self):
            if phase == 'connect': state['owners'] = state['owners'].replace('p1234', 'p9999')
        def request(self, method, path, headers): sent.append(headers)
        def getresponse(self):
            from types import SimpleNamespace
            if phase == 'response': state['owners'] = state['owners'].replace('p1234', 'p9999')
            return SimpleNamespace(status=200 if len(sent) == 1 else 401)
        def close(self):
            if phase == 'between_probes': state['raw'] = state['raw'].replace('pid = 1234', 'pid = 9999')
    monkeypatch.setattr(auth.http.client, 'HTTPConnection', Connection)
    with pytest.raises(auth.AuthRefused): adapter.verify(NEW, revoked=OLD)
    if phase == 'connect': assert sent == []


def test_restart_accepts_only_matching_new_job_listener(managed):
    create, state, _ = managed
    adapter = create()
    original = adapter.prep.run
    def run(argv):
        if 'kickstart' in argv:
            state['raw'] = state['raw'].replace('pid = 1234', 'pid = 2345')
            state['owners'] = state['owners'].replace('p1234', 'p2345')
        return original(argv)
    adapter.prep.run = run
    adapter.restart()
    assert adapter.binding() == 2345


def test_post_restart_impersonator_refuses_without_probe(managed, monkeypatch):
    create, state, _ = managed
    adapter = create()
    original = adapter.prep.run
    def run(argv):
        if 'kickstart' in argv:
            state['owners'] = state['owners'].replace('p1234', 'p9999')
        return original(argv)
    adapter.prep.run = run
    ticks = iter([0, 21])
    monkeypatch.setattr(auth.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(local_peer, 'status', lambda *a, **k: pytest.fail('unqualified probe'))
    with pytest.raises(auth.AuthRefused, match='restart_listener_unqualified'): adapter.restart()


def test_request_error_still_checks_listener_after_close(monkeypatch):
    checks = []
    class Connection:
        def __init__(self, *a, **k): self.sock = SyntheticSocket()
        def connect(self): pass
        def request(self, *a, **k): raise OSError('synthetic')
        def close(self): checks.append('closed')
    monkeypatch.setattr(auth.http.client, 'HTTPConnection', Connection)
    assert auth.status(NEW, binding=lambda: checks.append('binding'), peer=lambda sock: 'owned') == 0
    assert checks == ['binding', 'binding', 'closed', 'binding']
    with pytest.raises(auth.AuthRefused): auth.status(NEW)


@pytest.mark.parametrize('attack', ['handoff', 'uid', 'duplicate_server', 'missing_server', 'wrong_tuple',
    'duplicate_client', 'wrong_client_fd', 'unknown_field', 'missing_state', 'kernel_missing', 'kernel_duplicate',
    'kernel_not_established', 'incarnation', 'socket_replaced', 'socket_disappeared', 'server_fd_changed'])
def test_established_connection_attack_never_writes_token(managed, monkeypatch, attack):
    create, state, _ = managed
    adapter = create()
    sent = []
    class Connection:
        def __init__(self, *a, **k): self.sock = SyntheticSocket()
        def connect(self):
            # Listener and launchd remain approved throughout the accepted-socket
            # handoff attack. Only the established server endpoint is hostile.
            if attack == 'handoff': state['connections'] = state['connections'].replace('p1234', 'p9999')
            elif attack == 'uid': state['connections'] = state['connections'].replace('u' + str(os.getuid()), 'u0', 1)
            elif attack == 'duplicate_server': state['connections'] += state['connections'].split('p' + str(os.getpid()))[0]
            elif attack == 'missing_server': state['connections'] = 'p' + str(os.getpid()) + state['connections'].split('p' + str(os.getpid()))[1]
            elif attack == 'wrong_tuple': state['connections'] = state['connections'].replace(':54321', ':54322')
            elif attack == 'duplicate_client': state['connections'] += 'p' + str(os.getpid()) + state['connections'].split('p' + str(os.getpid()))[1]
            elif attack == 'wrong_client_fd': state['connections'] = state['connections'].replace('f9', 'f10')
            elif attack == 'unknown_field': state['connections'] += 'xunknown\n'
            elif attack == 'missing_state': state['connections'] = state['connections'].replace('TST=ESTABLISHED\n', '')
            elif attack == 'incarnation': state['birth'] += 1
        def request(self, *a, **k): sent.append(k)
        def getresponse(self):
            from types import SimpleNamespace
            return SimpleNamespace(status=200)
        def close(self): pass
    connection = Connection()
    monkeypatch.setattr(auth.http.client, 'HTTPConnection', lambda *a, **k: connection)
    native_run = adapter.prep.run
    established_calls = 0
    def run(argv):
        nonlocal established_calls
        raw = native_run(argv)
        if '-sTCP:ESTABLISHED' not in argv:
            return raw
        established_calls += 1
        if established_calls != 2:
            return raw
        text = raw.decode()
        if attack == 'kernel_missing':
            return text.split('p' + str(os.getpid()))[0].encode()
        if attack == 'kernel_duplicate':
            return (text + text.split('p' + str(os.getpid()))[0]).encode()
        if attack == 'kernel_not_established':
            return text.replace('ESTABLISHED', 'CLOSE_WAIT').encode()
        return raw
    adapter.prep.run = run
    original = adapter.connected_peer
    def inspected(sock, *args):
        value = original(sock, *args)
        if attack == 'socket_replaced': connection.sock = SyntheticSocket()
        if attack == 'socket_disappeared': connection.sock = None
        if attack == 'server_fd_changed': state['connections'] = state['connections'].replace('f8', 'f11')
        return value
    adapter.connected_peer = inspected
    with pytest.raises(auth.AuthRefused): adapter.verify(NEW, revoked=OLD)
    assert sent == []
    assert connection.auto_open == 0


def test_actual_connected_peer_all_four_probes_are_bound(managed, monkeypatch):
    create, state, _ = managed
    monkeypatch.setattr(auth.secrets, 'token_hex', lambda size: 'd' * 64)
    sent = []
    class Connection:
        def __init__(self, *a, **k): self.sock = SyntheticSocket()
        def connect(self): pass
        def request(self, method, path, headers):
            assert self.auto_open == 0
            self.headers = headers; sent.append(headers)
        def getresponse(self):
            from types import SimpleNamespace
            return SimpleNamespace(status=200 if self.headers.get('Authorization') == 'Bearer ' + NEW else 401)
        def close(self): pass
    monkeypatch.setattr(auth.http.client, 'HTTPConnection', Connection)
    assert create().verify(NEW, revoked=OLD)
    assert len(sent) == 4
    assert sum('-sTCP:ESTABLISHED' in argv for argv in state['calls']) == 24


@pytest.mark.parametrize('failure', [None, 'short', 'uid', 'pid', 'start'])
def test_process_incarnation_contract_uses_kernel_struct(monkeypatch, failure):
    class Query:
        def __call__(self, pid, flavor, arg, pointer, size):
            assert flavor == 3 and arg == 0 and size == 136
            info = pointer._obj
            info.pid = pid if failure != 'pid' else pid + 1
            info.uid = info.ruid = info.svuid = 501
            if failure == 'uid': info.svuid = 0
            info.start_seconds = 100 if failure != 'start' else 0
            return size if failure != 'short' else size - 1
    from types import SimpleNamespace
    monkeypatch.setattr(auth.ctypes, 'CDLL', lambda *a, **k: SimpleNamespace(proc_pidinfo=Query()))
    if failure:
        with pytest.raises(auth.AuthRefused): auth.process_identity(1234, 501)
    else: assert auth.process_identity(1234, 501) == (1234, 501, 100, 0)


@pytest.mark.parametrize('unknown', ['environment = { SECRET_SENTINEL => hidden }\n',
    'environment = {\nSECRET_SENTINEL => hidden\n',
    'environment = unknown\n'])
def test_managed_supervision_refuses_unparsed_environment(managed, unknown):
    create, state, _ = managed
    state['raw'] = state['raw'].split('environment = {')[0] + unknown
    with pytest.raises(auth.AuthRefused):
        create()

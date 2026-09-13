"""Explicit local oMLX auth transaction. Tokens remain in memory/private JSON only."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import tempfile
import time


class AuthRefused(Exception):
    pass


def read_private(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit):
            raise AuthRefused('unsafe_local_state')
        raw = os.read(fd, limit + 1)
        if len(raw) != info.st_size:
            raise AuthRefused('changed_local_state')
        return raw
    finally:
        os.close(fd)


def atomic_private(prep, path, raw):
    fd, temporary = tempfile.mkstemp(prefix='.auth-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        prep.sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def status(token):
    """No proxy, redirects, body/log collection, URL credentials or child process."""
    connection = http.client.HTTPConnection('127.0.0.1', 8000, timeout=2)
    try:
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        connection.request('GET', '/v1/models', headers=headers)
        response = connection.getresponse()
        return response.status
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        connection.close()


def qualifies(token, *, request=status, revoked=None):
    if not isinstance(token, str) or not token:
        return False
    wrong = secrets.token_hex(32)
    return (request(token) == 200 and request(None) in (401, 403) and
            request(wrong) in (401, 403) and
            (not revoked or revoked == token or request(revoked) in (401, 403)))


class ManagedOmlx:
    """Only an already loaded, independently qualified, exact launchd job."""
    def __init__(self, prep, authorization, authorization_sha, source):
        raw = Path(authorization).read_bytes()
        if hashlib.sha256(raw).hexdigest() != authorization_sha:
            raise AuthRefused('authorization_mismatch')
        doc = json.loads(raw)
        expected = {'schema_version', 'source_commit', 'action', 'uid', 'plist_sha256', 'executable_sha256', 'native_qualified'}
        if (set(doc) != expected or type(doc['schema_version']) is not int or doc['schema_version'] != 1 or
                doc['source_commit'] != source or doc['action'] != 'local-auth' or
                type(doc['uid']) is not int or doc['uid'] != os.getuid() or doc['uid'] == 0 or
                doc['native_qualified'] is not True):
            raise AuthRefused('authorization_mismatch')
        self.prep = prep
        self.path = Path.home() / 'Library/LaunchAgents/com.wisp.omlx.plist'
        self.raw = read_private(self.path)
        if hashlib.sha256(self.raw).hexdigest() != doc['plist_sha256']:
            raise AuthRefused('supervision_mismatch')
        plist = plistlib.loads(self.raw)
        argv = plist.get('ProgramArguments', [])
        executable = argv[0] if isinstance(argv, list) and argv else ''
        # Versioned immutable candidate layout produced by render_omlx.
        if not isinstance(executable, str) or not re.fullmatch(re.escape(str(Path.home())) +
                r'/Library/Application Support/Wisp/omlx/[0-9]+\.[0-9]+\.[0-9]+/bin/omlx', executable):
            raise AuthRefused('executable_unqualified')
        self.executable = executable
        # Exact qualified argv excludes secret flags, alternate ports and arbitrary commands.
        if (plist.get('Label') != 'com.wisp.omlx' or plist.get('ProgramArguments') !=
                [self.executable, 'serve', '--host', '127.0.0.1', '--port', '8000'] or
                plist.get('EnvironmentVariables', {}) or plist.get('Program') or
                plist.get('StandardOutPath') != '/dev/null' or plist.get('StandardErrorPath') != '/dev/null'):
            raise AuthRefused('supervision_mismatch')
        self.executable_sha256 = doc['executable_sha256']
        self.target = 'gui/' + str(os.getuid()) + '/com.wisp.omlx'
        self.check_loaded()

    def check_loaded(self):
        executable = Path(self.executable)
        if executable.resolve(strict=True) != executable:
            raise AuthRefused('executable_unqualified')
        fd = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022 or info.st_size > 64 * 1024 * 1024:
                raise AuthRefused('executable_unqualified')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                if hashlib.sha256(stream.read()).hexdigest() != self.executable_sha256:
                    raise AuthRefused('executable_changed')
        finally:
            os.close(fd)
        raw = self.prep.run(['/bin/launchctl', 'print', self.target]).decode('utf-8')
        # launchctl has no stable JSON contract. Accept only the qualified text
        # fields; unknown/multiple argument or environment sections fail closed.
        arguments = re.findall(r'(?m)^\s*arguments = \{\n([^}]+)\}', raw)
        if len(arguments) != 1 or [line.strip() for line in arguments[0].splitlines() if line.strip()] != [
                self.executable, 'serve', '--host', '127.0.0.1', '--port', '8000']:
            raise AuthRefused('loaded_arguments_unqualified')
        headers = re.findall(r'(?m)^\s*(?:default |inherited )?environment =', raw)
        blocks = re.findall(r'(?m)^\s*(?:default |inherited )?environment = \{\n([^}]*)\}', raw)
        if len(headers) != len(blocks):
            raise AuthRefused('loaded_environment_unqualified')
        for block in blocks:
            rows = [line.strip() for line in block.splitlines() if line.strip()]
            if any(row != 'PATH => /usr/bin:/bin:/usr/sbin:/sbin' for row in rows):
                raise AuthRefused('loaded_environment_unqualified')
        for field, value in [('program', self.executable), ('stdout path', '/dev/null'), ('stderr path', '/dev/null')]:
            if re.findall(r'(?m)^\s*' + re.escape(field) + r' = ([^\n]+)$', raw) != [value]:
                raise AuthRefused('loaded_service_unqualified')

    def restart(self):
        if read_private(self.path) != self.raw:
            raise AuthRefused('supervision_changed')
        self.check_loaded()
        self.prep.run(['/bin/launchctl', 'kickstart', '-k', self.target])
        self.check_loaded()

    def verify(self, token, revoked=None):
        from socket_posture import tcp_listeners
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            listeners = tcp_listeners()
            endpoints = [addr for addr, port in listeners if port == 8000] if listeners is not None else []
            if endpoints and all(addr in ('127.0.0.1', '::1') for addr in endpoints) and qualifies(token, revoked=revoked):
                return True
            time.sleep(0.2)
        return False


def transact(prep, directory, expected_source, operation, adapter, *, authorize_keychain=False):
    """Migration handles absent/legacy settings only; malformed Keychain always refuses.

    Any exception after possible native mutation retains a recovery journal, even
    after successful restoration. Crash recovery accepts only current agreement.
    """
    if authorize_keychain is not True or operation not in ('rotate', 'migrate'):
        raise AuthRefused('explicit_authorization_required')
    prep.clean_source(expected_source)
    parent = directory.parent
    prep.private_moe(parent)
    settings = parent.parent / '.omlx/settings.json'
    info = settings.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AuthRefused('unsafe_settings_directory')
    marker = parent / '.helper-transaction.json'
    with prep.provisioning_lock(parent):
        if os.path.lexists(marker):
            raise AuthRefused('recovery_required')
        binary = prep.verify_helper(directory, expected_source=expected_source)
        original = read_private(settings)
        config = json.loads(original)
        if not isinstance(config, dict) or not isinstance(config.get('auth', {}), dict):
            raise AuthRefused('invalid_auth_settings')
        old = config.get('auth', {}).get('api_key')
        if old is not None and (not isinstance(old, str) or '\r' in old or '\n' in old or len(old) > 512):
            raise AuthRefused('invalid_auth_settings')
        state = json.loads(prep.run([str(binary), 'local-state']))
        if state not in ({'local': 'present'}, {'local': 'missing'}):
            raise AuthRefused('invalid_keychain_state')
        existing = old if state == {'local': 'present'} else None
        if state == {'local': 'present'} and not re.fullmatch('[0-9a-f]{64}', old or ''):
            raise AuthRefused('local_auth_mismatch')
        if operation == 'rotate' and (existing is None or not adapter.verify(old)):
            raise AuthRefused('rotation_requires_valid_auth')
        if operation == 'migrate' and existing is not None:
            raise AuthRefused('rotation_required')
        check = {'expected': existing}
        if json.loads(prep.run([str(binary), 'local-check'], data=json.dumps(check).encode())) != {'local': 'verified'}:
            raise AuthRefused('local_auth_mismatch')
        new = secrets.token_hex(32)
        config.setdefault('auth', {})['api_key'] = new
        # No localhost auth bypass can coexist with the accepted checks.
        config['auth']['skip_api_key_verification'] = False
        record = {'schema_version': 2, 'operation': 'local-auth', 'source_commit': expected_source,
                  'helper': prep.helper_snapshot(directory), 'phase': 'changing'}
        prep.rotate_credential_generation(parent)
        prep.recovery_marker(marker, record)
        native_attempted = False
        settings_written = False
        proposed = json.dumps(config, sort_keys=True).encode()
        try:
            if read_private(settings) != original:
                raise AuthRefused('settings_changed')
            prep.clean_source(expected_source)
            atomic_private(prep, settings, proposed)
            settings_written = True
            native_attempted = True
            prep.run([str(binary), 'local-cas'], data=json.dumps({'expected': existing, 'replacement': new}).encode())
            adapter.restart()
            if not adapter.verify(new, revoked=old):
                raise AuthRefused('new_auth_unverified')
            prep.run([str(binary), 'local-check'], data=json.dumps({'expected': new}).encode())
            prep.verify_helper(directory, expected_source=expected_source)
            prep.clean_source(expected_source)
            if read_private(settings) != proposed:
                raise AuthRefused('settings_changed')
            marker.unlink()
            prep.sync_directory(parent)
            return {'schema_version': 1, 'status': 'complete', 'local_auth': 'verified'}
        except BaseException:
            try:
                if not settings_written or read_private(settings) != proposed:
                    raise AuthRefused('concurrent_settings_preserved')
                if native_attempted:
                    # A failed CAS may have applied. Only exact current state may restore.
                    current = json.loads(prep.run([str(binary), 'local-state']))
                    if current == {'local': 'present'}:
                        try:
                            prep.run([str(binary), 'local-check'], data=json.dumps({'expected': new}).encode())
                        except Exception:
                            prep.run([str(binary), 'local-check'], data=json.dumps({'expected': existing}).encode())
                        else:
                            prep.run([str(binary), 'local-cas'], data=json.dumps({'expected': new, 'replacement': existing}).encode())
                    elif existing is not None:
                        raise AuthRefused('restoration_unverified')
                atomic_private(prep, settings, original)
                adapter.restart()
                # Missing/invalid original auth cannot qualify acceptance; keep quarantine.
                if not old or not adapter.verify(old, revoked=new):
                    raise AuthRefused('restoration_unverified')
                record['phase'] = 'restored_unverified'
            except BaseException:
                record['phase'] = 'restoration_unverified'
            prep.recovery_marker(marker, record)
            raise AuthRefused('recovery_required') from None


def recover_local(prep, directory, expected_source, adapter, *, authorize_keychain=False, decision="verify-agreement"):
    if authorize_keychain is not True or decision not in ('verify-agreement', 'repair-agreement'):
        raise AuthRefused('explicit_authorization_required')
    prep.clean_source(expected_source)
    parent = directory.parent
    prep.private_moe(parent)
    settings_path = parent.parent / '.omlx/settings.json'
    info = settings_path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AuthRefused('unsafe_settings_directory')
    marker = parent / '.helper-transaction.json'
    with prep.provisioning_lock(parent):
        record = json.loads(read_private(marker, 65536))
        if (set(record) != {'schema_version', 'operation', 'source_commit', 'helper', 'phase'} or
                record['schema_version'] != 2 or record['operation'] != 'local-auth' or
                record['source_commit'] != expected_source or record['phase'] not in
                ('changing', 'restored_unverified', 'restoration_unverified')):
            raise AuthRefused('invalid_recovery_state')
        if prep.helper_snapshot(directory) != record['helper']:
            raise AuthRefused('helper_changed')
        binary = prep.verify_helper(directory, expected_source=expected_source)
        original = read_private(settings_path)
        settings = json.loads(original)
        token = settings.get('auth', {}).get('api_key')
        proposed = original
        if decision == 'repair-agreement':
            # The helper emits only to a machine pipe. Existing ACLs and token
            # shape are checked before an explicitly authorized roll-forward.
            current = json.loads(prep.run([str(binary), 'local-export']))
            if set(current) != {'local'} or current['local'] is not None and not re.fullmatch('[0-9a-f]{64}', current['local']):
                raise AuthRefused('invalid_keychain_state')
            if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token):
                token = secrets.token_hex(32)
                settings.setdefault('auth', {})['api_key'] = token
                settings['auth']['skip_api_key_verification'] = False
                proposed = json.dumps(settings, sort_keys=True).encode()
            if read_private(settings_path) != original:
                raise AuthRefused('settings_changed')
            atomic_private(prep, settings_path, proposed)
            prep.run([str(binary), 'local-cas'], data=json.dumps({'expected': current['local'], 'replacement': token}).encode())
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token):
            raise AuthRefused('auth_repair_required')
        prep.run([str(binary), 'local-check'], data=json.dumps({'expected': token}).encode())
        adapter.restart()
        if not adapter.verify(token):
            raise AuthRefused('auth_repair_required')
        prep.clean_source(expected_source)
        prep.verify_helper(directory, expected_source=expected_source)
        if prep.helper_snapshot(directory) != record['helper'] or read_private(settings_path) != proposed:
            raise AuthRefused('recovery_state_changed')
        prep.run([str(binary), 'local-check'], data=json.dumps({'expected': token}).encode())
        prep.rotate_credential_generation(parent)
        try:
            marker.unlink()
            prep.sync_directory(parent)
        except BaseException:
            prep.recovery_marker(marker, record)
            raise
        return {'schema_version': 1, 'status': 'complete', 'local_auth': 'verified'}

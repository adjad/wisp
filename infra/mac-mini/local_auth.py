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
import ctypes
import ipaddress


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from service.inference.local_peer import (AuthRefused, read_private, atomic_private, process_identity,
    socket_identity, connection_owners, status, qualifies, ManagedOmlx, runtime_manifest)


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

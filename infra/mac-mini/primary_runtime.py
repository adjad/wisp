"""Generation-bound primary backend refresh; no process discovery or termination."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time


def private_bytes(path, *, maximum=4096, exact_private=True):
    """Read one owned single-link inode and reject replacement during read."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or info.st_mode & 0o7022
                or exact_private and stat.S_IMODE(info.st_mode) != 0o600
                or not 0 <= info.st_size <= maximum):
            raise ValueError('unsafe_receipt')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            data = source.read(maximum + 1)
        after, named = os.fstat(fd), path.lstat()
        if (len(data) != info.st_size or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns
                or after.st_ctime_ns != info.st_ctime_ns
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                or stat.S_ISLNK(named.st_mode)):
            raise ValueError('changed_receipt')
        return data
    finally:
        os.close(fd)


def private_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate_field')
            result[key] = value
        return result
    value = json.loads(private_bytes(path), object_pairs_hook=unique)
    if not isinstance(value, dict): raise ValueError('invalid_receipt')
    return value


def current_generation(parent):
    value = private_bytes(parent / '.credential-generation', maximum=64).decode('ascii')
    if not re.fullmatch(r'[0-9a-f]{64}', value): raise ValueError('invalid_generation')
    return value


def config_digest(path):
    return hashlib.sha256(private_bytes(path, maximum=1048576, exact_private=False)).hexdigest()


def refresh(prep, *, expected_source, timeout=30, home=None, sleep=time.sleep, now=time.monotonic):
    """Retryable restoration. Timeout keeps quarantine; never resurrect an epoch."""
    prep.clean_source(expected_source)
    parent = Path(home) / '.moe' if home else Path.home() / '.moe'
    prep.private_moe(parent)
    marker = parent / '.helper-transaction.json'
    with prep.provisioning_lock(parent):
        if os.path.lexists(marker):
            previous = private_json(marker)
            if type(previous.get('schema_version')) is not int or previous != {'schema_version': 2, 'operation': 'backend-restore',
                             'source_commit': expected_source, 'phase': 'runtime_unverified'}:
                raise prep.Refused('unrelated_recovery_required')
        record = {'schema_version': 2, 'operation': 'backend-restore',
                  'source_commit': expected_source, 'phase': 'runtime_unverified'}
        prep.recovery_marker(marker, record)
        prep.rotate_credential_generation(parent)
        try:
            prep.restore_primary_local()
            config = parent / 'config.yaml'
            digest = config_digest(config)
            generation = current_generation(parent)
            prep.clean_source(expected_source)
            marker.unlink()
            prep.sync_directory(parent)
        except BaseException:
            prep.recovery_marker(marker, record)
            raise
    deadline = now() + timeout
    try:
        while now() < deadline:
            with prep.provisioning_lock(parent):
                if os.path.lexists(marker) or current_generation(parent) != generation:
                    raise prep.Refused('concurrent_recovery')
                try:
                    receipt = private_json(parent / 'backend-runtime.json')
                    verified = (set(receipt) == {'schema_version', 'generation', 'pid', 'config_sha256'} and
                                type(receipt['schema_version']) is int and receipt['schema_version'] == 1 and receipt['generation'] == generation and
                                receipt['config_sha256'] == digest and type(receipt['pid']) is int and receipt['pid'] > 0 and
                                config_digest(config) == digest)
                    if verified:
                        # Match the current sole backend listener, not merely a
                        # possibly reused process ID from an old receipt.
                        owners = prep.run(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8765',
                                           '-sTCP:LISTEN', '-Fp']).splitlines()
                        if owners != [f"p{receipt['pid']}".encode('ascii')]:
                            raise ValueError('runtime_listener_mismatch')
                        if (os.path.lexists(marker) or current_generation(parent) != generation
                                or config_digest(config) != digest
                                or private_json(parent / 'backend-runtime.json') != receipt):
                            raise ValueError('runtime_changed')
                        prep.clean_source(expected_source)
                        return
                except (OSError, ValueError, TypeError, KeyError):
                    pass
            sleep(0.1)
        raise prep.Refused('backend_refresh_unverified')
    except BaseException:
        with prep.provisioning_lock(parent):
            # Do not overwrite a newer writer's journal or epoch.
            if not os.path.lexists(marker) and current_generation(parent) == generation:
                prep.recovery_marker(marker, record)
                prep.rotate_credential_generation(parent)
        raise

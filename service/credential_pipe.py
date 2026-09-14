"""One-shot native credential capability: private inherited stdin, never envp."""
import fcntl
import json
import os
import re
import select
import stat
import struct
import time

NAMES = ('WISP_LOCAL_OMLX_KEY', 'WISP_MINI_INFERENCE_KEY', 'WISP_MINI_NODE_KEY')
MAGIC = b'WISPCP1\n'
_attempted = False


def consume(role, *, optional=False):
    global _attempted
    metadata = os.environ.pop('WISP_CREDENTIAL_PIPE', None)
    inherited = [os.environ.pop(name, None) for name in NAMES]
    if not _attempted and metadata is None and optional and not any(value is not None for value in inherited):
        return {}, 'absent'
    raw = bytearray()
    try:
        if _attempted:
            raise ValueError
        _attempted = True
        if any(value is not None for value in inherited) or not isinstance(metadata, str):
            raise ValueError
        parts = metadata.split(':')
        if len(parts) != 3 or parts[0] != 'v1' or any(not re.fullmatch('0|[1-9][0-9]*', x) for x in parts[1:]):
            raise ValueError
        info = os.fstat(0)
        if (not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid()
                or (info.st_dev, info.st_ino) != tuple(map(int, parts[1:]))
                or fcntl.fcntl(0, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY):
            raise ValueError
        os.set_inheritable(0, False)
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
                raise ValueError
            block = os.read(0, 4097-len(raw))
            if not block:
                break
            raw.extend(block)
            if len(raw) > 4096:
                raise ValueError
        if len(raw) < 12 or raw[:8] != MAGIC or struct.unpack('!I', raw[8:12])[0] != len(raw)-12:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        doc = json.loads(raw[12:], object_pairs_hook=unique)
        if (set(doc) != {'version','pid','uid','role','generation','credentials'}
                or type(doc['version']) is not int or doc['version'] != 1
                or type(doc['pid']) is not int or doc['pid'] != os.getpid()
                or type(doc['uid']) is not int or doc['uid'] != os.getuid() or doc['role'] != role
                or not isinstance(doc['generation'], str)
                or not re.fullmatch('absent|[0-9a-f]{64}', doc['generation'])):
            raise ValueError
        values = doc['credentials']
        required = {'primary':set(NAMES), 'gateway':set(NAMES[:2]), 'node':{NAMES[2]}}[role]
        if (not isinstance(values, dict) or (set(values) - required if role == 'primary' else set(values) != required)
                or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in values.values())
                or len(set(values.values())) != len(values)):
            raise ValueError
        return values, doc['generation']
    except (OSError, ValueError, TypeError, KeyError, RecursionError, struct.error):
        raise ValueError('Native credential pipe unavailable') from None
    finally:
        raw[:] = b'\0' * len(raw)
        if metadata is not None:
            try:
                os.close(0)
            except OSError:
                pass

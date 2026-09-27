"""A04 authenticated IPC frames. No listener, persistence, logging or effects.

The transport MUST be private inherited IPC (or an equivalently protected local
channel): authentication provides integrity, not confidentiality. Provision keys
only through trusted bootstrap, never through a network registration endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

from service.browser.contracts import ContractViolation, require

MAX_FRAME = 262144
MAX_PAYLOAD = 131072
KINDS = frozenset(('challenge', 'register', 'registered', 'observation', 'snapshot',
                   'command', 'result', 'decision', 'disconnect'))
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


def identifier(value):
    require(type(value) is str and TOKEN.fullmatch(value) is not None, 'Invalid bridge identifier')
    return value


def loads(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate bridge key')
            result[key] = value
        return result

    def constant(_):
        raise ValueError()

    require(type(raw) is bytes and len(raw) <= MAX_FRAME, 'Invalid bridge frame size')
    # Bound nesting before json.loads: Python runtimes differ in whether the
    # decoder itself hits a recursion limit. Ignore delimiters inside strings.
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            require(depth <= 33, 'Invalid bridge JSON depth')
        elif byte in (125, 93):
            depth -= 1
    # Root is depth zero in the Swift scanner. Scalar children of a 33rd
    # container would be depth 33, so enforce the exact limit after decoding.
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        stack = [(value, 0)]
        while stack:
            child, level = stack.pop()
            require(level <= 32, 'Invalid bridge JSON depth')
            if type(child) is dict:
                stack.extend((v, level + 1) for v in child.values())
            elif type(child) is list:
                stack.extend((v, level + 1) for v in child)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ContractViolation('Invalid bridge JSON') from None


def dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContractViolation('Invalid bridge JSON') from None


def _body(frame, direction):
    require(direction in ('to_service', 'to_peer'), 'Invalid bridge direction')
    return '\n'.join(('wisp-browser-bridge/1', direction, frame['credential_id'],
        frame['session_id'], str(frame['sequence']), frame['kind'], frame['payload'])).encode('ascii')


def seal(key, credential_id, session_id, sequence, kind, payload, direction):
    raw = dumps(payload)
    require(len(raw) <= MAX_PAYLOAD, 'Invalid bridge payload size')
    frame = dict(version='1', credential_id=identifier(credential_id),
        session_id=identifier(session_id), sequence=sequence, kind=kind,
        payload=base64.b64encode(raw).decode('ascii'))
    _shape({**frame, 'mac': '0' * 64})
    frame['mac'] = hmac.new(key, _body(frame, direction), hashlib.sha256).hexdigest()
    return dumps(frame)


def _shape(frame):
    require(type(frame) is dict and set(frame) == {'version', 'credential_id', 'session_id',
        'sequence', 'kind', 'payload', 'mac'}, 'Invalid bridge envelope')
    require(frame['version'] == '1', 'Unsupported bridge version', 'incompatible_version')
    identifier(frame['credential_id'])
    identifier(frame['session_id'])
    require(type(frame['sequence']) is int and 0 <= frame['sequence'] <= 9007199254740991,
            'Invalid bridge sequence')
    require(type(frame['kind']) is str and frame['kind'] in KINDS, 'Invalid bridge kind')
    require(type(frame['payload']) is str and len(frame['payload']) <= 4 * ((MAX_PAYLOAD + 2) // 3),
            'Invalid bridge payload size')
    require(type(frame['mac']) is str and re.fullmatch('[0-9a-f]{64}', frame['mac']) is not None,
            'Invalid bridge MAC')
    try:
        payload = base64.b64decode(frame['payload'], validate=True)
        require(len(payload) <= MAX_PAYLOAD and base64.b64encode(payload).decode('ascii') == frame['payload'],
                'Invalid bridge encoding')
    except (ValueError, UnicodeError):
        raise ContractViolation('Invalid bridge encoding') from None
    return payload


def inspect(raw):
    frame = loads(raw)
    _shape(frame)
    return frame


def authenticate(frame, key, direction):
    """Verify MAC before decoding payload JSON; return authenticated bytes."""
    payload = _shape(frame)
    expected = hmac.new(key, _body(frame, direction), hashlib.sha256).hexdigest()
    require(hmac.compare_digest(frame['mac'], expected), 'Bridge authentication failed', 'bridge_unauthorized')
    return payload


def open_frame(raw, key, direction):
    frame = inspect(raw)
    return frame, loads(authenticate(frame, key, direction))

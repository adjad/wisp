"""A10 WP3: service-side endpoint around BrowserBridge. Observations only.

The native app owns activation. It listens on a private Unix socket ONLY while a
browser is enabled (default off), verifies the connecting process is this
backend (same UID and its own launched child PID), and then drives this module
as a trusted control channel. This module never listens. When the socket is
absent nothing runs except a cheap existence check.

Trust model
  * Credentials are provisioned and revoked ONLY by the native control channel.
    No HTTP route, tool, model or wire frame can install or remove a key. Only
    extension peers with the native_bridge role exist here: the app_approval
    role and every approval/decision path are refused, so this endpoint holds no
    approval authority even though BrowserBridge needs an A03 context to exist.
  * BridgeRuntimeContext is built from native control messages, never from a
    browser frame. It is short lived and single use: an accepted (or failed)
    observation consumes it. Missing, expired or unknown context denies.
  * Chrome (WP1 host) and Safari (WP7 appex) are separate identities. The
    browser is encoded in the credential and profile IDs and must agree.
  * Peer frames are opaque HMAC-sealed BrowserBridge frames relayed by the app;
    BrowserBridge remains the authority. Only registration and observation are
    accepted. Snapshots, results and commands close the session: no effects.
  * Observations land in a bounded in-memory inbox for the discovery pipeline.
    Nothing is persisted, logged or echoed in errors here.
"""
from __future__ import annotations

import base64
import binascii
import collections
import json
import os
import re
import select
import socket
import stat
import struct
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from service.browser.bridge import BridgeIdentity, BridgeRuntimeContext, BridgeSessionClosed, BrowserBridge
from service.browser.bridge_protocol import identifier, inspect
from service.browser.contracts import ContractViolation, require
from service.discovery.approvals import AppApprovalContext, ApprovalStore

BROWSERS = ('chrome', 'safari')
CONTROL_ENV = 'WISP_BROWSER_BRIDGE_CONTROL'
MAX_CONTROL = 1048576
MAX_CONNECTIONS = 16
MAX_PROFILES = 16
MAX_INBOX = 16
CONTEXT_TTL_MAX_MS = 60000
# macOS SOL_LOCAL socket options.
_SOL_LOCAL, _LOCAL_PEERCRED, _LOCAL_PEERPID = 0, 1, 2
_KEY = re.compile(r'[0-9a-f]{64}\Z')
_ORIGIN = re.compile(r'https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?\Z')
_CODES = frozenset(('incompatible_version', 'missing_capability', 'invalid_payload', 'disabled',
                    'site_permission_denied', 'private_context', 'foreground_preempted',
                    'bridge_unauthorized'))


def browser_of(credential_id: str, profile_id: str) -> str:
    """Return the browser both IDs name, or deny. IDs are native-generated."""
    for browser in BROWSERS:
        if credential_id.startswith(browser + '-') and profile_id.startswith(browser + ':'):
            return browser
    raise ContractViolation('Bridge identity denied', 'bridge_unauthorized')


def _exact(message, fields):
    require(type(message) is dict and set(message) == fields, 'Invalid control message')
    return message


def _integer(value, low, high):
    require(type(value) is int and low <= value <= high, 'Invalid control message')
    return value


def _bool(value):
    require(type(value) is bool, 'Invalid control message')
    return value


@dataclass(frozen=True)
class _Grant:
    context: BridgeRuntimeContext
    expires: float


class NativeContextStore:
    """Native-owned, short-lived capture context. Populated only by control messages."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._grants: dict[str, _Grant] = {}
        self._lock = threading.Lock()

    def grant(self, message):
        m = _exact(message, {'op', 'profile_id', 'enabled', 'private_context', 'trigger',
                             'allowed_origins', 'url', 'ttl_ms'})
        profile = identifier(m['profile_id'])
        origins = m['allowed_origins']
        require(type(origins) is list and 1 <= len(origins) <= 4 and
                all(type(o) is str and _ORIGIN.fullmatch(o) for o in origins), 'Invalid control message')
        url = m['url']
        require(type(url) is str and 0 < len(url) <= 2048 and url.isascii(), 'Invalid control message')
        parts = urlsplit(url)
        require(parts.scheme in ('http', 'https') and f'{parts.scheme}://{parts.netloc}' in origins,
                'Invalid control message', 'site_permission_denied')
        ttl = _integer(m['ttl_ms'], 1, CONTEXT_TTL_MAX_MS)
        # D1: the only capture is one the user just clicked. A04's `background`
        # field predates D1 and means "this read does not preempt the user's
        # foreground". A user-click read is the user's own action, so it cannot
        # preempt them; any other trigger is not a defined capture and is refused.
        require(m['trigger'] == 'user_click', 'Invalid control message')
        context = BridgeRuntimeContext(profile_id=profile, enabled=_bool(m['enabled']),
            private_context=_bool(m['private_context']), background=True,
            allowed_origins=frozenset(origins), url=url)
        with self._lock:
            self._grants[profile] = _Grant(context, self._clock() + ttl / 1000)

    def clear(self, profile_id):
        with self._lock:
            self._grants.pop(profile_id, None)

    def clear_all(self):
        with self._lock:
            self._grants.clear()

    def provider(self, identity):
        """Bridge runtime-context callable. None (unknown/expired) denies."""
        with self._lock:
            grant = self._grants.get(identity.profile_id)
            if grant is None:
                return None
            if self._clock() >= grant.expires:
                del self._grants[identity.profile_id]
                return None
            return grant.context


class ObservationInbox:
    """Bounded, memory-only hand-off of accepted observations. Oldest drop first."""

    def __init__(self, limit=MAX_INBOX):
        self._items = collections.deque(maxlen=limit)
        self._lock = threading.Lock()

    def add(self, identity, observation):
        with self._lock:
            self._items.append(dict(credential_id=identity.credential_id, profile_id=identity.profile_id,
                                    observation=observation))

    def drain(self):
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def __len__(self):
        with self._lock:
            return len(self._items)


class BridgeHost:
    """Trusted in-process endpoint. Every method is a control-channel operation."""

    def __init__(self, assistant, *, clock=None, inbox=None):
        # Held privately and never handed out. The app_approval role is refused
        # in provision(), so no decision can ever reach this authority.
        authority = AppApprovalContext()
        self.contexts = NativeContextStore(clock)
        self.inbox = inbox if inbox is not None else ObservationInbox()
        self.bridge = BrowserBridge(runtime_context=self.contexts.provider,
                                    approvals=ApprovalStore(assistant, app_context=authority),
                                    app_context=authority, clock=clock)
        self._identities: dict[str, BridgeIdentity] = {}
        self._profiles: dict[str, str] = {}
        self._conns: dict[int, tuple[str, BridgeIdentity]] = {}
        self._lock = threading.RLock()

    # -- control operations ------------------------------------------------
    def handle(self, message):
        """Dispatch one closed control message; return a closed reply. Never raises."""
        try:
            op = message.get('op') if type(message) is dict else None
            handler = {'provision': self.provision, 'revoke': self.revoke, 'context': self.context,
                       'clear_context': self.clear_context, 'peer_open': self.peer_open,
                       'peer_frame': self.peer_frame, 'peer_close': self.peer_close,
                       'hello': self.hello}.get(op)
            require(handler is not None, 'Invalid control message')
            return handler(message)
        except ContractViolation as error:
            return dict(ok=False, code=error.code if error.code in _CODES else 'invalid_payload')
        except Exception:  # noqa: BLE001 - control replies never carry details
            return dict(ok=False, code='invalid_payload')

    def hello(self, message):
        _exact(message, {'op'})
        with self._lock:
            return dict(ok=True, credentials=sorted(self._identities))

    def provision(self, message):
        m = _exact(message, {'op', 'credential_id', 'peer', 'credential_role', 'profile_id', 'key'})
        require(m['peer'] == 'extension' and m['credential_role'] == 'native_bridge',
                'Bridge role denied', 'bridge_unauthorized')
        cid, pid = identifier(m['credential_id']), identifier(m['profile_id'])
        browser_of(cid, pid)
        require(type(m['key']) is str and _KEY.fullmatch(m['key']) is not None, 'Invalid bridge key')
        identity = BridgeIdentity(cid, 'extension', 'native_bridge', pid)
        with self._lock:
            require(pid not in self._profiles and len(self._profiles) < MAX_PROFILES,
                    'Bridge credential unavailable', 'bridge_unauthorized')
            self.bridge.provision(identity, bytes.fromhex(m['key']))
            self._identities[cid] = identity
            self._profiles[pid] = cid
        return dict(ok=True)

    def revoke(self, message):
        m = _exact(message, {'op', 'credential_id'})
        cid = identifier(m['credential_id'])
        with self._lock:
            uncertain = self.bridge.revoke(cid)
            identity = self._identities.pop(cid, None)
            if identity is not None:
                self._profiles.pop(identity.profile_id, None)
                self.contexts.clear(identity.profile_id)
            for conn in [c for c, (_, i) in self._conns.items() if i.credential_id == cid]:
                del self._conns[conn]
            return dict(ok=True, uncertain_actions=list(uncertain))

    def context(self, message):
        _exact(message, {'op', 'profile_id', 'enabled', 'private_context', 'trigger',
                         'allowed_origins', 'url', 'ttl_ms'})
        with self._lock:
            require(message['profile_id'] in self._profiles, 'Native context unavailable', 'bridge_unauthorized')
            self.contexts.grant(message)
        return dict(ok=True)

    def clear_context(self, message):
        m = _exact(message, {'op', 'profile_id'})
        self.contexts.clear(identifier(m['profile_id']))
        return dict(ok=True)

    def peer_open(self, message):
        m = _exact(message, {'op', 'conn', 'credential_id'})
        conn = _integer(m['conn'], 0, 2 ** 31 - 1)
        with self._lock:
            identity = self._identities.get(m['credential_id'])
            require(identity is not None and conn not in self._conns and len(self._conns) < MAX_CONNECTIONS,
                    'Bridge authentication failed', 'bridge_unauthorized')
            raw = self.bridge.challenge(identity.credential_id)
            self._conns[conn] = (inspect(raw)['session_id'], identity)
        return dict(ok=True, frames=[_b64(raw)])

    def peer_frame(self, message):
        m = _exact(message, {'op', 'conn', 'frame'})
        conn = _integer(m['conn'], 0, 2 ** 31 - 1)
        with self._lock:
            entry = self._conns.get(conn)
            require(entry is not None, 'Bridge authentication failed', 'bridge_unauthorized')
            session, identity = entry
            try:
                raw = _unb64(m['frame'])
                envelope = inspect(raw)
                # A relay must not steer a frame into another peer's session.
                require(envelope['session_id'] == session and envelope['credential_id'] == identity.credential_id,
                        'Bridge authentication failed', 'bridge_unauthorized')
                event = self.bridge.receive(raw)
            except BridgeSessionClosed as error:
                return self._closed(conn, identity, error.code, error.uncertain_actions)
            except ContractViolation as error:
                return self._closed(conn, identity, error.code, self.bridge.close(session))
            kind = event['kind']
            if kind == 'registered':
                return dict(ok=True, event=kind, closed=False, frames=[_b64(event['response'])],
                            uncertain_actions=list(event['uncertain_actions']))
            if kind == 'observation':
                # One click, one read: the native grant is spent either way.
                self.contexts.clear(identity.profile_id)
                if event['payload']['private_context'] is not False:
                    # A sender's claim can only deny; `false` never proves a page public.
                    return self._closed(conn, identity, 'private_context', self.bridge.close(session))
                self.inbox.add(identity, event['payload'])
                return dict(ok=True, event=kind, closed=False, frames=[], uncertain_actions=[])
            if kind == 'disconnect':
                self._conns.pop(conn, None)
                return dict(ok=True, event=kind, closed=True, frames=[],
                            uncertain_actions=list(event['uncertain_actions']))
            # Snapshots, results and commands are outside observation-only scope.
            return self._closed(conn, identity, 'bridge_unauthorized', self.bridge.close(session))

    def peer_close(self, message):
        m = _exact(message, {'op', 'conn'})
        conn = _integer(m['conn'], 0, 2 ** 31 - 1)
        with self._lock:
            entry = self._conns.pop(conn, None)
            uncertain = self.bridge.close(entry[0]) if entry is not None else []
            if entry is not None:
                self.contexts.clear(entry[1].profile_id)
        return dict(ok=True, uncertain_actions=list(uncertain))

    def _closed(self, conn, identity, code, uncertain):
        self._conns.pop(conn, None)
        self.contexts.clear(identity.profile_id)
        return dict(ok=False, code=code if code in _CODES else 'invalid_payload', closed=True,
                    uncertain_actions=list(uncertain))

    def reset(self):
        """Control channel lost: fail closed. Restart-equivalent for all credentials."""
        with self._lock:
            for cid in list(self._identities):
                self.bridge.revoke(cid)
            self._identities.clear()
            self._profiles.clear()
            self._conns.clear()
            self.contexts.clear_all()


def _b64(raw):
    return base64.b64encode(raw).decode('ascii')


def _unb64(value):
    require(type(value) is str and len(value) <= 4 * ((MAX_CONTROL + 2) // 3), 'Invalid control message')
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ContractViolation('Invalid control message') from None


def control_loads(raw):
    """Closed control JSON: no duplicate keys, no NaN, bounded nesting."""
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Invalid control message')
            result[key] = value
        return result

    def constant(_):
        raise ValueError()

    require(type(raw) is bytes and 0 < len(raw) <= MAX_CONTROL and raw.count(b'[') <= 8 and raw.count(b'{') <= 8,
            'Invalid control message')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ContractViolation('Invalid control message') from None


# -- transport: the backend is the CLIENT of the app's control socket ----------
def peer_process(sock):
    """(uid, pid) of the process on the other end of a connected Unix socket."""
    cred = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 76)
    _version, uid = struct.unpack_from('=II', cred)
    return uid, sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID)


def _read(sock, count, stop):
    data = bytearray()
    while len(data) < count:
        if stop.is_set():
            raise EOFError()
        if not select.select([sock], [], [], 0.25)[0]:
            continue
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise EOFError()
        data.extend(chunk)
    return bytes(data)


def _write(sock, payload):
    sock.settimeout(5)
    sock.sendall(struct.pack('>I', len(payload)) + payload)


class ControlLink:
    """Serves the app's control socket. Polls only for the socket's existence."""

    def __init__(self, host: BridgeHost, path: str, *, expected_pid: int, poll_seconds=2.0, connector=None):
        require(os.path.isabs(path) and len(path.encode()) < 100, 'Invalid control path')
        require(type(expected_pid) is int and expected_pid > 1, 'Invalid control peer')
        self.host, self.path, self.expected_pid, self.poll = host, path, expected_pid, poll_seconds
        # Tests inject an already-connected socket; production connects to the private path.
        self._connector = connector or self._connect_private
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name='browser-bridge-control', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.host.reset()

    def _socket_is_private(self):
        try:
            directory, target = os.lstat(os.path.dirname(self.path)), os.lstat(self.path)
        except OSError:
            return False
        return (stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.getuid() and
                directory.st_mode & 0o077 == 0 and stat.S_ISSOCK(target.st_mode) and
                target.st_uid == os.getuid() and target.st_mode & 0o077 == 0)

    def _connect_private(self):
        """Connect only to an owner-only endpoint in an owner-only directory."""
        if not self._socket_is_private():
            return None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(2)
            sock.connect(self.path)
            return sock
        except OSError:
            sock.close()
            return None

    def _run(self):
        while not self._stop.is_set():
            sock = None
            try:
                sock = self._connector()
                if sock is not None:
                    self._serve(sock)
            except Exception:  # noqa: BLE001 - never surface details; fail closed and retry
                pass
            finally:
                if sock is not None:
                    sock.close()
                    self.host.reset()
            self._stop.wait(self.poll)

    def _serve(self, sock):
        uid, pid = peer_process(sock)
        # Only the app that launched this backend may drive it. Same UID alone is
        # not enough: any local process of the user shares it.
        if uid != os.getuid() or pid != self.expected_pid:
            return
        while not self._stop.is_set():
            header = _read(sock, 4, self._stop)
            size = struct.unpack('>I', header)[0]
            if not 0 < size <= MAX_CONTROL:
                return
            body = _read(sock, size, self._stop)
            try:
                reply = self.host.handle(control_loads(body))
            except ContractViolation:
                reply = dict(ok=False, code='invalid_payload')
            _write(sock, json.dumps(reply, separators=(',', ':')).encode('utf-8'))


def start_from_environment(assistant, environ=None):
    """Start the control link only when the app named a control socket.

    Returns the running ControlLink, or None. Without the variable, or when this
    process was not launched by the app, nothing is created at all.
    """
    env = os.environ if environ is None else environ
    path = env.get(CONTROL_ENV)
    parent = os.getppid()
    if not path or parent <= 1:
        return None
    try:
        link = ControlLink(BridgeHost(assistant), path, expected_pid=parent)
    except ContractViolation:
        return None
    link.start()
    return link

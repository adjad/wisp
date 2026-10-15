"""Process-local revocation revisions, never persisted permission or config.

Readers inspect one immutable pointer without locks or I/O. Writers update it
before mutation and notify outside the ledger lock and every config/file lock.
A listener failure cannot make an earlier snapshot current again.
"""
from dataclasses import dataclass, field
import hashlib
import json
import threading
import weakref
from service.publication_authority import Authority, mutating


@dataclass(frozen=True)
class Snapshot:
    revision: int
    ready: bool
    pending: int
    _token: object = field(default=None,compare=False,repr=False)


_state = Snapshot(0, False, 0)
_lock = threading.RLock()
_pending = set()
_listeners = {}
_next_listener = 0
_projection = None
_authority = Authority("configuration")


def current():
    return _state


def publication_token(expected):
    """Known memory only; an old Snapshot cannot acquire a new state's token."""
    state = _state
    token = state._token
    return token if expected is state and state.ready and token is not None and token.current() else None


def _publish(ready):
    global _state
    ready = ready and not _pending
    _authority.publish() if ready else _authority.withdraw()
    _state = Snapshot(_state.revision+1,ready,len(_pending),_authority.token() if ready else None)
    callbacks = []
    for key, ref in tuple(_listeners.items()):
        callback = ref()
        if callback is None:
            _listeners.pop(key, None)
        else:
            callbacks.append(callback)
    return _state, callbacks


def _notify(state, callbacks):
    for callback in callbacks:
        try:
            callback(state)
        except Exception:
            # Currentness already changed. Every grant must independently bind
            # and compare its original snapshot even if eager cleanup fails.
            pass


def subscribe(callback):
    global _next_listener
    ref = weakref.WeakMethod(callback) if getattr(callback, "__self__", None) is not None else weakref.ref(callback)
    with _lock:
        for key, item in tuple(_listeners.items()):
            if item() is None:
                _listeners.pop(key, None)
        _next_listener += 1
        key = _next_listener
        _listeners[key] = ref
    def unsubscribe():
        with _lock:
            _listeners.pop(key, None)
    return unsubscribe


@mutating
def begin():
    token = object()
    with _lock:
        _pending.add(token)
        state, callbacks = _publish(False)
    _notify(state, callbacks)
    return token


@mutating
def finish(token):
    with _lock:
        if token not in _pending:
            return
        _pending.remove(token)
        state, callbacks = _publish(False)
    # Even success requires a subsequent stable, explicit config observation.
    _notify(state, callbacks)


@mutating
def invalidate():
    with _lock:
        state, callbacks = _publish(False)
    _notify(state, callbacks)


@mutating
def observe(projection, *, valid, expected):
    global _projection
    try:
        digest = hashlib.sha256(json.dumps(projection, sort_keys=True, allow_nan=False,
            separators=(",", ":")).encode()).hexdigest() if valid else None
    except (TypeError, ValueError, UnicodeError):
        digest = None
    with _lock:
        if expected is not _state or _pending:
            return False
        if digest == _projection and _state.ready == (digest is not None):
            return _state.ready
        _projection = digest
        state, callbacks = _publish(digest is not None)
    _notify(state, callbacks)
    return state.ready

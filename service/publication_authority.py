"""Process-local ordering and memory stamps for prepared publications.

Importing this module creates no thread, source, transport, store or permission.
Publishers and cooperating revokers share a separate outer application lease.
The lease never substitutes for source admission or a caller-owned transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
import inspect
import math
import threading
import time
import weakref


class PublicationBusy(RuntimeError):
    """A same-thread mutation was refused BEFORE changing any authority."""


class PublicationUnavailable(ValueError):
    """Prepared evidence has no complete current publication witness."""


_barrier = threading.RLock()
_local = threading.local()
_issuer = object()


def assert_mutation_allowed():
    """Refuse an unsafe same-thread request before observing or changing state."""
    if getattr(_local,"publisher",None) is not None:
        raise PublicationBusy("Permission or source change was not applied during publication; retry outside it")


@contextmanager
def revocation():
    """Order a supported mutation before taking any inner writer locks.

    Nested mutations are allowed. A publishing thread cannot synchronously
    mutate authority: this is an explicit not-applied error, never a deferral.
    """
    assert_mutation_allowed()
    with _barrier:
        depth = getattr(_local,"revokers",0)
        _local.revokers = depth+1
        try:
            yield
        finally:
            _local.revokers = depth


def mutating(function):
    """Synchronous public mutation boundary; never hold a lease across await."""
    if inspect.iscoroutinefunction(function):
        raise TypeError("Async writers must scope individual synchronous mutations before/after awaits")
    @wraps(function)
    def call(*args,**kwargs):
        with revocation():
            return function(*args,**kwargs)
    return call


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


class Clock:
    """Known clock primitives, never an arbitrary source callback in a check.

    Production uses builtin wall/monotonic clocks. Explicit synthetic clocks
    can publish numeric readings during preparation; no callable is retained.
    """
    def __init__(self, *, live=True):
        if type(live) is not bool:
            raise TypeError("A known clock mode is required")
        self._live = live
        self._reading = None
        self._invalid = False

    def observe(self, wall, monotonic):
        if not _finite(wall) or not _finite(monotonic):
            self._reading = None
            self._invalid = True
        else:
            if self._reading is not None and (wall<self._reading[0] or monotonic<self._reading[1]):
                self._invalid = True
            self._reading = (float(wall),float(monotonic))

    def read(self):
        if self._invalid:
            return None
        if self._live:
            reading = (time.time(),time.monotonic())
            self.observe(*reading)
        return None if self._invalid else self._reading


LIVE_CLOCK = Clock()


@dataclass(frozen=True,eq=False)
class _State:
    revision: int
    ready: bool


class Authority:
    """A source/application-owned head; construction itself grants nothing."""
    def __init__(self, label, *, owner=None):
        if not isinstance(label,str) or not label or len(label)>128:
            raise ValueError("A bounded source kind is required")
        self.label = label
        self._head = _State(0,False)
        self._owner = weakref.ref(owner) if owner is not None else None

    @mutating
    def withdraw(self):
        self._head = _State(self._head.revision+1,False)

    @mutating
    def publish(self):
        self._head = _State(self._head.revision+1,True)

    def token(self, *, clock=None, expires_wall=None, expires_monotonic=None, cancelled_by=None):
        if cancelled_by is not None and type(cancelled_by) is not threading.Event:
            raise TypeError("Only a known cancellation event can qualify a token")
        head = self._head
        if not head.ready:
            return None
        bounds = None
        if expires_wall is not None or expires_monotonic is not None:
            clock = LIVE_CLOCK if clock is None else clock
            if type(clock) is not Clock:
                raise TypeError("Only a known memory/builtin clock can qualify a token")
            now = clock.read()
            if (not now or not all(_finite(v) for v in (*now,expires_wall,expires_monotonic))
                    or now[0]>=expires_wall or now[1]>=expires_monotonic):
                return None
            bounds = (clock,now[0],expires_wall,now[1],expires_monotonic)
        return Token(self,head,bounds,_issuer,cancelled_by)


@dataclass(frozen=True,eq=False,repr=False)
class Token:
    _authority: Authority
    _state: _State
    _bounds: tuple | None = field(repr=False)
    _issuer: object = field(repr=False)
    _cancellation: threading.Event | None = field(default=None,repr=False)

    def __post_init__(self):
        if self._issuer is not _issuer or type(self._authority) is not Authority or type(self._state) is not _State:
            raise TypeError("Only a source-issued memory stamp is accepted")
        if self._cancellation is not None and type(self._cancellation) is not threading.Event:
            raise TypeError("Only a known cancellation event can qualify a token")

    def __reduce_ex__(self, protocol):
        raise TypeError("Publication tokens cannot restore authority")

    def current(self):
        # Known memory cells and clock primitives only. No callbacks or locks.
        if self._authority._head is not self._state or not self._state.ready:
            return False
        if self._authority._owner is not None and self._authority._owner() is None:
            return False
        if self._cancellation is not None and self._cancellation._flag:
            return False
        if self._bounds is not None:
            clock,start_wall,end_wall,start_mono,end_mono = self._bounds
            reading = clock.read()
            if not reading or not (start_wall<=reading[0]<end_wall and start_mono<=reading[1]<end_mono):
                return False
        return True


@dataclass(frozen=True,repr=False)
class Witness:
    tokens: tuple

    def __post_init__(self):
        if type(self.tokens) is not tuple or not self.tokens or any(type(t) is not Token for t in self.tokens):
            raise TypeError("A nonempty tuple of source-issued memory tokens is required")

    @classmethod
    def combine(cls,*values):
        tokens = []
        for value in values:
            if type(value) is Token:
                tokens.append(value)
            elif type(value) is cls:
                tokens.extend(value.tokens)
            else:
                return None
        return cls(tuple(tokens)) if tokens else None

    def current(self):
        return all(token.current() for token in self.tokens)

    def __reduce_ex__(self, protocol):
        raise TypeError("Publication witnesses cannot restore authority")


class PublicationLease:
    def __init__(self,witness,*,issuer=None):
        if issuer is not _issuer:
            raise TypeError("An active application publication lease is required")
        self.witness = witness
        self._thread = threading.get_ident()
        self._closed = False

    def current(self):
        return (not self._closed and self._thread==threading.get_ident()
                and getattr(_local,"publisher",None) is self and self.witness.current())


@contextmanager
def publication(witness):
    if getattr(_local,"publisher",None) is not None or getattr(_local,"revokers",0):
        raise PublicationBusy("Nested publication is not supported")
    with _barrier:
        if type(witness) is not Witness or not witness.current():
            raise PublicationUnavailable("Current prepared source witnesses are required")
        lease = PublicationLease(witness,issuer=_issuer)
        _local.publisher = lease
        try:
            yield lease
        finally:
            lease._closed = True
            _local.publisher = None

"""Unused S2a attestation metadata store; no production admission or I/O.

All observations and clock samples are supplied by callers. The builtin lock
serializes only bounded metadata; there are no callbacks, awaits or workers.
Timeout/close/cancel retire authority, never a real running job's accounting.
Call job_finished only after the actual job ends, normally after publish/fail.
This is not a dispatch gate, native qualifier, scheduler or real clock adapter.
"""

from _thread import allocate_lock
from dataclasses import dataclass

from .attestation_lease import (
    BuildToken, ClockSample, PublicationSnapshot, QualificationKey,
    QualificationLease, lease_generation_current, lease_usable,
    publication_eligible, token_owns_slot,
)


@dataclass(frozen=True, slots=True)
class StoreLimits:
    key_capacity: int
    transport_capacity: int
    nonce_capacity: int
    counter_ceiling: int
    record_units: int
    fact_depth: int

    def __post_init__(self) -> None:
        for name in self.__slots__:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class Reservation:
    token: BuildToken
    started: bool


@dataclass(frozen=True, slots=True)
class StoreStatus:
    admitted: bool
    disabled_reason: str | None
    global_generation: int
    keys: int
    transport_identities: int
    open_transports: int
    issued_nonces: int
    outstanding: tuple[BuildToken, ...]
    outstanding_refreshes: int


@dataclass(slots=True)
class _Entry:
    generation: int = 0
    revision: int = 0
    lease: QualificationLease | None = None
    token: BuildToken | None = None


@dataclass(frozen=True, slots=True)
class _Job:
    token: BuildToken
    refresh: bool


class AttestationStore:
    """One injected process/store lifetime, permanently bounded and default-unused.

    Caller must use a fresh process_lifetime for each new store lifetime, never
    reuse transport identities, and supply already-qualified immutable facts.
    No at-fork hook or real lifecycle qualification is implemented. Limits bound
    retained record units/depth as well as counts; they select no production W.
    """

    def __init__(
        self, *, process_lifetime: str, policy_revision: str,
        clock_domain_id: str, limits: StoreLimits,
    ) -> None:
        if type(limits) is not StoreLimits:
            raise ValueError("limits must be StoreLimits")
        self._limits = limits
        # Bound generated nonce representation before any token can be issued.
        self._bounded(format(limits.nonce_capacity, "x"))
        for value in (process_lifetime, policy_revision, clock_domain_id):
            self._identifier(value)
        self._process = process_lifetime
        self._policy = policy_revision
        self._domain = clock_domain_id
        self._lock = allocate_lock()
        self._entries: dict[QualificationKey, _Entry] = {}
        self._transports: dict[str, bool] = {}
        self._jobs: dict[str, _Job] = {}
        self._nonce = 0
        self._global = 0
        self._last_now: int | None = None
        self._disabled: str | None = None

    def _bounded(self, value, depth: int = 0) -> int:
        """Exact immutable scalars/tuples; caller inputs checked before locking.

        Units conservatively bound scalar bytes and tuple entries. String units
        use four bytes per character; integers use bit length. No user equality,
        hash, repr, iteration or callback is accepted through arbitrary objects.
        Generated tokens receive the same pure bounded check inside the lock.
        """
        if depth > self._limits.fact_depth:
            raise ValueError("record nesting limit exceeded")
        kind = type(value)
        if kind is tuple:
            units = 1
            for part in value:
                units += self._bounded(part, depth + 1)
                if units > self._limits.record_units:
                    raise ValueError("record size limit exceeded")
        elif kind is str:
            units = 1 + 4 * len(value)
        elif kind is bytes:
            units = 1 + len(value)
        elif kind is int:
            units = 1 + (value.bit_length() + 7) // 8
        elif kind in (bool, type(None)):
            units = 1
        else:
            raise ValueError("record must contain exact immutable values")
        if units > self._limits.record_units:
            raise ValueError("record size limit exceeded")
        return units

    def _identifier(self, value: str) -> None:
        if type(value) is not str or not value:
            raise ValueError("identity must be a nonempty exact string")
        self._bounded(value)

    def _key(self, key: QualificationKey) -> None:
        if type(key) is not QualificationKey:
            raise ValueError("key must be QualificationKey")
        self._bounded(tuple(getattr(key, name) for name in key.__slots__))

    def _token(self, token: BuildToken) -> None:
        if type(token) is not BuildToken:
            raise ValueError("token must be BuildToken")
        self._key(token.key)
        self._bounded(tuple(getattr(token, name) for name in token.__slots__ if name != "key"))

    def _lease(self, lease: QualificationLease) -> None:
        if type(lease) is not QualificationLease:
            raise ValueError("lease must be QualificationLease")
        self._key(lease.key)
        self._bounded(tuple(getattr(lease, name) for name in lease.__slots__ if name != "key"))

    def _sample(self, sample: ClockSample) -> None:
        if type(sample) is not ClockSample:
            raise ValueError("sample must be ClockSample")
        self._bounded((sample.domain_id, sample.now_ns, sample.previous_ns))

    def _disable_locked(self, reason: str) -> None:
        if self._disabled is not None:
            return
        self._disabled = reason
        if self._global < self._limits.counter_ceiling:
            self._global += 1
        for entry in self._entries.values():
            entry.lease = None
            entry.token = None
        # Running-job accounting and transport tombstones are never reset.

    def _observe_locked(self, sample: ClockSample) -> bool:
        if self._disabled is not None or sample.domain_id != self._domain:
            return False
        if self._last_now is not None and sample.now_ns < self._last_now:
            self._disable_locked("clock-regression")
            return False
        self._last_now = sample.now_ns
        return True

    def _snapshot_locked(self, key: QualificationKey, entry: _Entry) -> PublicationSnapshot:
        return PublicationSnapshot(
            key=key, global_generation=self._global, key_generation=entry.generation,
            process_lifetime=self._process, policy_revision=self._policy,
            replacement_revision=entry.revision, clock_domain_id=self._domain,
            current_token=entry.token, published_lease=entry.lease,
            open_transport_lifetimes=tuple(k for k, opened in self._transports.items() if opened),
            policy_admitted=self._disabled is None, manifest_absent=key.manifest_absent,
            quarantined=self._disabled == "quarantine",
        )

    def _owned_locked(self, token: BuildToken) -> _Entry | None:
        entry = self._entries.get(token.key)
        if self._disabled is not None or entry is None:
            return None
        if not token_owns_slot(token, self._snapshot_locked(token.key, entry)):
            return None
        return entry

    def _revoke_locked(self, entry: _Entry) -> None:
        if entry.generation >= self._limits.counter_ceiling:
            self._disable_locked("counter-exhaustion")
            return
        entry.generation += 1
        entry.lease = None
        entry.token = None

    def register_transport(self, lifetime: str) -> bool:
        self._identifier(lifetime)
        with self._lock:
            if self._disabled is not None or lifetime in self._transports:
                return False
            if len(self._transports) >= self._limits.transport_capacity:
                return False
            self._transports[lifetime] = True
            return True

    def reserve(
        self, key: QualificationKey, transport_lifetime: str,
        sample: ClockSample, *, build_deadline_ns: int,
    ) -> Reservation | None:
        """Reserve accounting before returning a new token; joins start no job.

        A valid published lease makes a new job a refresh regardless of caller
        intent. At most one outstanding refresh and two total jobs exist. Expired
        current tokens are retired conservatively before a replacement is tried.
        """
        self._key(key)
        self._identifier(transport_lifetime)
        self._sample(sample)
        if type(build_deadline_ns) is not int or build_deadline_ns <= sample.now_ns:
            raise ValueError("build deadline must be an absolute future integer")
        self._bounded(build_deadline_ns)
        with self._lock:
            if key.policy_revision != self._policy or not key.manifest_absent:
                return None
            if not self._transports.get(transport_lifetime, False):
                return None
            if not self._observe_locked(sample):
                return None
            entry = self._entries.get(key)
            if entry is not None and entry.token is not None:
                if sample.now_ns < entry.token.absolute_build_deadline_ns:
                    return Reservation(entry.token, False)
                self._revoke_locked(entry)
                if self._disabled is not None:
                    return None
            if len(self._jobs) >= 2 or self._nonce >= self._limits.nonce_capacity:
                return None
            if entry is None and len(self._entries) >= self._limits.key_capacity:
                return None
            refresh = entry is not None and entry.lease is not None and lease_usable(
                entry.lease, self._snapshot_locked(key, entry), sample,
            )
            if refresh and any(job.refresh for job in self._jobs.values()):
                return None
            new_entry = entry is None
            if new_entry:
                entry = _Entry()
            next_nonce = self._nonce + 1
            token = BuildToken(
                process_lifetime=self._process, key=key,
                global_generation=self._global, key_generation=entry.generation,
                policy_revision=self._policy, expected_revision=entry.revision,
                unique_nonce=format(next_nonce, "x"), initiating_transport_lifetime=transport_lifetime,
                absolute_build_deadline_ns=build_deadline_ns,
            )
            # This bounded check is pure; reject before mutating reservations.
            self._token(token)
            if new_entry:
                self._entries[key] = entry
            self._nonce = next_nonce
            entry.token = token
            self._jobs[token.unique_nonce] = _Job(token, bool(refresh))
            return Reservation(token, True)

    def publish(self, token: BuildToken, lease: QualificationLease, sample: ClockSample) -> bool:
        self._token(token)
        self._lease(lease)
        self._sample(sample)
        with self._lock:
            entry = self._owned_locked(token)
            if entry is None:
                return False  # Stale input cannot advance time or revoke a successor.
            if not self._observe_locked(sample):
                return False
            if not publication_eligible(token, lease, self._snapshot_locked(token.key, entry), sample):
                self._revoke_locked(entry)
                return False
            if entry.revision >= self._limits.counter_ceiling:
                self._disable_locked("counter-exhaustion")
                return False
            entry.lease = lease
            entry.revision += 1
            entry.token = None
            return True

    def fail(self, token: BuildToken) -> bool:
        """Qualified current-job refusal/error, or logical caller cancellation."""
        self._token(token)
        with self._lock:
            entry = self._owned_locked(token)
            if entry is None:
                return False
            self._revoke_locked(entry)
            return True

    def cancel(self, token: BuildToken) -> bool:
        return self.fail(token)

    def expire(self, token: BuildToken, sample: ClockSample) -> bool:
        """Timeout retires authority; running work still consumes its real slot."""
        self._token(token)
        self._sample(sample)
        with self._lock:
            entry = self._owned_locked(token)
            if entry is None:
                return False
            if not self._observe_locked(sample) or sample.now_ns < token.absolute_build_deadline_ns:
                return False
            self._revoke_locked(entry)
            return True

    def job_finished(self, token: BuildToken) -> bool:
        """Settle only this exact outstanding job once, after it actually ends.

        Normally publish/fail first. Finishing a still-current unreported job
        conservatively revokes it rather than leave an unfinishable join target.
        A stale job settles accounting only, never a successor's metadata.
        """
        self._token(token)
        with self._lock:
            job = self._jobs.get(token.unique_nonce)
            if job is None or job.token != token:
                return False
            entry = self._owned_locked(token)
            if entry is not None:
                self._revoke_locked(entry)
            del self._jobs[token.unique_nonce]
            return True

    def close_transport(self, lifetime: str) -> bool:
        self._identifier(lifetime)
        with self._lock:
            if not self._transports.get(lifetime, False):
                return False
            self._transports[lifetime] = False
            for entry in self._entries.values():
                if entry.token is not None and entry.token.initiating_transport_lifetime == lifetime:
                    self._revoke_locked(entry)
            return True

    def revoke_lease(self, lease: QualificationLease) -> bool:
        self._lease(lease)
        with self._lock:
            entry = self._entries.get(lease.key)
            if self._disabled is not None or entry is None:
                return False
            if not lease_generation_current(lease, self._snapshot_locked(lease.key, entry)):
                return False
            self._revoke_locked(entry)
            return True

    def disable(self, reason: str) -> None:
        if type(reason) is not str or reason not in {"rollback", "quarantine", "manifest-transition"}:
            raise ValueError("disable reason must be rollback, quarantine or manifest-transition")
        with self._lock:
            self._disable_locked(reason)

    def snapshot(self, key: QualificationKey) -> PublicationSnapshot | None:
        self._key(key)
        with self._lock:
            entry = self._entries.get(key)
            return None if entry is None else self._snapshot_locked(key, entry)

    def lookup(self, key: QualificationKey, sample: ClockSample) -> QualificationLease | None:
        self._key(key)
        self._sample(sample)
        with self._lock:
            if not self._observe_locked(sample):
                return None
            entry = self._entries.get(key)
            if entry is None or entry.lease is None:
                return None
            return entry.lease if lease_usable(entry.lease, self._snapshot_locked(key, entry), sample) else None

    def status(self) -> StoreStatus:
        with self._lock:
            return StoreStatus(
                admitted=self._disabled is None, disabled_reason=self._disabled,
                global_generation=self._global, keys=len(self._entries),
                transport_identities=len(self._transports), open_transports=sum(self._transports.values()),
                issued_nonces=self._nonce, outstanding=tuple(job.token for job in self._jobs.values()),
                outstanding_refreshes=sum(job.refresh for job in self._jobs.values()),
            )

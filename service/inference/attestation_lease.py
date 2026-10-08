"""Inert attestation values and predicates for the default-off S1 design.

No production caller imports this module. Inputs are caller-supplied observations,
not qualified facts. A future store must serialize snapshot comparison and commit;
a True result here grants neither publication nor permission to emit bytes.
Times are absolute integer nanoseconds in an explicitly identified clock domain.
No clock, freshness policy, native inspection, store or worker is selected here.
"""

from dataclasses import dataclass


def _natural(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _identifier(value: str, name: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a nonempty string")


def _boolean(value: bool, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _facts(value: tuple, name: str) -> None:
    """Reject mutable aliases and user-defined objects/equality, recursively.

    Exact tuples containing exact scalar types are descriptive fact snapshots.
    Their completeness and truth remain the qualifier's responsibility. Authority
    objects are never retained: authority_reference is an opaque immutable ID.
    """
    if type(value) is not tuple or not value:
        raise ValueError(f"{name} must be a nonempty immutable tuple")
    for item in value:
        if type(item) is tuple:
            _facts(item, name)
        elif type(item) not in (str, bytes, int, bool, type(None)):
            raise ValueError(f"{name} contains an unsupported or mutable value")


def _same_facts(left: tuple, right: tuple) -> bool:
    # Python scalar equality aliases True and 1; fact types must also agree.
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if type(a) is not type(b):
            return False
        if type(a) is tuple:
            if not _same_facts(a, b):
                return False
        elif a != b:
            return False
    return True


@dataclass(frozen=True, slots=True)
class QualificationKey:
    uid: int
    port: int
    app_root: str
    app_executable: str
    python_root: str
    server_entry: str
    manifest_path: str
    manifest_absent: bool
    qualification_rule_revision: str
    policy_revision: str

    def __post_init__(self) -> None:
        _natural(self.uid, "uid")
        _natural(self.port, "port")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be in 1..65535")
        for name in (
            "app_root", "app_executable", "python_root", "server_entry",
            "manifest_path", "qualification_rule_revision", "policy_revision",
        ):
            _identifier(getattr(self, name), name)
        _boolean(self.manifest_absent, "manifest_absent")


@dataclass(frozen=True, slots=True)
class ClockSample:
    domain_id: str
    now_ns: int
    previous_ns: int

    def __post_init__(self) -> None:
        _identifier(self.domain_id, "domain_id")
        _natural(self.now_ns, "now_ns")
        _natural(self.previous_ns, "previous_ns")
        if self.now_ns < self.previous_ns:
            raise ValueError("clock sample regressed")


@dataclass(frozen=True, slots=True)
class QualificationLease:
    key: QualificationKey
    qualification_id: str
    global_generation: int
    key_generation: int
    clock_domain_id: str
    process_lifetime: str
    server_facts: tuple
    parent_facts: tuple
    boundary_fingerprints: tuple
    authority_reference: str
    qualification_start_ns: int
    expiry_ns: int

    def __post_init__(self) -> None:
        if type(self.key) is not QualificationKey:
            raise ValueError("key must be a QualificationKey")
        for name in (
            "qualification_id", "clock_domain_id", "process_lifetime",
            "authority_reference",
        ):
            _identifier(getattr(self, name), name)
        for name in ("global_generation", "key_generation", "qualification_start_ns", "expiry_ns"):
            _natural(getattr(self, name), name)
        if self.expiry_ns <= self.qualification_start_ns:
            raise ValueError("expiry must follow original qualification start")
        for name in ("server_facts", "parent_facts", "boundary_fingerprints"):
            _facts(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class BuildToken:
    process_lifetime: str
    key: QualificationKey
    global_generation: int
    key_generation: int
    policy_revision: str
    expected_revision: int
    unique_nonce: str
    initiating_transport_lifetime: str
    absolute_build_deadline_ns: int

    def __post_init__(self) -> None:
        if type(self.key) is not QualificationKey:
            raise ValueError("key must be a QualificationKey")
        for name in (
            "process_lifetime", "policy_revision", "unique_nonce",
            "initiating_transport_lifetime",
        ):
            _identifier(getattr(self, name), name)
        for name in (
            "global_generation", "key_generation", "expected_revision",
            "absolute_build_deadline_ns",
        ):
            _natural(getattr(self, name), name)
        if self.absolute_build_deadline_ns == 0:
            raise ValueError("build deadline must be positive")
        if self.policy_revision != self.key.policy_revision:
            raise ValueError("token and key policy revisions differ")


@dataclass(frozen=True, slots=True)
class PublicationSnapshot:
    """One key's supplied metadata, not a mutable cache or atomic observation.

    current_token may be None after retirement. Replacement alone does not revoke
    streams: lease usability deliberately does not compare replacement_revision.
    open_transport_lifetimes must reflect close tombstones; ID reuse is forbidden
    by the future issuer, as are nonce and process-lifetime reuse.
    """

    key: QualificationKey
    global_generation: int
    key_generation: int
    process_lifetime: str
    policy_revision: str
    replacement_revision: int
    clock_domain_id: str
    current_token: BuildToken | None
    published_lease: QualificationLease | None
    open_transport_lifetimes: tuple[str, ...]
    policy_admitted: bool
    manifest_absent: bool
    quarantined: bool

    def __post_init__(self) -> None:
        if type(self.key) is not QualificationKey:
            raise ValueError("key must be a QualificationKey")
        for name in ("global_generation", "key_generation", "replacement_revision"):
            _natural(getattr(self, name), name)
        for name in ("process_lifetime", "policy_revision", "clock_domain_id"):
            _identifier(getattr(self, name), name)
        if self.current_token is not None and type(self.current_token) is not BuildToken:
            raise ValueError("current_token must be a BuildToken or None")
        if self.published_lease is not None and type(self.published_lease) is not QualificationLease:
            raise ValueError("published_lease must be a QualificationLease or None")
        if type(self.open_transport_lifetimes) is not tuple:
            raise ValueError("open transport lifetimes must be an immutable tuple")
        for lifetime in self.open_transport_lifetimes:
            _identifier(lifetime, "transport lifetime")
        if len(set(self.open_transport_lifetimes)) != len(self.open_transport_lifetimes):
            raise ValueError("duplicate transport lifetime")
        for name in ("policy_admitted", "manifest_absent", "quarantined"):
            _boolean(getattr(self, name), name)


def lease_time_valid(lease: QualificationLease, sample: ClockSample) -> bool:
    """Original audit-start interval; no completion, refresh or use credit."""
    return (
        type(lease) is QualificationLease
        and type(sample) is ClockSample
        and sample.domain_id == lease.clock_domain_id
        and lease.qualification_start_ns <= sample.now_ns < lease.expiry_ns
    )


def lease_generation_current(lease: QualificationLease, state: PublicationSnapshot) -> bool:
    """Captured-generation CAS eligibility, including for an expired stream.

    Replacement revision is intentionally absent. This predicate performs no
    revocation and cannot establish serialization with publication or dispatch.
    """
    return (
        type(lease) is QualificationLease
        and type(state) is PublicationSnapshot
        and lease.key == state.key
        and lease.global_generation == state.global_generation
        and lease.key_generation == state.key_generation
        and lease.process_lifetime == state.process_lifetime
        and lease.key.policy_revision == state.policy_revision
        and lease.clock_domain_id == state.clock_domain_id
    )


def lease_usable(lease: QualificationLease, state: PublicationSnapshot, sample: ClockSample) -> bool:
    return (
        lease_generation_current(lease, state)
        and lease_time_valid(lease, sample)
        and state.policy_admitted
        and state.manifest_absent
        and lease.key.manifest_absent
        and not state.quarantined
    )


def token_owns_slot(token: BuildToken, state: PublicationSnapshot) -> bool:
    """Full-token CAS eligibility for completion/failure/timeout retirement.

    An expired current job can still be retired. A stale job cannot retire or
    revoke a successor even when its success/failure arrives after its deadline.
    """
    return (
        type(token) is BuildToken
        and type(state) is PublicationSnapshot
        and token == state.current_token
        and token.key == state.key
        and token.global_generation == state.global_generation
        and token.key_generation == state.key_generation
        and token.process_lifetime == state.process_lifetime
        and token.policy_revision == state.policy_revision
        and token.expected_revision == state.replacement_revision
        and token.initiating_transport_lifetime in state.open_transport_lifetimes
    )


def publication_eligible(
    token: BuildToken,
    lease: QualificationLease,
    state: PublicationSnapshot,
    sample: ClockSample,
) -> bool:
    """Pure precondition only; caller must atomically compare/commit later.

    A changed incarnation/fingerprint cannot replace a same-generation lease.
    Revoke first and reserve a new token; stale completions never do this for it.
    """
    if not token_owns_slot(token, state) or not lease_usable(lease, state, sample):
        return False
    if sample.now_ns >= token.absolute_build_deadline_ns:
        return False
    previous = state.published_lease
    if previous is not None:
        if not lease_generation_current(previous, state):
            return False
        if (
            not _same_facts(previous.server_facts, lease.server_facts)
            or not _same_facts(previous.parent_facts, lease.parent_facts)
            or not _same_facts(previous.boundary_fingerprints, lease.boundary_fingerprints)
        ):
            return False
    return True

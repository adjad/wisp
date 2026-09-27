"""Inactive native bridge registration and routing boundary for A04.

Bootstrap owns this object, its per-install credentials and A03 app capability.
Never publish provision/revoke/dispatch as adapter RPCs. A06/A07 must attach a
private transport and native runtime provider; A13 owns scheduling, budgets and
independent receipt verification. This module never executes an effect or saves
an adapter's receipt as verified completion. Restart drops all credentials and
sessions; bootstrap must explicitly reprovision protected keys.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import secrets
import hashlib
import threading
import time
from urllib.parse import urlsplit

from service.browser.contracts import ContractViolation, negotiate, require, validate
from service.browser.bridge_protocol import authenticate, identifier, inspect, loads, seal
from service.discovery.approvals import AppApprovalContext, ApprovalStore


@dataclass(frozen=True)
class BridgeIdentity:
    credential_id: str
    peer: str
    credential_role: str
    profile_id: str


@dataclass(frozen=True)
class BridgeRuntimeContext:
    """Native-owned current state, NEVER decoded from a browser message.

    None/unknown context denies. allowed_origins reflects current explicit site
    grants. Refresh on every call; capture adapters independently exclude private
    data before it reaches this boundary. Profile identifiers are opaque IDs.
    """
    profile_id: str
    enabled: bool
    private_context: bool
    background: bool
    allowed_origins: frozenset[str]
    url: str
    task_id: str | None = None
    snapshot_id: str | None = None
    # Set only from the app's trusted, profile-scoped proposal review state.
    # A wire decision must never populate this expected proposal identifier.
    approval_proposal_id: str | None = None


class BridgeSessionClosed(ContractViolation):
    """Sanitized failure plus IDs the trusted caller must reconcile, never retry."""
    def __init__(self, uncertain_actions, code='invalid_payload'):
        super().__init__('Bridge session closed; reconcile pending actions', code)
        self.uncertain_actions = tuple(uncertain_actions)


@dataclass(repr=False)
class _Credential:
    identity: BridgeIdentity
    key: bytes


@dataclass(repr=False)
class _Session:
    credential: _Credential
    expires: float
    registered: bool = False
    incoming: int = 0
    outgoing: int = 0
    capabilities: frozenset[str] = frozenset()
    pending: dict = field(default_factory=dict)
    used_actions: set = field(default_factory=set)
    results: dict = field(default_factory=dict)


class BrowserBridge:
    def __init__(self, *, runtime_context, approvals: ApprovalStore, app_context: AppApprovalContext,
                 clock=None):
        require(type(app_context) is AppApprovalContext, 'Trusted app context required', 'bridge_unauthorized')
        self._runtime_context = runtime_context
        self._approvals = approvals
        self._app_context = app_context
        self._clock = clock or time.monotonic
        self._credentials = {}
        self._issued_ids = set()
        self._issued_keys = set()
        self._sessions = {}
        self._lock = threading.RLock()

    def provision(self, identity: BridgeIdentity, key: bytes):
        """Trusted bootstrap only. Keys are 256-bit random, separately issued per role.

        No wire registration can install keys. Replacement requires revoke first.
        Backend keys live only in memory, not files, environment or arguments.
        """
        with self._lock:
            require(type(identity) is BridgeIdentity, 'Invalid bridge identity')
            identifier(identity.credential_id)
            identifier(identity.profile_id)
            require((identity.peer in ('app', 'extension') and identity.credential_role == 'native_bridge') or
                    (identity.peer == 'app' and identity.credential_role == 'app_approval'), 'Invalid bridge role')
            require(type(key) is bytes and len(key) == 32, 'Invalid bridge key')
            require(identity.credential_id not in self._issued_ids and len(self._credentials) < 32 and
                    len(self._issued_ids) < 1024,
                    'Bridge credential unavailable')
            fingerprint = hashlib.sha256(key).digest()
            require(fingerprint not in self._issued_keys, 'Bridge keys must be separate')
            self._credentials[identity.credential_id] = _Credential(identity, key)
            self._issued_ids.add(identity.credential_id)
            self._issued_keys.add(fingerprint)

    def revoke(self, credential_id):
        """Immediately invalidate active/pending channels. Return uncertain action IDs.

        Bootstrap must also remove the native Keychain record; never reprovision
        a revoked key. No reconnect/restart replays any pending effect.
        """
        with self._lock:
            self._credentials.pop(credential_id, None)
            return self._close_credential(credential_id)

    def _close_credential(self, credential_id):
        uncertain = []
        for sid, s in list(self._sessions.items()):
            if s.credential.identity.credential_id == credential_id:
                uncertain.extend(s.pending)
                del self._sessions[sid]
        return uncertain

    def close(self, session_id):
        """Trusted transport hook for EOF, timeout or delivery failure."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            return list(session.pending) if session is not None else []

    def challenge(self, credential_id):
        """Private transport bootstrap; an unregistered challenge cannot evict a session."""
        with self._lock:
            for sid, s in list(self._sessions.items()):
                if not s.registered and self._clock() >= s.expires:
                    del self._sessions[sid]
            c = self._credentials.get(credential_id)
            require(c is not None, 'Bridge authentication failed', 'bridge_unauthorized')
            require(len(self._sessions) < 64, 'Bridge session limit')
            sid = secrets.token_hex(32)
            s = _Session(c, self._clock() + 30)
            self._sessions[sid] = s
            return self._send(sid, s, 'challenge', self._handshake(c.identity))

    @staticmethod
    def _handshake(identity):
        caps = ['exact_app_approval'] if identity.credential_role == 'app_approval' else ['dom_text', 'verified_receipts']
        return dict(schema_version='1.0', peer='service', credential_role='native_bridge',
                    supported_versions=['1.0'], capabilities=caps, required_capabilities=caps)

    def _send(self, sid, session, kind, payload):
        raw = seal(session.credential.key, session.credential.identity.credential_id,
                   sid, session.outgoing, kind, payload, 'to_peer')
        session.outgoing += 1
        return raw

    def _profile_context(self, identity):
        c = self._runtime_context(identity)
        require(type(c) is BridgeRuntimeContext and c.profile_id == identity.profile_id,
                'Native context unavailable', 'bridge_unauthorized')
        return c

    def _approval_context(self, identity, proposal_id, intent):
        # App review is separate from capture policy, but never from profile
        # identity or the proposal/task/snapshot selected in trusted app state.
        c = self._profile_context(identity)
        require(c.approval_proposal_id == proposal_id and c.task_id == intent['task_id'] and
                c.snapshot_id == intent['snapshot_id'], 'Native approval scope mismatch', 'bridge_unauthorized')

    def _context(self, identity, url=None):
        c = self._profile_context(identity)
        require(c.enabled is True, 'Browser bridge disabled', 'disabled')
        require(c.private_context is False, 'Private context excluded', 'private_context')
        require(c.background is True, 'Foreground browser preempted', 'foreground_preempted')
        require(type(c.allowed_origins) is frozenset, 'Native permissions unavailable', 'site_permission_denied')
        for value in (c.url, url) if url is not None else (c.url,):
            require(type(value) is str, 'Native URL unavailable', 'site_permission_denied')
            u = urlsplit(value)
            require(u.scheme in ('https', 'http') and f'{u.scheme}://{u.netloc}' in c.allowed_origins,
                    'Site permission denied', 'site_permission_denied')
        return c

    def receive(self, raw: bytes):
        """Authenticate before routing. Returns a typed event for trusted integration.

        Authenticated malformed/replayed messages tear down their session. Events
        are transient data, never approval capabilities or proof of completion.
        Failed handlers do not return payloads in errors or log private content.
        """
        with self._lock:
            envelope = inspect(raw)
            sid = envelope['session_id']
            s = self._sessions.get(sid)
            require(s is not None and envelope['credential_id'] == s.credential.identity.credential_id and
                    self._credentials.get(envelope['credential_id']) is s.credential,
                    'Bridge authentication failed', 'bridge_unauthorized')
            # An unauthenticated attacker cannot close another peer's channel.
            authenticated = authenticate(envelope, s.credential.key, 'to_service')
            try:
                frame, payload = envelope, loads(authenticated)
                require(frame['sequence'] == s.incoming, 'Bridge replay or reordering', 'bridge_unauthorized')
                s.incoming += 1
                if not s.registered:
                    require(frame['kind'] == 'register' and self._clock() < s.expires,
                            'Bridge registration expired', 'bridge_unauthorized')
                    require(type(payload) is dict and set(payload) == {'handshake', 'client_nonce'},
                            'Invalid bridge registration')
                    nonce = identifier(payload['client_nonce'])
                    require(len(nonce) == 64 and all(c in '0123456789abcdef' for c in nonce),
                            'Invalid bridge client nonce')
                    p = validate('Handshake', payload['handshake'])
                    i = s.credential.identity
                    require(p['peer'] == i.peer and p['credential_role'] == i.credential_role,
                            'Bridge identity mismatch', 'bridge_unauthorized')
                    allowed = set(self._handshake(i)['capabilities'])
                    require(set(p['capabilities']) <= allowed, 'Bridge capability denied', 'bridge_unauthorized')
                    agreed = negotiate(self._handshake(i), p)
                    # One authenticated connection per credential. Discard prior pending work.
                    uncertain = self._close_credential(i.credential_id)
                    self._sessions[sid] = s
                    s.registered = True
                    s.capabilities = frozenset(agreed['capabilities'])
                    return dict(kind='registered', identity=i, uncertain_actions=uncertain,
                                response=self._send(sid, s, 'registered',
                                                    dict(negotiated=agreed, client_nonce=nonce)))
                return self._route(sid, s, frame['kind'], payload)
            except Exception as error:
                uncertain = self.close(sid)
                code = error.code if isinstance(error, ContractViolation) else 'invalid_payload'
                raise BridgeSessionClosed(uncertain, code) from None

    def _route(self, sid, s, kind, payload):
        i = s.credential.identity
        if kind == 'disconnect':
            require(type(payload) is dict and payload == {}, 'Invalid bridge disconnect')
            del self._sessions[sid]
            return dict(kind='disconnect', identity=i, uncertain_actions=list(s.pending))
        if i.credential_role == 'app_approval':
            require(kind == 'decision' and 'exact_app_approval' in s.capabilities,
                    'Bridge capability denied', 'bridge_unauthorized')
            fields = {'proposal_id', 'decision', 'expected_proposal_revision',
                      'expected_item_storage_revision', 'intent', 'evidence_ids', 'expires_at_ms'}
            require(type(payload) is dict and set(payload) == fields, 'Invalid app decision')
            identifier(payload['proposal_id'])
            intent = validate('ActionIntent', payload['intent'])
            self._approval_context(i, payload['proposal_id'], intent)
            p = deepcopy(payload)
            decision = self._approvals.decide(p.pop('proposal_id'), app_context=self._app_context, **p)
            return dict(kind='decision', identity=i, payload=decision)
        require(kind in ('observation', 'snapshot', 'result'), 'Bridge capability denied', 'bridge_unauthorized')
        require(('verified_receipts' if kind == 'result' else 'dom_text') in s.capabilities,
                'Bridge capability denied', 'bridge_unauthorized')
        contract = {'observation': 'SourceObservation', 'snapshot': 'BrowserSnapshot', 'result': 'ActionReceipt'}[kind]
        p = validate(contract, payload)
        c = self._context(i)
        if kind == 'observation':
            require(p['source_kind'] == 'browser' and p['source_url'] == c.url, 'Browser observation scope mismatch')
        elif kind == 'snapshot':
            require(p['url'] == c.url and p['task_id'] == c.task_id and p['id'] == c.snapshot_id,
                    'Native snapshot mismatch', 'stale_snapshot')
        else:
            pending = s.pending.get(p['action_id'])
            require(pending is not None and p['task_id'] == c.task_id and p['task_id'] == pending['intent']['task_id'] and
                    p['proposal_id'] == pending['proposal_id'], 'Unsolicited bridge result')
            require(not p['completes_obligation'], 'Adapter cannot complete obligations')
            require(p['action_id'] not in s.results, 'Duplicate bridge result')
            s.results[p['action_id']] = deepcopy(p)
        return dict(kind=kind, identity=i, payload=p)

    def acknowledge_result(self, session_id, receipt_id, *, persist):
        """Trusted integration only: persist accepted receipt before issuing ACK.

        persist must synchronously commit recovery state, or raise. The ACK is
        transport acceptance, never verification or obligation completion. Lost
        ACKs leave the native side uncertain; neither side replays the effect.
        No wire peer can supply this callback. A13 owns durable reconciliation.
        """
        with self._lock:
            s = self._sessions.get(session_id)
            require(s is not None and s.registered, 'Bridge session unavailable')
            matches = [(aid, p) for aid, p in s.results.items() if p['id'] == receipt_id]
            require(len(matches) == 1, 'Bridge result unavailable')
            aid, receipt = matches[0]
            persist(deepcopy(receipt))
            # A reentrant integration callback must not ACK a revoked session.
            require(self._sessions.get(session_id) is s, 'Bridge session unavailable')
            raw = self._send(session_id, s, 'result_ack', dict(action_id=aid, receipt_id=receipt_id))
            del s.results[aid]
            del s.pending[aid]
            return raw

    def dispatch(self, session_id, action, *, evidence_ids):
        """Trusted executor only, AFTER budget/policy gates. Claim consent before send.

        No retry after a lost send: caller reconciles uncertainty. A valid wire
        approval is compared to A03's durable decision, never trusted by itself.
        """
        with self._lock:
            s = self._sessions.get(session_id)
            require(s is not None and s.registered and s.credential.identity.credential_role == 'native_bridge',
                    'Bridge authentication failed', 'bridge_unauthorized')
            p = validate('BrowserAction', action)
            intent = p['intent']
            c = self._context(s.credential.identity, intent['url'])
            require(intent['task_id'] == c.task_id and intent['snapshot_id'] == c.snapshot_id,
                    'Native snapshot mismatch', 'stale_snapshot')
            require(intent['action_id'] not in s.used_actions and len(s.used_actions) < 25 and not s.pending,
                    'Bridge action unavailable')
            proposal_id = None
            if p['approval'] is not None:
                a = p['approval']
                proposal_id = a['proposal_id']
                d = self._approvals.get(proposal_id)
                require(d is not None and a['approved_at_ms'] == d['decided_at_ms'] and
                        a['expires_at_ms'] == d['expires_at_ms'], 'Stale app approval', 'stale_approval')
                self._approvals.consume(proposal_id, intent=intent, evidence_ids=evidence_ids)
            # Reserve even if encoding/delivery fails: effects must never be replayed.
            s.used_actions.add(intent['action_id'])
            s.pending[intent['action_id']] = dict(intent=intent, proposal_id=proposal_id)
            return self._send(session_id, s, 'command', p)

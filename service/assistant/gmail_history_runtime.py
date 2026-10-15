"""Explicit history controls over a trusted source-owned bridge.

No default source, transport, scheduler or restored permission. Host callbacks
read one immutable memory value; they never consult settings, SQLite or config.
The application must synchronously withdraw this host before changing grants.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
import time
from typing import Protocol

from service.gmail_behavior_history import AccountIdentity, HistoryScope, Limits, history_source_guard
from service.gmail_source import HistoryPermissionState as Permission
from service.config import authority_changes


class HistoryUnavailable(ValueError):
    pass


class HistoryConflict(ValueError):
    pass


class BridgeProtocol(Protocol):
    def status(self) -> dict: ...
    def snapshot(self, *, expected_receipt=None, required_refs=()): ...
    def invalidate(self, reason=""): ...
    async def step(self, *, http, budget): ...


class BackendProtocol(Protocol):
    """Installed by trusted application composition, never by an HTTP payload.

    open must bind the configured source lease and this zero-I/O probe using
    the source's public application/grant APIs. It may not read credentials or
    mint account authority in behavior code.
    """
    def accounts(self) -> tuple[AccountIdentity, ...]: ...
    def open(self, account: AccountIdentity, scope: HistoryScope, probe) -> BridgeProtocol: ...


class PublicHistoryBackend:
    """Cached configured accounts and source-issued grants, with no implicit I/O.

    Existing Gmail connection/configuration work publishes the account registry.
    Construction and account/status reads do not refresh it. A caller supplies an
    explicit source-owned HTTP client and budget to each later host step.
    """
    def __init__(self, *, clock=time.time, monotonic=time.monotonic):
        self._clock, self._monotonic = clock, monotonic

    def accounts(self):
        from service import gmail_source
        return tuple(row.account for row in gmail_source.history_account_status())

    def observe_accounts(self):
        """Explicit worker boundary only; getters never call this method."""
        from service import gmail_source
        return gmail_source.refresh_history_configuration()

    def account_labels(self):
        from service import gmail_source
        return {row.account.account_id:row.display_label for row in gmail_source.history_account_status()}

    def open(self, account, scope, probe):
        from service import gmail_source
        # Nothing here reads config, creates a client or performs network I/O.
        # The probe is the host's atomic trusted memory projection, never JSON.
        with history_source_guard():
            lease = gmail_source.history_account_lease(account.account_id)
            if lease is None or lease.account != account:
                raise HistoryUnavailable("Gmail account changed before Start")
            application = gmail_source.install_history_application(probe=probe)
            bridge = None
            try:
                grant = application.issue(lease, scope)
                bridge = gmail_source.new_public_history_bridge(application=application,
                    limits=Limits(80000,8000000,32000000,1200),
                    clock=self._clock,monotonic=self._monotonic)
                bridge.start(grant)
            except Exception:
                application.revoke()
                raise
        return _PublicJob(application, bridge, account, scope)


class _PublicJob:
    """Each Start owns one terminal application installation and fresh grant."""
    def __init__(self, application, bridge, account, scope):
        self.application, self.bridge = application, bridge
        self.account, self.scope = account, scope

    def status(self):
        return self.bridge.status()

    def snapshot(self, *, expected_receipt=None, required_refs=()):
        return self.bridge.snapshot(expected_receipt=expected_receipt,required_refs=required_refs)

    def invalidate(self, reason="Application permission withdrawn"):
        with history_source_guard():
            self.application.revoke()
            self.bridge.invalidate(reason)

    async def step(self, *, http, budget):
        return await self.bridge.step(http=http,budget=budget)

    def handoff(self):
        from service import gmail_source
        with history_source_guard():
            state = self.bridge.status()["continuation"]
            if state["blocked_reason"]:
                try:
                    self.bridge.prepare_continuation()  # Source stops truthful partial.
                except PermissionError:
                    return False
            if not state["available"]:
                return False
            lease = gmail_source.history_account_lease(self.account.account_id)
            if lease is None or lease.account != self.account:
                raise HistoryUnavailable("Gmail account changed before continuation")
            grant = self.application.issue(lease,self.scope)
            handle = self.bridge.prepare_continuation()
            self.bridge.continue_scan(handle,grant=grant)
            return True


@dataclass
class _Job:
    account: AccountIdentity
    scope: HistoryScope
    revision: int
    permission: Permission | None = None
    bridge: BridgeProtocol | None = None
    state: str = "off"
    authority: object = None
    detail: str = ""


class HistoryHost:
    def __init__(self, *, backend: BackendProtocol | None = None):
        self._backend = backend
        self._lock = threading.RLock()
        self._session = secrets.token_hex(24)
        self._policy = None
        self._policy_authority = None
        self._policy_revision = 0
        self._role_revision = 0
        self._jobs = {}
        self._revisions = {}
        self._closed = False
        self._unsubscribe_authority = authority_changes.subscribe(self._configuration_changed)

    def _configuration_changed(self, _snapshot):
        self.invalidate("Configuration authority changed")

    @staticmethod
    def _permission(job):
        # This is the callback used under the source guard: one immutable
        # pointer comparison, no locks, role/config resolution or I/O.
        current = authority_changes.current()
        return job.permission if current.ready and current is job.authority else None

    def _withdraw(self, reason):
        # Lock order: host -> source guard. The source's callback never takes
        # this host lock, so source publication cannot reverse that order.
        with history_source_guard():
            jobs = tuple(self._jobs.values())
            for job in jobs:
                job.permission = None
                job.state = "paused"
            for job in jobs:
                if job.bridge is not None:
                    job.bridge.invalidate(reason)

    def bind(self, *, enabled, settings, epoch, role):
        """Trusted server settings snapshot; calling this does not start a job."""
        with self._lock:
            policy = (enabled is True, settings.get("email") is True, str(epoch), str(role))
            authority = authority_changes.current()
            if not authority.ready:
                self.invalidate("Configuration authority is unsettled")
                return
            if policy != self._policy or authority is not self._policy_authority:
                self._withdraw("Application permission changed")
                if self._policy is None or policy[3] != self._policy[3]:
                    self._role_revision += 1
                self._policy_revision += 1
                self._policy = policy
                self._policy_authority = authority

    def invalidate(self, reason="Application permission withdrawn", *, close=False):
        with self._lock:
            self._withdraw(reason)
            self._policy_revision += 1
            self._policy = None
            self._policy_authority = None
            if close:
                self._closed = True
                self._unsubscribe_authority()

    def _accounts(self):
        if self._backend is None or self._closed:
            return ()
        try:
            rows = self._backend.accounts()
            if not isinstance(rows, tuple) or any(not isinstance(a, AccountIdentity) for a in rows):
                return ()
            ids = [a.account_id for a in rows]
            return rows if len(ids) == len(set(ids)) else ()
        except Exception:
            return ()

    @staticmethod
    def _continuation(value):
        if not isinstance(value,dict) or type(value.get("schema")) is not int or value["schema"] != 1:
            return None
        if (type(value.get("segment")) is not int or not 0 <= value["segment"] <= 32
                or type(value.get("maximum_segments")) is not int or value["maximum_segments"] != 32
                or any(type(value.get(name)) is not bool for name in ("handoff_due","available","incomplete"))
                or not isinstance(value.get("blocked_reason"),str)
                or value["blocked_reason"] not in {"","no_progress","segment_limit"}):
            return None
        return {name:value[name] for name in ("schema","segment","maximum_segments","handoff_due",
                "available","incomplete","blocked_reason")}

    def status(self):
        """Pure metadata. Source status must not fetch or renew any evidence."""
        with self._lock:
            accounts = self._accounts()
            try:
                labels = self._backend.account_labels() if self._backend is not None else {}
            except (AttributeError, TypeError, ValueError):
                labels = {}
            rows = []
            for account in accounts:
                job = self._jobs.get(account.account_id)
                counts = {}
                continuation = None
                detail = job.detail if job else ""
                if job is not None and job.account == account and job.permission is not None and job.bridge is not None:
                    value = job.bridge.status()
                    for name in ("listed", "fetched", "eligible", "failed", "evicted", "cached",
                                 "cached_eligible", "pending", "http_calls"):
                        if type(value.get(name)) is int and value[name] >= 0:
                            counts[name] = value[name]
                    for name in ("has_next_page", "eof_observed", "capped", "fresh"):
                        counts[name] = value.get(name) is True
                    state = value.get("state") if isinstance(value.get("state"), str) else "unavailable"
                    continuation = self._continuation(value.get("continuation"))
                    if state not in {"running","waiting","exhausted","capped","partial"}:
                        counts = {}
                        detail = "Current history evidence is unavailable or expired. Start again to resume."
                    elif state == "partial":
                        detail = "The scan stopped with partial coverage. A new Start requires current permission."
                else:
                    state = job.state if job is not None else "off"
                rows.append(dict(account_id=account.account_id, connection_revision=account.connection_revision,
                    display_label=(labels.get(account.account_id, "") if isinstance(labels,dict)
                        and isinstance(labels.get(account.account_id,""),str) else ""),
                    account_set_revision=account.account_set_revision,
                    control_revision=self._revisions.get(account.account_id, 0), state=state, counts=counts,
                    detail=detail, continuation=continuation,
                    semantic_processing="separate", scope=(dict(start=job.scope.start, end=job.scope.end,
                        selection=job.scope.selection, message_cap=job.scope.message_cap) if job else None)))
            return dict(version=1, session=self._session,
                available=self._backend is not None and not self._closed, accounts=rows,
                permission_revision=self._policy_revision,
                detail="Explicitly start a bounded history scan; fetching and interpretation have separate progress."
                    if self._backend is not None else "Gmail history is unavailable until the source connection is ready.")

    def control(self, payload):
        allowed = {"command", "account_id", "connection_revision", "account_set_revision", "revision", "scope",
                   "session", "permission_revision"}
        if (not isinstance(payload, dict) or set(payload)-allowed
                or payload.get("command") not in {"start", "pause", "reset"}
                or not isinstance(payload.get("account_id"), str)
                or type(payload.get("revision")) is not int or payload["revision"] < 0):
            raise ValueError("A current account, revision and supported history command are required")
        with self._lock, history_source_guard():
            ident, command = payload["account_id"], payload["command"]
            revision = self._revisions.get(ident, 0)
            if (self._closed or payload["revision"] != revision or payload.get("session") != self._session
                    or type(payload.get("permission_revision")) is not int
                    or payload["permission_revision"] != self._policy_revision):
                raise HistoryConflict("History control changed; refresh before trying again")
            account = next((a for a in self._accounts() if a.account_id == ident), None)
            job = self._jobs.get(ident)
            if account is None:
                if job is not None:
                    job.permission = None
                    if job.bridge is not None:
                        job.bridge.invalidate("Source account unavailable")
                raise HistoryUnavailable("Connected Gmail account is unavailable")
            if (payload.get("connection_revision") != account.connection_revision
                    or payload.get("account_set_revision") != account.account_set_revision):
                raise HistoryConflict("Gmail connection changed; refresh before trying again")
            if command == "start":
                authority = authority_changes.current()
                if (self._policy is None or self._policy[:2] != (True, True) or not authority.ready
                        or authority is not self._policy_authority):
                    raise HistoryUnavailable("Enable learning and email evidence before starting history")
                scope_data = payload.get("scope")
                if not isinstance(scope_data, dict) or set(scope_data) != {"start", "end", "selection", "message_cap"}:
                    raise ValueError("An explicit bounded history scope is required")
                scope = HistoryScope(**scope_data)
            elif "scope" in payload:
                raise ValueError("Pause and reset do not change the history scope")
            if job is not None:
                job.permission = None
                job.state = "paused"
                if job.bridge is not None:
                    job.bridge.invalidate("History control changed")
            revision += 1
            self._revisions[ident] = revision
            if command == "reset":
                self._jobs.pop(ident, None)
            elif command == "start":
                job = _Job(account, scope, revision)
                job.authority = authority
                self._jobs[ident] = job
                job.permission = Permission((ident,), (scope.digest,), self._session, 1,
                    "gmail_history", self._role_revision, self._policy_revision, True, True, True,
                    ((ident,scope.digest),))
                try:
                    job.bridge = self._backend.open(account, scope, lambda: self._permission(job))
                    job.state = "waiting"
                except Exception:
                    job.permission = None
                    job.state = "unavailable"
                    if job.bridge is not None:
                        job.bridge.invalidate("History start failed")
                    raise HistoryUnavailable("Gmail history could not start") from None
            return self.status()

    def worker_jobs(self):
        """Pure current explicitly started jobs; never restores an old Start."""
        with self._lock:
            accounts = self._accounts()
            rows = []
            for ident, job in self._jobs.items():
                if self._permission(job) is None or job.bridge is None or job.account not in accounts:
                    continue
                value = job.bridge.status()
                state = value.get("state")
                if state not in {"waiting","running","capped","exhausted","partial"}:
                    continue
                pending = value.get("pending",0)
                rows.append(dict(account_id=ident,revision=job.revision,
                    continuation=self._continuation(value.get("continuation")),
                    acquisition_pending=(state != "partial" and
                        (state in {"waiting","running"} or type(pending) is int and pending > 0))))
            return tuple(rows)

    def continue_job(self, ident, revision):
        with self._lock, history_source_guard():
            job = self._jobs.get(ident)
            if (job is None or job.revision != revision or self._permission(job) is None
                    or job.bridge is None or job.account not in self._accounts()):
                raise HistoryConflict("History changed before continuation")
            handoff = getattr(job.bridge,"handoff",None)
            return handoff() if handoff is not None else False

    def observe_accounts(self):
        # Resolve source configuration outside both the host and source locks.
        observe = getattr(self._backend,"observe_accounts",None)
        if observe is not None and not self._closed:
            observe()

    def pause_job(self, ident, revision, reason):
        """Withdraw only the exact failing worker, without pausing a new Start."""
        with self._lock, history_source_guard():
            job = self._jobs.get(ident)
            if job is None or job.revision != revision:
                return
            job.permission = None
            job.state = "paused"
            job.detail = reason
            if job.bridge is not None:
                job.bridge.invalidate("paused")
            self._revisions[ident] = self._revisions.get(ident,0)+1

    def cached_getters(self):
        with self._lock:
            getters = []
            accounts = self._accounts()
            for ident, job in self._jobs.items():
                if self._permission(job) is None or job.bridge is None or job.account not in accounts:
                    continue
                def getter(*, expected_receipt=None, required_refs=(), ident=ident, job=job):
                    with self._lock:
                        if (self._closed or self._jobs.get(ident) is not job or self._permission(job) is None
                                or job.account not in self._accounts()):
                            return None
                        return job.bridge.snapshot(expected_receipt=expected_receipt, required_refs=required_refs)
                getters.append(getter)
            return tuple(getters)

    async def step(self, ident, *, http, budget, revision=None):
        """One explicitly budgeted source step; no scheduler or default transport."""
        with self._lock:
            job = self._jobs.get(ident)
            if (self._closed or job is None or self._permission(job) is None or job.bridge is None
                    or job.account not in self._accounts()
                    or revision is not None and revision != job.revision):
                raise HistoryUnavailable("No current explicitly started history job")
            bridge = job.bridge
        await bridge.step(http=http, budget=budget)
        with self._lock:
            if (self._jobs.get(ident) is not job or self._permission(job) is None
                    or job.account not in self._accounts()):
                raise HistoryConflict("History permission changed during source work")
            return self.status()

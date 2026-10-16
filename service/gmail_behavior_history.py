"""Explicit, disabled-by-default Gmail history provider foundation.

There is deliberately no production caller, token source, HTTP client, model call,
startup job or disk store here. An application adapter must supply a synchronous,
zero-I/O admission callback from trusted current account/consent state. Control
payloads and checkpoints never install that authority. A later consumer must
recheck snapshot(expected_receipt=..., required_refs=...) before committing work.

Gmail list/get semantics: developers.google.com/workspace/gmail/api/reference/rest/
v1/users.messages/{list,get}; inline MIME bodies: v1/users.messages.attachments.
EOF below means the selected bounded query's pagination ended, not a stable
mailbox snapshot, attachment completeness, or semantic processing completion.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
import weakref
from contextlib import contextmanager
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from email.message import Message
from typing import Awaitable, Callable

from service.publication_authority import Authority, Clock, Witness, revocation

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_LOCK = threading.RLock()


@contextmanager
def history_source_guard():
    """Shared source/application/cache publication lock; no I/O or await inside.

    The application revocation barrier is outermost, before source/cache locks.
    Configuration writers enter that barrier before their I/O mutex, release
    this source guard for I/O, then publish. Admission probes only read memory
    and must never acquire a configuration/settings/SQLite/network lock.
    """
    with revocation(), _LOCK:
        yield


def _positive(value, maximum, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value <= maximum or not math.isfinite(value)
            or (integer and not isinstance(value, int))):
        raise ValueError("History budget outside the approved envelope")


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class AccountIdentity:
    """Configured provider identity; labels and RFC Message-ID are not authority."""
    provider: str
    account_id: str
    connection_revision: str
    account_set_revision: str

    def __post_init__(self):
        if self.provider != "gmail" or not all(_identifier(v) for v in (
                self.account_id, self.connection_revision, self.account_set_revision)):
            raise ValueError("Configured Gmail account identity required")


@dataclass(frozen=True)
class Admission:
    account: AccountIdentity
    consent_epoch: int
    session_epoch: int
    explicit_start: bool
    global_learning: bool
    email_consent: bool
    scope_digest: str

    def allows(self, scope):
        return (isinstance(self.account, AccountIdentity)
                and type(self.consent_epoch) is int and self.consent_epoch >= 0
                and type(self.session_epoch) is int and self.session_epoch >= 0
                and self.explicit_start is True and self.global_learning is True
                and self.email_consent is True and self.scope_digest == scope.digest)


@dataclass(frozen=True)
class HistoryAccessToken:
    """Trusted token adapter must bind its result to the same configured session.

    This carrier prevents an accidental other-account token adapter being used
    for a job. It is not a substitute for a trusted adapter validating the token's
    actual configured owner. Never persist, log, or include it in checkpoints.
    """
    admission: Admission
    value: str = field(repr=False)


@dataclass(frozen=True)
class HistoryScope:
    """Explicit provider query using epoch seconds: start < internalDate < end."""
    start: int
    end: int
    selection: str
    message_cap: int

    def __post_init__(self):
        if (type(self.start) is not int or type(self.end) is not int
                or not 0 <= self.start < self.end or self.end - self.start > 730 * 86400
                or self.selection not in ("inbox", "mailbox")):
            raise ValueError("Explicit bounded Gmail range and selection required")
        _positive(self.message_cap, 2000, integer=True)

    @property
    def digest(self):
        return _digest(asdict(self))


@dataclass(frozen=True)
class Limits:
    body_bytes: int
    account_cache_bytes: int
    global_cache_bytes: int
    ttl_seconds: float

    def __post_init__(self):
        for value, maximum in ((self.body_bytes, 80000), (self.account_cache_bytes, 8000000),
                               (self.global_cache_bytes, 32000000)):
            _positive(value, maximum, integer=True)
        _positive(self.ttl_seconds, 1200)


@dataclass(frozen=True)
class StepBudget:
    page_size: int
    full_bodies: int
    http_calls: int
    accepted_body_bytes: int
    dispatch_seconds: float

    def __post_init__(self):
        for value, maximum in ((self.page_size, 100), (self.full_bodies, 5), (self.http_calls, 6),
                               (self.accepted_body_bytes, 400000)):
            _positive(value, maximum, integer=True)
        _positive(self.dispatch_seconds, 30)


@dataclass(frozen=True)
class HistoryBody:
    text: str
    status: str
    revision: str
    omissions: tuple[str, ...] = ()

    @property
    def end_offset(self):
        """Offsets are Unicode code points into this exact normalized text."""
        return len(self.text)

    @property
    def eligible(self):
        return self.status == "complete" and bool(self.text.strip())


def parse_history_body(payload: dict, *, byte_cap: int) -> HistoryBody:
    """Conservative inline plain text only; never use the display body's clip.

    CRLF/CR -> LF is the only normalization. No HTML heuristics, replacement
    decoding, external attachment downloads, title fallbacks, or silent omission.
    Complete denotes supported inline plain text, not the entire MIME message.
    """
    _positive(byte_cap, 80000, integer=True)
    texts, omissions, problems = [], [], []
    visited = 0
    used_bytes = 0

    def walk(part, depth=0):
        nonlocal visited, used_bytes
        visited += 1
        if not isinstance(part, dict) or depth > 30 or visited > 1000:
            problems.append("unsupported")
            return
        mime = part.get("mimeType", "")
        if not isinstance(mime, str):
            problems.append("unsupported")
            return
        if part.get("filename"):
            omissions.append("attachment")
            return
        if mime.startswith("multipart/"):
            children = part.get("parts")
            if not isinstance(children, list) or not children or len(children) > 1000:
                problems.append("omitted")
                return
            if mime == "multipart/alternative":
                plain = [p for p in children if isinstance(p, dict) and p.get("mimeType") == "text/plain"
                         and not p.get("filename")]
                if len(plain) == 1:
                    omissions.append("alternative_representations")
                    walk(plain[0], depth + 1)
                    return
                problems.append("unsupported")
                return
            if mime != "multipart/mixed":
                problems.append("unsupported")
                return
            for child in children:
                walk(child, depth + 1)
            return
        body = part.get("body")
        if mime != "text/plain":
            problems.append("unsupported")
            return
        if not isinstance(body, dict) or body.get("attachmentId") or "data" not in body:
            problems.append("omitted")
            return
        data = body["data"]
        if (not isinstance(data, str) or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", data)):
            problems.append("decode_error")
            return
        # Reject an oversized representation before allocating decoded raw bytes.
        if len(data) > 4 * ((byte_cap + 2) // 3) + 4:
            problems.append("truncated")
            return
        try:
            raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
            if type(body.get("size")) is not int or body["size"] != len(raw):
                problems.append("decode_error")
                return
            content_type = Message()
            headers = part.get("headers", [])
            if not isinstance(headers, list):
                raise ValueError("Invalid headers")
            for header in headers:
                if not isinstance(header, dict):
                    raise ValueError("Invalid header")
                if str(header.get("name", "")).lower() == "content-type":
                    content_type["Content-Type"] = str(header.get("value", ""))
            charset = content_type.get_content_charset() or "utf-8"
            if charset.lower() not in ("utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1"):
                problems.append("unsupported")
                return
            text = raw.decode(charset, errors="strict").replace("\r\n", "\n").replace("\r", "\n")
            encoded = text.encode("utf-8")
            if texts and used_bytes >= byte_cap:
                problems.append("truncated")
                return
            remaining = max(0, byte_cap - used_bytes - (1 if texts else 0))
            if len(encoded) > remaining:
                text = encoded[:remaining].decode("utf-8", errors="ignore")
                problems.append("truncated")
            texts.append(text)
            used_bytes += len(text.encode("utf-8")) + (1 if len(texts) > 1 else 0)
        except (ValueError, LookupError, UnicodeError, binascii.Error):
            problems.append("decode_error")

    walk(payload)
    text = "\n".join(texts)
    status = next((s for s in ("decode_error", "truncated", "unsupported", "omitted") if s in problems), "complete")
    return HistoryBody(text, status, hashlib.sha256(text.encode()).hexdigest(), tuple(sorted(set(omissions))))


@dataclass(frozen=True)
class TimeQualification:
    """Provider ordering is never verified utterance/event time in P1."""
    provider_internal_at: float | None
    provider_basis: str
    verified_event_at: None = None
    event_origin: str = "unknown"
    relative_deadlines_supported: bool = False

    def __post_init__(self):
        stamp = self.provider_internal_at
        if ((stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp) or stamp < 0))
                or self.provider_basis != ("unknown" if stamp is None else "provider_internal_date_unverified")
                or self.verified_event_at is not None or self.event_origin != "unknown"
                or self.relative_deadlines_supported is not False):
            raise ValueError("Unsupported source-time qualification")

    def to_metadata(self):
        return {"schema": 1, "kind": "gmail_history_time", "value": asdict(self)}

    @classmethod
    def from_metadata(cls, data):
        return _decode_metadata(cls, data, "gmail_history_time")


@dataclass(frozen=True)
class RecordRef:
    account: AccountIdentity
    job_token: str
    resource_id: str
    revision: str
    proof_digest: str
    time_qualification: TimeQualification

    def to_metadata(self):
        return {"schema": 1, "kind": "gmail_history_ref", "value": asdict(self)}

    @classmethod
    def from_metadata(cls, data):
        return _decode_metadata(cls, data, "gmail_history_ref")


@dataclass(frozen=True)
class HistoryRecord:
    account: AccountIdentity
    job_token: str
    resource_id: str
    rfc_message_id: str
    provider_internal_at: float | None
    body: HistoryBody
    read_started_at: float
    fetch_started_at: float
    received_at: float
    published_at: float
    expires_at: float
    generation: int
    proof_digest: str

    @property
    def ref(self):
        return RecordRef(self.account, self.job_token, self.resource_id, self.body.revision,
                         self.proof_digest, self.time_qualification)

    @property
    def time_qualification(self):
        return TimeQualification(self.provider_internal_at,
            "unknown" if self.provider_internal_at is None else "provider_internal_date_unverified")

    @property
    def event_at(self):
        """No supported verified source-event time is available from this reader."""
        return None

    @property
    def event_origin(self):
        return "unknown"


@dataclass(frozen=True)
class HistoryReceipt:
    account: AccountIdentity
    consent_epoch: int
    session_epoch: int
    job_token: str
    scope_digest: str
    generation: int
    read_started_at: float
    published_at: float
    expires_at: float
    proof_digest: str
    refs: tuple[RecordRef, ...]

    def to_metadata(self):
        """Serializable body-free recheck input, never restored permission."""
        return {"schema": 1, "kind": "gmail_history_receipt", "value": asdict(self)}

    @classmethod
    def from_metadata(cls, data):
        return _decode_metadata(cls, data, "gmail_history_receipt")


def _decode_metadata(cls, data, kind):
    """Strict versioned public carrier parsing; only a live source admits it."""
    try:
        if (not isinstance(data, dict) or set(data) != {"schema", "kind", "value"}
                or type(data["schema"]) is not int or data["schema"] != 1 or data["kind"] != kind):
            raise ValueError()
        if not isinstance(data["value"], dict):
            raise ValueError()
        value = dict(data["value"])
        if set(value) != set(cls.__dataclass_fields__):
            raise ValueError()
        if cls is TimeQualification:
            return cls(**value)
        value["account"] = AccountIdentity(**value["account"])
        if cls is RecordRef:
            value["time_qualification"] = TimeQualification(**value["time_qualification"])
            if (not _identifier(value["job_token"])
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value["resource_id"])
                    or not all(re.fullmatch(r"[a-f0-9]{64}", value[k]) for k in ("revision", "proof_digest"))):
                raise ValueError()
        else:
            if not isinstance(value["refs"], (list, tuple)) or len(value["refs"]) > 2000:
                raise ValueError()
            value["refs"] = tuple(RecordRef.from_metadata(
                {"schema": 1, "kind": "gmail_history_ref", "value": ref}) for ref in value["refs"])
            if (not _identifier(value["job_token"])
                    or not all(type(value[k]) is int and value[k] >= 0
                               for k in ("consent_epoch", "session_epoch", "generation"))
                    or not all(isinstance(value[k], (int, float)) and not isinstance(value[k], bool)
                               and math.isfinite(value[k]) and value[k] >= 0
                               for k in ("read_started_at", "published_at", "expires_at"))
                    or not all(re.fullmatch(r"[a-f0-9]{64}", value[k]) for k in ("scope_digest", "proof_digest"))):
                raise ValueError()
        return cls(**value)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("Invalid Gmail history metadata schema") from None


@dataclass(frozen=True)
class HistorySnapshot:
    """complete covers required refs only; query/semantic coverage is in status."""
    receipt: HistoryReceipt
    records: tuple[HistoryRecord, ...]
    missing_refs: tuple[RecordRef, ...]
    complete: bool
    # Cache/job proof only. The public bridge must additionally qualify its
    # configured account, application permission and credential recovery gate.
    cache_witness: Witness | None = field(default=None, compare=False, repr=False)
    publication_witness: Witness | None = field(default=None, compare=False, repr=False)


# Raw bodies live only here. Owners are weak, so dropping a source drops its
# entries on the next operation. The strictest live owner's global cap applies.
class _Cache:
    def __init__(self):
        self.entries = OrderedDict()
        self.owners = weakref.WeakKeyDictionary()

    def sweep(self):
        for owner in list(self.owners):
            if owner._job is None or not owner._current(owner._job):
                owner.invalidate()
        for key, (owner_ref, row) in list(self.entries.items()):
            owner = owner_ref()
            if owner is None or not owner._record_current(row):
                self._drop(key)

    def _drop(self, key):
        item = self.entries.get(key)
        if item is not None:
            owner_ref,row = item
            owner = owner_ref()
            if owner is not None:
                owner._withdraw_record(row.ref)
            self.entries.pop(key, None)

    def remove(self, owner):
        for key in list(self.entries):
            if key[0] == owner._cache_id:
                self._drop(key)

    def rows(self, owner):
        self.sweep()
        return [row for ref, row in self.entries.values() if ref() is owner]

    def put(self, owner, row):
        self.sweep()
        if not owner._record_current(row):
            raise _Withdrawn()
        key = (owner._cache_id, row.resource_id)
        self._drop(key)
        owner._publish_record(row)
        self.entries[key] = (weakref.ref(owner), row)
        self.owners[owner] = owner.limits.global_cache_bytes
        global_cap = min(self.owners.values())
        def size(rows):
            return sum(len(item.body.text.encode()) for item in rows)
        while (size(r for _, r in self.entries.values()) > global_cap
               or size(self.rows(owner)) > owner.limits.account_cache_bytes):
            own_over = size(self.rows(owner)) > owner.limits.account_cache_bytes
            key = next(k for k, (ref, _) in self.entries.items() if not own_over or ref() is owner)
            ref, _ = self.entries[key]
            self._drop(key)
            victim = ref()
            if victim is not None and victim._job is not None:
                victim._job.evicted += 1


_CACHE = _Cache()


@dataclass
class _Job:
    admission: Admission
    scope: HistoryScope
    token: str
    read_started_at: float
    monotonic_started_at: float
    pending: list[str] = field(default_factory=list)
    listed: dict[str, None] = field(default_factory=dict)
    page_tokens: set[str] = field(default_factory=set)
    next_page: str = ""
    eof: bool = False
    capped: bool = False
    fetched: int = 0
    eligible: int = 0
    failed: int = 0
    evicted: int = 0
    http_calls: int = 0
    error: str = ""
    segment: int = 1
    segment_listed: int = 0
    segment_fetched: int = 0
    stopped: bool = False


class _Withdrawn(Exception):
    pass


class _BudgetStop(Exception):
    pass


class _HTTP:
    """The token adapter receives only this counted, epoch-checked facade.

    No hidden token retries/alternate HTTP clients are allowed by this contract.
    A supplied client must disable redirects and transport retries; P1 fake
    clients dispatch once. Each request here counts before the single await.
    """
    def __init__(self, provider, job, http, budget):
        self.provider, self.job, self.http, self.budget = provider, job, http, budget
        self.calls = 0
        self.started = provider._monotonic()

    @property
    def admission(self):
        return self.job.admission

    async def request(self, method, url, **kwargs):
        with revocation(), _LOCK:
            self.provider._require(self.job)
            if (self.calls >= self.budget.http_calls
                    or not 0 <= self.provider._monotonic() - self.started < self.budget.dispatch_seconds):
                raise _BudgetStop()
            valid_get = (method == "GET" and (url in (API + "/messages", API + "/profile") or
                re.fullmatch(re.escape(API) + r"/messages/[A-Za-z0-9_-]{1,256}", url)))
            if not (valid_get or (method == "POST" and url == TOKEN_URL)):
                raise ValueError("History transport endpoint not allowed")
            self.calls += 1
            self.job.http_calls += 1
        response = await self.http.request(method, url, **{**kwargs, "follow_redirects": False})
        with revocation(), _LOCK:
            self.provider._require(self.job)
        return response

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)


class HistoryProvider:
    """One explicit account history job, with pure current cached getters.

    The admission callable is trusted application code, not a control payload.
    Equality binds account/connection/account-set/consent/session/scope epochs.
    No built-in adapter currently enables this provider in Wisp.
    """
    def __init__(self, *, admission: Callable[[], Admission | None], limits: Limits,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        if not callable(admission) or not isinstance(limits, Limits):
            raise ValueError("Trusted admission callback and explicit limits required")
        self._admission, self.limits = admission, limits
        self._clock, self._monotonic = clock, monotonic
        self._cache_id = secrets.token_hex(16)
        self._proof_key = secrets.token_bytes(32)
        self._job = None
        self._generation = 0
        self._receipt = None
        self._state = "off"
        self._stepping = False
        self._publication = Authority("gmail-history-cache-job", owner=self)
        self._record_stamps = {}
        self._publication_clock = None

    def _withdraw_record(self, ref):
        stamp = self._record_stamps.get(ref)
        if stamp is not None:
            stamp.withdraw()
            self._record_stamps.pop(ref, None)

    def _publish_record(self, row):
        if row.body.eligible:
            stamp = Authority("gmail-history-cache-record", owner=self)
            stamp.publish()
            self._record_stamps[row.ref] = stamp

    def _start_publication(self):
        # Only builtin clocks run from a pure production witness. Injected
        # synthetic clock callbacks are sampled during source preparation.
        self._publication_clock = Clock(live=self._clock is time.time and self._monotonic is time.monotonic)
        self._sample_publication_clock()
        self._publication.publish()

    def _sample_publication_clock(self):
        if self._publication_clock.live:
            # A currentness reader can use this exposed clock concurrently.
            # Production sampling must be inside its own ordering operation.
            self._publication_clock.read()
        else:
            # Injected callbacks run only during preparation, never from a
            # publication/currentness check of the resulting numeric clock.
            self._publication_clock.observe(self._clock(), self._monotonic())

    def _cache_witness(self, job, selected):
        if not selected or any(not row.body.eligible for row in selected):
            return None
        self._sample_publication_clock()
        tokens = [self._publication.token(clock=self._publication_clock,
            expires_wall=job.read_started_at+self.limits.ttl_seconds,
            expires_monotonic=job.monotonic_started_at+self.limits.ttl_seconds)]
        tokens.extend(self._record_stamps[row.ref].token() if row.ref in self._record_stamps else None
                      for row in selected)
        return Witness.combine(*tokens)

    def _current_admission(self):
        try:
            value = self._admission()
            return value if isinstance(value, Admission) else None
        except Exception:
            return None

    def _current(self, job):
        now, mono = self._clock(), self._monotonic()
        admission = self._current_admission()
        return (self._job is job and admission == job.admission
                and job.admission.allows(job.scope)
                and math.isfinite(now) and math.isfinite(mono)
                and 0 <= now - job.read_started_at < self.limits.ttl_seconds
                and 0 <= mono - job.monotonic_started_at < self.limits.ttl_seconds)

    def _require(self, job):
        if not self._current(job):
            if self._job is job:
                self.invalidate("invalidated")
            raise _Withdrawn()

    def _record_current(self, row):
        job = self._job
        return (job is not None and row.job_token == job.token and self._current(job)
                and row.read_started_at <= row.fetch_started_at <= row.received_at
                <= row.published_at <= self._clock() < row.expires_at)

    def invalidate(self, reason="invalidated"):
        """Synchronous withdrawal: call before Forget/revoke/disconnect awaits."""
        with revocation(), _LOCK:
            self._publication.withdraw()
            _CACHE.remove(self)
            _CACHE.owners.pop(self, None)
            self._job = self._receipt = None
            self._generation += 1
            self._state = reason if reason in (
                "paused", "cancelled", "revoked", "forgotten", "disconnected", "invalidated") else "invalidated"

    def control(self, action, *, scope=None):
        with revocation(), _LOCK:
            actions = {"pause": "paused", "cancel": "cancelled", "revoke": "revoked", "forget": "forgotten"}
            if action in actions:
                self.invalidate(actions[action])
                return self.status()
            if action != "start" or not isinstance(scope, HistoryScope):
                raise ValueError("Explicit history action and scope required")
            admission = self._current_admission()
            if admission is None or not admission.allows(scope):
                self.invalidate("revoked")
                raise PermissionError("Current account and explicit learning/email consent required")
            self.invalidate()
            now, mono = self._clock(), self._monotonic()
            if not math.isfinite(now) or not math.isfinite(mono) or now < 0:
                raise ValueError("Finite history clock required")
            self._job = _Job(admission, scope, secrets.token_hex(24), now, mono)
            self._start_publication()
            _CACHE.owners[self] = self.limits.global_cache_bytes
            self._state = "running"
            return self.status()

    def status(self):
        """No HTTP, token, lazy provider initialization or freshness renewal."""
        with revocation(), _LOCK:
            job = self._job
            if job is not None and not self._current(job):
                self.invalidate()
                job = None
            rows = _CACHE.rows(self)
            if self._job is not job:
                job = self._job
            return {"state": self._state, "listed": len(job.listed) if job else 0,
                    "fetched": job.fetched if job else 0, "eligible": job.eligible if job else 0,
                    "failed": job.failed if job else 0, "evicted": job.evicted if job else 0,
                    "cached": len(rows), "cached_eligible": sum(r.body.eligible for r in rows),
                    "pending": len(job.pending) if job else 0, "has_next_page": bool(job and job.next_page),
                    "eof_observed": bool(job and job.eof), "capped": bool(job and job.capped),
                    "http_calls": job.http_calls if job else 0, "error": job.error if job else "",
                    "semantic_processing": "not_performed",
                    "account": asdict(job.admission.account) if job else None,
                    "read_started_at": job.read_started_at if job else None,
                    "published_at": self._receipt.published_at if self._receipt else None,
                    "expires_at": self._receipt.expires_at if self._receipt else None,
                    "fresh": self._receipt is not None,
                    "scope": asdict(job.scope) if job else None}

    def checkpoint(self):
        """Metadata export only. No API restores admission/proofs from this data."""
        with revocation(), _LOCK:
            status = self.status()
            job = self._job
            return {"schema": 1, "status": status,
                    "listed_ids": list(job.listed) if job else [],
                    "pending_ids": list(job.pending) if job else [],
                    "next_page": job.next_page if job else ""}

    def _continuation_status(self):
        """Private source seam; public receipts and reference schemas stay v3."""
        with revocation(), _LOCK:
            job = self._job
            current = job is not None and self._current(job)
            pending = bool(current and (job.pending or (job.next_page and not job.capped)))
            due = bool(current and min(self._clock() - job.read_started_at,
                       self._monotonic() - job.monotonic_started_at) >= self.limits.ttl_seconds * .75)
            progress = bool(current and (len(job.listed) > job.segment_listed
                                         or job.fetched > job.segment_fetched))
            reason = ("segment_limit" if current and job.segment >= 32 else
                      "no_progress" if current and not progress else "")
            incomplete = bool(pending or (current and job.capped and not job.eof))
            return {"schema": 1, "segment": job.segment if current else 0, "maximum_segments": 32,
                    "handoff_due": due, "available": bool(pending and due and progress and not reason
                        and not job.error and not job.stopped and not self._stepping),
                    "incomplete": incomplete, "blocked_reason": reason if pending and due else ""}

    def _stop_continuation(self, reason):
        with revocation(), _LOCK:
            job = self._job
            if job is not None and self._current(job):
                job.stopped = True
                job.error = reason
                self._state = "partial"

    def _continue_segment(self):
        """Source bridge only, while current and under the shared guard.

        Copy bounded traversal metadata, then withdraw ALL old raw/proof state.
        The caller installs a fresh source grant first; no token or body crosses.
        """
        with revocation(), _LOCK:
            old = self._job
            if not self._continuation_status()["available"]:
                raise PermissionError("Current progressing history segment required")
            self._require(old)
            now, mono = self._clock(), self._monotonic()
            new = _Job(old.admission, old.scope, secrets.token_hex(24), now, mono,
                pending=list(old.pending), listed=dict(old.listed), page_tokens=set(old.page_tokens),
                next_page=old.next_page, eof=old.eof, capped=old.capped,
                fetched=old.fetched, eligible=old.eligible, failed=old.failed, evicted=old.evicted,
                http_calls=old.http_calls, segment=old.segment + 1,
                segment_listed=len(old.listed), segment_fetched=old.fetched)
            self._require(old)  # Copying metadata must not cross the old expiry.
            self.invalidate()
            self._job = new
            self._start_publication()
            _CACHE.owners[self] = self.limits.global_cache_bytes
            self._state = "running"
            self._require(new)

    def snapshot(self, *, expected_receipt=None, required_refs=()):
        with revocation(), _LOCK:
            self.status()  # Current trusted state, expiry and withdrawals, no I/O.
            job, current = self._job, self._receipt
            if job is None or current is None:
                return None
            receipt = current if expected_receipt is None else expected_receipt
            if not isinstance(receipt, HistoryReceipt):
                return None
            try:
                if receipt.proof_digest != self._receipt_digest(receipt):
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
            if (receipt.account != job.admission.account
                    or receipt.consent_epoch != job.admission.consent_epoch
                    or receipt.session_epoch != job.admission.session_epoch or receipt.job_token != job.token
                    or receipt.scope_digest != job.scope.digest or receipt.read_started_at != job.read_started_at
                    or not receipt.read_started_at <= receipt.published_at <= self._clock() < receipt.expires_at):
                return None
            if not isinstance(required_refs, (list, tuple)) or len(required_refs) > 2000:
                return None
            refs = tuple(required_refs) or receipt.refs
            if any(not isinstance(ref, RecordRef) or ref not in receipt.refs for ref in refs):
                return None
            rows = {r.ref: r for r in _CACHE.rows(self)}
            try:
                self._require(job)
            except _Withdrawn:
                return None
            missing = tuple(ref for ref in refs if ref not in rows or not rows[ref].body.eligible)
            selected = tuple(rows[ref] for ref in refs if ref in rows)
            for row in selected:
                _CACHE.entries.move_to_end((self._cache_id, row.resource_id))
            return HistorySnapshot(receipt, selected, missing, not missing,
                                   self._cache_witness(job, selected) if not missing else None)

    def _receipt_digest(self, receipt):
        data = asdict(receipt)
        data.pop("proof_digest")
        return hmac.new(self._proof_key, _digest(data).encode(), hashlib.sha256).hexdigest()

    def _publish_receipt(self, job, published):
        self._require(job)
        refs = tuple(r.ref for r in _CACHE.rows(self))
        receipt = HistoryReceipt(job.admission.account, job.admission.consent_epoch, job.admission.session_epoch,
                                 job.token, job.scope.digest, self._generation, job.read_started_at,
                                 published, job.read_started_at + self.limits.ttl_seconds, "", refs)
        self._require(job)
        self._receipt = HistoryReceipt(**{**receipt.__dict__, "proof_digest": self._receipt_digest(receipt)})

    def _publish(self, job, mid, data, fetch_started, received):
        self._require(job)
        if data.get("id") != mid:
            raise ValueError("Gmail resource identity mismatch")
        stamp = data.get("internalDate")
        try:
            event = int(stamp) / 1000 if isinstance(stamp, str) and stamp.isdigit() else None
        except (ValueError, OverflowError):
            event = None
        labels = data.get("labelIds")
        in_scope = (event is not None and job.scope.start < event < job.scope.end and event <= received
                    and isinstance(labels, list) and "SPAM" not in labels and "TRASH" not in labels
                    and (job.scope.selection != "inbox" or "INBOX" in labels))
        body = parse_history_body(data.get("payload"), byte_cap=self.limits.body_bytes)
        if not in_scope:
            body = HistoryBody("", "omitted", hashlib.sha256(b"").hexdigest(), ("outside_selected_scope_or_unknown_date",))
        rfc_id = ""
        payload = data.get("payload")
        for h in (payload.get("headers", []) if isinstance(payload, dict) else []):
            if isinstance(h, dict) and h.get("name", "").lower() == "message-id":
                value = h.get("value", "")
                if isinstance(value, str) and len(value) <= 998:
                    rfc_id = value
        published = self._clock()
        if not job.read_started_at <= fetch_started <= received <= published:
            self.invalidate()
            raise _Withdrawn()
        self._generation += 1
        fields = dict(account=job.admission.account, job_token=job.token, resource_id=mid,
                      rfc_message_id=rfc_id, provider_internal_at=event,
                      body=body, read_started_at=job.read_started_at, fetch_started_at=fetch_started,
                      received_at=received, published_at=published,
                      expires_at=job.read_started_at + self.limits.ttl_seconds, generation=self._generation)
        proof = _digest({**fields, "account": asdict(fields["account"]), "body": asdict(body),
                         "consent_epoch": job.admission.consent_epoch, "session_epoch": job.admission.session_epoch,
                         "scope_digest": job.scope.digest})
        row = HistoryRecord(**fields, proof_digest=proof)
        self._require(job)  # Last synchronous check immediately before cache publication.
        _CACHE.put(self, row)
        job.eligible += int(body.eligible)
        self._publish_receipt(job, published)
        return len(body.text.encode())

    async def step(self, *, http, token: Callable[[_HTTP], Awaitable[HistoryAccessToken]], budget: StepBudget):
        """Caller-driven bounded step. No retry, scheduler or implicit activation.

        A token adapter may POST only via its supplied guarded facade. Supplied
        HTTP clients must have redirects/retries disabled; exceptions remain
        private and body-free. A timeout stops new dispatch, not an in-flight HTTP
        await; caller cancellation invalidates before propagating CancelledError.
        """
        if not isinstance(budget, StepBudget):
            raise ValueError("Explicit history step budget required")
        with revocation(), _LOCK:
            job = self._job
            if job is None or self._stepping or job.stopped:
                return self.status()
            if not self._current(job):
                self.invalidate()
                return self.status()
            if not job.pending and (job.eof or job.capped):
                return self.status()
            self._stepping = True
        transport = _HTTP(self, job, http, budget)
        try:
            self._require(job)
            bearer = await token(transport)
            self._require(job)
            if (not isinstance(bearer, HistoryAccessToken) or bearer.admission != job.admission
                    or not isinstance(bearer.value, str) or not bearer.value):
                raise ValueError("No current account-bound token")
            headers = {"Authorization": f"Bearer {bearer.value}"}
            if not job.pending:
                if len(job.page_tokens) >= job.scope.message_cap + 1:
                    job.capped = True
                    job.error = "History pagination budget reached; coverage is partial"
                    self._state = "capped"
                    return self.status()
                params = {"maxResults": min(budget.page_size, job.scope.message_cap - len(job.listed)),
                          "q": f"after:{job.scope.start} before:{job.scope.end}", "includeSpamTrash": "false"}
                if job.scope.selection == "inbox":
                    params["labelIds"] = "INBOX"
                if job.next_page:
                    params["pageToken"] = job.next_page
                response = await transport.get(API + "/messages", params=params, headers=headers)
                self._require(job)
                if response.status_code != 200:
                    raise ValueError("Provider page failed")
                page = response.json()
                items = page.get("messages", [])
                next_page = page.get("nextPageToken", "")
                if (not isinstance(items, list) or len(items) > params["maxResults"]
                        or not isinstance(next_page, str) or len(next_page) > 8192
                        or (next_page and next_page in job.page_tokens)):
                    raise ValueError("Invalid provider page")
                ids = []
                for item in items:
                    mid = item.get("id") if isinstance(item, dict) else None
                    if not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", mid):
                        raise ValueError("Invalid provider resource identity")
                    if mid not in job.listed and mid not in ids:
                        ids.append(mid)
                with revocation(), _LOCK:
                    self._require(job)
                    job.pending.extend(ids)
                    job.listed.update(dict.fromkeys(ids))
                    job.next_page = next_page
                    if next_page:
                        job.page_tokens.add(next_page)
                    job.eof = not next_page
                    job.capped = len(job.listed) >= job.scope.message_cap
                    self._generation += 1
                    self._publish_receipt(job, self._clock())
            accepted = 0
            for _ in range(budget.full_bodies):
                self._require(job)
                if not job.pending or accepted >= budget.accepted_body_bytes:
                    break
                # Reserve the worst-case accepted bytes before dispatch, so a
                # smaller caller budget never downloads bodies it cannot admit.
                if budget.accepted_body_bytes - accepted < self.limits.body_bytes:
                    break
                mid = job.pending[0]
                fetch_started = self._clock()
                response = await transport.get(API + "/messages/" + mid,
                                               params={"format": "full"}, headers=headers)
                self._require(job)
                received = self._clock()
                if response.status_code != 200:
                    raise ValueError("Provider body failed")
                data = response.json()
                with revocation(), _LOCK:
                    self._require(job)
                    job.fetched += 1
                    accepted += self._publish(job, mid, data, fetch_started, received)
                    job.pending.pop(0)
            with revocation(), _LOCK:
                self._require(job)
                job.error = ""
                self._state = "capped" if job.capped else "exhausted" if job.eof and not job.pending else "running"
        except _BudgetStop:
            pass
        except _Withdrawn:
            pass
        except asyncio.CancelledError:
            if self._job is job:
                self.invalidate("cancelled")
            raise
        except Exception:
            with revocation(), _LOCK:
                if self._job is job and self._current(job):
                    job.failed += 1
                    job.error = "History step failed; coverage is partial"
        finally:
            with revocation(), _LOCK:
                self._stepping = False
        return self.status()

"""Daily workspace routes. Mounted by the existing Today API, once integrated.

All defaults are inert: no self identity, no behavior provider, no sender.
Account receipts are current-process only and never restored as authorization.
"""
from __future__ import annotations

import threading
import time
import secrets
from ipaddress import ip_address

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from service.assistant import daily_workspace as daily
from service.assistant.day_plan_email import EmailStore, SelfIdentity, address
from service.assistant.today import RevisionConflict, day_bounds
from service.paths import MOE_DIR

_LOCAL_ORIGINS = frozenset({"http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"})


async def require_workspace_mutation(request: Request):
    """Admit native loopback requests and the fixed local browser origin.

    CORS alone cannot prevent simple browser requests from changing state.
    The trusted origins are fixed independently of the caller's Host header.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    try:
        local_peer = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        local_peer = False
    hosts = request.headers.getlist("host")
    origins = request.headers.getlist("origin")
    sites = request.headers.getlist("sec-fetch-site")
    host_origin = "http://" + hosts[0].lower() if len(hosts) == 1 else ""
    if (not local_peer or request.url.scheme != "http" or host_origin not in _LOCAL_ORIGINS
            or len(origins) > 1 or (origins and origins[0] != host_origin)
            or len(sites) > 1 or (sites and sites[0] not in {"same-origin", "none"})):
        raise HTTPException(403, "Daily workspace changes require the local Wisp app or its own origin")


router = APIRouter(prefix="/workspace", tags=["daily workspace"], dependencies=[Depends(require_workspace_mutation)])
_email_store = None
_store_lock = threading.Lock()
_identity = SelfIdentity()
_identity_started = 0.0
_identity_lock = threading.RLock()
_identity_session = secrets.token_urlsafe(24)
_identity_generation = 0
_identity_capture_at = 0.0
_identity_epoch = secrets.token_hex(16)
_identity_previous = ()
_identity_settled = False


def email_store():
    global _email_store
    with _store_lock:
        if _email_store is None:
            _email_store = EmailStore(MOE_DIR / "daily-workspace" / "state.sqlite3")
        return _email_store


def self_identity():
    global _identity, _identity_epoch
    with _identity_lock:
        if _identity.complete and not 0 <= time.time() - _identity.received_at <= 600:
            _identity_epoch = secrets.token_hex(16)
            _identity = SelfIdentity(epoch=_identity_epoch)
        return _identity


def publish_identity(addresses, *, available, complete, started_at, now):
    """Accept an ordered full account receipt from the native account reader.

    Unavailable/empty receipts revoke eligibility immediately. A receipt can
    authorize no arbitrary destination: preview and submission still require
    exact membership and explicit scoped consent.
    """
    global _identity, _identity_started, _identity_epoch, _identity_previous, _identity_settled
    if (not daily.finite(started_at) or not 0 <= now - started_at <= 600
            or type(available) is not bool or type(complete) is not bool
            or not isinstance(addresses, list) or len(addresses) > 100):
        raise ValueError("Invalid connected-account identity receipt")
    with _identity_lock:
        if started_at < _identity_started:
            return
        revoked = not available or not complete
        # Equal timestamp failures dominate any replayed success. Native
        # transport additionally uses server-issued capture generations.
        if started_at == _identity_started and _identity_settled and not revoked:
            return
        normalized = () if revoked else tuple(sorted({address(value) for value in addresses}))
        if revoked or normalized != _identity_previous:
            _identity_epoch = secrets.token_hex(16)
        _identity_started = started_at
        _identity_previous = normalized
        _identity_settled = True
        _identity = SelfIdentity(normalized, started_at, not revoked, _identity_epoch)


@router.post("/email/identity/begin")
async def begin_identity():
    global _identity_generation, _identity_capture_at, _identity, _identity_settled
    with _identity_lock:
        _identity_generation += 1
        _identity_capture_at = time.time()
        _identity_settled = False
        _identity = SelfIdentity(epoch=_identity_epoch)
        return dict(session=_identity_session, generation=_identity_generation)


class IdentityReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(min_length=20, max_length=100)
    generation: StrictInt = Field(ge=1)
    addresses: list[str] = Field(max_length=100)
    available: bool = Field(strict=True)
    complete: bool = Field(strict=True)


@router.post("/email/identity")
async def complete_identity(body: IdentityReceipt):
    with _identity_lock:
        if body.session != _identity_session or body.generation != _identity_generation:
            return {"ok": False, "superseded": True}
        started = _identity_capture_at
        try:
            publish_identity(body.addresses, available=body.available, complete=body.complete,
                             started_at=started, now=time.time())
        except ValueError as exc:
            # A malformed latest success is unavailable, never permission to reuse
            # an older account list. Failures do not need valid address metadata.
            publish_identity([], available=False, complete=False, started_at=started, now=time.time())
            raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


def current_workspace(day, timezone, *, now=None, store=None):
    if store is None:
        from service.assistant.store import assistant_store
        store = assistant_store
    now = time.time() if now is None else now
    return daily.workspace(store, day, timezone, now=now, behavior=availability(day, timezone, now),
                           ignored_windows=email_store().ignored(day, timezone))


def availability(day, timezone, now):
    # The behavior owner supplies this public contract in its own workstream.
    # Legacy calendar habits are not a substitute for observed availability.
    return daily.availability_snapshot(day, timezone, now)


def email_contents(day, timezone, now):
    data = current_workspace(day, timezone, now=now)
    plan = data["proposed_plan"]
    ready = (plan["sources"]["calendar"]["ready"] and
             not any(b["kind"] == "calendar" and b["end"] is None for b in plan["blocks"]))
    return dict(ready=ready, subject="Your Wisp day plan · " + day,
                body=daily.render(data), workspace=data)


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(min_length=3, max_length=254)
    timezone: str = Field(min_length=1, max_length=100)
    minute: StrictInt = Field(ge=0, lt=1440)
    day: str


class Enable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=100)
    confirmed: bool = Field(strict=True)


class Pause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: str
    timezone: str
    window: str = Field(min_length=1, max_length=200)
    ignored: bool = Field(default=True, strict=True)


def fail(exc):
    raise HTTPException(409 if isinstance(exc, RevisionConflict) else 422, str(exc)) from exc


@router.get("")
def get_workspace(day: str, timezone: str):
    try:
        return current_workspace(day, timezone)
    except ValueError as exc:
        fail(exc)


@router.post("/availability")
def correct_availability(body: Correction):
    try:
        email_store().ignore(body.day, body.timezone, body.window, body.ignored)
        return {"ok": True}
    except ValueError as exc:
        fail(exc)


@router.get("/email")
def get_email_settings():
    own, now = self_identity(), time.time()
    addresses = [a for a in own.addresses if own.verified(a, now)]
    state = email_store().status()
    from service.assistant.day_plan_email import _CONSENT
    consent_current = (state["settings"].get("identity_epoch") == own.epoch
                       and state["settings"].get("identity_signature") == own.signature()
                       and state["settings"].get("consent") == _CONSENT) if addresses else False
    from service.assistant.day_plan_email import _policy_allows
    allowed = _policy_allows(state["settings"].get("recipient") or (addresses[0] if addresses else "")) if addresses else False
    return dict(**state, own_addresses=addresses, consent_current=consent_current,
                delivery_allowed=allowed,
                identity_ready=bool(addresses),
                identity_detail="Connected self addresses verified recently." if addresses else
                    "Waiting for a fresh complete connected Mail account read.")


@router.post("/email/preview")
def preview_email(body: Preview):
    try:
        day_bounds(body.day, body.timezone)
        now = time.time()
        data = email_contents(body.day, body.timezone, now)
        return email_store().preview(body.model_dump(exclude={"day"}), data["subject"], data["body"], self_identity(), now=now)
    except ValueError as exc:
        fail(exc)


@router.post("/email/enable")
async def enable_email(body: Enable):
    try:
        from service.assistant.day_plan_email import _policy_allows
        own = self_identity()
        return email_store().enable(body.token, own, confirmed=body.confirmed, now=time.time(), policy_allows=_policy_allows)
    except ValueError as exc:
        fail(exc)


@router.post("/email/pause")
async def pause_email(body: Pause):
    try:
        return email_store().pause(body.revision)
    except ValueError as exc:
        fail(exc)

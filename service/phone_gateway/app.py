"""The gateway FastAPI app. Exactly the routes in ios/docs/PHONE_GATEWAY.md."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import socket
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Sequence
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import DEFAULT_BACKEND, GATEWAY_VERSION
from .models import ModelCatalog
from .store import DeviceStore, state_dir

MAX_BODY_BYTES = 64 * 1024
MAX_PROMPT_CHARS = 16000
MAX_TOKEN_CHARS = 256
FAIL_LIMIT = 10
FAIL_WINDOW_S = 60.0
SID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
NONCE_RE = re.compile(r"[0-9a-f]{32,128}")
_MAX_ID_FIELD = 200
_SCOPES = ("once", "always", "never")
_FILE_CHUNK = 1024 * 1024
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_CONNECT_TIMEOUT = 3.0
_QUICK_TIMEOUT = httpx.Timeout(30.0, connect=_CONNECT_TIMEOUT)
_HELLO_TIMEOUT = httpx.Timeout(3.0, connect=_CONNECT_TIMEOUT)
# The agent stream may idle for as long as a model takes to think: no read timeout.
_STREAM_TIMEOUT = httpx.Timeout(30.0, connect=_CONNECT_TIMEOUT, read=None)

_UNREACHABLE = {"detail": "wisp_backend_unreachable"}


class FailureLimiter:
    """Counts auth failures per peer address in a sliding window."""

    def __init__(self, limit: int = FAIL_LIMIT, window: float = FAIL_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic, max_peers: int = 1024):
        self.limit, self.window, self.clock, self.max_peers = limit, window, clock, max_peers
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, peer: str) -> deque[float]:
        events = self._failures.setdefault(peer, deque())
        cutoff = self.clock() - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            self._failures.pop(peer, None)
        return events

    def blocked(self, peer: str) -> bool:
        return len(self._prune(peer)) >= self.limit

    def record(self, peer: str) -> None:
        if len(self._failures) >= self.max_peers and peer not in self._failures:
            for other in list(self._failures):
                self._prune(other)
            if len(self._failures) >= self.max_peers:
                self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(peer, deque()).append(self.clock())


def _check_backend(backend: str) -> str:
    parts = urlsplit(backend)
    if parts.scheme != "http" or (parts.hostname or "") not in _LOOPBACK_HOSTS:
        raise ValueError(f"backend must be an http loopback URL, got {backend!r}")
    return backend.rstrip("/")


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, rest = header.partition(" ")
    token = rest.strip()
    if scheme.lower() != "bearer" or not token or len(token) > MAX_TOKEN_CHARS:
        return None
    return token


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


async def _read_json_object(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body too large")
    try:
        value = json.loads(bytes(buf))
    except (ValueError, UnicodeDecodeError):
        raise _bad("body must be a JSON object") from None
    if not isinstance(value, dict):
        raise _bad("body must be a JSON object")
    return value


def _checked_sid(sid: Any, field: str = "session_id") -> str:
    if not isinstance(sid, str) or not SID_RE.fullmatch(sid):
        raise _bad(f"{field} must match [A-Za-z0-9_-]{{1,64}}")
    return sid


def _build_agent_body(raw: dict[str, Any]) -> dict[str, Any]:
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= MAX_PROMPT_CHARS:
        raise _bad(f"prompt must be a string of 1..{MAX_PROMPT_CHARS} characters")
    body: dict[str, Any] = {"prompt": prompt, "debug": False}
    if raw.get("session_id") is not None:
        body["session_id"] = _checked_sid(raw["session_id"])
    return body


def _build_approve_body(raw: dict[str, Any]) -> dict[str, Any]:
    action_id = raw.get("action_id")
    if not isinstance(action_id, str) or not 1 <= len(action_id) <= _MAX_ID_FIELD:
        raise _bad("action_id must be a non-empty string")
    if type(raw.get("approved")) is not bool:
        raise _bad("approved must be true or false")
    if raw.get("scope") not in _SCOPES:
        raise _bad('scope must be "once", "always" or "never"')
    body: dict[str, Any] = {
        "session_id": _checked_sid(raw.get("session_id")),
        "action_id": action_id,
        "approved": raw["approved"],
        "scope": raw["scope"],
    }
    if raw.get("request_id") is not None:
        request_id = raw["request_id"]
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= _MAX_ID_FIELD:
            raise _bad("request_id must be a non-empty string")
        body["request_id"] = request_id
    return body


def _parse_range(header: str, size: int) -> tuple[int, int] | None | str:
    """(start, end) inclusive; None = no/ignored range header; 'bad' = unsatisfiable."""
    match = re.fullmatch(r"bytes=(\d{0,19})-(\d{0,19})", header.strip())
    if not match:
        return None  # unsupported form (multi-range, other units): serve the whole file
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        count = int(last)
        if count == 0 or size == 0:
            return "bad"
        return max(size - count, 0), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return "bad"
    return start, min(end, size - 1)


def create_app(
    *,
    state: Path | None = None,
    backend: str = DEFAULT_BACKEND,
    transport: httpx.AsyncBaseTransport | None = None,
    model_dirs: Sequence[Path] = (),
    mac_name: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    backend = _check_backend(backend)
    directory = state if state is not None else state_dir()
    devices = DeviceStore(directory)
    catalog = ModelCatalog(directory, model_dirs)
    limiter = FailureLimiter(clock=clock)
    computer_name = mac_name or socket.gethostname()
    holder: dict[str, httpx.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # trust_env=False: never route loopback traffic through an env proxy.
        client = httpx.AsyncClient(base_url=backend, transport=transport,
                                   timeout=_QUICK_TIMEOUT, trust_env=False,
                                   follow_redirects=False)
        holder["client"] = client
        try:
            yield
        finally:
            holder.pop("client", None)
            await client.aclose()

    app = FastAPI(title="Wisp phone gateway", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.devices = devices
    app.state.catalog = catalog

    # -- helpers -----------------------------------------------------------
    def client() -> httpx.AsyncClient:
        return holder["client"]

    async def authenticate(request: Request) -> dict[str, Any]:
        peer = request.client.host if request.client else "unknown"
        if limiter.blocked(peer):
            raise HTTPException(status_code=429, detail="too many failures",
                                headers={"Retry-After": str(int(FAIL_WINDOW_S))})
        token = _bearer(request)
        device = None
        if token is not None:
            device = devices.match(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if device is None:
            limiter.record(peer)
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})
        if devices.due_for_touch(device["id"], clock):
            try:
                await asyncio.to_thread(devices.touch, device["id"], clock)
            except Exception:  # noqa: BLE001 — bookkeeping must never fail a request
                pass
        return device

    def unreachable() -> JSONResponse:
        return JSONResponse(_UNREACHABLE, status_code=502)

    async def forward_json(method: str, path: str, body: dict[str, Any] | None = None) -> Response:
        try:
            upstream = await client().request(method, path, json=body)
        except httpx.HTTPError:
            return unreachable()
        media = upstream.headers.get("content-type", "application/json").split(";")[0].strip()
        return Response(content=upstream.content, status_code=upstream.status_code,
                        media_type=media or "application/json")

    # -- routes ------------------------------------------------------------
    @app.get("/phone/v1/ping")
    async def ping(nonce: str | None = None) -> dict[str, Any]:
        # Without a nonce: the v1 answer, for older phones. With one, the phone only
        # sends its token once a proof matches, so a stranger at the address learns nothing.
        if nonce is None:
            return {"wisp_gateway": GATEWAY_VERSION}
        if not NONCE_RE.fullmatch(nonce):
            raise _bad("nonce must be 32..128 lowercase hex characters")
        proofs = await asyncio.to_thread(devices.ping_proofs, nonce)
        return {"wisp_gateway": GATEWAY_VERSION, "proofs": proofs}

    @app.get("/phone/v1/hello")
    async def hello(request: Request) -> dict[str, Any]:
        device = await authenticate(request)
        reachable, mode = False, None
        try:
            upstream = await client().get("/mode", timeout=_HELLO_TIMEOUT)
            if upstream.status_code < 300:
                reachable = True
                try:
                    mode = upstream.json()
                except ValueError:
                    mode = None
        except httpx.HTTPError:
            pass
        return {
            "gateway_version": GATEWAY_VERSION,
            "mac_name": computer_name,
            "device": {"id": device["id"], "name": device.get("name", "")},
            "backend": {"reachable": reachable, "mode": mode},
        }

    @app.post("/phone/v1/agent")
    async def agent(request: Request) -> Response:
        await authenticate(request)
        body = _build_agent_body(await _read_json_object(request))
        upstream_request = client().build_request("POST", "/agent", json=body, timeout=_STREAM_TIMEOUT)
        try:
            upstream = await client().send(upstream_request, stream=True)
        except httpx.HTTPError:
            return unreachable()

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                # Runs on completion, upstream failure and phone disconnect
                # (cancellation); shielded so the close itself is not cancelled.
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        media = upstream.headers.get("content-type", "text/event-stream")
        return StreamingResponse(
            relay(), status_code=upstream.status_code, media_type=media,
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @app.post("/phone/v1/agent/approve")
    async def approve(request: Request) -> Response:
        await authenticate(request)
        body = _build_approve_body(await _read_json_object(request))
        return await forward_json("POST", "/agent/approve", body)

    @app.get("/phone/v1/sessions")
    async def sessions(request: Request) -> Response:
        await authenticate(request)
        return await forward_json("GET", "/sessions")

    @app.get("/phone/v1/sessions/{sid}")
    async def session_get(sid: str, request: Request) -> Response:
        await authenticate(request)
        return await forward_json("GET", f"/sessions/{_checked_sid(sid, 'sid')}")

    @app.delete("/phone/v1/sessions/{sid}")
    async def session_delete(sid: str, request: Request) -> Response:
        await authenticate(request)
        return await forward_json("DELETE", f"/sessions/{_checked_sid(sid, 'sid')}")

    @app.get("/phone/v1/today")
    async def today(request: Request) -> Response:
        await authenticate(request)
        return await forward_json("GET", "/assistant/today")

    @app.get("/phone/v1/models")
    async def models(request: Request) -> Response:
        await authenticate(request)
        listing = await asyncio.to_thread(catalog.listing)
        return JSONResponse(listing, headers={"Cache-Control": "no-store"})

    @app.api_route("/phone/v1/models/{model_id}/files/{name}", methods=["GET", "HEAD"])
    async def model_file(model_id: str, name: str, request: Request) -> Response:
        await authenticate(request)
        opened = await asyncio.to_thread(catalog.open_file, model_id, name)
        if opened is None:
            raise HTTPException(status_code=404, detail="not found")
        handle, size = opened
        start, end, status = 0, size - 1, 200
        headers = {"Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff"}
        range_header = request.headers.get("range")
        if range_header:
            parsed = _parse_range(range_header, size)
            if parsed == "bad":
                handle.close()
                return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
            if isinstance(parsed, tuple):
                start, end = parsed
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(end - start + 1, 0)
        headers["Content-Length"] = str(length)
        media = "application/octet-stream"

        if request.method == "HEAD" or length == 0:
            handle.close()
            return Response(status_code=status, headers=headers, media_type=media)

        async def body() -> AsyncIterator[bytes]:
            remaining = length
            try:
                handle.seek(start)
                while remaining > 0:
                    chunk = await asyncio.to_thread(handle.read, min(_FILE_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                handle.close()

        return StreamingResponse(body(), status_code=status, headers=headers, media_type=media)

    return app

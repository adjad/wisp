"""Keep web pages out of Wisp's loopback API.

Wisp's service listens on 127.0.0.1:8765 for the Wisp app, the phone gateway
and the CLI (a backend the app launched answers the app, and the phone gateway
on the few routes it forwards: see AppKeyGuard below). None of those is a browser, so none of them sends `Origin` or
`Sec-Fetch-*` headers. A web page the user happens to have open can still
reach the port two ways:

* Cross-site requests. A page on any site can POST a form or a `no-cors`
  fetch to http://127.0.0.1:8765/mode or /agent/approve. CORS hides the
  response from the page but the request itself still runs.
* DNS rebinding. A page on evil.example re-points its own name at 127.0.0.1.
  The browser then treats Wisp as same-origin with evil.example, so the page
  can read responses too. The request's Host header still says evil.example.

This middleware refuses both, on every route and WebSocket, before any
handler runs:

* Every request and WebSocket handshake, browser or not, must name a loopback
  Host: 127.0.0.1, localhost or [::1], any port, in exactly one Host header.
  This applies to reads as well, because rebinding is how a page would read
  your data. It cannot wait for a browser header: a rebinding page's
  same-origin GET carries no `Origin`, and browsers leave `Sec-Fetch-*` off
  requests to a plain-http name that is not loopback, so such a read looks
  exactly like a native client apart from its Host. The app, the gateway and
  the CLI all address 127.0.0.1 or localhost, so they pass.
* A browser request (one carrying `Origin` or any `Sec-Fetch-*` header) that
  can change something (anything but GET, HEAD and OPTIONS) must also be
  same-origin: its Origin, when present, is exactly http://<that Host>, and
  Sec-Fetch-Site, when present, is same-origin or none. A WebSocket handshake
  is held to the same rule: it can push and receive anything, and browsers
  send Origin on every handshake.

What it does not do: a local program that is not a browser can send any
headers it likes, so this is no defence against code already running on the
Mac; AppKeyGuard below is that defence for a backend the app launched. The same-origin rule relies on the browser sending `Origin`
or `Sec-Fetch-Site` on a request that changes something; every current
Safari, Chrome and Firefox does.

Tests drive the app in-process under made-up names such as "testserver".
Those names are not accepted by a running Wisp: `_TEST_HOSTS` is empty unless
the repository's conftest.py fills it, and nothing reads it from the
environment, a file or a request.
"""
from __future__ import annotations

import hmac

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketClose

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})
# Extra Host names for in-process test clients. Always empty in a running
# Wisp; only the repository's conftest.py adds to it. Never fill it from the
# environment, settings or anything a request carries.
_TEST_HOSTS: set[str] = set()


def _host_name(host: str) -> str:
    """The host part of a Host header, lowercased, without its port."""
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        # Only "[v6]" or "[v6]:port"; anything else after the bracket is not ours.
        if end != -1 and host[end + 1:end + 2] in ("", ":"):
            return host[:end + 1]
        return host
    return host.split(":", 1)[0]


def browser_request_problem(method: str, headers: Headers) -> str | None:
    """Why a request must be refused, or None. See the module docstring."""
    # Host first, for every caller: a rebinding page's read carries no browser
    # header at all, only the attacker's name here.
    hosts = headers.getlist("host")
    name = _host_name(hosts[0]) if len(hosts) == 1 else ""
    if name not in _LOOPBACK_HOSTS and name not in _TEST_HOSTS:
        return "Wisp only answers web pages served from this Mac's own address"
    origins = headers.getlist("origin")
    sites = headers.getlist("sec-fetch-site")
    if not origins and not any(key.startswith("sec-fetch-") for key in headers.keys()):
        return None                          # not a browser, and it named this Mac
    if method.upper() in _SAFE_METHODS:
        return None
    if len(origins) > 1 or (origins and origins[0].strip().lower()
                            != "http://" + hosts[0].strip().lower()):
        return "Wisp does not accept changes from other websites"
    if len(sites) > 1 or (sites and sites[0].strip().lower() not in _SAME_ORIGIN_SITES):
        return "Wisp does not accept changes from other websites"
    return None


class LocalOriginGuard:
    """Pure ASGI middleware (no response buffering, so streams pass through)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind == "http":
            problem = browser_request_problem(scope.get("method", "GET"), Headers(scope=scope))
            if problem:
                await JSONResponse({"detail": problem}, status_code=403)(scope, receive, send)
                return
        elif kind == "websocket":
            # Judged like a request that changes something. Closing before
            # accept makes the server answer the handshake with 403.
            if browser_request_problem("WEBSOCKET", Headers(scope=scope)):
                await WebSocketClose(code=1008)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------
# The app's per-launch key: only the Wisp app that launched this backend
# --------------------------------------------------------------------------
#
# LocalOriginGuard keeps web pages out; it cannot tell the Wisp app from any other
# program on this Mac. When the app launches the backend it generates a random key
# for that launch and hands it over on the private credential pipe (never argv or the
# environment, which other programs of the same user can read). The app's request
# layer (BackendTrustProtocol) adds it to every request it sends, after checking that
# the listener is the process it spawned. Every route and WebSocket then requires it,
# except the few below that say nothing personal and change nothing.
#
# The phone gateway cannot hold this key; it sends its own narrower link secret on the
# few routes it forwards (see app_key_problem). A backend started any other way
# (scripts/run.sh, tests, the QA harnesses) was given no key and requires none; see
# credential_pipe.app_key_state.
APP_KEY_HEADER = "x-wisp-app-key"
# /identity: the app's own launch check asks it before it can send anything else.
# /health: liveness for scripts and the CLI; it reports engine state, nothing personal.
APP_KEY_OPEN_PATHS = frozenset({"/identity", "/health"})
_APP_KEY_REFUSED = "Only the Wisp app can use this service."


def app_key_problem(path: str, headers: Headers, method: str = "") -> str | None:
    """Why a request without this launch's app key must be refused, or None.

    A request with no valid app key is still let through when it carries the phone
    gateway's link secret and is one of the few routes the gateway forwards
    (service/gateway_link.py). The app key defends against other user accounts and
    sandboxed apps, which cannot read the private state directory; the gateway secret
    keeps that property while letting a gateway the user started themself work."""
    from service.credential_pipe import app_key_state
    required, key = app_key_state()
    if not required or path in APP_KEY_OPEN_PATHS:
        return None
    sent = headers.getlist(APP_KEY_HEADER)
    if not sent:
        from service.gateway_link import GATEWAY_KEY_HEADER, gateway_key_valid
        if method and gateway_key_valid(method, path, headers.getlist(GATEWAY_KEY_HEADER)):
            return None
    if key is None or len(sent) != 1:
        return _APP_KEY_REFUSED
    try:
        given = sent[0].encode("ascii")
    except UnicodeEncodeError:
        return _APP_KEY_REFUSED
    if not hmac.compare_digest(given, key.encode("ascii")):
        return _APP_KEY_REFUSED
    return None


class AppKeyGuard:
    """Pure ASGI middleware (no buffering, so streams pass through): every request
    and WebSocket handshake must carry this launch's app key, when one is required.
    The one exception is the phone gateway's link secret on the routes it forwards."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind in ("http", "websocket"):
            problem = app_key_problem(scope.get("path", ""), Headers(scope=scope),
                                      scope.get("method", "") if kind == "http" else "")
            if problem:
                if kind == "http":
                    await JSONResponse({"detail": problem}, status_code=403)(scope, receive, send)
                else:
                    await WebSocketClose(code=1008)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# The largest body /agent takes. The app sends a clip as at most 8 JPEG frames
# of at most 1568 px (typically under 1 MB each) plus 30 s of 16 kHz WAV, so a
# real clip is about 13 MB as base64. The service's own per-image ceiling is
# 8 MB, so 8 frames at that ceiling would be about 85 MB of base64. This cap
# sits between the two: everything the app sends fits, and the worst case
# the per-field checks allow is refused here. It is checked before the JSON is
# parsed, so an oversized body is never held as objects.
AGENT_BODY_LIMIT = 32 * 1024 * 1024
# /study/import: at most 12 pages of at most 150,000 characters (docs/STUDY_TAB.md), about
# 1.8 MB of Latin page text, up to about 5.4 MB in three-byte scripts; 6 MB fits both.
STUDY_IMPORT_BODY_LIMIT = 6 * 1024 * 1024
# /study/find/add: one batch of up to 60 read pages of at most 150,000 characters (the app sends
# batches of 60), about 9 MB of Latin text and up to about 27 MB in three-byte scripts.
STUDY_FIND_ADD_BODY_LIMIT = 28 * 1024 * 1024
# /study/find/resolve: at most 400 links of a 4,000-character url and a 1,000-character title.
STUDY_FIND_RESOLVE_BODY_LIMIT = 4 * 1024 * 1024
_LIMITED_PATHS = {"/agent": AGENT_BODY_LIMIT, "/study/import": STUDY_IMPORT_BODY_LIMIT,
                  "/study/find/add": STUDY_FIND_ADD_BODY_LIMIT,
                  "/study/find/resolve": STUDY_FIND_RESOLVE_BODY_LIMIT}
_TOO_LARGE = {"/study/import": "That's too much page text for Wisp to read at once. Try fewer tabs.",
              "/study/find/add": "That's too much page text to add at once. Try adding fewer at a time.",
              "/study/find/resolve": "That's too many links for Wisp to sort at once."}


class BodyLimit:
    """Pure ASGI middleware: refuse an over-large body on the media-carrying
    routes with 413 before any handler (or JSON parser) sees it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        limit = _LIMITED_PATHS.get(scope.get("path", "")) if scope.get("type") == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        message = _TOO_LARGE.get(scope.get("path", ""),
                                 "That's too large for Wisp to read. Try a smaller picture or a shorter clip.")
        # "message" too: the Study screens show a refusal's "message".
        too_large = JSONResponse({"detail": message, "message": message}, status_code=413)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await too_large(scope, receive, send)
            return
        # Chunked or undeclared: read up to the limit, then replay what was read.
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await too_large(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body, replayed = b"".join(chunks), False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

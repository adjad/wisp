"""Keep web pages out of Wisp's loopback API.

Wisp's service listens on 127.0.0.1:8765 for the Wisp app, the phone gateway
and the CLI. None of those is a browser, so none of them sends `Origin` or
`Sec-Fetch-*` headers. A web page the user happens to have open can still
reach the port two ways:

* Cross-site requests. A page on any site can POST a form or a `no-cors`
  fetch to http://127.0.0.1:8765/mode or /agent/approve. CORS hides the
  response from the page but the request itself still runs.
* DNS rebinding. A page on evil.example re-points its own name at 127.0.0.1.
  The browser then treats Wisp as same-origin with evil.example, so the page
  can read responses too. The request's Host header still says evil.example.

This middleware refuses both, on every route, before any handler runs:

* A browser request (one carrying `Origin` or any `Sec-Fetch-*` header) must
  name a loopback Host: 127.0.0.1, localhost or [::1], any port. This applies
  to reads as well, because rebinding is how a page would read your data.
* A browser request that can change something (anything but GET, HEAD and
  OPTIONS) must also be same-origin: its Origin, when present, is exactly
  http://<that Host>, and Sec-Fetch-Site, when present, is same-origin or none.

What it does not do: a local program that is not a browser can send any
headers it likes, so this is no defence against code already running on the
Mac (that is what the shell deny in safety/policy.py and the confirmation
cards are for). It also relies on the browser sending these headers; every
current Safari, Chrome and Firefox does for POST, and for GET they send
Sec-Fetch-Site. A browser too old to send either on GET is not covered.
"""
from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})


def _host_name(host: str) -> str:
    """The host part of a Host header, lowercased, without its port."""
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[:end + 1] if end != -1 else host
    return host.split(":", 1)[0]


def browser_request_problem(method: str, headers: Headers) -> str | None:
    """Why a request must be refused, or None. See the module docstring."""
    origins = headers.getlist("origin")
    sites = headers.getlist("sec-fetch-site")
    if not origins and not any(key.startswith("sec-fetch-") for key in headers.keys()):
        return None                          # not a browser; Host proves nothing here
    hosts = headers.getlist("host")
    if len(hosts) != 1 or _host_name(hosts[0]) not in _LOOPBACK_HOSTS:
        return "Wisp only answers web pages served from this Mac's own address"
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
        if scope.get("type") == "http":
            problem = browser_request_problem(scope.get("method", "GET"), Headers(scope=scope))
            if problem:
                await JSONResponse({"detail": problem}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)

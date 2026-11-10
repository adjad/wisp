"""Web pages cannot drive Wisp's loopback API (service/local_http.py).

A cross-site page could POST to http://127.0.0.1:8765/mode or /agent/approve,
and a DNS-rebinding page could also read responses. Every request must name a
loopback Host, and browser requests that change something must be
same-origin. Native clients (the Wisp app, the phone gateway, the CLI) address
127.0.0.1, send no browser headers and are unaffected. The real app is
exercised without its lifespan: a refused request never reaches a handler, so
nothing runs.
"""
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from service import local_http
from service.local_http import LocalOriginGuard, browser_request_problem
from starlette.datastructures import Headers

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def the_rules_a_running_wisp_has(monkeypatch):
    # conftest.py lets the in-process test names through for the other suites.
    # Here the guard is checked as it ships: loopback names only.
    monkeypatch.setattr(local_http, "_TEST_HOSTS", set())

ROUTES = ["/agent", "/mode", "/agent/approve", "/shutdown_omlx"]


def _recording_app():
    """A stand-in for Wisp's routes that only records what reached it, so a
    guard bug can never run /shutdown_omlx or an agent turn here."""
    reached = []

    async def app(scope, receive, send):
        reached.append((scope["method"], scope["path"]))
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})
    return app, reached


@pytest.fixture
def guarded():
    app, reached = _recording_app()
    return TestClient(LocalOriginGuard(app), base_url="http://127.0.0.1:8765"), reached


@pytest.fixture(scope="module")
def wisp():
    from service import main
    # Outermost, so it answers before routing, auth or any handler runs.
    assert main.app.user_middleware[0].cls is LocalOriginGuard
    return TestClient(main.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)


REFUSED = [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://localhost:3000"},                          # another local page
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Host": "evil.example:8765", "Origin": "http://evil.example:8765"},     # DNS rebinding
    {"Host": "evil.example:8765", "Sec-Fetch-Site": "same-origin"},
]


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("headers", REFUSED)
def test_browser_posts_from_elsewhere_are_refused(guarded, path, headers):
    client, reached = guarded
    response = client.post(path, json={}, headers=headers)
    assert response.status_code == 403
    assert "Wisp" in response.json()["detail"]
    assert not reached


@pytest.mark.parametrize("headers", REFUSED)
def test_the_real_app_refuses_them_too(wisp, headers):
    # /mode is the one route here that is harmless if reached: {} changes nothing.
    assert wisp.post("/mode", json={}, headers=headers).status_code == 403


# What a rebinding page's own GET really carries: the attacker's name as Host
# and nothing else a browser would add. No Origin (same-origin GET), and no
# Sec-Fetch-* (browsers leave those off plain-http names that are not loopback).
REBOUND_READS = [
    {"Host": "evil.example:8765"},
    {"Host": "evil.example:8765", "Referer": "http://evil.example:8765/",
     "User-Agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15", "Accept": "*/*"},
    {"Host": "evil.example:8765", "Accept": "text/event-stream"},      # EventSource
    {"Host": "evil.example:8765", "Sec-Fetch-Site": "same-origin"},
    {"Host": "testserver"},                    # a test name is no way in either
    {"Host": "127.0.0.1.evil.example:8765"},
]


@pytest.mark.parametrize("path", ["/health", "/sessions", "/audit", "/assistant/events"])
@pytest.mark.parametrize("headers", REBOUND_READS)
def test_a_rebinding_page_cannot_read_either(wisp, guarded, path, headers):
    response = wisp.get(path, headers=headers)
    assert response.status_code == 403
    assert "Wisp" in response.json()["detail"]
    client, reached = guarded
    assert client.get(path, headers=headers).status_code == 403
    assert not reached


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "127.0.0.1"])
def test_the_app_still_reads_and_writes_with_no_browser_headers(guarded, host):
    # URLSession, the gateway and the CLI: a loopback Host and nothing else.
    client, reached = guarded
    assert client.get("/sessions", headers={"Host": host}).status_code == 200
    assert client.post("/agent", json={}, headers={"Host": host}).status_code == 200
    assert reached == [("GET", "/sessions"), ("POST", "/agent")]


def test_the_real_app_answers_a_native_read(wisp):
    assert wisp.get("/mode").status_code == 200


def _raw_get(app, host_headers):
    """One GET through the ASGI app with exactly these Host headers, which a
    test client cannot send (it always adds one of its own)."""
    import asyncio
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/sessions", "raw_path": b"/sessions",
             "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000),
             "server": ("127.0.0.1", 8765),
             "headers": [(b"host", value.encode()) for value in host_headers]}
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_a_missing_or_doubled_host_is_refused():
    app, reached = _recording_app()
    guard = LocalOriginGuard(app)
    assert _raw_get(guard, []) == 403
    assert _raw_get(guard, ["127.0.0.1:8765", "evil.example:8765"]) == 403
    assert _raw_get(guard, ["evil.example:8765", "127.0.0.1:8765"]) == 403
    assert not reached
    assert _raw_get(guard, ["127.0.0.1:8765"]) == 200
    assert reached == [("GET", "/sessions")]


def test_test_names_are_only_allowed_by_the_test_run(monkeypatch):
    # As shipped: a fresh interpreter, no conftest, nothing in the set.
    probe = ("from starlette.datastructures import Headers\n"
             "from service import local_http as h\n"
             "assert h._TEST_HOSTS == set(), h._TEST_HOSTS\n"
             "for name in ('testserver', 'test', 'fixture', 'synthetic.invalid', 'evil.example:8765'):\n"
             "    assert h.browser_request_problem('GET', Headers({'host': name})), name\n"
             "assert h.browser_request_problem('GET', Headers({'host': '127.0.0.1:8765'})) is None\n")
    done = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    # Only local_http.py names the set inside the service, and it never fills it.
    users = [path.relative_to(ROOT).as_posix() for path in (ROOT / "service").rglob("*.py")
             if "_TEST_HOSTS" in path.read_text(encoding="utf-8")]
    assert users == ["service/local_http.py"]
    source = (ROOT / "service" / "local_http.py").read_text(encoding="utf-8")
    assert "_TEST_HOSTS." not in source and "os.environ" not in source
    # With the names conftest.py adds, the other suites' clients get through,
    # and a name it did not add still does not.
    monkeypatch.setattr(local_http, "_TEST_HOSTS", {"testserver", "synthetic.invalid"})
    assert browser_request_problem("POST", Headers({"host": "testserver"})) is None
    assert browser_request_problem("GET", Headers({"host": "synthetic.invalid"})) is None
    assert browser_request_problem("GET", Headers({"host": "evil.example:8765"}))
    assert browser_request_problem("POST", Headers({"host": "testserver",
                                                    "origin": "https://evil.example"}))


@pytest.mark.parametrize("headers", [
    {},                                                             # the Wisp app, CLI, gateway
    {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
])
def test_native_and_same_origin_requests_reach_the_route(wisp, guarded, headers):
    client, reached = guarded
    assert client.post("/agent/approve", json={}, headers=headers).status_code == 200
    assert reached == [("POST", "/agent/approve")]
    response = wisp.post("/mode", json={}, headers=headers)
    assert response.status_code == 200 and set(response.json()) == {"read_only", "full_access"}


@pytest.mark.parametrize("method,headers,refused", [
    ("POST", {"host": "127.0.0.1:8765"}, False),
    ("GET", {"host": "127.0.0.1:8765"}, False),                   # the app: no browser headers
    ("GET", {"host": "localhost:8765"}, False),
    ("GET", {"host": "LOCALHOST:8765"}, False),
    ("GET", {"host": "[::1]:8765"}, False),
    ("POST", {"host": "testserver"}, True),                       # Host is judged for every caller
    ("GET", {"host": "testserver"}, True),
    ("GET", {"host": "synthetic.invalid"}, True),
    ("GET", {"host": "evil.example:8765"}, True),                 # a rebinding page's read
    ("GET", {"host": "evil.example:8765", "referer": "http://evil.example:8765/",
             "user-agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15"}, True),
    ("HEAD", {"host": "evil.example:8765"}, True),
    ("GET", {}, True),                                            # no Host at all
    ("GET", {"host": ""}, True),
    ("GET", {"host": "localhost.:8765"}, True),
    ("GET", {"host": "0.0.0.0:8765"}, True),
    ("GET", {"host": "127.0.0.1.evil.example"}, True),
    ("GET", {"host": "evil.example@127.0.0.1:8765"}, True),
    ("GET", {"host": "[::1].evil.example:8765"}, True),
    ("POST", {"host": "localhost:8775", "origin": "http://localhost:8775"}, False),
    ("POST", {"host": "[::1]:8765", "origin": "http://[::1]:8765"}, False),
    ("POST", {"host": "127.0.0.1:8765", "origin": "http://localhost:8765"}, True),
    ("POST", {"host": "localhost.:8765", "origin": "http://localhost.:8765"}, True),
    ("GET", {"host": "127.0.0.1:8765", "sec-fetch-site": "cross-site"}, False),   # CORS hides reads
    ("GET", {"host": "evil.example", "sec-fetch-mode": "navigate"}, True),
    ("DELETE", {"host": "127.0.0.1:8765", "sec-fetch-site": "cross-site"}, True),
])
def test_guard_rules(method, headers, refused):
    assert bool(browser_request_problem(method, Headers(headers))) is refused


def test_browser_websockets_from_elsewhere_are_refused():
    from starlette.websockets import WebSocketDisconnect
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])
        await receive()
        await send({"type": "websocket.accept"})
        await receive()                      # the client's disconnect

    client = TestClient(LocalOriginGuard(app), base_url="http://127.0.0.1:8765")
    for url, headers in (("ws://127.0.0.1:8765/ws", {"Origin": "https://evil.example"}),   # cross-site
                         ("ws://evil.example:8765/ws", {"Origin": "http://evil.example:8765"}),  # rebinding
                         ("ws://evil.example:8765/ws", {}),            # no Origin: Host alone refuses
                         ("ws://testserver/ws", {})):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers):
                pass
    assert not reached
    with client.websocket_connect("ws://127.0.0.1:8765/ws", headers={"Origin": "http://127.0.0.1:8765"}):
        pass
    # A native client: loopback Host, no Origin. (A bare "/ws" would go out
    # as ws://testserver, the test client's fixed default for sockets.)
    with client.websocket_connect("ws://127.0.0.1:8765/ws"):
        pass
    assert reached == ["websocket", "websocket"]

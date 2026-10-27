"""Web pages cannot drive Wisp's loopback API (service/local_http.py).

A cross-site page could POST to http://127.0.0.1:8765/mode or /agent/approve,
and a DNS-rebinding page could also read responses. Browser requests must name
a loopback Host, and browser requests that change something must be
same-origin. Native clients (the Wisp app, the phone gateway, the CLI) send no
browser headers and are unaffected. The real app is exercised without its
lifespan: a refused request never reaches a handler, so nothing runs.
"""
import pytest
from starlette.testclient import TestClient

from service.local_http import LocalOriginGuard, browser_request_problem
from starlette.datastructures import Headers

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


def test_a_rebinding_page_cannot_read_either(wisp):
    response = wisp.get("/health", headers={"Host": "evil.example:8765",
                                            "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 403


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
    ("POST", {"host": "testserver"}, False),                      # no browser headers: not judged
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
                         ("ws://evil.example:8765/ws", {"Origin": "http://evil.example:8765"})):  # rebinding
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers):
                pass
    assert not reached
    with client.websocket_connect("ws://127.0.0.1:8765/ws", headers={"Origin": "http://127.0.0.1:8765"}):
        pass
    with client.websocket_connect("/ws"):
        pass
    assert reached == ["websocket", "websocket"]

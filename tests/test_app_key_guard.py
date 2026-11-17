"""Only the Wisp app that launched the backend can use it (service/local_http.AppKeyGuard).

The app hands its backend a random per-launch key on the private credential pipe and
sends it on every request; every route but /identity and /health refuses a request
without it. Refused requests never reach a handler, so nothing here sends, approves,
installs or deletes anything. A backend launched any other way requires no key.
Also: ~/.moe/mcp.json is only acted on when it is the person's own regular file.
"""
import json
import os

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from service import credential_pipe, local_http
from service.local_http import APP_KEY_HEADER, AppKeyGuard
from tests.credential_pipe_fixture import fixture_pipe

KEY = "a1" * 32                      # invented
OTHER = "b2" * 32
VALUES = {credential_pipe.NAMES[0]: "c" * 64}


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(credential_pipe, "_app_key", KEY)
    monkeypatch.setattr(credential_pipe, "_app_launch", True)


def _recording_app():
    reached = []

    async def app(scope, receive, send):
        reached.append((scope.get("method"), scope["path"]))
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})
    return app, reached


@pytest.fixture
def guarded():
    app, reached = _recording_app()
    return TestClient(AppKeyGuard(app), base_url="http://127.0.0.1:8765"), reached


@pytest.fixture(scope="module")
def wisp():
    from service import main
    # No lifespan: a refused request never reaches a handler.
    return TestClient(main.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)


# The routes the audit named, plus the side-effecting GET. Each one acts, changes
# settings or reads personal data.
ACTING = [
    ("POST", "/assistant/send_message_draft"),
    ("POST", "/assistant/email_reply/send"),
    ("POST", "/assistant/proactive/cards/card-1/fire"),
    ("POST", "/agent/approve"),
    ("POST", "/agent"),
    ("POST", "/inference/cloud"),
    ("DELETE", "/inference/cloud"),
    ("POST", "/inference/cloud/everything"),
    ("POST", "/mcp/reload"),
    ("POST", "/skills/install"),
    ("POST", "/mode"),
    ("PUT", "/stage_mode"),
    ("DELETE", "/sessions/s-1"),
    ("POST", "/memory/facts"),
    ("DELETE", "/memory/facts/f-1"),
]
READING = [
    "/sessions/s-1", "/sessions", "/memory/facts", "/assistant/today", "/audit",
    "/behavior/email-history", "/behavior/activity/lease", "/inference/cloud", "/mode",
]


def _call(client, method, path, headers=None):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.request(method, path, json={}, headers=headers)


@pytest.mark.parametrize("method,path", ACTING + [("GET", p) for p in READING])
@pytest.mark.parametrize("sent", [None, OTHER, KEY[:-1], KEY + "0", KEY.upper(), ""])
def test_real_routes_refuse_without_this_launchs_key(keyed, wisp, method, path, sent):
    headers = None if sent is None else {APP_KEY_HEADER: sent}
    response = _call(wisp, method, path, headers)
    assert response.status_code == 403, (method, path, response.status_code)
    assert response.json()["detail"] == "Only the Wisp app can use this service."


def test_two_key_headers_are_refused(keyed, guarded):
    client, reached = guarded
    response = client.get("/mode", headers=[(APP_KEY_HEADER, KEY), (APP_KEY_HEADER, KEY)])
    assert response.status_code == 403 and not reached


@pytest.mark.parametrize("method,path", ACTING + [("GET", p) for p in READING])
def test_every_route_passes_with_the_key(keyed, guarded, method, path):
    client, reached = guarded
    response = _call(client, method, path, {APP_KEY_HEADER: KEY})
    assert response.status_code == 200 and reached == [(method, path)]


def test_the_real_app_answers_the_key(keyed, wisp):
    assert wisp.get("/mode", headers={APP_KEY_HEADER: KEY}).status_code == 200
    assert wisp.get("/mode").status_code == 403


@pytest.mark.parametrize("path", sorted(local_http.APP_KEY_OPEN_PATHS))
def test_identity_and_health_stay_open(keyed, guarded, path):
    client, reached = guarded
    assert client.get(path).status_code == 200 and reached == [("GET", path)]


def test_identity_open_on_the_real_app(keyed, wisp):
    assert wisp.get("/identity").status_code == 200


def test_open_paths_are_exact(keyed, guarded):
    client, reached = guarded
    for path in ("/identity/", "/identity/x", "/healthz", "/health/../mode"):
        assert client.get(path).status_code == 403
    assert not reached


def test_websocket_handshake_needs_the_key(keyed, guarded):
    client, reached = guarded
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/agent/stream"):
            pass
    assert not reached
    with client.websocket_connect("/agent/stream", headers={APP_KEY_HEADER: KEY}):
        pass
    assert reached == [(None, "/agent/stream")]


def test_a_backend_not_launched_by_the_app_needs_no_key(monkeypatch, guarded):
    monkeypatch.setattr(credential_pipe, "_app_key", None)
    monkeypatch.setattr(credential_pipe, "_app_launch", False)
    client, reached = guarded
    assert client.post("/agent/approve", json={}).status_code == 200


def test_app_launch_without_a_key_refuses_everything(monkeypatch, guarded):
    # Fails closed: launched by the app, but no key came on the pipe.
    monkeypatch.setattr(credential_pipe, "_app_key", None)
    monkeypatch.setattr(credential_pipe, "_app_launch", True)
    client, reached = guarded
    assert client.get("/mode", headers={APP_KEY_HEADER: KEY}).status_code == 403
    assert client.get("/identity").status_code == 200
    assert reached == [("GET", "/identity")]


# --- the key arrives on the private pipe ---------------------------------------------

def test_pipe_carries_the_key(monkeypatch):
    monkeypatch.setenv("WISP_LAUNCH_NONCE", "nonce-1")
    with fixture_pipe(VALUES, app_key=KEY):
        assert credential_pipe.consume("primary") == (VALUES, "absent")
        assert credential_pipe.app_key_state() == (True, KEY)
    assert credential_pipe.app_key_state() == (False, None)


def test_app_launched_frame_without_key_requires_one(monkeypatch):
    monkeypatch.setenv("WISP_LAUNCH_NONCE", "nonce-1")
    with fixture_pipe(VALUES):
        credential_pipe.consume("primary")
        assert credential_pipe.app_key_state() == (True, None)


def test_frame_without_key_outside_the_app_requires_none(monkeypatch):
    monkeypatch.delenv("WISP_LAUNCH_NONCE", raising=False)
    with fixture_pipe(VALUES):
        credential_pipe.consume("primary")
        assert credential_pipe.app_key_state() == (False, None)


@pytest.mark.parametrize("bad", ["short", KEY.upper(), 7, False, "g" * 64, KEY + "0"])
def test_malformed_key_refuses_the_frame(bad):
    with fixture_pipe(VALUES, app_key=bad):
        with pytest.raises(ValueError, match="^Native credential pipe unavailable$"):
            credential_pipe.consume("primary")
    assert credential_pipe.app_key_state() == (False, None)


def test_only_the_primary_backend_takes_a_key():
    node = {credential_pipe.NAMES[2]: "d" * 64}
    with fixture_pipe(node, role="node", app_key=KEY):
        with pytest.raises(ValueError, match="^Native credential pipe unavailable$"):
            credential_pipe.consume("node")


# --- mcp.json must be the person's own file -------------------------------------------

@pytest.fixture
def mcp_config(monkeypatch, tmp_path):
    from service import mcp
    path = tmp_path / "mcp.json"
    monkeypatch.setattr(mcp, "CONFIG_PATH", path)
    body = json.dumps({"servers": {"demo": {"command": "/usr/bin/true", "enabled": False}}})
    return mcp, path, body


def test_own_private_config_is_read(mcp_config):
    mcp, path, body = mcp_config
    path.write_text(body)
    os.chmod(path, 0o600)
    assert list(mcp.MCPManager()._read_config()) == ["demo"]
    os.chmod(path, 0o644)                      # readable by others is fine; writable is not
    assert list(mcp.MCPManager()._read_config()) == ["demo"]


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_config_others_can_write_is_ignored(mcp_config, mode):
    mcp, path, body = mcp_config
    path.write_text(body)
    os.chmod(path, mode)
    assert mcp.MCPManager()._read_config() == {}


def test_symlinked_config_is_ignored(mcp_config, tmp_path):
    mcp, path, body = mcp_config
    target = tmp_path / "elsewhere.json"
    target.write_text(body)
    os.chmod(target, 0o600)
    path.symlink_to(target)
    assert mcp.MCPManager()._read_config() == {}


def test_config_owned_by_someone_else_is_ignored(mcp_config, monkeypatch):
    mcp, path, body = mcp_config
    path.write_text(body)
    os.chmod(path, 0o600)
    real = os.getuid()
    monkeypatch.setattr(mcp.os, "getuid", lambda: real + 1)
    assert mcp.MCPManager()._read_config() == {}


def test_folder_named_mcp_json_is_ignored(mcp_config):
    mcp, path, _body = mcp_config
    path.mkdir()
    assert mcp.MCPManager()._read_config() == {}


def test_absent_config_is_empty(mcp_config):
    mcp, _path, _body = mcp_config
    assert mcp.MCPManager()._read_config() == {}


# --- the app side, read from its source ------------------------------------------------

def _swift(name):
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "app/Sources/WispApp" / name).read_text()


def test_app_sends_the_key_the_backend_reads():
    trust = _swift("BackendTrust.swift")
    assert 'static let headerField = "X-Wisp-App-Key"' in trust
    assert APP_KEY_HEADER == "x-wisp-app-key"
    forward = trust[trust.index("private func forward()"):trust.index("override func stopLoading()")]
    # Added after the gate admitted the request, on the copy that is actually sent.
    assert "marked.setValue(key, forHTTPHeaderField: BackendAppKey.headerField)" in forward
    manager = _swift("BackendManager.swift")
    assert "let appKey = BackendAppKey.make()" in manager
    assert "BackendAppKey.set(appKey)" in manager and "appKey: appKey)" in manager
    # Never through argv or the environment, which other programs of this user can read.
    assert "environment?[\"WISP_APP_KEY" not in manager and "appKey]" not in manager


def test_backend_never_reads_or_writes_a_bytecode_cache():
    manager = _swift("BackendManager.swift")
    assert '"PYTHONDONTWRITEBYTECODE": "1"' in manager
    assert '"PYTHONPYCACHEPREFIX":' not in manager and ".moe/cache/pycache\")" not in manager
    assert '"PYTHONPYCACHEPREFIX",' in manager           # an inherited one is dropped

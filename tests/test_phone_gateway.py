"""Phone gateway tests. Hermetic: fake upstream via httpx.MockTransport, MOE_DIR in
tmp_path, no processes, no real backend, no Tailscale."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from service.phone_gateway import __main__ as cli
from service.phone_gateway import qr as qrmod
from service.phone_gateway import tailscale
from service.phone_gateway.app import create_app
from service.phone_gateway.models import ModelCatalog
from service.phone_gateway.store import DeviceStore, hash_token, new_token

SSE_CHUNKS = [
    b'data: {"type":"session","id":"s1"}\n\n',
    b'data: {"type":"delta","text":"Hel"}\n\n',
    b'data: {"type":"delta","text":"lo"}\n\n',
    b'data: {"type":"done"}\n\n',
]


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, hang_after_first=False):
        self.chunks, self.hang, self.closed = chunks, hang_after_first, False

    async def __aiter__(self):
        for i, chunk in enumerate(self.chunks):
            yield chunk
            if self.hang and i == 0:
                await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class Upstream:
    """Records every request the gateway makes and answers like the backend."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.down = False
        self.stream = ChunkStream(SSE_CHUNKS)
        self.overrides: dict[tuple[str, str], httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        key = (request.method, request.url.path)
        if key in self.overrides:
            return self.overrides[key]
        path = request.url.path
        if key == ("GET", "/mode"):
            return httpx.Response(200, json={"mode": "production"})
        if key == ("POST", "/agent"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.stream)
        if key == ("POST", "/agent/approve"):
            return httpx.Response(200, json={"ok": True})
        if key == ("GET", "/sessions"):
            return httpx.Response(200, json=[{"id": "s1"}])
        if path.startswith("/sessions/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], "method": request.method})
        if key == ("GET", "/assistant/today"):
            return httpx.Response(200, json={"plan": []})
        return httpx.Response(404, json={"detail": "nope"})

    def body(self, index=-1):
        return json.loads(self.requests[index].content)


class Env:
    pass


@contextmanager
def make_env(tmp_path, monkeypatch, **app_kwargs):
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))
    env = Env()
    env.state = tmp_path / "moe" / "phone_gateway"
    env.upstream = Upstream()
    app = create_app(state=None, transport=httpx.MockTransport(env.upstream), mac_name="Test Mac",
                     **app_kwargs)
    env.store = app.state.devices
    env.token = new_token()
    env.device = env.store.add("Test iPhone", env.token)
    env.auth = {"Authorization": f"Bearer {env.token}"}
    env.app = app
    with TestClient(app) as client:
        env.client = client
        yield env


@pytest.fixture
def env(tmp_path, monkeypatch):
    with make_env(tmp_path, monkeypatch) as e:
        yield e


# ---------------------------------------------------------------- ping / auth

def test_ping_is_unauthenticated_and_minimal(env):
    r = env.client.get("/phone/v1/ping")
    assert r.status_code == 200
    assert r.json() == {"wisp_gateway": 1}
    assert env.upstream.requests == []


@pytest.mark.parametrize("path", ["/phone/v1/hello", "/phone/v1/sessions", "/phone/v1/today",
                                  "/phone/v1/models", "/phone/v1/sessions/abc"])
def test_requires_bearer_token(env, path):
    assert env.client.get(path).status_code == 401
    assert env.client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert env.client.get(path, headers={"Authorization": f"Basic {env.token}"}).status_code == 401
    r = env.client.get(path, headers={"Authorization": "Bearer nope"})
    assert r.json() == {"detail": "unauthorized"}
    assert env.upstream.requests == []


def test_post_routes_check_auth_before_body(env):
    assert env.client.post("/phone/v1/agent", content=b"not json").status_code == 401
    assert env.client.post("/phone/v1/agent/approve", json={}).status_code == 401


def test_rate_limit_after_ten_failures(tmp_path, monkeypatch):
    now = [1000.0]
    with make_env(tmp_path, monkeypatch, clock=lambda: now[0]) as env:
        for _ in range(10):
            assert env.client.get("/phone/v1/hello", headers={"Authorization": "Bearer x"}).status_code == 401
        # Over the limit: even the right token is refused for the rest of the window.
        assert env.client.get("/phone/v1/hello", headers=env.auth).status_code == 429
        assert env.client.get("/phone/v1/hello", headers={"Authorization": "Bearer x"}).status_code == 429
        assert env.client.get("/phone/v1/ping").status_code == 200
        now[0] += 61
        assert env.client.get("/phone/v1/hello", headers=env.auth).status_code == 200


def test_successful_requests_do_not_count_as_failures(env):
    for _ in range(25):
        assert env.client.get("/phone/v1/ping").status_code == 200
        assert env.client.get("/phone/v1/hello", headers=env.auth).status_code == 200


def test_last_seen_written_at_most_once_a_minute(tmp_path, monkeypatch):
    now = [5000.0]
    saves = []
    real_save = DeviceStore._save
    monkeypatch.setattr(DeviceStore, "_save", lambda self, d: (saves.append(1), real_save(self, d))[1])
    with make_env(tmp_path, monkeypatch, clock=lambda: now[0]) as env:
        saves.clear()
        for _ in range(5):
            env.client.get("/phone/v1/hello", headers=env.auth)
        assert len(saves) == 1
        assert env.store.devices()[0]["last_seen"]
        now[0] += 30
        env.client.get("/phone/v1/hello", headers=env.auth)
        assert len(saves) == 1
        now[0] += 31
        env.client.get("/phone/v1/hello", headers=env.auth)
        assert len(saves) == 2


# ---------------------------------------------------------------- pairing store

def test_store_keeps_only_token_hash_with_private_modes(env):
    text = (env.state / "devices.json").read_text()
    assert env.token not in text
    stored = json.loads(text)
    assert stored[0]["token_sha256"] == hashlib.sha256(env.token.encode()).hexdigest()
    assert set(stored[0]) == {"id", "name", "token_sha256", "created_at", "last_seen"}
    assert stat.S_IMODE(os.stat(env.state).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(env.state / "devices.json").st_mode) == 0o600
    assert not [p for p in env.state.iterdir() if p.name.endswith(".tmp")]


def test_new_token_is_32_bytes_base64url_unpadded():
    token = new_token()
    assert len(token) == 43 and "=" not in token and "+" not in token and "/" not in token
    assert new_token() != token


def test_revoke_takes_effect_on_running_server(env):
    assert env.client.get("/phone/v1/hello", headers=env.auth).status_code == 200
    assert env.store.revoke(env.device["id"]) is True
    assert env.client.get("/phone/v1/hello", headers=env.auth).status_code == 401
    assert env.store.revoke(env.device["id"]) is False


def test_touch_never_resurrects_a_revoked_device(env):
    env.store.revoke(env.device["id"])
    assert env.store.touch(env.device["id"]) is False
    assert env.store.devices() == []


def test_cli_revoke_waits_for_a_server_write_in_progress(tmp_path):
    # Two store instances stand in for the server and a separate `revoke` CLI process:
    # the revoke must not run between the server's read and its write.
    import threading

    server = DeviceStore(tmp_path)
    device = server.add("Phone", new_token())
    cli_store = DeviceStore(tmp_path)
    done = threading.Event()

    with server._exclusive():
        thread = threading.Thread(target=lambda: (cli_store.revoke(device["id"]), done.set()))
        thread.start()
        assert not done.wait(0.3), "revoke ran while another writer held the file lock"
        server._save(server._read_file())  # the server's stale write lands first
    thread.join(5)
    assert done.is_set()
    assert DeviceStore(tmp_path).devices() == []
    assert stat.S_IMODE(os.stat(tmp_path / "devices.lock").st_mode) == 0o600


def test_cli_pair_devices_revoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))
    monkeypatch.setattr(tailscale, "tailscale_ipv4", lambda: "100.101.102.103")
    monkeypatch.setattr(tailscale, "magicdns_name", lambda: "macbook.tail1234.ts.net")
    monkeypatch.setattr(cli, "_computer_name", lambda: "Adi's MacBook")
    monkeypatch.setattr(qrmod, "render_qr", lambda text, color=True: "<QR>")
    assert cli.main(["pair", "--name", "Adi's iPhone"]) == 0
    out = capsys.readouterr().out
    url = next(line for line in out.splitlines() if line.startswith("wisp-pair://"))
    assert "<QR>" in out
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert (parts.scheme, parts.netloc) == ("wisp-pair", "v1")
    assert q["host"] == "macbook.tail1234.ts.net" and q["ip"] == "100.101.102.103"
    assert q["port"] == "8799" and q["mac"] == "Adi's MacBook"
    stored = json.loads((tmp_path / "moe" / "phone_gateway" / "devices.json").read_text())
    assert len(stored) == 1 and stored[0]["name"] == "Adi's iPhone"
    assert stored[0]["token_sha256"] == hash_token(q["token"])
    assert q["token"] not in json.dumps(stored)

    assert cli.main(["devices"]) == 0
    listing = capsys.readouterr().out
    assert stored[0]["id"] in listing and "Adi's iPhone" in listing
    assert q["token"] not in listing and stored[0]["token_sha256"] not in listing

    assert cli.main(["revoke", "deadbeef"]) == 1
    assert cli.main(["revoke", stored[0]["id"]]) == 0
    assert json.loads((tmp_path / "moe" / "phone_gateway" / "devices.json").read_text()) == []


def test_cli_pair_without_tailscale_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))
    monkeypatch.setattr(tailscale, "tailscale_ipv4", lambda: None)
    assert cli.main(["pair"]) == 2
    assert not (tmp_path / "moe" / "phone_gateway" / "devices.json").exists()


def test_cli_pair_falls_back_to_url_when_qr_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))
    monkeypatch.setattr(tailscale, "tailscale_ipv4", lambda: "100.64.0.9")
    monkeypatch.setattr(tailscale, "magicdns_name", lambda: None)
    monkeypatch.setattr(qrmod, "render_qr", lambda text, color=True: None)
    assert cli.main(["pair"]) == 0
    out = capsys.readouterr().out
    assert "QR code unavailable" in out and "wisp-pair://v1?host=100.64.0.9&ip=100.64.0.9" in out


# ---------------------------------------------------------------- qr rendering

def test_qr_matrix_parsing_and_halfblocks():
    assert qrmod.matrix_from_output("101\n010\n101\n") == [[True, False, True], [False, True, False],
                                                          [True, False, True]]
    assert qrmod.matrix_from_output("10\n01\n10\n") is None  # not square
    assert qrmod.matrix_from_output("1x1\n") is None
    art = qrmod.render_halfblocks([[True, False], [False, True]], quiet=1, color=True)
    plain = art.replace("\x1b[38;5;16;48;5;231m", "").replace("\x1b[0m", "").split("\n")
    assert plain == [" ▄  ", "  ▀ "]  # 4 module rows -> 2 text rows, dark on white
    mono = qrmod.render_halfblocks([[True]], quiet=1, color=False)
    assert "\x1b" not in mono


# ---------------------------------------------------------------- hello

def test_hello(env):
    r = env.client.get("/phone/v1/hello", headers=env.auth)
    assert r.status_code == 200
    assert r.json() == {
        "gateway_version": 1, "mac_name": "Test Mac",
        "device": {"id": env.device["id"], "name": "Test iPhone"},
        "backend": {"reachable": True, "mode": {"mode": "production"}}}
    assert env.upstream.requests[-1].url.path == "/mode"


def test_hello_with_backend_down_is_still_200(env):
    env.upstream.down = True
    r = env.client.get("/phone/v1/hello", headers=env.auth)
    assert r.status_code == 200
    assert r.json()["backend"] == {"reachable": False, "mode": None}


# ---------------------------------------------------------------- agent

def test_agent_body_is_filtered_and_debug_forced_off(env):
    r = env.client.post("/phone/v1/agent", headers=env.auth, json={
        "prompt": "hello", "session_id": "abc_123-X", "debug": True, "test_mode": True,
        "model": "evil", "extra": {"a": 1}})
    assert r.status_code == 200
    assert env.upstream.body() == {"prompt": "hello", "session_id": "abc_123-X", "debug": False}
    assert env.upstream.requests[-1].url.path == "/agent"
    assert "authorization" not in env.upstream.requests[-1].headers


def test_agent_without_session_id_sends_none(env):
    env.client.post("/phone/v1/agent", headers=env.auth, json={"prompt": "hi", "session_id": None})
    assert env.upstream.body() == {"prompt": "hi", "debug": False}


def test_agent_sse_passes_through_in_order(env):
    r = env.client.post("/phone/v1/agent", headers=env.auth, json={"prompt": "hi"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.content == b"".join(SSE_CHUNKS)
    assert env.upstream.stream.closed is True


@pytest.mark.parametrize("body", [
    {}, {"prompt": ""}, {"prompt": 5}, {"prompt": None}, {"prompt": "x" * 16001},
    {"prompt": "hi", "session_id": "bad id"}, {"prompt": "hi", "session_id": "../x"},
    {"prompt": "hi", "session_id": 7}, {"prompt": "hi", "session_id": "a" * 65},
    ["prompt"], "prompt"])
def test_agent_validation_422(env, body):
    r = env.client.post("/phone/v1/agent", headers=env.auth, json=body)
    assert r.status_code == 422
    assert env.upstream.requests == []


def test_agent_accepts_maximum_prompt(env):
    r = env.client.post("/phone/v1/agent", headers=env.auth, json={"prompt": "x" * 16000})
    assert r.status_code == 200


def test_agent_invalid_json_and_oversize(env):
    assert env.client.post("/phone/v1/agent", headers=env.auth, content=b"{nope").status_code == 422
    assert env.client.post("/phone/v1/agent", headers=env.auth,
                           content=b'{"prompt":"' + b"x" * 70000 + b'"}').status_code == 413
    assert env.upstream.requests == []


def test_agent_upstream_down_is_502(env):
    env.upstream.down = True
    r = env.client.post("/phone/v1/agent", headers=env.auth, json={"prompt": "hi"})
    assert r.status_code == 502 and r.json() == {"detail": "wisp_backend_unreachable"}


def test_agent_upstream_error_status_passes_through(env):
    env.upstream.overrides[("POST", "/agent")] = httpx.Response(503, json={"detail": "busy"})
    r = env.client.post("/phone/v1/agent", headers=env.auth, json={"prompt": "hi"})
    assert r.status_code == 503 and r.json() == {"detail": "busy"}


def test_phone_disconnect_closes_upstream_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))
    upstream = Upstream()
    upstream.stream = ChunkStream(SSE_CHUNKS, hang_after_first=True)
    app = create_app(transport=httpx.MockTransport(upstream))
    token = new_token()
    app.state.devices.add("p", token)
    payload = json.dumps({"prompt": "hi"}).encode()

    async def run():
        async with app.router.lifespan_context(app):
            sent, first, delivered = [], asyncio.Event(), []

            async def receive():
                if not delivered:
                    delivered.append(1)
                    return {"type": "http.request", "body": payload, "more_body": False}
                await first.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)
                if message["type"] == "http.response.body" and message.get("body"):
                    first.set()

            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                     "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/phone/v1/agent",
                     "raw_path": b"/phone/v1/agent", "query_string": b"", "root_path": "",
                     "headers": [(b"authorization", f"Bearer {token}".encode()),
                                 (b"content-type", b"application/json"),
                                 (b"content-length", str(len(payload)).encode())],
                     "client": ("100.1.2.3", 5555), "server": ("100.9.9.9", 8799)}
            await asyncio.wait_for(app(scope, receive, send), 5)
            return sent

    sent = asyncio.run(run())
    assert any(m.get("body") == SSE_CHUNKS[0] for m in sent if m["type"] == "http.response.body")
    assert upstream.stream.closed is True


# ---------------------------------------------------------------- approve

GOOD_APPROVE = {"session_id": "s1", "action_id": "a1", "approved": True, "scope": "once"}


def test_approve_forwards_only_allowed_fields(env):
    r = env.client.post("/phone/v1/agent/approve", headers=env.auth,
                        json={**GOOD_APPROVE, "request_id": "r9", "extra": 1, "tool": "send"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert env.upstream.body() == {**GOOD_APPROVE, "request_id": "r9"}
    assert env.upstream.requests[-1].url.path == "/agent/approve"


@pytest.mark.parametrize("patch", [
    {"approved": "true"}, {"approved": 1}, {"approved": None}, {"scope": "forever"},
    {"scope": None}, {"scope": ["once"]}, {"action_id": ""}, {"action_id": 3}, {"action_id": None},
    {"session_id": "no spaces"}, {"session_id": ""}, {"session_id": None}, {"request_id": ""},
    {"request_id": 5}])
def test_approve_validation_422(env, patch):
    r = env.client.post("/phone/v1/agent/approve", headers=env.auth, json={**GOOD_APPROVE, **patch})
    assert r.status_code == 422
    assert env.upstream.requests == []


def test_approve_requires_every_field(env):
    for key in ("session_id", "action_id", "approved", "scope"):
        body = {k: v for k, v in GOOD_APPROVE.items() if k != key}
        assert env.client.post("/phone/v1/agent/approve", headers=env.auth, json=body).status_code == 422


# ---------------------------------------------------------------- sessions / today

def test_sessions_and_today_pass_through(env):
    assert env.client.get("/phone/v1/sessions", headers=env.auth).json() == [{"id": "s1"}]
    assert env.client.get("/phone/v1/today", headers=env.auth).json() == {"plan": []}
    assert env.upstream.requests[-1].url.path == "/assistant/today"
    r = env.client.get("/phone/v1/sessions/abc-1_Z", headers=env.auth)
    assert r.json() == {"id": "abc-1_Z", "method": "GET"}
    r = env.client.delete("/phone/v1/sessions/abc-1_Z", headers=env.auth)
    assert r.json() == {"id": "abc-1_Z", "method": "DELETE"}


def test_query_strings_are_not_forwarded(env):
    env.client.get("/phone/v1/sessions?limit=5&x=../../etc", headers=env.auth)
    assert env.upstream.requests[-1].url.query == b""


@pytest.mark.parametrize("sid", ["a b", "x" * 65, "a.b", "a%00", "a%24b", "%E2%82%AC"])
def test_sid_regex_enforced(env, sid):
    assert env.client.get(f"/phone/v1/sessions/{sid}", headers=env.auth).status_code == 422
    assert env.client.delete(f"/phone/v1/sessions/{sid}", headers=env.auth).status_code == 422
    assert env.upstream.requests == []


def test_sid_slash_and_traversal_never_reach_upstream(env):
    for path in ("/phone/v1/sessions/..", "/phone/v1/sessions/a/b", "/phone/v1/sessions/a%2Fb"):
        assert env.client.get(path, headers=env.auth).status_code in (404, 422)
    assert [r for r in env.upstream.requests if r.url.path != "/sessions"] == []


def test_upstream_status_passes_through(env):
    env.upstream.overrides[("GET", "/sessions/gone")] = httpx.Response(404, json={"detail": "no session"})
    r = env.client.get("/phone/v1/sessions/gone", headers=env.auth)
    assert r.status_code == 404 and r.json() == {"detail": "no session"}


@pytest.mark.parametrize("method,path,body", [
    ("get", "/phone/v1/sessions", None), ("get", "/phone/v1/sessions/abc", None),
    ("delete", "/phone/v1/sessions/abc", None), ("get", "/phone/v1/today", None),
    ("post", "/phone/v1/agent/approve", GOOD_APPROVE)])
def test_upstream_down_is_502(env, method, path, body):
    env.upstream.down = True
    r = getattr(env.client, method)(path, headers=env.auth, **({"json": body} if body else {}))
    assert r.status_code == 502 and r.json() == {"detail": "wisp_backend_unreachable"}


def test_no_routes_beyond_the_spec(env):
    for method, path in [("get", "/phone/v1/nope"), ("get", "/docs"), ("get", "/openapi.json"),
                         ("get", "/redoc"), ("get", "/sessions"), ("get", "/mode"), ("post", "/agent"),
                         ("get", "/phone/v1/agent"), ("put", "/phone/v1/sessions/abc"),
                         ("post", "/phone/v1/hello"), ("get", "/phone/v1/")]:
        r = getattr(env.client, method)(path, headers=env.auth)
        assert r.status_code in (404, 405), (method, path, r.status_code)
    assert env.upstream.requests == []


# ---------------------------------------------------------------- models

BLOB = bytes(range(256)) * 12  # 3072 bytes


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "bundles"
    d = root / "Ling-3.0 Tiny_V3"
    d.mkdir(parents=True)
    (d / "model.safetensors").write_bytes(BLOB)
    (d / "config.json").write_text(json.dumps({"model_type": "bailing_hybrid"}))
    (d / "tokenizer.json").write_text("{}")
    (d / "chat_template.jinja").write_text("x")
    (d / "evil.py").write_text("print('no')")
    (d / "README.md").write_text("no")
    (d / ".hidden.json").write_text("{}")
    (d / "sub").mkdir()
    (d / "sub" / "inner.json").write_text("{}")
    outside = tmp_path / "secret.json"
    outside.write_text('{"secret": true}')
    (d / "link.json").symlink_to(outside)
    (d / "linkdir.json").symlink_to(root)
    return d


@pytest.fixture
def menv(tmp_path, monkeypatch, bundle):
    with make_env(tmp_path, monkeypatch, model_dirs=[bundle]) as e:
        yield e


MODEL_ID = "ling-3.0-tiny_v3"


def test_models_listing_is_filtered_and_hashed(menv):
    r = menv.client.get("/phone/v1/models", headers=menv.auth)
    assert r.status_code == 200
    (model,) = r.json()["models"]
    assert model["id"] == MODEL_ID and model["model_type"] == "bailing_hybrid"
    names = [f["name"] for f in model["files"]]
    assert names == ["chat_template.jinja", "config.json", "model.safetensors", "tokenizer.json"]
    by_name = {f["name"]: f for f in model["files"]}
    assert by_name["model.safetensors"]["sha256"] == hashlib.sha256(BLOB).hexdigest()
    assert by_name["model.safetensors"]["size"] == len(BLOB)
    assert model["total_bytes"] == sum(f["size"] for f in model["files"])
    for banned in ("evil.py", "README.md", "link.json", "linkdir.json", "sub", "inner.json", ".hidden.json"):
        assert banned not in names


def test_hash_cache_reused_and_invalidated_on_change(menv, bundle, monkeypatch):
    calls = []
    real = ModelCatalog._hash_file
    monkeypatch.setattr(ModelCatalog, "_hash_file", staticmethod(lambda p, i: (calls.append(i.name), real(p, i))[1]))
    menv.client.get("/phone/v1/models", headers=menv.auth)
    first = len(calls)
    assert first == 4
    menv.client.get("/phone/v1/models", headers=menv.auth)
    assert len(calls) == first  # served from the cache
    cache = menv.state / "model_hashes.json"
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    new = BLOB + b"more"
    (bundle / "model.safetensors").write_bytes(new)
    r = menv.client.get("/phone/v1/models", headers=menv.auth)
    assert calls[first:] == ["model.safetensors"]
    sha = {f["name"]: f["sha256"] for f in r.json()["models"][0]["files"]}["model.safetensors"]
    assert sha == hashlib.sha256(new).hexdigest()
    keys = json.loads(cache.read_text())["entries"]
    assert len([k for k in keys if "model.safetensors" in k]) == 1  # stale entry pruned


def test_models_json_configuration(tmp_path, monkeypatch, bundle):
    with make_env(tmp_path, monkeypatch) as env:
        (env.state / "models.json").write_text(json.dumps([
            {"id": "Custom.Id", "path": str(bundle)}, {"id": "rel", "path": "relative/dir"},
            {"id": "gone", "path": str(tmp_path / "missing")}, "junk"]))
        models = env.client.get("/phone/v1/models", headers=env.auth).json()["models"]
        assert [m["id"] for m in models] == ["custom.id"]


def test_models_empty_when_unconfigured(env):
    assert env.client.get("/phone/v1/models", headers=env.auth).json() == {"models": []}


def test_hashing_runs_off_the_event_loop(menv, monkeypatch):
    import threading
    seen = []
    real = ModelCatalog._hash_file
    monkeypatch.setattr(ModelCatalog, "_hash_file", staticmethod(
        lambda p, i: (seen.append(threading.current_thread() is threading.main_thread()), real(p, i))[1]))
    menv.client.get("/phone/v1/models", headers=menv.auth)
    assert seen and not any(seen)


def url(name, model_id=MODEL_ID):
    return f"/phone/v1/models/{model_id}/files/{name}"


def test_file_download_full(menv):
    r = menv.client.get(url("model.safetensors"), headers=menv.auth)
    assert r.status_code == 200 and r.content == BLOB
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == str(len(BLOB))


@pytest.mark.parametrize("spec,start,end", [
    ("bytes=10-19", 10, 19), ("bytes=3000-", 3000, 3071), ("bytes=-5", 3067, 3071),
    ("bytes=0-0", 0, 0), ("bytes=3070-99999", 3070, 3071), ("bytes=-99999", 0, 3071)])
def test_file_range_206(menv, spec, start, end):
    r = menv.client.get(url("model.safetensors"), headers={**menv.auth, "Range": spec})
    assert r.status_code == 206
    assert r.content == BLOB[start:end + 1]
    assert r.headers["content-range"] == f"bytes {start}-{end}/{len(BLOB)}"
    assert r.headers["content-length"] == str(end - start + 1)


@pytest.mark.parametrize("spec", ["bytes=3072-", "bytes=5000-6000", "bytes=9-3", "bytes=-0"])
def test_file_range_unsatisfiable_416(menv, spec):
    r = menv.client.get(url("model.safetensors"), headers={**menv.auth, "Range": spec})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(BLOB)}"


def test_file_range_with_huge_numbers_serves_whole_file(menv):
    # int() refuses ~4300+ digit strings; the header must be ignored, not turned into a 500.
    r = menv.client.get(url("model.safetensors"), headers={**menv.auth, "Range": "bytes=" + "9" * 5000 + "-"})
    assert r.status_code == 200 and r.content == BLOB


def test_file_head(menv):
    r = menv.client.head(url("model.safetensors"), headers=menv.auth)
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-length"] == str(len(BLOB)) and r.headers["accept-ranges"] == "bytes"
    r = menv.client.head(url("model.safetensors"), headers={**menv.auth, "Range": "bytes=0-9"})
    assert r.status_code == 206 and r.headers["content-length"] == "10"


def test_file_requires_auth(menv):
    assert menv.client.get(url("model.safetensors")).status_code == 401
    assert menv.client.head(url("model.safetensors")).status_code == 401


@pytest.mark.parametrize("name", [
    "evil.py", "README.md", "link.json", "linkdir.json", "sub", "inner.json", ".hidden.json",
    "..", "%2e%2e", "..%2Fsecret.json", "%2e%2e%2fsecret.json", "a%2Fb.json", "%5Csecret.json",
    "..%5Csecret.json", "model.safetensors%00.py", "model.safetensors.py", "missing.json",
    "%2Fetc%2Fpasswd", "model.safetensors%20", "MODEL.SAFETENSORS"])
def test_file_name_attacks_rejected(menv, name):
    r = menv.client.get(url(name), headers=menv.auth)
    assert r.status_code in (404, 422, 400), (name, r.status_code)
    assert b"secret" not in r.content and r.content != BLOB


@pytest.mark.parametrize("model_id", ["..", "%2e%2e", "nope", "Ling-3.0 Tiny_V3", "a%2Fb", "%2e%2e%2fbundles"])
def test_model_id_attacks_rejected(menv, model_id):
    r = menv.client.get(url("config.json", model_id), headers=menv.auth)
    assert r.status_code in (404, 422, 400)


def test_symlinked_bundle_file_is_refused_even_by_exact_name(menv):
    assert menv.client.get(url("link.json"), headers=menv.auth).status_code == 404


# ---------------------------------------------------------------- binding

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "localhost", "", "100.64.0.1"])
def test_bind_override_only_loopback(host):
    with pytest.raises(tailscale.BindError):
        tailscale.resolve_bind_host(host)


def test_bind_override_loopback_allowed():
    assert tailscale.resolve_bind_host("127.0.0.1") == "127.0.0.1"
    assert tailscale.resolve_bind_host("::1") == "::1"


def test_bind_uses_tailscale_address(monkeypatch):
    monkeypatch.setattr(tailscale, "_run", lambda args, timeout=8.0: "100.101.102.103\n")
    assert tailscale.resolve_bind_host(None) == "100.101.102.103"


@pytest.mark.parametrize("output", [None, "", "\n", "192.168.1.5\n", "0.0.0.0\n", "fd7a::1\n", "garbage"])
def test_bind_refuses_without_tailscale_address(monkeypatch, output):
    monkeypatch.setattr(tailscale, "_run", lambda args, timeout=8.0: output)
    with pytest.raises(tailscale.BindError):
        tailscale.resolve_bind_host(None)


def test_magicdns_name_strips_trailing_dot(monkeypatch):
    monkeypatch.setattr(tailscale, "_run", lambda a, timeout=8.0: json.dumps(
        {"Self": {"DNSName": "mac.tail1.ts.net."}}))
    assert tailscale.magicdns_name() == "mac.tail1.ts.net"
    monkeypatch.setattr(tailscale, "_run", lambda a, timeout=8.0: "{}")
    assert tailscale.magicdns_name() is None


def test_serve_refuses_0000_and_never_starts_uvicorn(tmp_path, monkeypatch, capsys):
    import uvicorn
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))
    started = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: started.append(self.config.host))
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert cli.main(["serve", "--host", "::"]) == 2
    monkeypatch.setattr(tailscale, "_run", lambda a, timeout=8.0: None)
    assert cli.main(["serve"]) == 2
    assert started == []
    assert "refused" in capsys.readouterr().err


def test_serve_binds_tailscale_ip_and_loopback_override(tmp_path, monkeypatch):
    import uvicorn
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))
    started = []
    monkeypatch.setattr(uvicorn.Server, "run",
                        lambda self, *a, **k: started.append((self.config.host, self.config.port)))
    monkeypatch.setattr(tailscale, "_run", lambda a, timeout=8.0: "100.88.1.2\n")
    monkeypatch.setattr(cli, "_computer_name", lambda: "Mac")
    assert cli.main(["serve"]) == 0
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "18799"]) == 0
    assert started == [("100.88.1.2", 8799), ("127.0.0.1", 18799)]


def test_backend_must_be_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("MOE_DIR", str(tmp_path / "moe"))
    with pytest.raises(ValueError):
        create_app(backend="http://example.com:8765")
    with pytest.raises(ValueError):
        create_app(backend="https://127.0.0.1:8765")
    create_app(backend="http://127.0.0.1:8775")
    assert cli.main(["serve", "--host", "127.0.0.1", "--backend", "http://10.0.0.1:1"]) == 2


def test_state_dir_defaults_under_home(tmp_path, monkeypatch):
    from service.phone_gateway.store import state_dir
    monkeypatch.delenv("MOE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = state_dir()
    assert path == Path(tmp_path) / ".moe" / "phone_gateway"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


def test_qr_matrix_trims_coreimage_border():
    framed = "00000\n01110\n01010\n01110\n00000\n"
    assert qrmod.matrix_from_output(framed) == [[True] * 3, [True, False, True], [True] * 3]

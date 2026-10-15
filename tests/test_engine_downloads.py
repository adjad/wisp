"""Private downloader contract, using ASGI and a fake pinned-oMLX admin API.

No sockets, real engine, model, network, app or user state is used.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from service.engine import downloads

MODEL = "Ling-3.0-tiny-oQ4e"
REPO = "mlx-works/" + MODEL
SECRET = "synthetic-engine-key-that-must-never-reach-the-ui"


@pytest.fixture
def wire(monkeypatch):
    from service.inference import attributed_transport
    engine = SimpleNamespace(running=lambda: True, verified=True, api_key=SECRET)
    monkeypatch.setattr(downloads.mode, "is_bundled", lambda: True)
    monkeypatch.setattr(downloads.runtime, "_manager", engine)
    state = SimpleNamespace(tasks=[], seen=[], starts=0, status=200, payload=None, login=True, cookie=True)

    async def ensure():
        state.starts += 1
        engine.verified = True
    monkeypatch.setattr(downloads, "_ensure_engine", ensure)
    monkeypatch.setattr(downloads, "_operation_lock", asyncio.Lock())

    def upstream(request):
        state.seen.append(request)
        assert str(request.url).startswith("http://127.0.0.1:8791/admin/api/")
        if request.url.path == "/admin/api/login":
            assert json.loads(request.content) == {"api_key": SECRET, "remember": False}
            if not state.login:
                return httpx.Response(401, json={"detail": SECRET})
            return httpx.Response(200, json={"success": True},
                                  headers={"set-cookie": "omlx_admin_session=fake-session; HttpOnly; Path=/"} if state.cookie else {})
        assert request.headers["cookie"] == "omlx_admin_session=fake-session"
        if state.status != 200:
            return httpx.Response(state.status, json={"detail": SECRET},
                                  headers={"location": "https://example.invalid/" + SECRET})
        if state.payload is not None:
            return httpx.Response(200, content=state.payload)
        path = request.url.path
        if path == "/admin/api/hf/tasks":
            return httpx.Response(200, json={"tasks": state.tasks})
        if path == "/admin/api/hf/download":
            assert json.loads(request.content) == {"repo_id": REPO}
            state.tasks.append(row())
        elif path == "/admin/api/hf/cancel/" + state.tasks[0]["task_id"]:
            state.tasks[0]["status"] = "cancelled"
        elif path == "/admin/api/hf/retry/task-1":
            assert json.loads(request.content) == {}
            state.tasks[0] = {**state.tasks[0], "task_id": "task-2", "status": "pending", "created_at": 2}
        else:
            pytest.fail("Unexpected upstream operation: " + path)
        return httpx.Response(200, json={"success": True})

    def checked_transport(origin, key):
        assert origin == "http://127.0.0.1:8791" and key == SECRET
        return httpx.MockTransport(upstream)
    monkeypatch.setattr(attributed_transport, "bundled_transport", checked_transport)
    app = FastAPI()
    app.include_router(downloads.router, prefix="/engine")
    state.app, state.engine = app, engine
    return state


def row(**changes):
    return dict(task_id="task-1", repo_id=REPO, status="downloading", progress=42.5,
                downloaded_size=425, total_size=1000, speed_bps=100, created_at=1,
                error=SECRET, token=SECRET, private_path="/synthetic/private") | changes


def request(wire, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wire.app),
                                     base_url="http://127.0.0.1:8765") as client:
            return await client.request(method, "/engine" + path, **kwargs)
    return asyncio.run(run())


def test_start_download_poll_cancel_retry_and_reopen(wire):
    response = request(wire, "POST", "/downloads", json={"model": MODEL})
    assert response.status_code == 200 and wire.starts == 1
    task = response.json()["tasks"][0]
    assert task == {"model": MODEL, "state": "downloading", "progress": 42.5,
                    "downloaded_bytes": 425, "total_bytes": 1000, "bytes_per_second": 100,
                    "message": None}
    # Progress comes from the engine's persisted state, not the setup window.
    assert request(wire, "GET", "/downloads").json() == response.json()
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 200
    assert sum(r.url.path == "/admin/api/hf/download" for r in wire.seen) == 1
    cancelled = request(wire, "POST", "/downloads/cancel", json={"model": MODEL})
    assert cancelled.json()["tasks"][0]["state"] == "cancelled"
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).json()["tasks"][0]["state"] == "pending"
    assert any(r.url.path == "/admin/api/hf/retry/task-1" for r in wire.seen)
    request(wire, "POST", "/downloads/cancel", json={"model": MODEL})
    assert wire.seen[-2].url.path == "/admin/api/login"
    assert any(r.url.path == "/admin/api/hf/cancel/task-2" for r in wire.seen)
    wire.tasks[0]["status"] = "completed"
    assert request(wire, "GET", "/downloads").json()["tasks"][0]["state"] == "completed"
    assert SECRET not in response.text and "fake-session" not in response.text


def test_concurrent_clicks_only_start_one_transfer(wire):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wire.app), base_url="http://127.0.0.1:8765") as client:
            return await asyncio.gather(*(client.post("/engine/downloads", json={"model": MODEL}) for _ in range(4)))
    assert all(r.status_code == 200 for r in asyncio.run(run()))
    assert sum(r.url.path == "/admin/api/hf/download" for r in wire.seen) == 1


@pytest.mark.parametrize("body", [{}, {"model": "other/repo"}, {"model": MODEL, "url": SECRET},
                                  {"model": MODEL, "hf_token": SECRET}, {"model": []}, [], {"model": True}])
def test_invalid_inputs_never_start_or_authenticate(wire, body):
    for path in ("/downloads", "/downloads/cancel"):
        response = request(wire, "POST", path, json=body)
        assert response.status_code == 422 and SECRET not in response.text
    assert not wire.seen and wire.starts == 0


def test_simple_cross_site_malformed_and_oversized_requests_are_refused(wire):
    for path, kwargs, status in [
        ("/downloads", {"content": 'model=' + MODEL, "headers": {"content-type": "text/plain"}}, 415),
        ("/downloads", {"json": {"model": MODEL}, "headers": {"origin": "https://evil.invalid"}}, 403),
        ("/start", {"json": {}, "headers": {"origin": "https://evil.invalid"}}, 403),
        ("/start", {"json": {"api_key": SECRET}}, 422),
        ("/downloads", {"content": "{" + SECRET, "headers": {"content-type": "application/json"}}, 422),
        ("/downloads", {"content": "x" * 4097, "headers": {"content-type": "application/json"}}, 413),
    ]:
        response = request(wire, "POST", path, **kwargs)
        assert response.status_code == status and SECRET not in response.text
    assert not wire.seen and wire.starts == 0


def test_matching_hostile_host_and_origin_are_not_trusted(wire):
    response = request(wire, "POST", "/downloads", json={"model": MODEL},
                       headers={"host": "rebound.example:8765", "origin": "http://rebound.example:8765"})
    assert response.status_code == 403 and not wire.seen and wire.starts == 0
    assert request(wire, "GET", "/downloads", headers={"host": "rebound.example:8765"}).status_code == 403
    response = request(wire, "POST", "/start", json={}, headers={"origin": "http://127.0.0.1:8765"})
    assert response.status_code == 200 and wire.starts == 1


def test_unavailable_status_and_cancel_never_start_engine(wire):
    wire.engine.verified = False
    assert request(wire, "GET", "/downloads").json() == {"tasks": []}
    assert request(wire, "POST", "/downloads/cancel", json={"model": MODEL}).status_code == 503
    assert wire.starts == 0 and not wire.seen
    assert request(wire, "POST", "/start", json={}).json() == {"running": True}
    assert wire.starts == 1


def test_external_mode_is_never_started_or_queried(wire, monkeypatch):
    monkeypatch.setattr(downloads.mode, "is_bundled", lambda: False)
    for method, path, kwargs in [("GET", "/downloads", {}), ("POST", "/start", {"json": {}}),
                                  ("POST", "/downloads", {"json": {"model": MODEL}})]:
        assert request(wire, method, path, **kwargs).status_code == 409
    assert not wire.seen and wire.starts == 0


def test_untrusted_engine_values_and_errors_never_escape(wire):
    wire.tasks = [row(), {**row(), "repo_id": "unrelated/private"}, {**row(), "task_id": "../../login"}]
    wire.tasks[0].update(status="failed", progress=1000, speed_bps=-1, total_size=10**30)
    response = request(wire, "GET", "/downloads")
    task = response.json()["tasks"][0]
    assert len(response.json()["tasks"]) == 1 and task["progress"] == 100
    assert task["bytes_per_second"] == 0 and task["total_bytes"] == 10**13
    assert SECRET not in response.text and "private" not in response.text
    assert "Retry" in task["message"]


@pytest.mark.parametrize("status", [302, 401, 500])
def test_upstream_redirects_and_failures_are_sanitized(wire, status):
    wire.status = status
    response = request(wire, "GET", "/downloads")
    assert response.status_code == 503 and SECRET not in response.text
    assert all(r.url.host == "127.0.0.1" for r in wire.seen)


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b'{"tasks":null}', b"x" * (256 * 1024 + 1)])
def test_invalid_or_oversized_status_is_refused(wire, payload):
    wire.payload = payload
    assert request(wire, "GET", "/downloads").status_code == 503


def test_login_refusal_never_reaches_downloader(wire):
    wire.login = False
    response = request(wire, "POST", "/downloads", json={"model": MODEL})
    assert response.status_code == 503 and SECRET not in response.text
    assert all(r.url.path == "/admin/api/login" for r in wire.seen)


def test_login_without_session_cookie_is_refused(wire):
    wire.cookie = False
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 503
    assert all(r.url.path == "/admin/api/login" for r in wire.seen)


def test_total_deadline_includes_startup_and_lock_wait(wire, monkeypatch):
    monkeypatch.setattr(downloads, "_REQUEST_DEADLINE", 0.03)
    async def slow_start():
        await asyncio.sleep(1)
    with monkeypatch.context() as patch:
        patch.setattr(downloads, "_ensure_engine", slow_start)
        response = request(wire, "POST", "/downloads", json={"model": MODEL})
        assert response.status_code == 504 and not downloads._operation_lock.locked() and not wire.seen

    async def contended():
        lock = asyncio.Lock()
        monkeypatch.setattr(downloads, "_operation_lock", lock)
        await lock.acquire()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wire.app), base_url="http://127.0.0.1:8765") as client:
                response = await client.post("/engine/downloads", json={"model": MODEL})
                assert response.status_code == 504 and lock.locked()
        finally:
            lock.release()
    asyncio.run(contended())
    assert wire.starts == 0 and not wire.seen


def test_timeout_after_start_is_reconciled_without_duplicate_download(wire, monkeypatch):
    monkeypatch.setattr(downloads, "_REQUEST_DEADLINE", 0.03)
    real_call = downloads._call
    started = False
    async def stall_last_poll(method, path, payload=None):
        nonlocal started
        result = await real_call(method, path, payload)
        if path == "/admin/api/hf/download":
            started = True
        elif started and path == "/admin/api/hf/tasks":
            await asyncio.sleep(1)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(downloads, "_call", stall_last_poll)
        assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 504
    assert not downloads._operation_lock.locked()
    assert request(wire, "GET", "/downloads").json()["tasks"][0]["state"] == "downloading"
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 200
    assert sum(r.url.path == "/admin/api/hf/download" for r in wire.seen) == 1


def test_timeout_during_login_cancels_request_and_releases_lock(wire, monkeypatch):
    from service.inference import attributed_transport
    monkeypatch.setattr(downloads, "_REQUEST_DEADLINE", 0.03)
    seen = []
    async def stalled_login(request):
        seen.append(request.url.path)
        await asyncio.sleep(1)
        pytest.fail("Timed-out login should have been cancelled")
    monkeypatch.setattr(attributed_transport, "bundled_transport", lambda *args: httpx.MockTransport(stalled_login))
    response = request(wire, "POST", "/downloads", json={"model": MODEL})
    assert response.status_code == 504 and seen == ["/admin/api/login"]
    assert not downloads._operation_lock.locked() and SECRET not in response.text


def test_attribution_refusal_has_no_network_fallback(wire, monkeypatch):
    from service.inference import attributed_transport
    def refused(*args):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(attributed_transport, "bundled_transport", refused)
    response = request(wire, "GET", "/downloads")
    assert response.status_code == 503 and SECRET not in response.text and not wire.seen


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None, reason="Requires macOS Swift compiler")
def test_native_setup_model_state_uses_only_intercepted_http(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "SetupGuideChecks"
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("WISP_", "MOE_"))}
    environment.update(HOME=str(tmp_path), WISP_HOME=str(tmp_path), TMPDIR=str(tmp_path))
    command = ["swiftc", "-swift-version", "5", "-module-cache-path", str(tmp_path / "module-cache"),
               str(root / "tests/SetupGuideChecks.swift"),
               str(root / "app/Sources/WispApp/SetupGuideView.swift"),
               str(root / "app/Sources/WispApp/Theme.swift"), "-o", str(output)]
    compiled = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120)
    assert compiled.returncode == 0, compiled.stderr[-5000:]
    checked = subprocess.run([str(output)], capture_output=True, text=True, env=environment, timeout=30)
    assert checked.returncode == 0, checked.stderr[-5000:]
    assert "setup model-state checks passed; all HTTP intercepted" in checked.stdout

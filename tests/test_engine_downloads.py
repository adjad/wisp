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
def wire(monkeypatch, tmp_path):
    from service.inference import attributed_transport
    monkeypatch.setattr(downloads.mode, "engine_data_dir", lambda: tmp_path / "engine")
    engine = SimpleNamespace(running=lambda: True, verified=True, api_key=SECRET)
    monkeypatch.setattr(downloads.mode, "is_bundled", lambda: True)
    monkeypatch.setattr(downloads.runtime, "_manager", engine)
    state = SimpleNamespace(tasks=[], seen=[], starts=0, status=200, payload=None, login=True, cookie=True,
                            delete_fails=False, active_after_delete=False)

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
        elif path.startswith("/admin/api/hf/retry/"):
            assert json.loads(request.content) == {}
            task = next(t for t in state.tasks if t["task_id"] == path.rsplit("/", 1)[1])
            if task["status"] not in ("failed", "cancelled"):     # as oMLX's retry_download refuses
                return httpx.Response(400, json={"detail": "not retryable"})
            state.tasks[state.tasks.index(task)] = {**task, "task_id": "task-2", "status": "pending", "created_at": 2}
        elif path.startswith("/admin/api/hf/task/") and request.method == "DELETE":
            if state.delete_fails:
                if state.active_after_delete:         # the task restarted on its own: "still active"
                    state.tasks[0] = {**state.tasks[0], "status": "downloading", "downloaded_size": 1,
                                      "total_size": 10**10}
                return httpx.Response(404, json={"detail": "Task not found or still active"})
            state.tasks = [t for t in state.tasks if t["task_id"] != path.rsplit("/", 1)[1]]
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
                    "message": None, "reason": None}
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
    wire.tasks[0].update(status="completed", downloaded_size=10**10, total_size=10**10)
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
    assert task["message"] == "Download failed. Check your connection and free disk space."


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


@pytest.mark.parametrize("error, reason, needle", [
    ("Repository not found: Neo-Arid/Gemma-Wisp. This may be a gated model", "not_found", "isn't available"),
    ("Repository 'x' is gated. Please provide a valid HF token with access.", "gated", "sign-in"),
    ("GatedRepoError: Cannot access gated repo for url https://huggingface.co/x", "gated", "sign-in"),
    ("401 Client Error. Access to model x is restricted.", "gated", "sign-in"),
    ("[Errno 28] No space left on device: '/synthetic/private'", "no_space", "disk space"),
    ("ConnectError: [Errno 8] nodename nor servname provided", "offline", "huggingface.co"),
    ("Max retries exceeded with url: /api/models (Caused by ReadTimeoutError)", "offline", "huggingface.co"),
    (SECRET, "other", "Check your connection and free disk space"),
    (None, "other", "Check your connection and free disk space"),
])
def test_a_failed_download_says_why_in_fixed_words(wire, error, reason, needle):
    wire.tasks = [row(status="failed", error=error)]
    response = request(wire, "GET", "/downloads")
    task = response.json()["tasks"][0]
    assert task["reason"] == reason and needle in task["message"]
    assert SECRET not in response.text and "/synthetic/private" not in response.text
    assert "Errno" not in task["message"]


def test_a_missing_repo_names_the_page_people_can_check(wire):
    wire.tasks = [row(status="failed", error="Repository not found: whatever")]
    message = request(wire, "GET", "/downloads").json()["tasks"][0]["message"]
    model = downloads.catalog.by_id(MODEL)
    assert f"huggingface.co/{model.repo}" in message and model.label in message


def test_a_running_download_has_no_reason_or_message(wire):
    wire.tasks = [row()]
    task = request(wire, "GET", "/downloads").json()["tasks"][0]
    assert task["reason"] is None and task["message"] is None


def test_retry_after_an_incomplete_download_starts_a_fresh_one(wire):
    # The engine calls a README-only repo "completed" and refuses to retry a completed task.
    wire.tasks = [row(status="completed", progress=100, downloaded_size=1547, total_size=1547)]
    response = request(wire, "POST", "/downloads", json={"model": MODEL})
    assert response.status_code == 200, response.text
    paths = [(r.method, r.url.path) for r in wire.seen]
    assert ("DELETE", "/admin/api/hf/task/task-1") in paths and ("POST", "/admin/api/hf/download") in paths
    assert not any(p.startswith("/admin/api/hf/retry/") for _, p in paths)
    assert response.json()["tasks"][0]["state"] == "downloading"


def _folder(wire_tmp, model, files):
    folder = wire_tmp / "engine" / "models" / model.repo
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder


def test_a_finished_download_is_checked_on_disk(wire, tmp_path):
    import hashlib
    from dataclasses import replace
    weights = b"w" * 64
    pinned = (("config.json", hashlib.sha256(b"{}").hexdigest(), 2),
              ("model.safetensors", hashlib.sha256(weights).hexdigest(), len(weights)))
    model = replace(downloads.catalog.by_id(MODEL), pinned_files=pinned)
    real_by_id = downloads.catalog.by_id
    downloads.catalog.by_id = lambda m: model if m == MODEL else real_by_id(m)
    try:
        done = row(status="completed", progress=100, downloaded_size=10**10, total_size=10**10)
        folder = _folder(tmp_path, model, {"config.json": b"{}", "model.safetensors": weights})
        wire.tasks = [dict(done)]
        assert request(wire, "GET", "/downloads").json()["tasks"][0]["state"] == "completed"
        (folder / "model.safetensors").write_bytes(b"x" * 64)               # same size, other bytes
        task = request(wire, "GET", "/downloads").json()["tasks"][0]
        assert task["state"] == "failed" and task["reason"] == "mismatch" and "won't use them" in task["message"]
        (folder / "model.safetensors").unlink()                               # shards missing
        task = request(wire, "GET", "/downloads").json()["tasks"][0]
        assert task["state"] == "failed" and task["reason"] == "incomplete"
    finally:
        downloads.catalog.by_id = real_by_id


def test_gemma_wisp_pins_every_tested_file_but_the_card():
    pins = {name for name, _, _ in downloads.catalog.GEMMA_E4B_WISP.pinned_files}
    assert {"config.json", "chat_template.jinja", "model-00001-of-00002.safetensors",
            "model-00002-of-00002.safetensors", "model-mtp.safetensors", "tokenizer.json"} <= pins
    assert "README.md" not in pins and len(pins) == 10


def test_a_repo_without_the_weights_is_not_a_finished_download(wire):
    # A README-only repo "completes" in a second; Wisp must not treat that as installed.
    size = downloads.catalog.by_id(MODEL).size_gb * 1e9
    wire.tasks = [row(status="completed", progress=100, downloaded_size=1547, total_size=1547)]
    task = request(wire, "GET", "/downloads").json()["tasks"][0]
    assert task["state"] == "failed" and task["reason"] == "incomplete"
    assert "aren't all on Hugging Face yet" in task["message"]
    wire.tasks = [row(status="completed", progress=100, downloaded_size=int(size), total_size=int(size))]
    task = request(wire, "GET", "/downloads").json()["tasks"][0]
    assert task["state"] == "completed" and task["message"] is None



def _pinned(tmp_path, files):
    import hashlib
    from dataclasses import replace
    pins = tuple((n, hashlib.sha256(d).hexdigest(), len(d)) for n, d in files.items())
    return replace(downloads.catalog.by_id(MODEL), pinned_files=pins)


def test_a_finished_pinned_download_with_no_folder_is_not_usable(wire, tmp_path, monkeypatch):
    model = _pinned(tmp_path, {"config.json": b"{}", "model.safetensors": b"w" * 8})
    real = downloads.catalog.by_id
    monkeypatch.setattr(downloads.catalog, "by_id", lambda m: model if m == MODEL else real(m))
    wire.tasks = [row(status="completed", progress=100, downloaded_size=10**10, total_size=10**10)]
    task = request(wire, "GET", "/downloads").json()["tasks"][0]
    assert task["state"] == "failed" and task["reason"] == "incomplete"


def test_an_added_file_is_a_mismatch_but_hub_and_card_files_are_not(tmp_path):
    from service.setup.local_models import verify_download
    files = {"config.json": b"{}", "model.safetensors": b"w" * 8}
    model = _pinned(tmp_path, files)
    folder = _folder(tmp_path, model, {**files, "README.md": b"card", ".gitattributes": b"*"})
    (folder / ".cache" / "huggingface" / "download").mkdir(parents=True)
    assert verify_download(folder, model) is None
    (folder / "chat_template.json").write_text("{}")             # changes how the model is prompted
    assert verify_download(folder, model) == "mismatch"


def test_retry_after_a_mismatch_drops_only_the_bad_pinned_files_first(wire, tmp_path, monkeypatch):
    files = {"config.json": b"{}", "model.safetensors": b"w" * 8}
    model = _pinned(tmp_path, files)
    real = downloads.catalog.by_id
    monkeypatch.setattr(downloads.catalog, "by_id", lambda m: model if m == MODEL else real(m))
    folder = _folder(tmp_path, model, {"config.json": b"{}", "model.safetensors": b"x" * 8, "README.md": b"c"})
    meta = folder / ".cache" / "huggingface" / "download"
    meta.mkdir(parents=True)
    for name in ("config.json", "model.safetensors"):
        (meta / f"{name}.metadata").write_text("commit\netag\n1\n")
    wire.tasks = [row(status="completed", progress=100, downloaded_size=10**10, total_size=10**10)]
    assert request(wire, "GET", "/downloads").json()["tasks"][0]["reason"] == "mismatch"
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 200
    assert not (folder / "model.safetensors").exists() and not (meta / "model.safetensors.metadata").exists()
    assert (folder / "config.json").exists() and (meta / "config.json.metadata").exists()   # good file kept
    assert (folder / "README.md").exists()


def test_a_failed_delete_still_starts_the_fresh_download(wire):
    wire.tasks = [row(status="completed", progress=100, downloaded_size=1547, total_size=1547)]
    wire.delete_fails = True
    response = request(wire, "POST", "/downloads", json={"model": MODEL})
    assert response.status_code == 200, response.text
    paths = [(r.method, r.url.path) for r in wire.seen]
    assert ("DELETE", "/admin/api/hf/task/task-1") in paths and ("POST", "/admin/api/hf/download") in paths


def test_downloads_land_where_wisp_checks_them():
    # oMLX's HFDownloader writes to model_dir / repo_id with model_dir = dir_list[0], and
    # Wisp's engine settings put its own models folder first.
    from pathlib import Path
    from service.engine import manager
    settings = manager.engine_settings(8791, "k" * 40, [Path("/synthetic/engine/models"), Path("/x")], ["/user"])
    assert settings["model"]["model_dirs"][0] == "/synthetic/engine/models"
    layout = manager.EngineLayout(Path("/code"), Path("/synthetic/engine"))
    assert layout.models_dir == Path("/synthetic/engine/models")
    source = Path("/Applications/oMLX.app/Contents/Resources/omlx")
    if not (source / "server.py").is_file():
        pytest.skip("oMLX.app isn't installed here")
    assert "model_dir=dir_list[0]" in (source / "server.py").read_text()
    assert "target_dir = self._model_dir / task.repo_id" in (source / "admin" / "hf_downloader.py").read_text()


def _pinned_setup(wire, tmp_path, monkeypatch, disk):
    files = {"config.json": b"{}", "model.safetensors": b"w" * 8}
    model = _pinned(tmp_path, files)
    real = downloads.catalog.by_id
    monkeypatch.setattr(downloads.catalog, "by_id", lambda m: model if m == MODEL else real(m))
    folder = _folder(tmp_path, model, disk)
    wire.tasks = [row(status="completed", progress=100, downloaded_size=10**10, total_size=10**10)]
    return model, folder


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_an_extra_file_is_named_and_a_retry_removes_it(wire, tmp_path, monkeypatch):
    _, folder = _pinned_setup(wire, tmp_path, monkeypatch,
                              {"config.json": b"{}", "model.safetensors": b"w" * 8, "added_tokens.json": b"{}"})
    task = request(wire, "GET", "/downloads").json()["tasks"][0]
    assert task["reason"] == "mismatch" and "added_tokens.json" in task["message"]
    assert (folder / "added_tokens.json").exists()                 # a poll never deletes
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 200
    assert not (folder / "added_tokens.json").exists()
    assert (folder / "config.json").exists() and (folder / "model.safetensors").exists()


def test_an_extra_folder_is_named_for_removal_by_hand(wire, tmp_path, monkeypatch):
    _, folder = _pinned_setup(wire, tmp_path, monkeypatch,
                              {"config.json": b"{}", "model.safetensors": b"w" * 8, "stray.json": b"{}"})
    (folder / "onnx").mkdir()
    (folder / "onnx" / "model.onnx").write_bytes(b"1")
    task = request(wire, "GET", "/downloads").json()["tasks"][0]
    assert task["reason"] == "mismatch"
    assert "a folder the tested model didn't: onnx. Remove it in Finder" in task["message"]
    assert "files the tested model didn't: stray.json. Try again" in task["message"]
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 200
    assert (folder / "onnx" / "model.onnx").exists()        # never a recursive delete
    assert not (folder / "stray.json").exists()


def test_polls_and_the_startup_check_never_delete(wire, tmp_path, monkeypatch):
    _, folder = _pinned_setup(wire, tmp_path, monkeypatch,
                              {"config.json": b"{}", "model.safetensors": b"x" * 8, "extra.bin": b"1"})
    before = _tree(folder)
    for _ in range(3):
        assert request(wire, "GET", "/downloads").json()["tasks"][0]["reason"] == "mismatch"
    monkeypatch.setattr(downloads.catalog, "PRIMARY_MODELS", (downloads.catalog.by_id(MODEL),))
    downloads.warm_checks()
    assert _tree(folder) == before


def test_folders_outside_wisps_download_are_never_touched(wire, tmp_path, monkeypatch):
    # Base Gemma, the pre-release folder and a hand-placed copy have no task and no pins here.
    models = tmp_path / "engine" / "models"
    keep = {models / "Jundot" / downloads.catalog.GEMMA_E4B_MTP.id: b"x",
            models / "local" / downloads.catalog.GEMMA_E4B_WISP_PRERELEASE.id: b"x",
            tmp_path / "elsewhere" / "Gemma-Wisp": b"x"}
    for folder, data in keep.items():
        folder.mkdir(parents=True)
        (folder / "model.safetensors").write_bytes(data)
        (folder / "stray.txt").write_bytes(data)
    _pinned_setup(wire, tmp_path, monkeypatch, {"config.json": b"{}", "model.safetensors": b"x" * 8})
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 200
    for folder in keep:
        assert (folder / "model.safetensors").exists() and (folder / "stray.txt").exists()
    for kept in (downloads.catalog.GEMMA_E4B_MTP.id, downloads.catalog.GEMMA_E4B_WISP_PRERELEASE.id):
        assert downloads._deletion_plan(kept) == []


def _victim(tmp_path):
    other = tmp_path / "other-model"
    other.mkdir()
    for name in ("config.json", "model.safetensors", "notes.txt"):
        (other / name).write_bytes(b"precious")
    return other


def test_a_symlinked_catalogue_folder_is_never_deleted_from(wire, tmp_path, monkeypatch):
    files = {"config.json": b"{}", "model.safetensors": b"w" * 8}
    model = _pinned(tmp_path, files)
    real = downloads.catalog.by_id
    monkeypatch.setattr(downloads.catalog, "by_id", lambda m: model if m == MODEL else real(m))
    other = _victim(tmp_path)
    owner = tmp_path / "engine" / "models" / model.repo.split("/")[0]
    owner.mkdir(parents=True)
    (owner / model.repo.split("/")[1]).symlink_to(other)          # the model folder itself is a link
    assert downloads._deletion_plan(MODEL) == []
    (owner / model.repo.split("/")[1]).unlink()
    owner.rmdir()
    owner.symlink_to(tmp_path)                                      # an ancestor is a link
    (tmp_path / model.repo.split("/")[1]).symlink_to(other) if not (tmp_path / model.repo.split("/")[1]).exists() else None
    assert downloads._deletion_plan(MODEL) == []
    assert sorted(p.name for p in other.iterdir()) == ["config.json", "model.safetensors", "notes.txt"]


def test_a_symlinked_pinned_file_or_metadata_folder_is_never_followed(wire, tmp_path, monkeypatch):
    other = _victim(tmp_path)
    _, folder = _pinned_setup(wire, tmp_path, monkeypatch, {"config.json": b"{}"})
    (folder / "model.safetensors").symlink_to(other / "model.safetensors")   # a pinned name as a link
    (folder / ".cache").symlink_to(other)                                    # hub's metadata folder as a link
    for path in downloads._deletion_plan(MODEL):
        assert not path.is_symlink() and path.resolve().parent == folder.resolve()
    assert request(wire, "POST", "/downloads", json={"model": MODEL}).status_code == 200
    assert sorted(p.name for p in other.iterdir()) == ["config.json", "model.safetensors", "notes.txt"]
    assert (other / "model.safetensors").read_bytes() == b"precious"


def test_a_still_active_task_is_never_downloaded_twice(wire, tmp_path, monkeypatch):
    # oMLX answers 404 for "still active" too; Wisp looks again instead of posting a second download.
    _pinned_setup(wire, tmp_path, monkeypatch, {"config.json": b"{}", "model.safetensors": b"x" * 8})
    wire.delete_fails = wire.active_after_delete = True
    response = request(wire, "POST", "/downloads", json={"model": MODEL})
    assert response.status_code == 200 and response.json()["tasks"][0]["state"] == "downloading"
    assert not any(r.url.path == "/admin/api/hf/download" for r in wire.seen)

"""Synthetic cached-client, stream, lock, restart and redaction adversaries."""
import asyncio
import fcntl
import json
import os
from pathlib import Path

import httpx
import pytest

from service.config import credentials, quarantine as q


def marker():
    parent = q._gate.directory
    parent.mkdir(mode=0o700, exist_ok=True)
    return parent / ".helper-transaction.json"


@pytest.mark.asyncio
async def test_cached_client_cannot_resume_after_marker_removal(tmp_path):
    secret = "a" * 64
    credentials._VALUES["WISP_LOCAL_OMLX_KEY"] = secret
    seen = []
    async def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"ok": True})
    client = q.guard_client(httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer " + secret}))
    await client.get("https://synthetic.test")
    marker().write_text("untrusted content " + secret)
    with pytest.raises(q.CredentialQuarantined) as failure:
        await client.get("https://synthetic.test")
    assert secret not in str(failure.value)
    assert "authorization" not in client.headers and not credentials._VALUES
    marker().unlink()
    with pytest.raises(q.CredentialQuarantined):
        await client.get("https://synthetic.test")
    assert len(seen) == 1
    fresh = q.RecoveryGate(home=lambda: tmp_path)
    fresh.check()  # A fresh process may load new native credentials after recovery.
    await client.aclose()


@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "generation", "unsafe-generation", "probe-error"])
def test_every_ambiguous_recovery_state_blocks_and_redacts(kind, monkeypatch):
    gate = q._gate
    gate.check()
    path = marker()
    if kind == "file": path.write_text("synthetic-secret")
    elif kind == "directory": path.mkdir()
    elif kind == "dangling": path.symlink_to("missing")
    elif kind in ("generation", "unsafe-generation"):
        epoch = path.parent / ".credential-generation"
        epoch.write_text("b" * 64 if kind == "generation" else "synthetic-secret")
        epoch.chmod(0o600)
    else:
        def denied(): raise PermissionError("synthetic-secret")
        monkeypatch.setattr(gate, "generation", denied)
    with pytest.raises(q.CredentialQuarantined) as failure:
        gate.check()
    assert "synthetic-secret" not in str(failure.value)
    assert gate.blocked


@pytest.mark.asyncio
async def test_stream_lease_excludes_writer_until_close_and_cancel():
    class Body(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b"first"
            await asyncio.Event().wait()
        async def aclose(self): self.closed = True
    body = Body()
    client = q.guard_client(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))))
    async with client.stream("GET", "https://synthetic.test") as response:
        assert await anext(response.aiter_bytes()) == b"first"
        fd = os.open(q._gate.directory / ".provisioning.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)
    assert body.closed
    with open(q._gate.directory / ".provisioning.lock", "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not q._gate.active
    await client.aclose()


@pytest.mark.asyncio
async def test_out_of_band_marker_suppresses_stream_completion():
    closed = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            marker().write_text("synthetic")
            yield b"must-not-escape"
        async def aclose(self): closed.append(True)
    client = q.guard_client(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))))
    chunks = []
    with pytest.raises(q.CredentialQuarantined):
        async with client.stream("GET", "https://synthetic.test") as response:
            async for chunk in response.aiter_bytes(): chunks.append(chunk)
    assert chunks == [b"first"] and closed and not q._gate.active
    await client.aclose()


@pytest.mark.asyncio
async def test_watcher_cancels_existing_request_and_clears_bridge():
    entered = asyncio.Event()
    async def handler(request):
        entered.set()
        await asyncio.Event().wait()
    credentials._VALUES["WISP_LOCAL_OMLX_KEY"] = "c" * 64
    client = q.guard_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    pending = asyncio.create_task(client.get("https://synthetic.test"))
    await entered.wait()
    marker().write_text("synthetic")
    await q.watch(close=client.aclose, interval=0)
    assert pending.done() and not credentials._VALUES and q._gate.blocked
    assert not q._gate.active


def test_quarantine_never_falls_back_to_local_settings(monkeypatch):
    import service.config as config
    monkeypatch.setattr(config, "omlx_settings", lambda: pytest.fail("quarantine bypassed with local settings"))
    marker().write_text("synthetic")
    with pytest.raises(q.CredentialQuarantined): config.omlx_api_key()


@pytest.mark.asyncio
async def test_quarantine_does_not_open_readiness_fallback():
    from service.inference.readiness import TurnInferenceClient
    class Client:
        managed = True
        async def ensure_only(self, *args, **kwargs): raise q.CredentialQuarantined()
    async def no_op(): pass
    async def forbidden(): pytest.fail("quarantine triggered fallback")
    client = TurnInferenceClient(Client(), no_op, fallback_start=forbidden)
    with pytest.raises(q.CredentialQuarantined): await client._ensure_generation("synthetic")


@pytest.mark.asyncio
async def test_server_health_is_unavailable_after_recovery_marker():
    calls = []
    async def app(scope, receive, send): calls.append(True)
    middleware = q.RecoveryMiddleware(app)
    marker().symlink_to("missing")
    output = []
    async def send(message): output.append(message)
    await middleware({"type": "http"}, None, send)
    assert not calls and output[0]["status"] == 503
    assert json.loads(output[1]["body"]) == {"error": "credential_recovery_required"}


def test_exclusive_writer_blocks_cached_dispatch_without_waiting():
    q.check()
    path = marker().parent / ".provisioning.lock"
    path.touch(mode=0o600)
    with open(path, "r+") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX)
        with pytest.raises(q.CredentialQuarantined):
            with q.lease(): pytest.fail("entered writer transaction")
    assert not q._gate.blocked
    with q.lease(): pass


def test_production_generation_rotation_invalidates_old_process(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/mac-mini"))
    import node_prep
    q.check()
    marker().parent.mkdir(mode=0o700, exist_ok=True)
    with node_prep.provisioning_lock(q._gate.directory):
        node_prep.rotate_credential_generation(q._gate.directory)
    epoch = (q._gate.directory / ".credential-generation").read_text()
    assert len(epoch) == 64
    with pytest.raises(q.CredentialQuarantined): q.check()
    fresh = q.RecoveryGate(home=lambda: tmp_path, expected=epoch)
    with fresh.lease(): pass


def test_fifo_generation_is_rejected_without_blocking():
    os.mkfifo(marker().parent / ".credential-generation", 0o600)
    with pytest.raises(q.CredentialQuarantined): q.check()


@pytest.mark.asyncio
async def test_embedding_batches_queued_before_marker_cannot_dispatch(monkeypatch):
    from service.search import embedder
    # Exercise the real batch fan-out with one permit and a synthetic transport.
    monkeypatch.setattr(embedder, "_BATCH", 1)
    monkeypatch.setattr(embedder, "_MAX_CONCURRENT_BATCHES", 1)
    class SyntheticTarget:
        model = "fixture-model"
        class endpoint:
            base_url = "http://127.0.0.1:8000"
            @staticmethod
            def api_key(): return "fixture-key"
    calls = []
    async def handler(request):
        calls.append(True)
        marker().write_text("synthetic")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(embedder.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(q.CredentialQuarantined):
        await embedder._embed(["one", "two", "three"], timeout=1, target=SyntheticTarget())
    assert len(calls) == 1

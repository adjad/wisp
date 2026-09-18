"""Synthetic endpoint, stream, lifecycle and rollback contracts. No live server."""
import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from service import config
from service.config.endpoints import Endpoint, Target, endpoint, role_target, EndpointConfigurationError
from service.inference.omlx_client import OMLXClient, IncompleteStreamError, ModelLoadError
from service.inference.heartbeat import with_heartbeats


@pytest.fixture
def configured(monkeypatch):
    data = {"omlx": {"base_url": "http://127.0.0.1:8000"},
            "roles": {"fast": "shared", "coding": "shared", "agent": "shared"}, "default_role": "fast",
            "inference": {"endpoints": {"mini": {"base_url": "https://mini.test", "credential_ref": "env:MINI_KEY"}},
                          "bindings": {"coding": {"endpoint": "mini", "model_id": "shared", "context_window": 4096}}}}
    monkeypatch.setattr(config, "models_config", lambda: data)
    monkeypatch.setattr(config, "omlx_api_key", lambda: "local-secret")
    monkeypatch.setenv("MINI_KEY", "mini-secret")
    return data


def test_shared_model_does_not_share_endpoint_or_key(configured):
    local, remote = role_target("fast"), role_target("coding")
    assert local.model == remote.model
    assert local.endpoint.api_key() == "local-secret"
    assert remote.endpoint.api_key() == "mini-secret"
    assert local.identity != remote.identity
    assert remote.context_window == 4096


def test_missing_remote_credential_fails_closed(configured, monkeypatch):
    monkeypatch.delenv("MINI_KEY")
    with pytest.raises(EndpointConfigurationError):
        OMLXClient(target=role_target("coding"))
    with pytest.raises(EndpointConfigurationError):
        OMLXClient(base_url="https://mini.test")


@pytest.mark.parametrize("url", ["https://user:secret@mini.test", "http://mini.test", "https://mini.test/?key=x"])
def test_remote_url_rejects_unsafe_forms(configured, url):
    configured["inference"]["endpoints"]["mini"]["base_url"] = url
    with pytest.raises(EndpointConfigurationError):
        role_target("coding")


def test_global_remote_switch_is_rejected(configured):
    configured["omlx"]["base_url"] = "https://mini.test"
    with pytest.raises(EndpointConfigurationError):
        endpoint()


def test_atomic_overlay_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("roles:\n  fast: original\n")
    monkeypatch.setattr(config, "USER_CONFIG", path)
    import os
    def fail(*args):
        raise OSError("injected replace failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        config._save_overlay({"roles": {"fast": "new"}})
    assert "original" in path.read_text()
    assert not list(tmp_path.glob(".config-*"))


def test_role_rollback_clears_remote_metadata(monkeypatch):
    saved = []
    monkeypatch.setattr(config, "_save_overlay", saved.append)
    config.set_role("general", "local-model")
    for role in ["general", "agent"]:
        assert saved[0]["inference"]["bindings"][role]["endpoint"] == "local"
        assert saved[0]["inference"]["bindings"][role]["revision"] == ""


async def mocked_client(handler, *, target=None):
    c = OMLXClient(base_url="http://127.0.0.1:8000", api_key="fixture", target=target)
    await c._client.aclose()
    c._client = httpx.AsyncClient(base_url=c.base_url, transport=httpx.MockTransport(handler))
    return c


@pytest.mark.parametrize("status", [401, 403, 507])
def test_load_unload_errors_raise(status):
    async def run():
        c = await mocked_client(lambda req: httpx.Response(status))
        try:
            for method in (c.load, c.unload):
                with pytest.raises(httpx.HTTPStatusError):
                    await method("model")
        finally:
            await c.aclose()
    asyncio.run(run())


def test_remote_readiness_never_mutates_residency(configured):
    async def run():
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json={"status": "ok"} if request.url.path == "/health" else {"data": [{"id": "shared"}]})
        c = await mocked_client(handler, target=role_target("coding"))
        try:
            await c.ensure_only("shared", exclusive=True)
            assert all(method == "GET" for method, _ in calls)
        finally:
            await c.aclose()
    asyncio.run(run())


def test_readiness_includes_slow_http_in_deadline(configured):
    async def run():
        async def handler(request):
            await asyncio.sleep(10)
        target = role_target("coding")
        target = replace(target, endpoint=replace(target.endpoint, readiness_timeout=.02))
        c = await mocked_client(handler, target=target)
        try:
            with pytest.raises(ModelLoadError):
                await asyncio.wait_for(c.ensure_only("shared"), .2)
        finally:
            await c.aclose()
    asyncio.run(run())


def sse(delta=None, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta or {}, "finish_reason": finish}]}) + "\n\n"


@pytest.mark.parametrize("body", [sse({"content": "partial"}), sse({"tool_calls": [
    {"index": 0, "id": "call1", "function": {"name": "send", "arguments": '{"to":'}}]})])
def test_clean_eof_is_not_a_final_result(body):
    async def run():
        c = await mocked_client(lambda req: httpx.Response(200, text=body))
        events = []
        try:
            with pytest.raises(IncompleteStreamError):
                async for event in c.stream_events("model", []):
                    events.append(event)
            assert not any(e["kind"] == "final" for e in events)
        finally:
            await c.aclose()
    asyncio.run(run())


def test_usage_chunk_and_valid_terminal():
    async def run():
        body = sse({"content": "ok"}, "stop") + 'data: {"choices": [], "usage": {}}\n\ndata: [DONE]\n\n'
        c = await mocked_client(lambda req: httpx.Response(200, text=body))
        try:
            events = [e async for e in c.stream_events("model", [])]
            assert events[-1]["message"]["content"] == "ok"
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("finish,arguments", [("length", "{}"), ("tool_calls", '{"x":')])
def test_incomplete_tool_result_is_rejected(finish, arguments):
    async def run():
        body = sse({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "send", "arguments": arguments}}]}, finish)
        c = await mocked_client(lambda req: httpx.Response(200, text=body))
        try:
            with pytest.raises(IncompleteStreamError):
                _ = [e async for e in c.stream_events("model", [])]
        finally:
            await c.aclose()
    asyncio.run(run())


def test_heartbeat_retains_and_closes_silent_producer():
    async def run():
        closed, heartbeats = [], []
        async def producer():
            try:
                await asyncio.sleep(.04)
                yield "first"
                await asyncio.sleep(.04)
                yield "second"
            finally:
                closed.append(True)
        async def emit(ev):
            heartbeats.append(ev)
        assert [e async for e in with_heartbeats(producer(), emit, interval=.01)] == ["first", "second"]
        assert heartbeats and closed == [True]
    asyncio.run(run())


def test_remote_lifecycle_never_restarts_local(monkeypatch):
    from service import main
    async def run():
        fake = type("Remote", (), {"managed": False})()
        cli = AsyncMock()
        monkeypatch.setattr(main, "client", fake, raising=False)
        monkeypatch.setattr(main, "_omlx_cli", cli)
        with pytest.raises(ModelLoadError):
            await main.ensure_omlx()
        assert await main.shutdown_omlx() == {"stopped": False}
        cli.assert_not_called()
    asyncio.run(run())


def test_remote_context_preserves_runtime_system(configured):
    async def run():
        c = OMLXClient(target=role_target("coding"))
        try:
            msgs = [{"role": "system", "content": "policy"}, {"role": "system", "content": "runtime"},
                    {"role": "user", "content": "old" * 5000}, {"role": "assistant", "content": "old"},
                    {"role": "user", "content": "question"}]
            _, fitted, _, _ = c._fit_request("shared", msgs, None, 1000)
            assert fitted[:2] == msgs[:2]
        finally:
            await c.aclose()
    asyncio.run(run())


def test_optional_keep_warm_failure_does_not_fail_target():
    async def run():
        c = await mocked_client(lambda req: httpx.Response(507))
        c.loaded_models = AsyncMock(return_value=["target"])
        c.set_keep_warm({"optional"})
        try:
            await c.ensure_only("target")
        finally:
            await c.aclose()
    asyncio.run(run())


def test_heartbeat_cancellation_reaps_producer():
    async def run():
        started, closed = asyncio.Event(), asyncio.Event()
        async def producer():
            try:
                started.set()
                await asyncio.sleep(60)
                yield "never"
            finally:
                closed.set()
        async def consume():
            async for _ in with_heartbeats(producer(), AsyncMock(), interval=.01):
                pass
        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    asyncio.run(run())


def test_embedding_identity_invalidates_cached_document(configured, monkeypatch):
    from service.search import embedder
    from service.search.chunker import chunk
    from collections import OrderedDict
    async def run():
        monkeypatch.setattr(embedder, "_cache", OrderedDict())
        monkeypatch.setattr(embedder, "_inflight", {})
        monkeypatch.setattr(embedder, "_cache_lock", asyncio.Lock())
        target = Target("embedding", endpoint(), "embedding", revision="a", dimensions=2)
        mocked = AsyncMock(return_value=[[1., 0.]])
        monkeypatch.setattr(embedder, "_embed", mocked)
        for rev in ["a", "a", "b"]:
            await embedder.index_document("doc", chunk("Synthetic text."), target=replace(target, revision=rev))
        assert mocked.call_count == 2
    asyncio.run(run())


def test_optional_retrieval_failure_uses_lexical(monkeypatch):
    import service.router.router as router
    from service.router import reranker
    monkeypatch.setattr(router, "models_config", lambda: {"tool_retrieval": {"provider": "reranker"}})
    monkeypatch.setattr(reranker, "candidates", AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(reranker, "lexical_candidates", lambda *a, **kw: ["get_weather"])
    # The retrieval helper preserves lexical candidates through its tool gates.
    assert "get_weather" in asyncio.run(router._semantic_core("weather"))


def test_readiness_fallback_is_pre_generation_only(configured, monkeypatch):
    from service.inference import readiness
    async def run():
        readiness._CIRCUITS.clear()
        target = role_target("coding")
        remote = type("Remote", (), {"managed": False, "target": target, "base_url": "https://mini.test",
                                      "ensure_only": AsyncMock(), "chat": AsyncMock()})()
        local = type("Local", (), {"ensure_only": AsyncMock(), "chat": AsyncMock(return_value={"ok": True}),
                                   "aclose": AsyncMock()})()
        monkeypatch.setattr(readiness, "OMLXClient", lambda **kw: local)
        start = AsyncMock(side_effect=httpx.ConnectError("offline"))
        fallback = AsyncMock()
        turn = readiness.TurnInferenceClient(remote, start, fallback_start=fallback, emit=AsyncMock())
        assert await turn.chat("shared", [{"role": "user", "content": "hello"}]) == {"ok": True}
        fallback.assert_awaited_once()
        remote.chat.assert_not_called()
        await turn.close_fallback()
        local.aclose.assert_awaited_once()
        # The same failed readiness with tool schemas must never choose fallback.
        turn = readiness.TurnInferenceClient(remote, start, fallback_start=fallback)
        with pytest.raises(httpx.ConnectError):
            await turn.chat("shared", [], tools=[{"type": "function"}])
        assert fallback.await_count == 1
        # Once generation starts, its failure propagates without trying local.
        readiness._CIRCUITS.clear()
        remote.chat = AsyncMock(side_effect=httpx.ReadError("interrupted after request"))
        turn = readiness.TurnInferenceClient(remote, AsyncMock(), fallback_start=fallback)
        with pytest.raises(httpx.ReadError):
            await turn.chat("shared", [])
        assert fallback.await_count == 1
    asyncio.run(run())


def test_same_id_remote_reranking_never_evicts_local_embedder(configured, monkeypatch):
    from service.router import reranker
    async def run():
        configured["roles"]["reranker"] = "shared"
        configured["roles"]["embedding"] = "embed"
        configured["inference"]["bindings"]["reranker"] = {"endpoint": "mini", "model_id": "shared"}
        requests = []
        original = httpx.AsyncClient
        def handler(request):
            requests.append(request)
            assert request.headers["Authorization"] == "Bearer mini-secret"
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.0}]})
        monkeypatch.setattr(reranker.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
        monkeypatch.setattr(reranker, "lexical_shortlist", lambda *a, **kw: ["get_weather"])
        await reranker.candidates("get weather", writing=False)
        assert requests and all(r.url.path == "/v1/rerank" for r in requests)
    asyncio.run(run())


def test_plain_stream_done_requires_known_terminal_reason():
    from service.inference.omlx_client import IncompleteStreamError
    async def run():
        for reason in (None, 'unknown', 'tool_calls'):
            def handler(request):
                lines = [{'choices':[{'delta':{'content':'partial'},'finish_reason':reason}]}]
                import json
                return httpx.Response(200, content=''.join('data: '+json.dumps(line)+'\n\n' for line in lines)+'data: [DONE]\n\n')
            client = await mocked_client(handler)
            try:
                with pytest.raises(IncompleteStreamError):
                    _ = [event async for event in client.stream_events('fixture', [{'role':'user','content':'fixture'}])]
            finally:
                await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("path,body", [
    ("/health", b"invalid JSON"), ("/health", b"[]"), ("/health", b"null"),
    ("/health", b'{"status": []}'), ("/health", b'{"error":"private"}'),
    ("/v1/models", b"invalid JSON"), ("/v1/models", b"[]"),
    ("/v1/models", b'{"data":{}}'), ("/v1/models", b'{"data":[null]}'),
    ("/v1/models", b'{"data":[{}]}'), ("/v1/models", b'{"data":[{"id":1}]}'),
    ("/v1/models", b'{"data":[{"id":""}]}'), ("/v1/models", b'{"data":[{"id":" shared"}]}'),
    ("/v1/models", b'{"data":[{"id":"shared"},{"id":"shared"}]}')])
def test_http_200_readiness_shape_failure_uses_fallback(configured, monkeypatch, path, body):
    from service.inference import readiness
    async def run():
        readiness._CIRCUITS.clear()
        def handler(request):
            if request.url.path == path:
                return httpx.Response(200, content=body)
            return httpx.Response(200, json={"status": "ok"} if request.url.path == "/health" else {"data": [{"id": "shared"}]})
        remote = await mocked_client(handler, target=role_target("coding"))
        local = type("Local", (), {"ensure_only": AsyncMock(), "chat": AsyncMock(return_value={"fallback": True}), "aclose": AsyncMock()})()
        monkeypatch.setattr(readiness, "OMLXClient", lambda **kw: local)
        try:
            with pytest.raises(ModelLoadError):
                await remote.ensure_only("shared")
            turn = readiness.TurnInferenceClient(remote, AsyncMock(), fallback_start=AsyncMock())
            assert await turn.chat("shared", []) == {"fallback": True}
            tools_turn = readiness.TurnInferenceClient(remote, AsyncMock(), fallback_start=AsyncMock())
            with pytest.raises(ModelLoadError):
                await tools_turn.chat("shared", [], tools=[{"type": "function"}])
            assert local.chat.await_count == 1
        finally:
            await remote.aclose()
            readiness._CIRCUITS.clear()
    asyncio.run(run())


@pytest.mark.parametrize("changed", ["same", "model", "role", "revision", "profile", "context_window", "capabilities", "dimensions", "credential_ref", "endpoint_name"])
def test_readiness_circuit_uses_complete_target(configured, monkeypatch, changed):
    from service.inference import readiness
    async def run():
        readiness._CIRCUITS.clear()
        first = role_target("coding")
        local = type("Local", (), {"ensure_only": AsyncMock(), "chat": AsyncMock(return_value={"fallback": True})})()
        monkeypatch.setattr(readiness, "OMLXClient", lambda **kw: local)
        def remote(target, failure=False):
            return type("Remote", (), {"target": target, "managed": False, "base_url": first.endpoint.base_url,
                "ensure_only": AsyncMock(side_effect=ModelLoadError("fixture") if failure else None),
                "chat": AsyncMock(return_value={"remote": True})})()
        await readiness.TurnInferenceClient(remote(first, True), AsyncMock(), fallback_start=AsyncMock()).chat(first.model, [])
        values = {"model": "other", "role": "other", "revision": "r2", "profile": "p2", "context_window": 8192,
                  "capabilities": ("chat",), "dimensions": 512}
        if changed in values:
            second = replace(first, **{changed: values[changed]})
        elif changed == "credential_ref":
            second = replace(first, endpoint=replace(first.endpoint, credential_ref="env:OTHER_KEY"))
        elif changed == "endpoint_name":
            second = replace(first, endpoint=replace(first.endpoint, name="other"))
        else:
            second = replace(first)
        healthy = remote(second)
        result = await readiness.TurnInferenceClient(healthy, AsyncMock(), fallback_start=AsyncMock()).chat(second.model, [])
        assert result == ({"fallback": True} if changed == "same" else {"remote": True})
        assert healthy.ensure_only.await_count == (0 if changed == "same" else 1)
        readiness._CIRCUITS.clear()
    asyncio.run(run())


@pytest.mark.parametrize("method,maximum", [("health", 64 * 1024), ("models", 1024 * 1024), ("status", 1024 * 1024)])
@pytest.mark.parametrize("failure", ["length", "stream", "compressed", "slow"])
def test_remote_readiness_bounded_before_parse(configured, failure, method, maximum):
    async def run():
        class Body(httpx.AsyncByteStream):
            closed = False
            yielded = 0
            async def __aiter__(self):
                if failure == "slow":
                    await asyncio.sleep(10)
                for _ in range(1000):
                    self.yielded += 1
                    yield b"x" * 8192
            async def aclose(self):
                self.closed = True
        body = Body()
        headers = {"length": {"Content-Length": "999999999"}, "compressed": {"Content-Encoding": "gzip"}}.get(failure, {})
        def handler(request):
            assert request.headers["Accept-Encoding"] == "identity"
            return httpx.Response(200, stream=body, headers=headers)
        target = role_target("coding")
        if failure == "slow":
            target = replace(target, endpoint=replace(target.endpoint, readiness_timeout=.02))
        client = await mocked_client(handler, target=target)
        try:
            with pytest.raises(ModelLoadError):
                await getattr(client, method)()
            assert body.closed and body.yielded <= maximum // 8192 + 1
            if failure in ("length", "compressed"):
                assert body.yielded == 0
        finally:
            await client.aclose()
    asyncio.run(run())

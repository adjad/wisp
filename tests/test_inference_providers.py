"""Provider contracts use synthetic responses, never live HTTP or Keychain."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from service import config
from service.config.endpoints import endpoint, role_target, EndpointConfigurationError
from service.config import provider_credentials as secrets
from service.inference.omlx_client import OMLXClient, IncompleteStreamError


@pytest.fixture
def configured(monkeypatch):
    data = {"roles": {"coding": "local-model", "fast": "local-model"},
            "inference": {"endpoints": {"cloud": {
                "provider": "openrouter", "base_url": "https://provider.test",
                "credential_ref": "keychain:cloud"}},
                "bindings": {"coding": {"endpoint": "cloud", "model_id": "vendor/model",
                    "context_window": 8192, "qualified_capabilities": ["tools"]}}}}
    monkeypatch.setattr(config, "models_config", lambda: data)
    monkeypatch.setattr(secrets, "resolve_keychain", lambda *args: chr(120) * 32)
    return data


async def client(handler, target):
    instance = OMLXClient(target=target)
    await instance._client.aclose()
    instance._client = httpx.AsyncClient(base_url=instance.base_url,
        transport=httpx.MockTransport(handler), follow_redirects=False)
    return instance


@pytest.mark.parametrize("provider,prefix", [
    ("openrouter", "/api/v1"), ("openai-compatible", "/v1"),
    ("openai-compatible", "/gateway/v2")])
def test_provider_requests_and_readiness(configured, provider, prefix):
    configured["inference"]["endpoints"]["cloud"].update(provider=provider, api_prefix=prefix)
    async def run():
        seen = []
        def handler(request):
            seen.append(request.url.path)
            if request.method == "GET":
                assert request.url.path == prefix + "/models"
                return httpx.Response(200, json={"data": [{"id": "vendor/model"}]})
            payload = json.loads(request.content)
            assert payload["model"] == "vendor/model"
            assert request.url.path == prefix + "/chat/completions"
            if payload["stream"]:
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        c = await client(handler, role_target("coding"))
        try:
            await c.ensure_only("vendor/model")
            assert seen == [prefix + "/models"]
            assert (await c.chat("vendor/model", []))["choices"][0]["message"]["content"] == "ok"
            assert [e async for e in c.stream_events("vendor/model", [])][-1]["message"]["content"] == "ok"
            for operation in (c.load, c.unload):
                with pytest.raises(EndpointConfigurationError):
                    await operation("vendor/model")
            with pytest.raises(EndpointConfigurationError):
                await c.status()
            assert len(seen) == 3
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("patch", [
    {"provider": "unknown"}, {"api_prefix": "//elsewhere"},
    {"api_prefix": "/v1/../admin"}, {"api_prefix": "/v1?key=value"},
    {"api_prefix": "/%2e%2e"}, {"api_prefix": 1},
    {"base_url": "http://provider.test"}, {"base_url": "https://provider.test/api/v1"},
    {"base_url": "https://user:password@provider.test"},
    {"api_key": ""}, {"headers": {}},
    {"provider": "omlx", "api_prefix": "/api/v1"}])
def test_invalid_provider_config_fails_closed(configured, patch):
    configured["inference"]["endpoints"]["cloud"].update(patch)
    with pytest.raises(EndpointConfigurationError):
        endpoint("cloud")


@pytest.mark.parametrize("role", ["fast", "router", "embedding", "reranker"])
def test_restricted_roles_stay_unchanged(configured, role):
    configured["inference"]["bindings"][role] = {"endpoint": "cloud"}
    with pytest.raises(EndpointConfigurationError):
        role_target(role)


def test_provider_identity(configured):
    original = role_target("coding")
    for ep in (replace(original.endpoint, provider="openai-compatible"),
               replace(original.endpoint, api_prefix="/custom/v1")):
        assert replace(original, endpoint=ep).identity != original.identity


def test_unqualified_tools_never_send(configured):
    async def run():
        handler = AsyncMock()
        c = await client(handler, replace(role_target("coding"), capabilities=()))
        try:
            with pytest.raises(EndpointConfigurationError):
                await c.chat("vendor/model", [], tools=[{"type": "function"}])
            handler.assert_not_called()
        finally:
            await c.aclose()
    asyncio.run(run())


def test_fragmented_cloud_tool_stream(configured):
    async def run():
        chunks = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1",
            "function": {"name": "lookup", "arguments": '{"q":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": '"synthetic"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {}}]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        c = await client(lambda r: httpx.Response(200, text=body), role_target("coding"))
        try:
            events = [e async for e in c.stream_events("vendor/model", [], tools=[{"type": "function", "function": {"name": "lookup"}}])]
            call = events[-1]["message"]["tool_calls"][0]
            assert call["id"] == "call1" and json.loads(call["function"]["arguments"]) == {"q": "synthetic"}
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("body", ['data: {"error":{"message":"private response"}}\n\n',
    'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'])
def test_cloud_stream_error_has_no_final_result(configured, body):
    async def run():
        c = await client(lambda r: httpx.Response(200, text=body), role_target("coding"))
        try:
            with pytest.raises(IncompleteStreamError) as error:
                _ = [e async for e in c.stream_events("vendor/model", [])]
            assert "private response" not in str(error.value)
        finally:
            await c.aclose()
    asyncio.run(run())


def test_cancel_cloud_stream_closes_response(configured):
    async def run():
        started, closed = asyncio.Event(), asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.sleep(60)
                yield b""
            async def aclose(self):
                closed.set()
        c = await client(lambda r: httpx.Response(200, stream=Body()), role_target("coding"))
        async def consume():
            return [e async for e in c.stream_events("vendor/model", [])]
        try:
            task = asyncio.create_task(consume())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
        finally:
            await c.aclose()
    asyncio.run(run())


def test_keychain_read_is_origin_bound_and_secret_free(monkeypatch):
    value = bytes([120]) * 32
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=value + b"\n", stderr=b"")
    monkeypatch.setattr(secrets.subprocess, "run", run)
    assert secrets.resolve_keychain("cloud", "https://provider.test") == value.decode()
    assert value.decode() not in repr(calls)
    assert secrets.keychain_account("cloud", "https://provider.test") != secrets.keychain_account("cloud", "https://other.test")


@pytest.mark.parametrize("output,code", [(b"", 0), (b"bad\nheader", 0), (b"x", 1)])
def test_keychain_errors_are_redacted(monkeypatch, output, code):
    monkeypatch.setattr(secrets.subprocess, "run", lambda *a, **kw:
        SimpleNamespace(returncode=code, stdout=output, stderr=b""))
    with pytest.raises(EndpointConfigurationError, match="^Provider credential unavailable$"):
        secrets.resolve_keychain("cloud", "https://provider.test")


def test_missing_keychain_prevents_network(configured, monkeypatch):
    def fail(*args):
        raise EndpointConfigurationError("Provider credential unavailable")
    monkeypatch.setattr(secrets, "resolve_keychain", fail)
    async def run():
        c = OMLXClient(target=role_target("coding"))
        send = AsyncMock()
        monkeypatch.setattr(c._credential_transport.pool, "handle_async_request", send)
        try:
            with pytest.raises(EndpointConfigurationError):
                await c.models()
            send.assert_not_called()
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reasoning", [None, "synthetic reasoning"])
def test_cloud_length_preserves_answer_and_reasoning(configured, stream, reasoning):
    async def run():
        message = {"content": "partial answer", "reasoning": reasoning}
        def handler(request):
            choice = {"delta" if stream else "message": message, "finish_reason": "length"}
            if stream:
                return httpx.Response(200, text="data: " + json.dumps({"choices": [choice]}) + "\n\n")
            return httpx.Response(200, json={"choices": [choice]})
        c = await client(handler, role_target("coding"))
        try:
            result = ([e async for e in c.stream_events("vendor/model", [])][-1]["message"]
                      if stream else (await c.chat("vendor/model", []))["choices"][0]["message"])
            assert result["content"] == "partial answer"
            assert (result.get("reasoning_content") or None) == reasoning
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_reasoning_tool_replay_fails_closed(configured, stream):
    async def run():
        message = {"reasoning_details": [{"type": "reasoning.encrypted", "data": "synthetic"}],
            "tool_calls": [{"index": 0, "id": "c1", "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"}}]}
        def handler(request):
            choice = {"delta" if stream else "message": message, "finish_reason": "tool_calls"}
            if stream:
                return httpx.Response(200, text="data: " + json.dumps({"choices": [choice]}) + "\n\n")
            return httpx.Response(200, json={"choices": [choice]})
        c = await client(handler, role_target("coding"))
        try:
            with pytest.raises(IncompleteStreamError, match="replay is not supported"):
                if stream:
                    _ = [e async for e in c.stream_events("vendor/model", [])]
                else:
                    await c.chat("vendor/model", [])
        finally:
            await c.aclose()
    asyncio.run(run())


def test_cloud_inventory_is_bounded(configured):
    async def run():
        c = await client(lambda r: httpx.Response(200,
            headers={"content-length": str(8 * 1024 * 1024 + 1)}), role_target("coding"))
        try:
            from service.inference.omlx_client import ModelLoadError
            with pytest.raises(ModelLoadError):
                await c.models()
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("status", [401, 403, 307])
def test_keychain_transport_attribution_and_redaction(configured, monkeypatch, status):
    import httpcore
    async def run():
        c = OMLXClient(target=role_target("coding"))
        captured = []
        async def send(request):
            captured.append(request)
            async def body():
                yield b"private error"
            return httpcore.Response(status, headers={"location": "https://other.test/private-data"}, content=body())
        monkeypatch.setattr(c._credential_transport.pool, "handle_async_request", send)
        try:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await c.models()
            value = chr(120) * 32
            assert (b"Authorization", ("Bearer " + value).encode()) in captured[0].headers
            assert value not in str(error.value)
            assert "authorization" not in error.value.request.headers
            assert "private error" not in str(error.value)
            assert "private-data" not in str(error.value)
            with pytest.raises(Exception, match="attribution unavailable"):
                await c._client.get("https://other.test/models")
            assert len(captured) == 1
        finally:
            await c.aclose()
    asyncio.run(run())


def test_keychain_lookup_does_not_block_cancellation(configured, monkeypatch):
    import threading
    started, release = threading.Event(), threading.Event()
    def lookup(*args):
        started.set()
        release.wait(2)
        return chr(120) * 32
    monkeypatch.setattr(secrets, "resolve_keychain", lookup)
    async def run():
        c = OMLXClient(target=role_target("coding"))
        send = AsyncMock()
        monkeypatch.setattr(c._credential_transport.pool, "handle_async_request", send)
        task = asyncio.create_task(c.models())
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.001)
            assert started.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, .1)
            send.assert_not_called()
        finally:
            release.set()
            await c.aclose()
    asyncio.run(run())

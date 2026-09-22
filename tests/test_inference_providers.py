"""Provider contracts use synthetic responses, never live HTTP or Keychain."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from service import config
from service.config.endpoints import endpoint, role_target, EndpointConfigurationError
from service.config import provider_credentials as secrets
from service.errors import translate
from service.inference.omlx_client import (
    OMLXClient, IncompleteStreamError, SanitizedHTTPStatusError,
)


PRIVATE_MARKER = "PRIVATE-PROVIDER-MARKER-4f391"


@pytest.fixture
def configured(monkeypatch):
    data = {"roles": {"coding": "local-model", "fast": "local-model"},
            "inference": {"endpoints": {"cloud": {
                "provider": "openrouter", "base_url": "https://provider.test",
                "credential_ref": "keychain:cloud"}},
                "bindings": {"coding": {"endpoint": "cloud", "model_id": "vendor/model",
                    "context_window": 8192, "qualified_capabilities": ["tools"]}}}}
    monkeypatch.setattr(config, "models_config", lambda: data)
    monkeypatch.setattr(config, "omlx_base_url", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(secrets, "resolve_keychain", lambda *args: chr(120) * 32)
    return data


async def client(handler, target):
    instance = OMLXClient(target=target)
    await instance._client.aclose()
    instance._client = httpx.AsyncClient(base_url=instance.base_url,
        transport=httpx.MockTransport(handler), follow_redirects=False)
    return instance


def remote_target(configured, provider):
    configured["inference"]["endpoints"]["cloud"].update(
        provider=provider, api_prefix="/api/v1" if provider == "openrouter" else "/v1")
    return role_target("coding")


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


@pytest.mark.parametrize("role", ["fast", "router"])
def test_restricted_roles_stay_unchanged(configured, role):
    configured["inference"]["bindings"][role] = {"endpoint": "cloud"}
    with pytest.raises(EndpointConfigurationError):
        role_target(role)


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("role", ["embedding", "reranker"])
def test_every_unmanaged_retrieval_binding_fails_closed(configured, provider, role):
    remote_target(configured, provider)
    configured["inference"]["bindings"][role] = {
        "endpoint": "cloud", "model_id": "vendor/retrieval",
        "revision": "synthetic", "dimensions": 384,
    }
    with pytest.raises(EndpointConfigurationError,
                       match="Unmanaged endpoints support generation roles only"):
        role_target(role)


@pytest.mark.parametrize("role", ["embedding", "reranker"])
def test_managed_local_retrieval_bindings_remain_supported(configured, role):
    configured["inference"]["bindings"][role] = {
        "endpoint": "local", "model_id": "local-retrieval",
        "dimensions": 384,
    }
    target = role_target(role)
    assert target.endpoint.managed and target.endpoint.provider == "omlx"


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


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("stream", [False, True])
def test_reasoning_tool_replay_fails_closed(configured, provider, stream, capsys):
    async def run():
        message = {"reasoning_details": [{"type": "reasoning.encrypted", "data": PRIVATE_MARKER}],
            "tool_calls": [{"index": 0, "id": "c1", "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"}}]}
        def handler(request):
            choice = {"delta" if stream else "message": message, "finish_reason": "tool_calls"}
            if stream:
                return httpx.Response(200, text="data: " + json.dumps({"choices": [choice]}) + "\n\n")
            return httpx.Response(200, json={"choices": [choice]})
        c = await client(handler, remote_target(configured, provider))
        emitted = []
        try:
            with pytest.raises(IncompleteStreamError, match="replay is not supported") as raised:
                if stream:
                    emitted.extend([e async for e in c.stream_events("vendor/model", [])])
                else:
                    await c.chat("vendor/model", [])
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        repr(emitted), captured.out, captured.err]
            assert all(PRIVATE_MARKER not in value for value in surfaces)
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_managed_local_omlx_keeps_existing_reasoning_tool_semantics(monkeypatch, stream):
    async def run():
        message = {"reasoning_details": [{"type": "synthetic"}],
            "tool_calls": [{"index": 0, "id": "c1", "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"}}]}
        def handler(request):
            choice = {"delta" if stream else "message": message, "finish_reason": "tool_calls"}
            if stream:
                return httpx.Response(200, text="data: " + json.dumps({"choices": [choice]}) + "\n\n")
            return httpx.Response(200, json={"choices": [choice]})
        c = OMLXClient(base_url="http://127.0.0.1:8000", api_key="fixture")
        await c._client.aclose()
        c._client = httpx.AsyncClient(base_url=c.base_url, transport=httpx.MockTransport(handler))
        try:
            result = ([e async for e in c.stream_events("model", [])][-1]["message"]
                      if stream else (await c.chat("model", []))["choices"][0]["message"])
            assert result["tool_calls"]
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_remote_length_never_uses_managed_omlx_think_heuristic(configured, provider):
    async def run():
        response = {"choices": [{"message": {"content": "valid partial answer"},
                                  "finish_reason": "length"}]}
        c = await client(lambda r: httpx.Response(200, json=response),
                         remote_target(configured, provider))
        try:
            message = (await c.chat("vendor/model", []))["choices"][0]["message"]
            assert message["content"] == "valid partial answer"
            assert not message.get("_think_leak")
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


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("status", [307, 429])
@pytest.mark.parametrize("stream", [False, True])
def test_remote_http_failure_retains_no_provider_data(
        configured, provider, status, stream, capsys):
    async def run():
        def handler(request):
            return httpx.Response(status, content=PRIVATE_MARKER,
                headers={"location": "https://attacker.invalid/" + PRIVATE_MARKER,
                         "x-private": PRIVATE_MARKER})
        c = await client(handler, remote_target(configured, provider))
        emitted = []
        try:
            with pytest.raises(SanitizedHTTPStatusError) as raised:
                if stream:
                    emitted.extend([e async for e in c.stream_events("vendor/model", [])])
                else:
                    await c.chat("vendor/model", [])
            error = raised.value
            message, detail = translate(error, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(error), repr(error), message, detail,
                        str(error.request.url), repr(error.request.headers),
                        error.response.text, repr(error.response.headers),
                        repr(emitted), captured.out, captured.err]
            assert all(PRIVATE_MARKER not in value for value in surfaces)
            assert error.response.status_code == status
            assert str(error.request.url) == "https://inference.invalid/request"
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error_value", [{}, {"message": PRIVATE_MARKER}])
def test_http_200_error_object_is_rejected_without_data_escape(
        configured, provider, stream, error_value, monkeypatch, capsys):
    async def run():
        normalized = Mock()
        monkeypatch.setattr("service.inference.omlx_client._ensure_choices", normalized)
        payload = {"error": error_value, "provider_detail": PRIVATE_MARKER,
                   "choices": [{"delta" if stream else "message":
                                {"content": PRIVATE_MARKER}, "finish_reason": "stop"}]}
        if stream:
            body = "data: " + json.dumps(payload) + "\n\n"
            handler = lambda r: httpx.Response(200, text=body)
        else:
            handler = lambda r: httpx.Response(200, json=payload)
        c = await client(handler, remote_target(configured, provider))
        emitted = []
        try:
            with pytest.raises(IncompleteStreamError) as raised:
                if stream:
                    emitted.extend([e async for e in c.stream_events("vendor/model", [])])
                else:
                    await c.chat("vendor/model", [])
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        repr(emitted), captured.out, captured.err]
            assert all(PRIVATE_MARKER not in value for value in surfaces)
            if not stream:
                normalized.assert_not_called()
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("choices", [None, [], [{}], [{"message": "invalid"}]])
def test_malformed_remote_completion_is_rejected_without_data_escape(
        configured, provider, choices, monkeypatch, capsys):
    async def run():
        normalized = Mock()
        monkeypatch.setattr("service.inference.omlx_client._ensure_choices", normalized)
        payload = {"provider_detail": PRIVATE_MARKER}
        if choices is not None:
            payload["choices"] = choices
        c = await client(lambda r: httpx.Response(200, json=payload),
                         remote_target(configured, provider))
        try:
            with pytest.raises(IncompleteStreamError,
                               match="Invalid inference response data") as raised:
                await c.chat("vendor/model", [])
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        captured.out, captured.err]
            assert all(PRIVATE_MARKER not in value for value in surfaces)
            normalized.assert_not_called()
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


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("kind", ["success", "declared_success", "error_object", "http_error"])
def test_remote_nonstream_body_budget_is_bounded_and_redacted(
        configured, provider, kind, monkeypatch, capsys):
    async def run():
        class Body(httpx.AsyncByteStream):
            def __init__(self):
                self.yielded = 0
                self.closed = False
            async def __aiter__(self):
                for _ in range(20):
                    self.yielded += 1
                    yield (PRIVATE_MARKER.encode() + b"x" * 9000)
            async def aclose(self):
                self.closed = True
        body = Body()
        def handler(request):
            if kind == "http_error":
                return httpx.Response(413, headers={"x-private": PRIVATE_MARKER}, stream=body)
            prefix = (b'{"error":{"message":"' if kind == "error_object"
                      else b'{"choices":[{"message":{"content":"')
            headers = {"content-length": "129"} if kind == "declared_success" else None
            return httpx.Response(200, headers=headers, stream=BodyWithPrefix(prefix, body))

        class BodyWithPrefix(httpx.AsyncByteStream):
            def __init__(self, prefix, source):
                self.prefix, self.source = prefix, source
            async def __aiter__(self):
                yield self.prefix
                async for part in self.source:
                    yield part
            async def aclose(self):
                self.source.closed = True

        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (64, 128)))
        c = await client(handler, remote_target(configured, provider))
        try:
            with pytest.raises((IncompleteStreamError, SanitizedHTTPStatusError)) as raised:
                await c.chat("vendor/model", [], max_tokens=8)
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        captured.out, captured.err]
            assert all(PRIVATE_MARKER not in value for value in surfaces)
            assert body.closed
            if kind in {"http_error", "declared_success"}:
                assert body.yielded == 0
            else:
                assert body.yielded <= 2
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("field", ["content", "reasoning_content", "tool_arguments"])
def test_remote_nonstream_output_fields_have_cumulative_budget(
        configured, provider, field, monkeypatch, capsys):
    async def run():
        value = PRIVATE_MARKER + "x" * 96
        if field == "tool_arguments":
            message = {"tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "lookup", "arguments": value}}]}
            finish = "tool_calls"
        else:
            message = {field: value}
            finish = "stop"
        response = {"choices": [{"message": message, "finish_reason": finish}]}
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (64, 4096)))
        c = await client(lambda r: httpx.Response(200, json=response),
                         remote_target(configured, provider))
        try:
            with pytest.raises(IncompleteStreamError,
                               match="output exceeded the allowed size") as raised:
                await c.chat("vendor/model", [], max_tokens=8)
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        captured.out, captured.err]
            assert all(PRIVATE_MARKER not in item for item in surfaces)
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_remote_reasoning_details_obey_real_minimum_budget_at_one_token(
        configured, provider, capsys):
    async def run():
        output_limit, _ = OMLXClient._remote_limits(1)
        details = [{"type": "reasoning.text",
                    "text": PRIVATE_MARKER + "x" * output_limit}]
        response = {"choices": [{"message": {"content": "", "reasoning_details": details},
                                  "finish_reason": "stop"}]}
        c = await client(lambda r: httpx.Response(200, json=response),
                         remote_target(configured, provider))
        try:
            with pytest.raises(IncompleteStreamError,
                               match="output exceeded the allowed size") as raised:
                await c.chat("vendor/model", [], max_tokens=1)
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        captured.out, captured.err]
            assert all(PRIVATE_MARKER not in item for item in surfaces)
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_nested_fragmented_reasoning_details_remain_compatible_under_budget(
        configured, provider, monkeypatch):
    async def run():
        details = [
            {"type": "reasoning.text", "text": "first",
             "meta": {"accepted": True, "score": 1.25, "empty": None}},
            {"type": "reasoning.text", "text": "second",
             "parts": ["nested", 2, False]},
        ]
        response = {"choices": [{"message": {
            "content": "answer", "reasoning_details": details},
            "finish_reason": "stop"}]}
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (512, 4096)))
        c = await client(lambda r: httpx.Response(200, json=response),
                         remote_target(configured, provider))
        try:
            result = await c.chat("vendor/model", [], max_tokens=1)
            assert result["choices"][0]["message"]["reasoning_details"] == details
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_reasoning_details_have_an_independent_bounded_budget(
        configured, provider, monkeypatch):
    async def run():
        response = {"choices": [{"message": {
            "content": "c" * 45,
            "reasoning_details": {"data": PRIVATE_MARKER + "r" * 20}},
            "finish_reason": "stop"}]}
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (80, 4096)))
        c = await client(lambda r: httpx.Response(200, json=response),
                         remote_target(configured, provider))
        try:
            result = await c.chat("vendor/model", [], max_tokens=1)
            assert result["choices"][0]["message"]["content"] == "c" * 45
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["cycle", "unsupported", "nonfinite"])
def test_reasoning_details_reject_non_json_structures_without_data_escape(kind):
    details = {"data": PRIVATE_MARKER}
    if kind == "cycle":
        details["nested"] = details
    elif kind == "nonfinite":
        details["nested"] = float("nan")
    else:
        details["nested"] = object()
    response = {"choices": [{"message": {"reasoning_details": details}}]}
    with pytest.raises(IncompleteStreamError,
                       match="Invalid inference response data") as raised:
        OMLXClient._check_remote_completion_output(response, 4096)
    assert PRIVATE_MARKER not in str(raised.value)
    assert PRIVATE_MARKER not in repr(raised.value)


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("location", ["key", "value"])
def test_reasoning_details_reject_lone_surrogates_without_data_escape(
        configured, provider, location, capsys):
    async def run():
        tainted = PRIVATE_MARKER + "\ud800"
        details = ({tainted: "value"} if location == "key"
                   else {"data": tainted})
        response = {"choices": [{"message": {"reasoning_details": details},
                                  "finish_reason": "stop"}]}
        body = json.dumps(response).encode("utf-8")
        c = await client(lambda r: httpx.Response(200, content=body),
                         remote_target(configured, provider))
        try:
            with pytest.raises(IncompleteStreamError,
                               match="Invalid inference response data") as raised:
                await c.chat("vendor/model", [], max_tokens=1)
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        captured.out, captured.err]
            assert all(PRIVATE_MARKER not in item for item in surfaces)
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("field", ["content", "reasoning_content", "tool_arguments"])
def test_remote_stream_output_fields_have_cumulative_budget(
        configured, provider, field, monkeypatch, capsys):
    async def run():
        value = PRIVATE_MARKER + "x" * 96
        if field == "tool_arguments":
            delta = {"tool_calls": [{"index": 0, "id": "c1",
                "function": {"name": "lookup", "arguments": value}}]}
            finish = "tool_calls"
        else:
            delta = {field: value}
            finish = "stop"
        body = "data: " + json.dumps({"choices": [{
            "delta": delta, "finish_reason": finish}]}) + "\n\ndata: [DONE]\n\n"
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (64, 4096)))
        c = await client(lambda r: httpx.Response(200, text=body),
                         remote_target(configured, provider))
        emitted = []
        try:
            with pytest.raises(IncompleteStreamError,
                               match="output exceeded the allowed size") as raised:
                emitted.extend([e async for e in c.stream_events(
                    "vendor/model", [], max_tokens=8)])
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        repr(emitted), captured.out, captured.err]
            assert all(PRIVATE_MARKER not in item for item in surfaces)
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_streamed_reasoning_details_obey_real_minimum_budget_at_one_token(
        configured, provider, capsys):
    async def run():
        output_limit, _ = OMLXClient._remote_limits(1)
        details = [{"type": "reasoning.text",
                    "text": PRIVATE_MARKER + "x" * output_limit}]
        body = "data: " + json.dumps({"choices": [{
            "delta": {"reasoning_details": details},
            "finish_reason": "stop"}]}) + "\n\ndata: [DONE]\n\n"
        c = await client(lambda r: httpx.Response(200, text=body),
                         remote_target(configured, provider))
        emitted = []
        try:
            with pytest.raises(IncompleteStreamError,
                               match="output exceeded the allowed size") as raised:
                emitted.extend([e async for e in c.stream_events(
                    "vendor/model", [], max_tokens=1)])
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        repr(emitted), captured.out, captured.err]
            assert all(PRIVATE_MARKER not in item for item in surfaces)
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_streamed_reasoning_detail_fragments_share_one_budget(
        configured, provider, monkeypatch):
    async def run():
        chunks = [
            {"choices": [{"delta": {
                "reasoning_details": {"data": "x" * 32}}, "finish_reason": None}]},
            {"choices": [{"delta": {
                "reasoning_details": {"data": "y" * 32}}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (80, 4096)))
        c = await client(lambda r: httpx.Response(200, text=body),
                         remote_target(configured, provider))
        try:
            with pytest.raises(IncompleteStreamError,
                               match="output exceeded the allowed size"):
                _ = [e async for e in c.stream_events(
                    "vendor/model", [], max_tokens=1)]
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_under_budget_streamed_reasoning_detail_fragments_remain_compatible(
        configured, provider, monkeypatch):
    async def run():
        chunks = [
            {"choices": [{"delta": {"reasoning_details": [
                {"type": "reasoning.text", "text": "first"}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_details": [
                {"type": "reasoning.text", "text": "second"}],
                "content": "answer"}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (512, 4096)))
        c = await client(lambda r: httpx.Response(200, text=body),
                         remote_target(configured, provider))
        try:
            events = [e async for e in c.stream_events(
                "vendor/model", [], max_tokens=1)]
            assert events[-1]["message"]["content"] == "answer"
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_streamed_reasoning_details_have_an_independent_bounded_budget(
        configured, provider, monkeypatch):
    async def run():
        chunks = [
            {"choices": [{"delta": {"content": "c" * 45},
                "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_details": {
                "data": PRIVATE_MARKER + "r" * 20}}, "finish_reason": "stop"}]},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (80, 4096)))
        c = await client(lambda r: httpx.Response(200, text=body),
                         remote_target(configured, provider))
        emitted = []
        try:
            emitted.extend([e async for e in c.stream_events(
                "vendor/model", [], max_tokens=1)])
            assert emitted[-1]["message"]["content"] == "c" * 45
            assert PRIVATE_MARKER not in repr(emitted)
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_direct_cloud_stream_uses_all_context_remaining_after_prompt(
        configured, provider):
    async def run():
        target = replace(remote_target(configured, provider), context_window=16_384)
        sent_max_tokens = []

        def handler(request):
            sent_max_tokens.append(json.loads(request.content)["max_tokens"])
            return httpx.Response(200, text=(
                'data: {"choices":[{"delta":{"content":"answer"},'
                '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'))

        c = await client(handler, target)
        try:
            events = [e async for e in c.stream_events(
                "vendor/model", [{"role": "user", "content": "brief prompt"}],
                max_tokens=8000, use_remaining_context=True)]
            assert events[-1]["message"]["content"] == "answer"
            assert 8000 < sent_max_tokens[0] < target.context_window
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
@pytest.mark.parametrize("kind", ["surrogate", "nonfinite"])
def test_streamed_reasoning_details_reject_invalid_json_values_safely(
        configured, provider, kind, capsys):
    async def run():
        value = PRIVATE_MARKER + "\ud800" if kind == "surrogate" else float("nan")
        chunk = {"choices": [{"delta": {
            "reasoning_details": {"data": value}}, "finish_reason": "stop"}]}
        body = "data: " + json.dumps(chunk) + "\n\n"
        c = await client(lambda r: httpx.Response(200, content=body.encode("utf-8")),
                         remote_target(configured, provider))
        emitted = []
        try:
            with pytest.raises(IncompleteStreamError,
                               match="Invalid inference response data") as raised:
                emitted.extend([e async for e in c.stream_events(
                    "vendor/model", [], max_tokens=1)])
            message, detail = translate(raised.value, endpoint_name="cloud")
            captured = capsys.readouterr()
            surfaces = [str(raised.value), repr(raised.value), message, detail,
                        repr(emitted), captured.out, captured.err]
            assert all(PRIVATE_MARKER not in item for item in surfaces)
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_continuous_remote_stream_cannot_evade_wire_budget(
        configured, provider, monkeypatch):
    async def run():
        class Endless(httpx.AsyncByteStream):
            def __init__(self):
                self.yielded = 0
                self.closed = False
            async def __aiter__(self):
                while True:
                    self.yielded += 1
                    yield b":" + b"x" * 4095
            async def aclose(self):
                self.closed = True
        body = Endless()
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (64, 256)))
        c = await client(lambda r: httpx.Response(200, stream=body),
                         remote_target(configured, provider))
        try:
            with pytest.raises(IncompleteStreamError,
                               match="stream exceeded the allowed size"):
                _ = [e async for e in c.stream_events("vendor/model", [], max_tokens=8)]
            assert body.closed and body.yielded <= 2
        finally:
            await c.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openrouter", "omlx"])
def test_remote_transport_abort_after_content_is_typed_and_sanitized(
        configured, provider):
    async def run():
        class Interrupted(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (b'data: {"choices":[{"delta":{"content":"partial"},'
                       b'"finish_reason":null}]}\n\n')
                raise httpx.ReadError(
                    PRIVATE_MARKER,
                    request=httpx.Request("POST", "https://provider.invalid"),
                )

        c = await client(lambda r: httpx.Response(200, stream=Interrupted()),
                         remote_target(configured, provider))
        emitted = []
        try:
            with pytest.raises(IncompleteStreamError,
                               match="stream ended unexpectedly") as raised:
                async for event in c.stream_events(
                        "vendor/model", [], max_tokens=8):
                    emitted.append(event)
            assert [event["text"] for event in emitted
                    if event["kind"] == "content"] == ["partial"]
            assert PRIVATE_MARKER not in str(raised.value)
            assert PRIVATE_MARKER not in repr(raised.value)
        finally:
            await c.aclose()
    asyncio.run(run())


def test_managed_local_completion_keeps_existing_unbounded_transport(
        monkeypatch):
    async def run():
        content = "local" * 100
        response = {"choices": [{"message": {"content": content},
                                  "finish_reason": "stop"}]}
        monkeypatch.setattr(OMLXClient, "_remote_limits",
                            staticmethod(lambda max_tokens: (1, 1)))
        c = OMLXClient(base_url="http://127.0.0.1:8000", api_key="fixture")
        await c._client.aclose()
        c._client = httpx.AsyncClient(base_url=c.base_url,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response)))
        try:
            result = await c.chat("model", [], max_tokens=8)
            assert result["choices"][0]["message"]["content"] == content
        finally:
            await c.aclose()
    asyncio.run(run())

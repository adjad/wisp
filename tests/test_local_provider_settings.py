"""Synthetic checks for external loopback inference configuration."""
import asyncio

import pytest

import service.config as config
from service.config.endpoints import EndpointConfigurationError, Target, endpoint_from_config
from service.inference.omlx_client import OMLXClient


def _endpoint_cfg(url: str = "http://127.0.0.1:8767") -> dict:
    return {"enabled": True, "provider": "openai-compatible", "base_url": url,
            "api_prefix": "/v1", "credential_ref": "none"}


def test_external_loopback_provider_is_unmanaged_and_anonymous() -> None:
    endpoint = endpoint_from_config("local_provider", _endpoint_cfg())
    assert endpoint.managed is False
    assert endpoint.api_key() == ""


@pytest.mark.parametrize("url", [
    "http://localhost:8767", "http://127.0.0.2:8767",
    "http://127.0.0.1:8000", "http://127.0.0.1:8765",
    "http://127.0.0.1:80", "http://127.0.0.1:8767/admin",
    "http://user@127.0.0.1:8767", "https://127.0.0.1:8767",
    "http://example.com:8767",
])
def test_external_provider_rejects_other_origins(url: str) -> None:
    with pytest.raises(EndpointConfigurationError):
        endpoint_from_config("local_provider", _endpoint_cfg(url))


def test_anonymous_ref_cannot_be_used_by_cloud() -> None:
    with pytest.raises(EndpointConfigurationError):
        endpoint_from_config("cloud", _endpoint_cfg("https://example.com"))


def test_local_provider_rejects_managed_profile() -> None:
    cfg = _endpoint_cfg()
    cfg["provider"] = "omlx"
    with pytest.raises(EndpointConfigurationError):
        endpoint_from_config("local_provider", cfg)


def test_cloud_update_preserves_independent_local_provider_binding(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(config, "models_config", lambda: {
        "roles": {"reasoning": "local-reasoning", "coding": "local-coding",
                  "research": "local-research"},
        "inference": {"bindings": {
            "reasoning": {"endpoint": "local_provider", "model_id": "Ling"},
        }},
    })
    monkeypatch.setattr(config, "_save_overlay", captured.append)
    config.set_cloud_provider({"enabled": True}, "cloud-model", 8192, ["coding"])
    bindings = captured[0]["inference"]["bindings"]
    assert bindings["coding"]["endpoint"] == "cloud"
    assert "reasoning" not in bindings


def test_local_provider_only_assigns_no_tool_reasoning(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(config, "_save_overlay", captured.append)
    config.set_local_provider(_endpoint_cfg(), "Ling", 8192, ["reasoning"])
    binding = captured[0]["inference"]["bindings"]["reasoning"]
    assert binding["endpoint"] == "local_provider"
    assert binding["qualified_capabilities"] == []
    with pytest.raises(ValueError):
        config.set_local_provider(_endpoint_cfg(), "Ling", 8192, ["coding"])


def test_super_model_makes_saved_local_binding_inactive(monkeypatch) -> None:
    monkeypatch.setattr(config, "models_config", lambda: {"inference": {
        "endpoints": {
            "local_provider": {**_endpoint_cfg(), "model_id": "Ling"},
            "cloud": {"enabled": True},
        },
        "bindings": {"reasoning": {"endpoint": "local_provider", "model_id": "Ling"}},
        "super_model": {"enabled": True},
    }})
    settings = config.local_provider_settings()
    assert settings["enabled"] is True
    assert settings["roles"] == ["reasoning"]
    assert settings["active"] is False


def test_cloud_reasoning_leaves_local_provider_connected_but_unassigned(monkeypatch) -> None:
    monkeypatch.setattr(config, "models_config", lambda: {"inference": {
        "endpoints": {
            "local_provider": {**_endpoint_cfg(), "model_id": "Ling"},
            "cloud": {"enabled": True},
        },
        "bindings": {"reasoning": {"endpoint": "cloud", "model_id": "cloud-model"}},
        "super_model": {"enabled": False},
    }})
    settings = config.local_provider_settings()
    assert settings["enabled"] is True
    assert settings["active"] is False
    assert settings["roles"] == []


def test_direct_reasoning_wire_payload_respects_local_app_cap() -> None:
    from service.main import _direct_generation_budget

    target = Target("reasoning", endpoint_from_config("local_provider", _endpoint_cfg()),
                    "Ling", context_window=8192)
    client = OMLXClient(target=target, timeout=30)
    try:
        budget, use_remaining = _direct_generation_budget(target)
        assert use_remaining is False
        model, messages, tools, tokens = client._fit_request(
            "Ling", [{"role": "user", "content": "Explain a rocket."}], None,
            budget, use_remaining_context=use_remaining)
        wire = client._payload(model, messages, tools, None, None, tokens, True)
        assert 0 < wire["max_tokens"] <= 2048
    finally:
        asyncio.run(client.aclose())


def test_connect_requires_synthetic_stream_before_saving(monkeypatch) -> None:
    import service.main as main

    captured = {}

    class FakeClient:
        def __init__(self, *, target, timeout):
            captured["target"] = target

        async def models(self):
            return ["Ling"]

        async def stream_events(self, model, messages, **kwargs):
            captured["prompt"] = messages[-1]["content"]
            captured["max_tokens"] = kwargs["max_tokens"]
            yield {"kind": "final", "message": {"role": "assistant", "content": "OK"}}

        async def aclose(self):
            pass

    async def saved_settings():
        return {"enabled": True}

    monkeypatch.setattr(main, "OMLXClient", FakeClient)
    monkeypatch.setattr(main, "set_local_provider",
                        lambda *args: captured.update(saved=True))
    monkeypatch.setattr(main, "get_local_provider_inference", saved_settings)
    result = asyncio.run(main.connect_local_provider_inference({
        "base_url": "http://127.0.0.1:8767", "api_prefix": "/v1",
        "model_id": "Ling", "context_window": 8192, "roles": ["reasoning"],
    }))
    assert result["enabled"] is True
    assert captured["saved"] is True
    assert captured["prompt"] == "Reply with OK."
    assert 0 < captured["max_tokens"] <= 2048


@pytest.mark.parametrize("reply", ["", "   "])
def test_connect_rejects_empty_streaming_reply(monkeypatch, reply: str) -> None:
    import service.main as main

    saved = []

    class FakeClient:
        def __init__(self, *, target, timeout):
            pass

        async def models(self):
            return ["Ling"]

        async def stream_events(self, model, messages, **kwargs):
            yield {"kind": "final", "message": {"role": "assistant", "content": reply}}

        async def aclose(self):
            pass

    monkeypatch.setattr(main, "OMLXClient", FakeClient)
    monkeypatch.setattr(main, "set_local_provider", lambda *args: saved.append(args))
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.connect_local_provider_inference({
            "base_url": "http://127.0.0.1:8767", "api_prefix": "/v1",
            "model_id": "Ling", "context_window": 8192, "roles": ["reasoning"],
        }))
    assert error.value.status_code == 400
    assert saved == []


def test_connect_accepts_nonempty_final_after_whitespace_chunk(monkeypatch) -> None:
    import service.main as main

    saved = []

    class FakeClient:
        def __init__(self, *, target, timeout):
            pass

        async def models(self):
            return ["Ling"]

        async def stream_events(self, model, messages, **kwargs):
            yield {"kind": "content", "text": " "}
            yield {"kind": "final", "message": {"role": "assistant", "content": "OK"}}

        async def aclose(self):
            pass

    async def saved_settings():
        return {"enabled": True}

    monkeypatch.setattr(main, "OMLXClient", FakeClient)
    monkeypatch.setattr(main, "set_local_provider", lambda *args: saved.append(args))
    monkeypatch.setattr(main, "get_local_provider_inference", saved_settings)
    result = asyncio.run(main.connect_local_provider_inference({
        "base_url": "http://127.0.0.1:8767", "api_prefix": "/v1",
        "model_id": "Ling", "context_window": 8192, "roles": ["reasoning"],
    }))
    assert result["enabled"] is True
    assert len(saved) == 1


@pytest.mark.parametrize("stream", [False, True])
def test_direct_chat_caps_local_provider_output(monkeypatch, stream: bool) -> None:
    import service.main as main

    target = Target("reasoning", endpoint_from_config("local_provider", _endpoint_cfg()),
                    "Ling", context_window=8192)
    captured = {}

    class FakeClient:
        def __init__(self, *, target):
            pass

        async def ensure_only(self, model):
            pass

        async def stream(self, model, messages, *, max_tokens):
            captured["max_tokens"] = max_tokens
            yield "OK"

        async def chat(self, model, messages, *, max_tokens):
            captured["max_tokens"] = max_tokens
            return {"choices": [{"message": {"content": "OK"}}]}

        async def aclose(self):
            pass

    monkeypatch.setattr(main, "role_target", lambda role: target)
    monkeypatch.setattr(main, "OMLXClient", FakeClient)

    async def exercise():
        response = await main.chat({"role": "reasoning", "prompt": "Explain a rocket.",
                                    "stream": stream})
        if stream:
            return [chunk async for chunk in response.body_iterator]
        return response["content"]

    assert asyncio.run(exercise())
    assert captured["max_tokens"] == 2048


@pytest.mark.parametrize("invalid", [0, -1, "8000", True])
def test_direct_chat_rejects_invalid_limit_before_opening_client(monkeypatch, invalid) -> None:
    import service.main as main

    target = Target("reasoning", endpoint_from_config("local_provider", _endpoint_cfg()),
                    "Ling", context_window=8192)
    opened = []
    monkeypatch.setattr(main, "role_target", lambda role: target)
    monkeypatch.setattr(main, "OMLXClient", lambda **kwargs: opened.append(kwargs))
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.chat({"role": "reasoning", "prompt": "Hi", "max_tokens": invalid}))
    assert error.value.status_code == 422
    assert opened == []


def test_external_local_reasoning_excludes_automatic_memory_context() -> None:
    from service.main import _local_provider_direct_messages

    messages = _local_provider_direct_messages("reasoning", "Explain a rocket.")
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "Explain a rocket."}


@pytest.mark.parametrize("context_window", [512, 1024, 8192])
def test_probe_http_payload_keeps_explicit_64_token_budget(context_window: int) -> None:
    target = Target("connection-test",
                    endpoint_from_config("local_provider", _endpoint_cfg()),
                    "Ling", context_window=context_window)
    client = OMLXClient(target=target, timeout=30)
    try:
        model, messages, tools, tokens = client._fit_request(
            "Ling", [{"role": "user", "content": "Reply with OK."}], None, 64)
        payload = client._payload(model, messages, tools, None, None, tokens, True)
        assert 0 < payload["max_tokens"] <= 64
        assert payload["stream"] is True
    finally:
        asyncio.run(client.aclose())

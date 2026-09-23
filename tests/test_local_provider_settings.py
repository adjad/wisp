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

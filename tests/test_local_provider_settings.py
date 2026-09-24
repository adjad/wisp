"""Synthetic checks for external loopback inference configuration."""
import asyncio
import json

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


@pytest.mark.parametrize("credential_ref", ["", "env:WISP_SECRET", "keychain:Wisp", "local_omlx"])
def test_external_loopback_provider_rejects_credentials(credential_ref: str) -> None:
    cfg = _endpoint_cfg()
    cfg["credential_ref"] = credential_ref
    with pytest.raises(EndpointConfigurationError, match="anonymous"):
        endpoint_from_config("local_provider", cfg)


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


@pytest.mark.parametrize("newer_action", ["disconnect", "reasoning", "cloud"])
def test_pending_local_connect_cannot_overwrite_newer_routing_choice(
        monkeypatch, newer_action: str) -> None:
    import service.main as main

    stream_started = asyncio.Event()
    release_stream = asyncio.Event()
    saved = []
    disabled = []
    reassigned = []

    class FakeClient:
        def __init__(self, *, target, timeout):
            pass

        async def models(self):
            return ["Ling"]

        async def stream_events(self, model, messages, **kwargs):
            stream_started.set()
            await release_stream.wait()
            yield {"kind": "final", "message": {"role": "assistant", "content": "OK"}}

        async def aclose(self):
            pass

    async def settings():
        return {"enabled": not disabled}

    monkeypatch.setattr(main, "OMLXClient", FakeClient)
    monkeypatch.setattr(main, "set_local_provider", lambda *args: saved.append(args))
    monkeypatch.setattr(main, "disable_local_provider", lambda: disabled.append(True))
    monkeypatch.setattr(main, "get_local_provider_inference", settings)
    monkeypatch.setattr(main, "set_role", lambda *args: reassigned.append(args))
    monkeypatch.setattr(main, "role_to_model", lambda role: "managed")
    monkeypatch.setattr(main, "models_config", lambda: {"roles": {"reasoning": "managed"}})
    monkeypatch.setattr(main, "disable_cloud_provider", lambda: reassigned.append("cloud"))
    monkeypatch.setattr(main, "get_cloud_inference", settings)

    async def exercise():
        pending = asyncio.create_task(main.connect_local_provider_inference({
            "base_url": "http://127.0.0.1:8767", "api_prefix": "/v1",
            "model_id": "Ling", "context_window": 8192, "roles": ["reasoning"],
        }))
        await asyncio.wait_for(stream_started.wait(), 1)
        if newer_action == "disconnect":
            await main.disconnect_local_provider_inference()
        elif newer_action == "reasoning":
            await main.config({"role": "reasoning", "model": "managed"})
        else:
            await main.disconnect_cloud_inference()
        release_stream.set()
        with pytest.raises(main.HTTPException) as error:
            await pending
        return error.value

    error = asyncio.run(exercise())
    assert error.status_code == 409
    assert saved == []
    assert disabled == ([True] if newer_action == "disconnect" else [])
    assert reassigned == ({"disconnect": [], "reasoning": [("reasoning", "managed")],
                           "cloud": ["cloud"]}[newer_action])


def test_pending_cloud_connect_cannot_overwrite_newer_reasoning_choice(monkeypatch) -> None:
    import service.main as main

    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    saved = []
    reassigned = []

    class FakeClient:
        def __init__(self, *, target, timeout):
            pass

        async def models(self):
            probe_started.set()
            await release_probe.wait()
            return ["cloud-model"]

        async def aclose(self):
            pass

    monkeypatch.setattr(main, "OMLXClient", FakeClient)
    monkeypatch.setattr(main, "set_cloud_provider", lambda *args, **kwargs: saved.append(args))
    monkeypatch.setattr(main, "set_role", lambda *args: reassigned.append(args))
    monkeypatch.setattr(main, "role_to_model", lambda role: "managed")
    monkeypatch.setattr(main, "models_config", lambda: {"roles": {"reasoning": "managed"}})

    async def exercise():
        pending = asyncio.create_task(main.connect_cloud_inference({
            "provider": "openai-compatible", "base_url": "https://example.com",
            "api_prefix": "/v1", "model_id": "cloud-model", "context_window": 8192,
            "credential_name": "Wisp", "roles": ["reasoning"],
        }))
        await asyncio.wait_for(probe_started.wait(), 1)
        await main.config({"role": "reasoning", "model": "managed"})
        release_probe.set()
        with pytest.raises(main.HTTPException) as error:
            await pending
        return error.value

    error = asyncio.run(exercise())
    assert error.status_code == 409
    assert saved == []
    assert reassigned == [("reasoning", "managed")]


def test_local_connect_has_total_deadline_despite_stream_progress(monkeypatch) -> None:
    import service.main as main

    saved = []
    chunks = []

    class FakeClient:
        def __init__(self, *, target, timeout):
            pass

        async def models(self):
            return ["Ling"]

        async def stream_events(self, model, messages, **kwargs):
            while True:
                await asyncio.sleep(0.005)
                chunks.append(True)
                yield {"kind": "content", "text": "OK"}

        async def aclose(self):
            pass

    monkeypatch.setattr(main, "OMLXClient", FakeClient)
    monkeypatch.setattr(main, "set_local_provider", lambda *args: saved.append(args))
    monkeypatch.setattr(main, "_LOCAL_PROVIDER_PROBE_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.connect_local_provider_inference({
            "base_url": "http://127.0.0.1:8767", "api_prefix": "/v1",
            "model_id": "Ling", "context_window": 8192, "roles": ["reasoning"],
        }))
    assert error.value.status_code == 504
    assert chunks
    assert saved == []


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


@pytest.mark.parametrize("stream", [False, True])
def test_direct_chat_does_not_forward_supplied_history_to_local_provider(
        monkeypatch, stream: bool) -> None:
    import service.main as main

    target = Target("reasoning", endpoint_from_config("local_provider", _endpoint_cfg()),
                    "Ling", context_window=8192)
    captured = []

    class FakeClient:
        def __init__(self, *, target):
            pass

        async def ensure_only(self, model):
            pass

        async def stream(self, model, messages, *, max_tokens):
            captured.extend(messages)
            yield "OK"

        async def chat(self, model, messages, *, max_tokens):
            captured.extend(messages)
            return {"choices": [{"message": {"content": "OK"}}]}

        async def aclose(self):
            pass

    monkeypatch.setattr(main, "role_target", lambda role: target)
    monkeypatch.setattr(main, "OMLXClient", FakeClient)

    async def exercise():
        response = await main.chat({
            "role": "reasoning", "prompt": "Current question", "stream": stream,
            "messages": [{"role": "system", "content": "PRIVATE_MEMORY"},
                         {"role": "user", "content": "PRIVATE_HISTORY"}],
        })
        if stream:
            return [chunk async for chunk in response.body_iterator]
        return response["content"]

    assert asyncio.run(exercise())
    assert len(captured) == 2
    assert captured[1] == {"role": "user", "content": "Current question"}
    assert "PRIVATE_MEMORY" not in str(captured)
    assert "PRIVATE_HISTORY" not in str(captured)


def test_direct_chat_without_current_prompt_rejects_before_local_connection(monkeypatch) -> None:
    import service.main as main

    target = Target("reasoning", endpoint_from_config("local_provider", _endpoint_cfg()),
                    "Ling", context_window=8192)
    opened = []
    monkeypatch.setattr(main, "role_target", lambda role: target)
    monkeypatch.setattr(main, "OMLXClient", lambda **kwargs: opened.append(kwargs))
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.chat({"role": "reasoning", "messages": [
            {"role": "user", "content": "PRIVATE_HISTORY"}]}))
    assert error.value.status_code == 422
    assert opened == []


def test_active_skill_uses_managed_model_instead_of_external_reasoning(
        monkeypatch, tmp_path) -> None:
    import service.main as main
    from service.config.endpoints import Endpoint
    from service.memory import context
    from service.memory.store import SessionStore
    from service.router.router import RouteDecision

    local_target = Target("reasoning", endpoint_from_config("local_provider", _endpoint_cfg()),
                          "Ling", context_window=8192)
    managed_target = Target("reasoning", Endpoint("local", "http://127.0.0.1:8000",
                            "local_omlx", managed=True), "managed-model",
                            context_window=8192)
    saved = SessionStore(tmp_path / "sessions.db")
    sid = saved.create_session()
    captured = []

    class ManagedClient:
        async def ensure_only(self, model, **kwargs):
            pass

        async def stream_events(self, model, messages, **kwargs):
            captured.append((model, messages))
            yield {"kind": "content", "text": "Answer"}
            yield {"kind": "final", "message": {"role": "assistant", "content": "Answer"}}

    async def routed(*args, **kwargs):
        return RouteDecision("reasoning", "Ling", False, "rules", "fixture")

    async def ready():
        pass

    monkeypatch.setattr(main, "client", ManagedClient(), raising=False)
    monkeypatch.setattr(main, "store", saved)
    monkeypatch.setattr(context, "store", saved)
    monkeypatch.setattr(main, "models_config", lambda: {
        "tool_retrieval": {"provider": "lexical"},
        "inference": {"bindings": {"reasoning": {"endpoint": "local_provider"}}},
    })
    monkeypatch.setattr(main, "route", routed)
    monkeypatch.setattr(main, "role_target", lambda role: local_target)
    monkeypatch.setattr(main, "local_role_target", lambda role: managed_target)
    monkeypatch.setattr(main, "cloud_super_model_enabled", lambda: False)
    monkeypatch.setattr(main, "ensure_omlx", ready)
    monkeypatch.setattr(main, "memory_block", lambda **kwargs: "")
    monkeypatch.setattr(main.skills, "select_for_turn", lambda *args: "idea-refine")
    monkeypatch.setattr(main.skills, "selected_skill_block",
                        lambda prompt, active_name: "\nSKILL:idea-refine")
    monkeypatch.setattr(main, "OMLXClient", lambda **kwargs: pytest.fail(
        "Active skill opened the external provider"))

    async def exercise():
        response = await main.agent({"prompt": "Explore this idea", "session_id": sid,
                                     "debug": False})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes):
                item = item.decode()
            events.append(json.loads(item.removeprefix("data: ").strip()))
        return events

    events = asyncio.run(exercise())
    assert not [event for event in events if event["type"] == "error"], events
    routed_event = next(event for event in events if event["type"] == "routed")
    assert routed_event["model"] == "managed-model"
    assert routed_event["route_source"] == "active_skill_local"
    assert len(captured) == 1
    assert captured[0][0] == "managed-model"
    assert "SKILL:idea-refine" in captured[0][1][0]["content"]


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

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


def test_local_provider_reasoning_alone_never_gets_tools(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(config, "_save_overlay", captured.append)
    config.set_local_provider(_endpoint_cfg(), "Ling", 8192, ["reasoning"])
    binding = captured[0]["inference"]["bindings"]["reasoning"]
    assert binding["endpoint"] == "local_provider"
    assert binding["qualified_capabilities"] == []
    # Tool-using workloads need a passing, server-recorded qualification.
    for roles in (["coding"], ["agent"], ["reasoning", "agent"]):
        with pytest.raises(ValueError):
            config.set_local_provider(_endpoint_cfg(), "Ling", 8192, roles)


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
                        lambda *args, **kwargs: captured.update(saved=True))
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
    monkeypatch.setattr(main, "set_local_provider", lambda *args, **kwargs: saved.append(args))
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
    monkeypatch.setattr(main, "set_local_provider", lambda *args, **kwargs: saved.append(args))
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
    monkeypatch.setattr(main, "set_local_provider", lambda *args, **kwargs: saved.append(args))
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
    monkeypatch.setattr(main, "set_local_provider", lambda *args, **kwargs: saved.append(args))
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


# ------------------------------ PR130 D3: skills stay on the managed model (agent path)
#
# An external Agent/Coding binding gets Wisp's tool loop. Skill content must never
# travel to that unauthenticated loopback app: not a matched ordinary skill's body,
# not the installed-skill catalog, not a use_skill result, and not a skill-defined
# tool. A turn that invokes a skill stays on the managed target; every other turn
# keeps working on the external one, with no skill content in what it receives.
# Everything is synthetic and offline: scripted in-process clients, a temporary
# session store, and the real skill selection and context helpers.

import copy as _copy
from pathlib import Path as _Path

_BODY_ORDINARY = "BODY-SENTINEL-ORDINARY file the expense report in four steps"
_BODY_WORKFLOW = "BODY-SENTINEL-WORKFLOW ask one question at a time"
_BODY_QUIET = "BODY-SENTINEL-QUIET never triggered"
_CATALOG_ORDINARY = "CATALOG-SENTINEL-ORDINARY"
_CATALOG_QUIET = "CATALOG-SENTINEL-QUIET"
_SKILL_SENTINELS = (_BODY_ORDINARY, _BODY_WORKFLOW, _BODY_QUIET, _CATALOG_ORDINARY,
                    _CATALOG_QUIET, "Installed skills", "use_skill", "expense-helper",
                    "quiet-helper")


def _synthetic_skills():
    from service.skills import Skill
    return {
        "expense-helper": Skill("expense-helper", _CATALOG_ORDINARY, _BODY_ORDINARY,
                                _Path("/nonexistent/expense-helper"),
                                triggers=["expense report"], conversation_workflow=False),
        "idea-refine": Skill("idea-refine", "workflow", _BODY_WORKFLOW,
                             _Path("/nonexistent/idea-refine"),
                             triggers=["explore this idea"], conversation_workflow=True),
        "quiet-helper": Skill("quiet-helper", _CATALOG_QUIET, _BODY_QUIET,
                              _Path("/nonexistent/quiet-helper"),
                              triggers=["zzz-never-typed"], conversation_workflow=False),
    }


class _ScriptedClient:
    """A synthetic inference client that records exactly what it is sent."""

    def __init__(self, *, managed: bool, script=None, target=None):
        self.managed = managed
        self.target = target
        self.endpoint_name = "local" if managed else "local_provider"
        self.script = list(script or [])
        self.requests: list[dict] = []

    async def health(self):
        return True

    async def ensure_only(self, *args, **kwargs):
        pass

    def set_keep_warm(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, **kwargs):
        self.requests.append({
            "model": model, "messages": _copy.deepcopy(messages),
            "tools": [t["function"]["name"] for t in (kwargs.get("tools") or [])]})
        response = self.script.pop(0) if self.script else {"role": "assistant", "content": "Done."}
        yield {"kind": "final", "message": response}

    async def aclose(self):
        pass


def _call(name, args=None, call_id="call-1"):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {})}}]}


_LATE_TOOL_RESULT = "LATE-TOOL-RESULT-SENTINEL"
_DRAFT_CALLS: list[dict] = []   # keyword arguments the loop passed to prepare_draft


def _install_authoring_stub(registry, monkeypatch):
    """Replace create_tool with a stub that registers a fresh skill-defined tool, the way
    the real one does after writing SKILL.md and reloading skills. Returns a restorer.
    The loop drafts the code with a model before running the tool, so that seam is faked
    too: no model is called."""
    # Import the real module first: it registers the real create_tool when first imported,
    # which would otherwise overwrite this stub later and run a model-backed authoring call.
    from service.tools import tool_authoring

    async def no_draft_error(args, **kwargs):
        _DRAFT_CALLS.append(kwargs)
        return ""

    monkeypatch.setattr(tool_authoring, "prepare_draft", no_draft_error)
    original = registry.REGISTRY.get("create_tool")

    async def late_tool(**kwargs):
        return _LATE_TOOL_RESULT

    async def create_tool(name: str = "", **kwargs):
        registry.register(name, "A synthetic tool a skill created in the middle of this turn.",
                          {"type": "object", "properties": {}, "required": []},
                          category="skill_tool")(late_tool)
        return f"created {name}"

    registry.register("create_tool", "Create a reusable tool (synthetic stand-in).",
                      {"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"]},
                      category="assistant_read")(create_tool)

    def restore():
        if original is not None:
            registry.REGISTRY["create_tool"] = original
        else:
            registry.REGISTRY.pop("create_tool", None)
        registry.REGISTRY.pop("late_skill_tool", None)
    return restore


def _run_skill_turn(monkeypatch, tmp_path, prompt, *, role="agent", skills_map="all",
                    external_script=None, managed_script=None, prior_tool_digest=None,
                    authoring_stub=False, force_first_tool=None,
                    tools=("probe_noop", "use_skill", "probe_skill_tool")):
    """Drive the real /agent endpoint for a tool route bound to an external provider."""
    import service.main as main
    from service import skills as skills_module
    from service.config.endpoints import Endpoint
    from service.memory import context
    from service.memory.store import SessionStore
    from service.router.router import RouteDecision
    from service.skills import tools as _skill_tools  # noqa: F401  (registers use_skill)
    from service.tools import registry

    external_target = Target(role, endpoint_from_config("local_provider", _endpoint_cfg()),
                             "ext-model", context_window=8192)
    managed_target = Target(role, Endpoint("local", "http://127.0.0.1:8000", "local_omlx",
                            managed=True), "managed-model", context_window=8192)
    external = _ScriptedClient(managed=False, script=external_script, target=external_target)
    managed = _ScriptedClient(managed=True, script=managed_script, target=managed_target)
    saved = SessionStore(tmp_path / "sessions.db")
    sid = saved.create_session()
    if prior_tool_digest:
        saved.add_turn(sid, "user", "load that skill for me")
        saved.add_turn(sid, "assistant", "Loaded.", tool_digest=prior_tool_digest)

    async def routed(*args, **kwargs):
        return RouteDecision(role, "ext-model", True, "rules", "fixture",
                             tool_subset=list(tools), force_first_tool=force_first_tool)

    async def ready():
        pass

    async def noop(**kwargs):
        return "NOOP-RESULT"

    restore_authoring = _install_authoring_stub(registry, monkeypatch) if authoring_stub else (lambda: None)
    assert "use_skill" in registry.REGISTRY, "the real use_skill tool must be registered"
    registry.register("probe_noop", "A harmless synthetic read-only probe.",
                      {"type": "object", "properties": {}, "required": []},
                      category="assistant_read")(noop)
    registry.register("probe_skill_tool", "A synthetic tool defined by an installed skill.",
                      {"type": "object", "properties": {}, "required": []},
                      category="skill_tool")(noop)
    monkeypatch.setattr(skills_module, "_skills",
                        _synthetic_skills() if skills_map == "all" else dict(skills_map or {}))
    monkeypatch.setattr(main, "client", managed, raising=False)
    monkeypatch.setattr(main, "store", saved)
    monkeypatch.setattr(context, "store", saved)
    monkeypatch.setattr(main, "models_config", lambda: {
        "tool_retrieval": {"provider": "lexical"},
        "inference": {"bindings": {role: {"endpoint": "local_provider"}}}})
    monkeypatch.setattr(main, "route", routed)
    monkeypatch.setattr(main, "role_target", lambda r: external_target)
    monkeypatch.setattr(main, "local_role_target", lambda r: managed_target)
    monkeypatch.setattr(main, "cloud_super_model_enabled", lambda: False)
    monkeypatch.setattr(main, "ensure_omlx", ready)
    monkeypatch.setattr(main, "memory_block", lambda **kwargs: "")
    monkeypatch.setattr(main, "OMLXClient", lambda **kwargs: external)

    async def exercise():
        response = await main.agent({"prompt": prompt, "session_id": sid, "debug": False})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes):
                item = item.decode()
            events.append(json.loads(item.removeprefix("data: ").strip()))
        return events

    try:
        events = asyncio.run(asyncio.wait_for(exercise(), 30))
    finally:
        registry.REGISTRY.pop("probe_noop", None)
        registry.REGISTRY.pop("probe_skill_tool", None)
        restore_authoring()
    routed_event = next((e for e in events if e["type"] == "routed"), None)
    assert not [e for e in events if e["type"] == "error"], events
    return events, routed_event, external, managed


def _everything_sent(client) -> str:
    return json.dumps(client.requests)


@pytest.mark.parametrize("role", ["agent", "coding"])
def test_ordinary_triggered_skill_stays_on_the_managed_model(monkeypatch, tmp_path, role) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "please file my expense report", role=role)
    assert routed_event["model"] == "managed-model"
    assert routed_event["route_source"] == "skill_local"
    assert external.requests == [], "the external app received a skill-bearing turn"
    sent = _everything_sent(managed)
    assert _BODY_ORDINARY in sent, "the managed model must still receive the skill it was asked for"


def test_active_conversational_workflow_still_stays_on_the_managed_model(
        monkeypatch, tmp_path) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "let us explore this idea")
    assert routed_event["model"] == "managed-model"
    assert routed_event["route_source"] == "active_skill_local"
    assert external.requests == []
    assert _BODY_WORKFLOW in _everything_sent(managed)


@pytest.mark.parametrize("role", ["agent", "coding"])
def test_untriggered_turn_reaches_the_external_app_with_no_skill_content(
        monkeypatch, tmp_path, role) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "run the noop probe", role=role,
        external_script=[_call("probe_noop"),
                         {"role": "assistant", "content": "The probe finished."}])
    assert routed_event["model"] == "ext-model"
    assert routed_event["route_source"] != "skill_local"
    assert managed.requests == []
    assert len(external.requests) >= 2, "the healthy external tool turn must still run its tool"
    sent = _everything_sent(external)
    for sentinel in _SKILL_SENTINELS:
        assert sentinel not in sent, f"{sentinel!r} reached the external transport"
    for request in external.requests:
        assert "probe_noop" in request["tools"]
        assert "use_skill" not in request["tools"], "use_skill was offered to the external app"
        assert "probe_skill_tool" not in request["tools"], "a skill-defined tool was offered"
    assert "NOOP-RESULT" in sent, "the external model received its ordinary tool result"


def test_external_model_cannot_load_a_skill_by_calling_use_skill(monkeypatch, tmp_path) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "run the noop probe",
        external_script=[_call("use_skill", {"name": "quiet-helper"}),
                         {"role": "assistant", "content": "Done."}])
    assert routed_event["model"] == "ext-model"
    sent = _everything_sent(external)
    assert _BODY_QUIET not in sent and _BODY_ORDINARY not in sent
    assert not [e for e in events if e["type"] == "tool_result"
                and "BODY-SENTINEL" in json.dumps(e)]


def test_external_model_cannot_run_a_skill_defined_tool(monkeypatch, tmp_path) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "run the noop probe",
        external_script=[_call("probe_skill_tool"),
                         {"role": "assistant", "content": "Done."}])
    assert routed_event["model"] == "ext-model"
    assert "NOOP-RESULT" not in _everything_sent(external), "a skill-defined tool ran for the external app"


@pytest.mark.parametrize("digest", ["use_skill", "get_weather, use_skill"])
def test_follow_up_after_use_skill_stays_on_the_managed_model(
        monkeypatch, tmp_path, digest) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "ok, continue", prior_tool_digest=digest)
    assert routed_event["model"] == "managed-model"
    assert routed_event["route_source"] == "skill_local"
    assert external.requests == []


def test_follow_up_after_an_unrelated_tool_is_not_treated_as_a_skill_turn(
        monkeypatch, tmp_path) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "ok, continue", prior_tool_digest="get_weather",
        external_script=[{"role": "assistant", "content": "Continuing."}])
    assert routed_event["model"] == "ext-model" and managed.requests == []
    assert external.requests


def test_external_tools_work_normally_when_no_skills_are_installed(monkeypatch, tmp_path) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "run the noop probe", skills_map={},
        external_script=[_call("probe_noop"),
                         {"role": "assistant", "content": "The probe finished."}])
    assert routed_event["model"] == "ext-model" and managed.requests == []
    assert "NOOP-RESULT" in _everything_sent(external)


def test_skill_helpers_recognise_only_enabled_matching_skills_and_skill_digests(monkeypatch) -> None:
    from service import skills as skills_module
    pool = _synthetic_skills()
    pool["disabled-helper"] = skills_module.Skill(
        "disabled-helper", "d", "b", _Path("/nonexistent/d"), triggers=["disabled phrase"], enabled=False)
    pool["broken-helper"] = skills_module.Skill(
        "broken-helper", "d", "b", _Path("/nonexistent/b"), triggers=["broken phrase"], error="bad")
    monkeypatch.setattr(skills_module, "_skills", pool)
    assert skills_module.turn_skill_names("file my Expense Report") == ["expense-helper"]
    assert skills_module.turn_skill_names("use @quiet-helper now") == ["quiet-helper"]
    assert skills_module.turn_skill_names("disabled phrase and broken phrase") == []
    assert skills_module.turn_skill_names("nothing relevant") == []
    assert skills_module.digest_used_skill("use_skill")
    assert skills_module.digest_used_skill("get_weather, use_skill")
    assert not skills_module.digest_used_skill("get_weather, get_upcoming")
    assert not skills_module.digest_used_skill("") and not skills_module.digest_used_skill(None)


def test_the_loop_boundary_is_scoped_to_the_local_provider_only(monkeypatch) -> None:
    """Cloud-bound and managed runs keep their existing skills behaviour."""
    from service import skills as skills_module
    from service.agent import loop
    from service.memory import identity

    monkeypatch.setattr(skills_module, "_skills", _synthetic_skills())
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr(loop.prompt_blocks, "memory_block", lambda **kwargs: "")

    class Approver:
        async def confirm(self, action):
            return True

    def sent_to(managed: bool, endpoint_name: str) -> str:
        client = _ScriptedClient(managed=managed)
        client.endpoint_name = endpoint_name

        async def emit(event):
            pass

        asyncio.run(loop.run_agent(client, "model", [{"role": "user", "content": "hello there"}],
                                   emit, Approver(), debug=False, max_steps=1))
        return json.dumps(client.requests)

    assert _CATALOG_QUIET in sent_to(True, "local"), "a managed run keeps the skill catalog"
    assert _CATALOG_QUIET in sent_to(False, "cloud"), "a cloud-bound run is unchanged by this repair"
    assert _CATALOG_QUIET not in sent_to(False, "local_provider")


# ---------------- PR130 E1: the skill boundary must survive a mid-turn tool registration
#
# create_tool writes a skill and reloads the registry, and the loop then rebuilds its
# schemas so the new tool is usable in the same turn. On the external local provider that
# rebuild must not admit a skill-defined tool, and neither the rebuilt menu nor dispatch
# may let one through: the protection is evaluated when a tool is offered or called, not
# from a snapshot taken at the start of the turn.

_AUTHORING_TOOLS = ("create_tool", "probe_noop", "probe_skill_tool")
_AUTHORING_PROMPT = "make me a reusable tool called late_skill_tool"


def test_a_tool_registered_mid_turn_is_never_offered_or_run_on_the_external_app(
        monkeypatch, tmp_path) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, _AUTHORING_PROMPT, authoring_stub=True, tools=_AUTHORING_TOOLS,
        external_script=[_call("create_tool", {"name": "late_skill_tool"}),
                         _call("late_skill_tool", call_id="call-2"),
                         {"role": "assistant", "content": "All done."}])
    assert routed_event["model"] == "ext-model" and managed.requests == []
    assert len(external.requests) >= 2, "the authoring turn must still run on the external app"
    assert "create_tool" in external.requests[0]["tools"], "ordinary authoring stays available"
    for request in external.requests:
        assert "late_skill_tool" not in request["tools"], "a skill tool registered mid-turn was offered"
        assert "probe_skill_tool" not in request["tools"], "an existing skill tool came back in the rebuild"
        assert "use_skill" not in request["tools"]
    assert _LATE_TOOL_RESULT not in _everything_sent(external), "a mid-turn skill tool ran for the external app"
    assert not [e for e in events if e.get("type") == "tool_call" and e.get("name") == "late_skill_tool"
                and e.get("decision") == "allow"], events


def test_managed_authoring_still_offers_and_runs_the_fresh_tool(monkeypatch, tmp_path) -> None:
    # The same authoring turn on the managed model keeps working: the fresh tool is usable
    # in the same turn. The prompt triggers the ordinary expense skill so the turn is managed.
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "build me a reusable tool for my expense report called late_skill_tool",
        authoring_stub=True, tools=_AUTHORING_TOOLS,
        managed_script=[_call("create_tool", {"name": "late_skill_tool"}),
                        _call("late_skill_tool", call_id="call-2"),
                        {"role": "assistant", "content": "All done."}])
    assert routed_event["model"] == "managed-model" and external.requests == []
    assert "late_skill_tool" in managed.requests[1]["tools"], "managed authoring lost the same-turn tool"
    assert _LATE_TOOL_RESULT in _everything_sent(managed)


def test_authoring_on_the_external_app_works_for_ordinary_tools_after_the_rebuild(
        monkeypatch, tmp_path) -> None:
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, _AUTHORING_PROMPT, authoring_stub=True, tools=_AUTHORING_TOOLS,
        external_script=[_call("create_tool", {"name": "late_skill_tool"}),
                         _call("probe_noop", call_id="call-2"),
                         {"role": "assistant", "content": "All done."}])
    assert routed_event["model"] == "ext-model"
    after_rebuild = external.requests[1]["tools"]
    assert "probe_noop" in after_rebuild and "create_tool" in after_rebuild
    assert "NOOP-RESULT" in _everything_sent(external), "an ordinary tool must still run after the rebuild"


def test_router_direct_skill_tool_call_is_not_run_for_the_external_app(monkeypatch) -> None:
    """Defensive: no currently reachable route emits one, but the predicate covers the path."""
    from service import skills as skills_module
    from service.agent import loop
    from service.memory import identity
    from service.tools import registry

    ran = []

    async def skill_tool(**kwargs):
        ran.append("skill")
        return "DIRECT-SKILL-RESULT"

    async def ordinary(**kwargs):
        ran.append("ordinary")
        return "DIRECT-ORDINARY-RESULT"

    schema = {"type": "object", "properties": {}, "required": []}
    registry.register("probe_direct_skill", "synthetic skill tool", schema,
                      category="skill_tool")(skill_tool)
    registry.register("probe_direct_ordinary", "synthetic ordinary tool", schema,
                      category="assistant_read")(ordinary)
    monkeypatch.setattr(skills_module, "_skills", {})
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr(loop.prompt_blocks, "memory_block", lambda **kwargs: "")

    class Approver:
        async def confirm(self, action):
            return True

    def run_on(managed: bool) -> list:
        client = _ScriptedClient(managed=managed)
        events = []

        async def emit(event):
            events.append(event)

        asyncio.run(loop.run_agent(
            client, "model", [{"role": "user", "content": "go"}], emit, Approver(), debug=False,
            max_steps=2, tools=["probe_direct_skill", "probe_direct_ordinary"],
            direct_calls=[("probe_direct_skill", {}), ("probe_direct_ordinary", {})]))
        return events

    try:
        ran.clear()
        run_on(False)
        assert ran == ["ordinary"], "the external run must skip the skill tool and keep the ordinary one"
        ran.clear()
        run_on(True)
        assert sorted(ran) == ["ordinary", "skill"], "a managed run is unchanged"
    finally:
        registry.REGISTRY.pop("probe_direct_skill", None)
        registry.REGISTRY.pop("probe_direct_ordinary", None)


def test_a_tool_reclassified_as_a_skill_tool_within_a_step_is_refused_at_dispatch(
        monkeypatch) -> None:
    """The menu for a step is fixed before its calls run. If an earlier call in the same
    step re-registers an OFFERED ordinary tool as a skill tool, the later call to it must
    be refused at dispatch on the external app, even though it is still in that step's
    offered names."""
    from service import skills as skills_module
    from service.agent import loop
    from service.memory import identity
    from service.tools import registry

    async def ordinary(**kwargs):
        return "ORDINARY-RESULT-SENTINEL"

    schema = {"type": "object", "properties": {}, "required": []}
    registry.register("reclass_tool", "synthetic ordinary tool", schema,
                      category="assistant_read")(ordinary)
    restore_authoring = _install_authoring_stub(registry, monkeypatch)
    monkeypatch.setattr(skills_module, "_skills", {})
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr(loop.prompt_blocks, "memory_block", lambda **kwargs: "")

    class Approver:
        async def confirm(self, action):
            return True

    two_calls = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call-1", "type": "function",
         "function": {"name": "create_tool", "arguments": json.dumps({"name": "reclass_tool"})}},
        {"id": "call-2", "type": "function",
         "function": {"name": "reclass_tool", "arguments": "{}"}}]}

    def run_on(managed: bool):
        client = _ScriptedClient(managed=managed, script=[_copy.deepcopy(two_calls),
                                                          {"role": "assistant", "content": "Done."}])
        events = []

        async def emit(event):
            events.append(event)

        asyncio.run(loop.run_agent(
            client, "model", [{"role": "user", "content": "go"}], emit, Approver(), debug=False,
            max_steps=3, tools=["create_tool", "reclass_tool"]))
        return client, events

    try:
        external, events = run_on(False)
        sent = _everything_sent(external)
        assert "ORDINARY-RESULT-SENTINEL" not in sent and _LATE_TOOL_RESULT not in sent, \
            "a tool reclassified as a skill tool mid-step ran for the external app"
        refused = [e for e in events if e.get("type") == "tool_result" and e.get("id") == "call-2"]
        assert refused and "is not available in this conversation" in refused[0]["result"]
    finally:
        restore_authoring()
        registry.REGISTRY.pop("reclass_tool", None)


# ------- review findings on the skill boundary: skill metadata tools and tool drafting

def test_listing_installed_skills_runs_on_the_managed_model(monkeypatch, tmp_path) -> None:
    """'List my installed skills' forces wisp_skills. Its answer is skill metadata, so the
    turn stays managed even though the prompt triggers no skill."""
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "list my installed skills", tools=("wisp_skills",),
        force_first_tool="wisp_skills",
        managed_script=[_call("wisp_skills"), {"role": "assistant", "content": "Here they are."}])
    assert routed_event["model"] == "managed-model"
    assert routed_event["route_source"] == "skill_local"
    assert external.requests == [], "the external app received a skill-listing turn"
    assert "expense-helper" in _everything_sent(managed), "the managed model still gets the listing"


def test_the_skill_listing_tool_is_never_offered_or_run_for_the_external_app(
        monkeypatch, tmp_path) -> None:
    """Even from a broad menu on a turn that stays external, wisp_skills is not offered, and
    a call to it from memory is refused with nothing about skills in the reply."""
    events, routed_event, external, managed = _run_skill_turn(
        monkeypatch, tmp_path, "run the noop probe", tools=("probe_noop", "wisp_skills"),
        external_script=[_call("wisp_skills"), {"role": "assistant", "content": "Done."}])
    assert routed_event["model"] == "ext-model" and managed.requests == []
    for request in external.requests:
        assert "wisp_skills" not in request["tools"] and "probe_noop" in request["tools"]
    sent = _everything_sent(external)
    for name in ("expense-helper", "quiet-helper", "idea-refine"):
        assert name not in sent, f"{name} reached the external app through the skill listing"


def test_skill_boundary_names_cover_the_listing_tool(monkeypatch) -> None:
    from service import skills as skills_module
    from service.tools import registry
    from service.tools import misc_t1  # noqa: F401  (registers wisp_skills)
    assert "wisp_skills" in registry.REGISTRY
    assert {"use_skill", "wisp_skills"} <= skills_module.skill_tool_names()
    assert skills_module.digest_used_skill("get_weather, wisp_skills")


def test_the_loop_drafts_on_the_managed_model_only_for_a_managed_turn(monkeypatch, tmp_path) -> None:
    _DRAFT_CALLS.clear()
    _run_skill_turn(
        monkeypatch, tmp_path,
        "build me a reusable tool for my expense report called late_skill_tool",
        authoring_stub=True, tools=_AUTHORING_TOOLS,
        managed_script=[_call("create_tool", {"name": "late_skill_tool"}),
                        {"role": "assistant", "content": "Done."}])
    assert _DRAFT_CALLS and all(call.get("managed_only") is True for call in _DRAFT_CALLS), \
        "a managed (skill) turn must draft tool code on the managed model"
    _DRAFT_CALLS.clear()
    _run_skill_turn(
        monkeypatch, tmp_path, _AUTHORING_PROMPT, authoring_stub=True, tools=_AUTHORING_TOOLS,
        external_script=[_call("create_tool", {"name": "late_skill_tool"}),
                         {"role": "assistant", "content": "Done."}])
    assert _DRAFT_CALLS and all(call.get("managed_only") is False for call in _DRAFT_CALLS), \
        "an ordinary external turn keeps drafting where it always did"


def test_tool_drafting_never_opens_the_external_app_for_a_managed_turn(monkeypatch) -> None:
    """The real drafting code, not a stub: with managed_only the task text goes to the
    managed model even though the Agent role is bound to the external app."""
    from service.config.endpoints import Endpoint
    from service.tools import tool_authoring

    external_target = Target("agent", endpoint_from_config("local_provider", _endpoint_cfg()),
                             "ext-model", context_window=8192)
    managed_target = Target("agent", Endpoint("local", "http://127.0.0.1:8000", "local_omlx",
                            managed=True), "managed-model", context_window=8192)
    reached: list[str] = []

    class Fake:
        def __init__(self, label):
            self.label = label

        async def ensure_only(self, *args, **kwargs):
            pass

        async def chat(self, model, messages, **kwargs):
            reached.append(f"{self.label}:{model}:{json.dumps(messages)}")
            return {"choices": [{"message": {"content": "import sys\nprint(sys.argv)\n"}}]}

        async def aclose(self):
            pass

    monkeypatch.setattr(tool_authoring, "role_target", lambda role: external_target)
    monkeypatch.setattr(tool_authoring, "local_role_target", lambda role: managed_target)
    monkeypatch.setattr(tool_authoring, "_c", lambda: Fake("managed"))
    monkeypatch.setattr(tool_authoring, "OMLXClient", lambda **kwargs: Fake("external"))

    asyncio.run(tool_authoring._generate("x_tool", "TASK-SENTINEL-FROM-A-SKILL", [], None, None,
                                         managed_only=True))
    assert len(reached) == 1 and reached[0].startswith("managed:managed-model:")
    assert "TASK-SENTINEL-FROM-A-SKILL" in reached[0]
    reached.clear()
    asyncio.run(tool_authoring._generate("x_tool", "TASK-SENTINEL-ORDINARY", [], None, None))
    assert len(reached) == 1 and reached[0].startswith("external:ext-model:"), \
        "without managed_only the drafting keeps its existing target"


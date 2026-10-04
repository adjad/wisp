"""Tool use on a non-oMLX local inference app is earned by a measured probe.

Every engine here is an in-process fake answering through a patched httpx
transport: no socket is opened (CI runs these under a sandbox that denies all
network, loopback included), and nothing touches the user's real apps, Keychain
or data. The fake reproduces the failure modes seen on real engines: silent
context truncation, tool calls written as text, and a missing ``usage`` block.
"""
import asyncio
import itertools
import json
import socket
import subprocess
from copy import deepcopy

import httpx
import pytest

from service import config
from service.config import endpoints
from service.config.endpoints import Target, endpoint_from_config, role_target
from service.inference import qualify as q


# --------------------------------------------------------------- fake engine

class FakeEngine:
    """Knobs: ctx (silently truncates the START of long prompts), tools mode
    ('structured' | 'text' | 'none'), usage (report prompt_tokens or not)."""

    def __init__(self, ctx=1_000_000, tools="structured", usage=True, reject_over_ctx=False,
                 call_for_hello=False, long_answer=0, api_prefix="/v1"):
        self.ctx, self.tools, self.usage = ctx, tools, usage
        self.reject_over_ctx, self.call_for_hello = reject_over_ctx, call_for_hello
        self.long_answer = long_answer
        self.api_prefix = api_prefix
        self.requests = []
        self.wire_requests = []
        self.port = next(_PORTS)
        self.closed = False

    def close(self):
        self.closed = True

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Answer one HTTP request exactly as an OpenAI-compatible server would."""
        self.wire_requests.append((request.method, request.url.path))
        if self.closed:
            raise httpx.ConnectError("connection refused", request=request)
        if request.method == "GET" and request.url.path == self.api_prefix + "/models":
            return httpx.Response(200, json={"object": "list",
                                             "data": [{"id": "fake-model", "object": "model"}]})
        if request.method == "POST" and request.url.path == self.api_prefix + "/chat/completions":
            body = json.loads(request.content)
            self.requests.append(body)
            code, payload, stream = self.complete(body)
            content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            return httpx.Response(code, stream=httpx.ByteStream(content), headers={
                "content-type": "text/event-stream" if stream else "application/json"})
        return httpx.Response(404, json={"error": "nope"})

    # tokens ~ chars/4, like a real tokenizer on prose
    @staticmethod
    def _tokens(messages):
        return sum(len(str(m.get("content") or "")) for m in messages) // 4 + 8 * len(messages)

    def complete(self, body):
        messages = body["messages"]
        total = self._tokens(messages)
        if self.reject_over_ctx and total > self.ctx:
            return 400, {"error": {"message": "context length exceeded"}}, False
        seen = min(total, self.ctx)
        truncated = total > self.ctx
        text = " ".join(str(m.get("content") or "") for m in messages)
        if truncated:  # the START of the prompt is what gets dropped
            text = text[-self.ctx * 4:]
        message, finish = {"role": "assistant", "content": "OK"}, "stop"
        last = messages[-1]
        wants_tool = bool(body.get("tools")) and "probe_multiply" in text
        if last.get("role") == "tool":
            message = {"role": "assistant", "content": "17 times 23 is 391."}
        elif wants_tool and last.get("role") == "user" and "17 times 23" in str(last.get("content")):
            if self.tools == "structured":
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "probe_multiply", "arguments": json.dumps({"a": 17, "b": 23})}}]}
                finish = "tool_calls"
            elif self.tools == "text":
                message = {"role": "assistant", "content":
                           '<tool_call>{"name": "probe_multiply", "arguments": {"a": 17, "b": 23}}</tool_call>'}
            else:
                message = {"role": "assistant", "content": "That is 391."}
        elif "secret code" in str(last.get("content")) and "Answer with the code only" in str(last.get("content")):
            import re
            found = re.search(r"ZEBRA-\d+", text)
            message = {"role": "assistant", "content": found.group(0) if found else "I do not know."}
        elif body.get("tools") and self.call_for_hello and "hello" in str(last.get("content")):
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_2", "type": "function",
                "function": {"name": "probe_multiply", "arguments": json.dumps({"a": 1, "b": 1})}}]}
            finish = "tool_calls"
        elif "hello" in str(last.get("content")):
            message = {"role": "assistant", "content": "hello"}
        if self.long_answer and body.get("stream"):
            message = {"role": "assistant", "content": "w " * self.long_answer}
        usage = {"prompt_tokens": seen, "completion_tokens": 3, "total_tokens": seen + 3} if self.usage else None
        if body.get("stream"):
            return 200, self._sse(message, finish, usage), True
        payload = {"id": "x", "object": "chat.completion", "model": "fake-model",
                   "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
        if usage:
            payload["usage"] = usage
        return 200, payload, False

    @staticmethod
    def _sse(message, finish, usage):
        def event(delta, reason=None):
            return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "fake-model",
                                          "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}) + "\n\n"
        out = [event({"role": "assistant"})]
        for call in message.get("tool_calls") or []:
            args = call["function"]["arguments"]
            out.append(event({"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                               "function": {"name": call["function"]["name"], "arguments": ""}}]}))
            for i in range(0, len(args), 6):  # fragmented arguments, as real engines stream them
                out.append(event({"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 6]}}]}))
        if message.get("content"):
            words = message["content"].split(" ") if len(message["content"]) > 4000 else None
            if words:  # one token per event, as real engines stream a long answer
                out.extend(event({"content": w + " "}) for w in words if w)
            else:
                out.append(event({"content": message["content"]}))
        out.append(event({}, finish))
        out.append("data: [DONE]\n\n")
        return "".join(out).encode()


class SixCharactersPerToken(FakeEngine):
    """Auditor counterexample: no universal characters-to-tokens lower bound."""

    @staticmethod
    def _tokens(messages):
        return sum(len(str(m.get("content") or "")) for m in messages) // 6 + 8 * len(messages)

    def complete(self, body):
        if self._tokens(body["messages"]) > self.ctx and not self.reject_over_ctx:
            body = {**body, "messages": [dict(m) for m in body["messages"]]}
            for message in body["messages"]:
                message["content"] = str(message.get("content") or "")[-max(0, (self.ctx - 8) * 6):]
        return super().complete(body)


class ArgumentEngine(FakeEngine):
    """Vary returned arguments independently in plain and fragmented calls."""

    def __init__(self, *, arguments, phase, **knobs):
        super().__init__(**knobs)
        self.arguments, self.phase = arguments, phase

    def complete(self, body):
        code, payload, _ = super().complete({**body, "stream": False})
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        is_stream = bool(body.get("stream"))
        if calls and is_stream == (self.phase == "stream"):
            calls[0]["function"]["arguments"] = self.arguments
        if is_stream and code == 200:
            return code, self._sse(message, choice.get("finish_reason"), payload.get("usage")), True
        return code, payload, False


INVALID_ARGUMENTS = [
    pytest.param('{"x":17,"y":23}', id="wrong-keys"),
    pytest.param('{"a":17}', id="missing-b"),
    pytest.param('{"b":23}', id="missing-a"),
    pytest.param('{"a":17,"b":23,"extra":0}', id="extra-key"),
    pytest.param('{"a":17.0,"b":23}', id="float-a"),
    pytest.param('{"a":17,"b":23.0}', id="float-b"),
    pytest.param('{"a":true,"b":23}', id="bool-a"),
    pytest.param('{"a":17,"b":true}', id="bool-b"),
    pytest.param('{"a":"17","b":23}', id="string"),
    pytest.param('{"a":null,"b":23}', id="null-value"),
    pytest.param('[17,23]', id="array"),
    pytest.param('null', id="null-object"),
    pytest.param('{"a":17,"b":', id="malformed-json"),
    pytest.param('{"a":18,"b":23}', id="wrong-operand"),
]


_PORTS = itertools.count(18100)
_ENGINES: dict[int, FakeEngine] = {}


async def _dispatch(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "127.0.0.1", "Only the synthetic loopback engine is allowed"
    engine = _ENGINES.get(request.url.port)
    if engine is None:
        raise httpx.ConnectError("connection refused", request=request)
    return engine.respond(request)


@pytest.fixture
def engine_factory(monkeypatch):
    """Route every httpx request to the fake engines; open no sockets."""
    from service.inference.attributed_transport import CredentialTransport
    from service.config import credentials, provider_credentials

    def forbidden(*args, **kwargs):
        raise AssertionError("Qualification fixtures forbid real sockets, processes and credentials")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(config, "omlx_api_key", forbidden)
    monkeypatch.setattr(config, "_credential", forbidden)
    monkeypatch.setattr(credentials, "resolve", forbidden)
    monkeypatch.setattr(provider_credentials, "resolve_keychain", forbidden)

    transport = httpx.MockTransport(_dispatch)

    async def plain(self, request):
        return await transport.handle_async_request(request)

    async def credentialed(self, request):
        return await transport.handle_async_request(request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", plain)
    monkeypatch.setattr(CredentialTransport, "handle_async_request", credentialed)

    def make(*, engine_type=FakeEngine, **knobs):
        engine = engine_type(**knobs)
        _ENGINES[engine.port] = engine
        ep = endpoint_from_config("local_provider", {
            "enabled": True, "provider": "openai-compatible", "base_url": f"http://127.0.0.1:{engine.port}",
            "api_prefix": engine.api_prefix, "credential_ref": "none", "readiness_timeout": 10})
        return engine, ep
    yield make
    _ENGINES.clear()


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- probe

def test_healthy_engine_qualifies(engine_factory):
    _, ep = engine_factory()
    report = run(q.qualify(ep, "fake-model", 16384))
    assert report.qualified, report.as_dict()
    assert report.effective_context == 16384
    assert {c.id for c in report.checks} == {"context", "call", "result", "stream", "restraint"}


@pytest.mark.parametrize("ctx", [2048, 4096])
def test_silent_truncation_is_measured_and_blocks_tools(engine_factory, ctx):
    """The Ollama failure: a 4k default window silently drops the prompt's start."""
    _, ep = engine_factory(ctx=ctx)
    report = run(q.qualify(ep, "fake-model", 16384))
    assert not report.qualified
    assert 0 < report.effective_context <= ctx
    assert "silently cutting prompts" in report.checks[0].detail
    assert str(16384) in report.hint and "context" in report.hint.lower()
    assert len(report.checks) == 1  # no point testing tools inside a window that cannot hold them


def test_engine_that_rejects_oversize_prompts_is_measured_by_stepping_down(engine_factory):
    _, ep = engine_factory(ctx=6000, reject_over_ctx=True)
    report = run(q.qualify(ep, "fake-model", 16384))
    assert not report.qualified and 0 < report.effective_context < q.MIN_TOOL_CONTEXT


@pytest.mark.parametrize("ctx", [16384, 15000, 14000, 11111, 9001, 8192])
def test_recorded_window_never_exceeds_a_truncating_engines_real_window(engine_factory, ctx):
    """Regression: a probe at 90% of the cap passed on 14k/15k engines, and the
    whole claimed 16,384 was recorded, so later turns overfilled the engine."""
    _, ep = engine_factory(ctx=ctx)
    report = run(q.qualify(ep, "fake-model", 16384))
    assert report.effective_context <= ctx, report.as_dict()
    assert report.effective_context > ctx - 256  # conservative, but not needlessly so
    assert report.qualified, report.as_dict()


def test_a_full_16k_engine_still_records_16k(engine_factory):
    for knobs in ({"ctx": 16384}, {"ctx": 1_000_000}):
        _, ep = engine_factory(**knobs)
        report = run(q.qualify(ep, "fake-model", 32768))
        assert report.qualified and report.effective_context == 16384, (knobs, report.as_dict())


@pytest.mark.parametrize("ctx", [16384, 15000, 14000, 9001])
def test_recorded_window_never_exceeds_a_rejecting_engines_real_window(engine_factory, ctx):
    _, ep = engine_factory(ctx=ctx, reject_over_ctx=True)
    report = run(q.qualify(ep, "fake-model", 16384))
    assert 0 < report.effective_context <= ctx, report.as_dict()
    assert report.qualified == (report.effective_context >= q.MIN_TOOL_CONTEXT)


@pytest.mark.parametrize("ctx", [16384, 15000, 14000, 8192, 4096])
def test_recorded_window_without_usage_never_exceeds_the_real_window(engine_factory, ctx):
    _, ep = engine_factory(ctx=ctx, usage=False)
    report = run(q.qualify(ep, "fake-model", 16384))
    assert report.effective_context == 0, report.as_dict()
    assert not report.qualified


@pytest.mark.parametrize("knobs,wording", [({"ctx": 4096}, "silently cutting prompts to about 4,096"),
                                           ({"ctx": 7000}, "silently cutting prompts to about 6,912"),
                                           ({"ctx": 6000, "reject_over_ctx": True}, "refuses prompts longer")])
def test_a_window_below_the_minimum_fails_in_plain_language(engine_factory, knobs, wording):
    _, ep = engine_factory(**knobs)
    report = run(q.qualify(ep, "fake-model", 16384))
    assert not report.qualified and report.effective_context <= knobs["ctx"]
    detail = report.checks[0].detail
    assert wording in detail and f"at least {q.MIN_TOOL_CONTEXT:,}" in detail, detail
    assert "16384" in report.hint


def test_a_smaller_claim_is_never_recorded_above_itself(engine_factory):
    _, ep = engine_factory()
    report = run(q.qualify(ep, "fake-model", 12000))
    assert report.qualified and report.effective_context <= 12000


@pytest.mark.parametrize("engine_type,ctx", [(FakeEngine, 1_000_000), (SixCharactersPerToken, 7000)])
def test_missing_usage_cannot_establish_a_verified_window(engine_factory, engine_type, ctx):
    engine, ep = engine_factory(engine_type=engine_type, usage=False, ctx=ctx)
    report = run(q.qualify(ep, "fake-model", 16384))
    assert not report.qualified and report.effective_context == 0, report.as_dict()
    assert "token usage" in report.checks[0].detail and "verify" in report.hint
    assert not any(request.get("tools") for request in engine.requests)


def _assert_argument_rejection(report, phase):
    assert not report.qualified, report.as_dict()
    check = next((c for c in report.checks if c.id == ("stream" if phase == "stream" else "call")), None)
    if check is None:
        # The production stream parser rejects malformed/nonobject arguments
        # before qualification can append its schema check.
        assert phase == "stream"
        error = next(c for c in report.checks if c.id == "error")
        assert not error.ok and "IncompleteStreamError" in error.detail
    else:
        assert not check.ok


@pytest.mark.parametrize("phase", ["nonstream", "stream"])
@pytest.mark.parametrize("arguments", INVALID_ARGUMENTS)
def test_invalid_arguments_cannot_qualify_in_either_call_path(engine_factory, phase, arguments):
    engine, ep = engine_factory(engine_type=ArgumentEngine, arguments=arguments, phase=phase)
    report = run(q.qualify(ep, "fake-model", 16384))
    _assert_argument_rejection(report, phase)
    results = [m for request in engine.requests for m in request["messages"] if m["role"] == "tool"]
    # A stream-only failure follows a separate, valid nonstream call. Invalid
    # nonstream calls must never receive the fabricated successful result.
    assert len(results) == (1 if phase == "stream" else 0)


@pytest.mark.parametrize("phase", ["nonstream", "stream"])
@pytest.mark.parametrize("arguments", ['{"b":23,"a":17}', '{"a":23,"b":17}'])
def test_valid_named_integer_operands_work_in_both_call_paths(engine_factory, phase, arguments):
    _, ep = engine_factory(engine_type=ArgumentEngine, arguments=arguments, phase=phase)
    assert run(q.qualify(ep, "fake-model", 16384)).qualified


def test_tool_calls_written_as_text_are_diagnosed(engine_factory):
    _, ep = engine_factory(tools="text")
    report = run(q.qualify(ep, "fake-model", 16384))
    assert not report.qualified
    call = next(c for c in report.checks if c.id == "call")
    assert not call.ok and "--jinja" in call.detail and "text" in call.detail


def test_model_that_never_calls_the_tool_fails(engine_factory):
    _, ep = engine_factory(tools="none")
    report = run(q.qualify(ep, "fake-model", 16384))
    assert not report.qualified
    assert not next(c for c in report.checks if c.id == "call").ok


def test_over_eager_tool_use_is_advisory_not_blocking(engine_factory):
    _, ep = engine_factory(call_for_hello=True)
    report = run(q.qualify(ep, "fake-model", 16384))
    restraint = next(c for c in report.checks if c.id == "restraint")
    assert not restraint.ok and not restraint.required
    assert report.qualified


def test_unreachable_app_reports_a_failure_instead_of_raising(engine_factory):
    engine, ep = engine_factory()
    engine.close()
    report = run(q.qualify(ep, "fake-model", 16384))
    assert not report.qualified and report.checks and not report.checks[-1].ok


def test_probe_only_sends_synthetic_text(engine_factory):
    engine, ep = engine_factory()
    run(q.qualify(ep, "fake-model", 16384))
    blob = json.dumps(engine.requests)
    assert engine.requests and "probe_multiply" in blob
    assert "@" not in blob.replace("@example", "")  # no addresses, contacts or account data


@pytest.mark.parametrize("port,needle", [(11434, "OLLAMA_CONTEXT_LENGTH"), (1234, "Context Length"),
                                         (8080, "-c 16384"), (9999, "context length")])
def test_hints_name_the_engine_behind_the_port(port, needle):
    assert needle in q.context_hint(f"http://127.0.0.1:{port}", 4096)


# ---------------------------------------------------------- config & policy

ENDPOINT = {"enabled": True, "provider": "openai-compatible", "base_url": "http://127.0.0.1:11434",
            "api_prefix": "/v1", "credential_ref": "none", "readiness_timeout": 10}
QUALIFIED = {"qualified": True, "effective_context": 16384}


@pytest.fixture
def overlay(monkeypatch):
    """Route overlay writes into an in-memory effective config."""
    state = {"roles": {"reasoning": "local-reasoning", "agent": "local-agent", "coding": "local-coding"},
             "inference": {}}
    monkeypatch.setattr(config, "models_config", lambda: state)

    def save(update):
        config_state = config._deep_merge(state, update)
        state.clear()
        state.update(config_state)
    monkeypatch.setattr(config, "_save_overlay", save)
    return state


def test_qualified_provider_gets_tools_only_on_tool_roles(overlay):
    config.set_local_provider(ENDPOINT, "m", 32768, ["reasoning", "agent", "coding"], qualification=QUALIFIED)
    bindings = overlay["inference"]["bindings"]
    assert bindings["agent"]["qualified_capabilities"] == ["tools"]
    assert bindings["coding"]["qualified_capabilities"] == ["tools"]
    assert bindings["reasoning"]["qualified_capabilities"] == []
    # The saved window is what the app was measured to honor, not what was typed.
    assert bindings["agent"]["context_window"] == 16384
    settings = config.local_provider_settings()
    assert settings["roles"] == ["reasoning", "agent", "coding"]
    assert settings["tools_qualified"] is True and settings["qualified_context"] == 16384


def test_tool_roles_refuse_to_save_without_a_qualification(overlay):
    for qualification in (None, {"qualified": False, "effective_context": 2048}):
        with pytest.raises(ValueError):
            config.set_local_provider(ENDPOINT, "m", 16384, ["agent"], qualification=qualification)
    assert "bindings" not in overlay["inference"]


def test_bad_role_lists_are_rejected(overlay):
    for roles in ([], ["fast"], ["router"], ["embedding"], ["agent", "agent"], ["nope"]):
        with pytest.raises(ValueError):
            config.set_local_provider(ENDPOINT, "m", 16384, roles, qualification=QUALIFIED)


def test_deselecting_a_role_returns_it_to_the_local_roster(overlay):
    config.set_local_provider(ENDPOINT, "m", 16384, ["reasoning", "agent"], qualification=QUALIFIED)
    config.set_local_provider(ENDPOINT, "m", 16384, ["reasoning"])
    bindings = overlay["inference"]["bindings"]
    assert bindings["agent"]["endpoint"] == "local"
    assert bindings["agent"]["qualified_capabilities"] == []
    assert bindings["reasoning"]["endpoint"] == "local_provider"


def test_disconnect_returns_every_role_and_clears_the_qualification(overlay):
    config.set_local_provider(ENDPOINT, "m", 16384, ["reasoning", "agent", "coding"], qualification=QUALIFIED)
    config.disable_local_provider()
    bindings = overlay["inference"]["bindings"]
    assert all(bindings[r]["endpoint"] == "local" for r in ("reasoning", "agent", "coding"))
    assert config.local_provider_settings()["tools_qualified"] is False


def test_role_target_honors_tools_only_for_the_qualified_app_and_model(overlay):
    config.set_local_provider(ENDPOINT, "m", 32768, ["agent"], qualification=QUALIFIED)
    target = role_target("agent")
    assert "tools" in target.capabilities and target.context_window == 16384

    # A different model on the same binding loses tool use...
    overlay["inference"]["bindings"]["agent"]["model_id"] = "other-model"
    assert "tools" not in role_target("agent").capabilities
    overlay["inference"]["bindings"]["agent"]["model_id"] = "m"
    assert "tools" in role_target("agent").capabilities

    # ...and so does pointing the endpoint at a different port.
    overlay["inference"]["endpoints"]["local_provider"]["base_url"] = "http://127.0.0.1:1234"
    assert "tools" not in role_target("agent").capabilities


def test_hand_edited_tools_claim_is_ignored_without_a_record(overlay):
    overlay["inference"] = {"endpoints": {"local_provider": {**ENDPOINT, "model_id": "m"}},
                            "bindings": {"agent": {"endpoint": "local_provider", "model_id": "m",
                                                   "context_window": 16384,
                                                   "qualified_capabilities": ["tools"]}}}
    assert "tools" not in role_target("agent").capabilities


def test_window_is_clamped_to_the_measured_one_even_if_the_binding_claims_more(overlay):
    config.set_local_provider(ENDPOINT, "m", 16384, ["agent"], qualification={"qualified": True,
                                                                              "effective_context": 12288})
    overlay["inference"]["bindings"]["agent"]["context_window"] = 65536
    assert role_target("agent").context_window == 12288


def test_the_client_refuses_tools_for_an_unqualified_local_provider(overlay):
    from service.inference.omlx_client import OMLXClient
    config.set_local_provider(ENDPOINT, "m", 16384, ["reasoning"])
    overlay["inference"]["bindings"]["reasoning"]["endpoint"] = "local_provider"
    client = OMLXClient(target=role_target("reasoning"))
    with pytest.raises(endpoints.EndpointConfigurationError):
        client._fit_request("m", [{"role": "user", "content": "hi"}], [q._TOOL], 64)


# ------------------------------------------------ persisted prefix identity

@pytest.fixture
def persisted_qualification(tmp_path, monkeypatch, engine_factory):
    """Actual temporary YAML persistence and qualification; no writer mocks."""
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "OMLX_SETTINGS", tmp_path / "unused-settings.json")
    monkeypatch.setattr(config, "OMLX_MODEL_SETTINGS", tmp_path / "unused-model-settings.json")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    config.set_roles({"agent": "managed-agent", "coding": "managed-coding",
                      "reasoning": "managed-reasoning"})

    def qualify_and_save(prefix="/v1", roles=("agent", "coding")):
        engine, ep = engine_factory(ctx=16384, api_prefix=prefix)
        report = run(q.qualify(ep, "fake-model", 16384))
        assert report.qualified and report.effective_context == 16384
        config.set_local_provider({**ENDPOINT, "base_url": ep.base_url,
                                   "api_prefix": prefix}, "fake-model", 16384,
                                  list(roles), qualification=report.as_dict())
        engine.qualification_report = report.as_dict()
        return engine

    yield qualify_and_save
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


def _reload_qualification():
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    return config._user_overlay()["inference"]["endpoints"]["local_provider"]


def _asgi_qualification_settings():
    # Real GET route after atomic configuration writes and cache reload.
    import service.main as main

    async def get():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://127.0.0.1:18888") as client:
            response = await client.get("/inference/local-provider")
            assert response.status_code == 200
            return response.json()
    return run(get())


@pytest.mark.parametrize("prefix", ["/v1", "/alternate/v2"])
def test_persisted_prefix_matches_settings_and_runtime_identity(persisted_qualification, prefix):
    engine = persisted_qualification(prefix)
    record = _reload_qualification()["qualification"]
    assert record["schema"] == q.QUALIFICATION_SCHEMA == 2
    assert record["api_prefix"] == prefix
    before_requests = len(engine.wire_requests)
    settings = _asgi_qualification_settings()
    assert settings["tools_qualified"] and settings["qualified_context"] == 16384
    assert settings["api_prefix"] == prefix
    for role in ("agent", "coding"):
        target = role_target(role)
        assert target.endpoint.api_prefix == prefix
        assert "tools" in target.capabilities and target.context_window == 16384
    assert len(engine.wire_requests) == before_requests


@pytest.mark.parametrize("prefix,changed", [("/v1", "/alternate/v2"),
                                             ("/alternate/v2", "/v1"),
                                             ("/v1", "/V1")])
def test_persisted_prefix_only_change_revokes_tools(persisted_qualification, prefix, changed):
    engine = persisted_qualification(prefix)
    saved_before = config.USER_CONFIG.read_bytes()
    overlay_before = deepcopy(config._user_overlay())
    identities = {role: role_target(role).identity for role in ("agent", "coding")}
    before_requests = len(engine.wire_requests)
    config._save_overlay({"inference": {"endpoints": {"local_provider": {"api_prefix": changed}}}})
    endpoint = _reload_qualification()
    expected = deepcopy(overlay_before)
    expected["inference"]["endpoints"]["local_provider"]["api_prefix"] = changed
    assert config._user_overlay() == expected  # exactly one persisted field changed
    assert endpoint["qualification"]["api_prefix"] == prefix
    settings = _asgi_qualification_settings()
    assert not settings["tools_qualified"] and settings["qualified_context"] == 0
    assert settings["qualified_at"] == ""
    for role in ("agent", "coding"):
        target = role_target(role)
        assert target.identity != identities[role] and "tools" not in target.capabilities
        client = q.OMLXClient(target=target)
        with pytest.raises(endpoints.EndpointConfigurationError):
            client._fit_request("fake-model", [{"role": "user", "content": "hello"}], [q._TOOL], 64)
        run(client.aclose())
    assert len(engine.wire_requests) == before_requests
    config._save_overlay({"inference": {"endpoints": {"local_provider": {"api_prefix": prefix}}}})
    _reload_qualification()
    assert config.USER_CONFIG.read_bytes() == saved_before
    assert _asgi_qualification_settings()["tools_qualified"]
    assert all("tools" in role_target(role).capabilities for role in ("agent", "coding"))


@pytest.mark.parametrize("mutation", [
    {"schema": 1}, {"schema": 0}, {"schema": 3}, {"schema": None}, {"schema": 2.0},
    {"api_prefix": None}, {"api_prefix": 1}, {"api_prefix": "/unmatched"},
    {"qualified": False}, {"effective_context": True}, {"effective_context": None},
    {"effective_context": "16384"}, {"effective_context": 16384.0},
])
@pytest.mark.parametrize("prefix", ["/v1", "/alternate/v2"])
def test_persisted_qualification_record_requires_explicit_matching_evidence(
        persisted_qualification, prefix, mutation):
    persisted_qualification(prefix)
    config._save_overlay({"inference": {"endpoints": {"local_provider": {"qualification": mutation}}}})
    _reload_qualification()
    settings = _asgi_qualification_settings()
    assert not settings["tools_qualified"] and settings["qualified_context"] == 0
    assert settings["qualified_at"] == ""
    assert all("tools" not in role_target(role).capabilities for role in ("agent", "coding"))


@pytest.mark.parametrize("field", ["api_prefix", "schema"])
@pytest.mark.parametrize("prefix", ["/v1", "/alternate/v2"])
def test_persisted_qualification_record_missing_identity_is_not_defaulted(
        persisted_qualification, prefix, field):
    persisted_qualification(prefix)
    saved = deepcopy(config._user_overlay())
    del saved["inference"]["endpoints"]["local_provider"]["qualification"][field]
    # Clear the deep-merged record, then persist the missing-field input through
    # the actual atomic writer rather than inventing a record default.
    config._save_overlay({"inference": {"endpoints": {"local_provider": {"qualification": None}}}})
    config._save_overlay({"inference": {"endpoints": {"local_provider": {
        "qualification": saved["inference"]["endpoints"]["local_provider"]["qualification"]}}}})
    _reload_qualification()
    settings = _asgi_qualification_settings()
    assert not settings["tools_qualified"] and settings["qualified_context"] == 0
    assert all("tools" not in role_target(role).capabilities for role in ("agent", "coding"))


@pytest.mark.parametrize("field,value", [("base_url", "http://127.0.0.1:12345"),
                                         ("model_id", "other-model")])
def test_persisted_qualification_record_url_and_model_still_fail_closed(
        persisted_qualification, field, value):
    persisted_qualification()
    config._save_overlay({"inference": {"endpoints": {"local_provider": {field: value}}}})
    if field == "model_id":
        config._save_overlay({"inference": {"bindings": {
            role: {"model_id": value} for role in ("agent", "coding")}}})
    _reload_qualification()
    assert not _asgi_qualification_settings()["tools_qualified"]
    assert all("tools" not in role_target(role).capabilities for role in ("agent", "coding"))


def test_persisted_prefix_matching_record_clamps_context(persisted_qualification):
    persisted_qualification("/alternate/v2")
    config._save_overlay({"inference": {"bindings": {
        role: {"context_window": 65536} for role in ("agent", "coding")}}})
    _reload_qualification()
    assert _asgi_qualification_settings()["qualified_context"] == 16384
    for role in ("agent", "coding"):
        target = role_target(role)
        assert "tools" in target.capabilities and target.context_window == 16384


def test_persisted_prefix_default_is_resolved_only_for_endpoint(persisted_qualification):
    engine = persisted_qualification()
    endpoint = {key: value for key, value in ENDPOINT.items() if key != "api_prefix"}
    endpoint["base_url"] = f"http://127.0.0.1:{engine.port}"
    config.set_local_provider(endpoint, "fake-model", 16384, ["agent"],
                              qualification=engine.qualification_report)
    saved = _reload_qualification()
    assert saved["api_prefix"] == saved["qualification"]["api_prefix"] == "/v1"
    assert _asgi_qualification_settings()["tools_qualified"]


@pytest.mark.parametrize("record_prefix,qualified", [("/v1", True),
                                                     ("/alternate/v2", False),
                                                     (None, False)])
def test_persisted_endpoint_prefix_default_does_not_default_record_evidence(
        persisted_qualification, record_prefix, qualified):
    engine = persisted_qualification(record_prefix or "/v1")
    saved = deepcopy(config._user_overlay())
    endpoint = saved["inference"]["endpoints"]["local_provider"]
    del endpoint["api_prefix"]
    if record_prefix is None:
        del endpoint["qualification"]["api_prefix"]
    # Replace the endpoint through the actual writer to remove its prefix field.
    config._save_overlay({"inference": {"endpoints": {"local_provider": None}}})
    config._save_overlay({"inference": {"endpoints": {"local_provider": endpoint}}})
    _reload_qualification()
    before_requests = len(engine.wire_requests)
    settings = _asgi_qualification_settings()
    assert settings["api_prefix"] == "/v1"
    assert settings["tools_qualified"] is qualified
    assert settings["qualified_context"] == (16384 if qualified else 0)
    for role in ("agent", "coding"):
        target = role_target(role)
        assert target.endpoint.api_prefix == "/v1"
        assert ("tools" in target.capabilities) is qualified
    assert len(engine.wire_requests) == before_requests


@pytest.mark.parametrize("prefix", ["/v1", "/alternate/v2"])
def test_persisted_prefix_identity_uses_the_validated_origin(
        persisted_qualification, prefix):
    engine = persisted_qualification(prefix)
    origin = f"http://127.0.0.1:{engine.port}"
    # The real probe used the validated origin; a trailing slash is an accepted
    # representation of that same origin, not a different API prefix.
    config.set_local_provider({**ENDPOINT, "base_url": origin + "/", "api_prefix": prefix},
                              "fake-model", 16384, ["agent", "coding"],
                              qualification=engine.qualification_report)
    endpoint = _reload_qualification()
    settings = _asgi_qualification_settings()
    targets = {role: role_target(role) for role in ("agent", "coding")}
    print(json.dumps({"persisted_endpoint": endpoint, "settings": settings,
                      "runtime": {role: {"base_url": target.endpoint.base_url,
                                         "api_prefix": target.endpoint.api_prefix,
                                         "capabilities": target.capabilities,
                                         "context_window": target.context_window}
                                  for role, target in targets.items()},
                      "measured_report": engine.qualification_report}))
    assert endpoint["base_url"] == endpoint["qualification"]["base_url"] == origin
    assert settings["tools_qualified"]
    for role in ("agent", "coding"):
        target = role_target(role)
        assert target.endpoint.base_url == origin
        assert target.endpoint.api_prefix == prefix
        assert "tools" in target.capabilities and target.context_window == 16384


# ------------------------------------------------------------ server connect

@pytest.fixture
def actual_connection(overlay, engine_factory, monkeypatch):
    """Actual server, qualification, config setter and returned settings.

    conftest isolates Wisp storage and the quarantine gate before this fixture;
    engine_factory blocks effects and overlay replaces only storage reads/writes.
    """
    import service.main as main
    monkeypatch.setattr(main, "set_local_provider", config.set_local_provider)
    monkeypatch.setattr(main, "_qualification_cache", {})
    monkeypatch.setattr(main, "_local_provider_operation_generation", 0)
    return main


def _actual_body(engine, roles, context=16384):
    return {"base_url": f"http://127.0.0.1:{engine.port}", "api_prefix": "/v1",
            "model_id": "fake-model", "context_window": context, "roles": roles}


@pytest.mark.parametrize("roles", [["agent"], ["coding"]])
@pytest.mark.parametrize("engine_type,ctx,usage,qualified,verified", [
    (FakeEngine, 8191, True, False, 7936),
    (FakeEngine, 8192, True, True, 8192),
    (FakeEngine, 16384, True, True, 16384),
    (FakeEngine, 1_000_000, False, False, 0),
    (SixCharactersPerToken, 7000, False, False, 0),
])
def test_actual_connect_save_requires_measured_context(
        actual_connection, overlay, engine_factory, roles, engine_type, ctx, usage, qualified, verified):
    main = actual_connection
    engine, _ = engine_factory(engine_type=engine_type, ctx=ctx, usage=usage)
    before = deepcopy(overlay)
    body = _actual_body(engine, roles)
    if qualified:
        response = run(main.connect_local_provider_inference(body))
        assert response["tools_qualified"] and response["qualified_context"] == verified
        target = role_target(roles[0])
        assert "tools" in target.capabilities and target.context_window == verified
        assert target.endpoint.name == "local_provider"
    else:
        with pytest.raises(main.HTTPException) as error:
            run(main.connect_local_provider_inference(body))
        assert error.value.status_code == 400
        assert overlay == before  # actual save was never reached
    report = next(iter(main._qualification_cache.values()))[1]
    assert report.qualified == qualified and report.effective_context == verified


@pytest.mark.parametrize("phase", ["nonstream", "stream"])
@pytest.mark.parametrize("roles", [["agent"], ["coding"]])
@pytest.mark.parametrize("arguments", INVALID_ARGUMENTS)
def test_actual_connect_save_rejects_invalid_arguments(
        actual_connection, overlay, engine_factory, roles, phase, arguments):
    main = actual_connection
    engine, _ = engine_factory(engine_type=ArgumentEngine, arguments=arguments, phase=phase)
    before = deepcopy(overlay)
    with pytest.raises(main.HTTPException) as error:
        run(main.connect_local_provider_inference(_actual_body(engine, roles)))
    assert error.value.status_code == 400
    assert overlay == before
    report = next(iter(main._qualification_cache.values()))[1]
    _assert_argument_rejection(report, phase)


def _connect(main, engine, roles, monkeypatch, context=16384):
    saved = {}

    async def settings():
        return {"enabled": True}
    monkeypatch.setattr(main, "set_local_provider", lambda *a, **k: saved.update(args=a, kwargs=k))
    monkeypatch.setattr(main, "get_local_provider_inference", settings)
    main._qualification_cache.clear()
    body = {"base_url": f"http://127.0.0.1:{engine.port}", "api_prefix": "/v1",
            "model_id": "fake-model", "context_window": context, "roles": roles}
    return body, saved


def test_connect_runs_its_own_probe_and_records_the_result(engine_factory, monkeypatch):
    import service.main as main
    engine, _ = engine_factory()
    body, saved = _connect(main, engine, ["reasoning", "agent"], monkeypatch)
    asyncio.run(main.connect_local_provider_inference(body))
    assert saved["args"][3] == ["reasoning", "agent"]
    assert saved["kwargs"]["qualification"]["qualified"] is True


def test_connect_records_only_the_verified_window_of_a_smaller_engine(engine_factory, monkeypatch):
    import service.main as main
    engine, _ = engine_factory(ctx=15000)
    body, saved = _connect(main, engine, ["agent"], monkeypatch)
    asyncio.run(main.connect_local_provider_inference(body))
    record = saved["kwargs"]["qualification"]
    assert record["qualified"] is True and record["effective_context"] <= 15000


def test_connect_refuses_tool_roles_on_a_truncating_app_with_the_fix(engine_factory, monkeypatch):
    import service.main as main
    engine, _ = engine_factory(ctx=4096)
    body, saved = _connect(main, engine, ["agent"], monkeypatch)
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.connect_local_provider_inference(body))
    assert error.value.status_code == 400
    assert "can't run Wisp's tools" in error.value.detail and "context" in error.value.detail
    assert saved == {}


def test_a_client_cannot_assert_a_qualification(engine_factory, monkeypatch):
    import service.main as main
    engine, _ = engine_factory(tools="text")
    body, saved = _connect(main, engine, ["agent"], monkeypatch)
    body["qualification"] = {"qualified": True, "effective_context": 99999}
    body["qualified_capabilities"] = ["tools"]
    with pytest.raises(main.HTTPException):
        asyncio.run(main.connect_local_provider_inference(body))
    assert saved == {}


def test_test_then_connect_reuses_the_probe(engine_factory, monkeypatch):
    import service.main as main
    engine, _ = engine_factory()
    body, saved = _connect(main, engine, ["agent"], monkeypatch)
    report = asyncio.run(main.qualify_local_provider_inference(body))
    assert report["qualified"] and report["recommended_context"] == q.RECOMMENDED_CONTEXT
    before = len(engine.requests)
    asyncio.run(main.connect_local_provider_inference(body))
    assert len(engine.requests) == before  # connect reused the fresh result
    assert saved["kwargs"]["qualification"]["qualified"] is True


def test_reasoning_only_connect_still_uses_the_light_test(engine_factory, monkeypatch):
    import service.main as main
    engine, _ = engine_factory(ctx=4096)
    body, saved = _connect(main, engine, ["reasoning"], monkeypatch)
    asyncio.run(main.connect_local_provider_inference(body))
    assert saved["kwargs"]["qualification"] is None
    assert len(engine.requests) == 1


@pytest.mark.parametrize("roles", [[], ["fast"], ["router"], ["nope"], "agent", ["agent", "agent"]])
def test_connect_rejects_bad_roles(engine_factory, monkeypatch, roles):
    import service.main as main
    engine, _ = engine_factory()
    body, saved = _connect(main, engine, roles, monkeypatch)
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.connect_local_provider_inference(body))
    assert error.value.status_code == 400 and saved == {}


# ------------------------------------------------ stream budget & wording

def test_wire_budget_scales_with_the_per_token_event_cost():
    """Regression: ~1,100-token answers were refused as 'larger than Wisp can safely hold'.

    A real engine sends ~240 bytes of SSE envelope per streamed token."""
    from service.inference.omlx_client import OMLXClient, REMOTE_WIRE_HARD_BYTES
    for max_tokens in (256, 1024, 2048, 8000):
        _, wire = OMLXClient._remote_limits(max_tokens)
        assert wire >= max_tokens * 300, (max_tokens, wire)   # real cost with headroom
        assert wire <= REMOTE_WIRE_HARD_BYTES                 # still hard-capped
    assert OMLXClient._remote_limits(10**9)[1] == REMOTE_WIRE_HARD_BYTES


def _stream_text(ep, max_tokens):
    from service.inference.omlx_client import OMLXClient
    target = Target("agent", ep, "fake-model", context_window=16384)

    async def go():
        client = OMLXClient(target=target, timeout=60)
        try:
            parts = []
            async for event in client.stream_events("fake-model", [{"role": "user", "content": "write"}],
                                                    max_tokens=max_tokens):
                if event["kind"] == "content":
                    parts.append(event["text"])
            return "".join(parts)
        finally:
            await client.aclose()
    return asyncio.run(go())


def test_long_token_by_token_answer_streams_to_completion(engine_factory):
    """~2,500 one-token events (~600 KB on the wire) is a normal long answer."""
    _, ep = engine_factory(long_answer=2500)
    text = _stream_text(ep, 4096)
    assert text.count("w") == 2500


def test_a_runaway_stream_is_still_cut_off_by_the_budget(engine_factory):
    from service.inference.omlx_client import IncompleteStreamError
    _, ep = engine_factory(long_answer=20000)   # far more than 256 tokens can legitimately produce
    with pytest.raises(IncompleteStreamError):
        _stream_text(ep, 256)


def test_user_facing_errors_do_not_expose_the_internal_endpoint_name():
    from service.errors import translate
    from service.inference.omlx_client import IncompleteStreamError
    message, _ = translate(IncompleteStreamError("Remote inference output exceeded the allowed size"),
                           endpoint_name="local_provider")
    assert "local_provider" not in message and "local app" in message


# ------------------------------------------------ actual persisted HTTP routes

@pytest.fixture
def persisted_http_provider(tmp_path, monkeypatch, engine_factory):
    """Use real HTTP handlers, real qualification and the atomic YAML writer."""
    import service.main as main

    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "OMLX_SETTINGS", tmp_path / "unused-settings.json")
    monkeypatch.setattr(config, "OMLX_MODEL_SETTINGS", tmp_path / "unused-model-settings.json")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    config.set_roles({"agent": "managed-agent", "coding": "managed-coding",
                      "reasoning": "managed-reasoning"})
    monkeypatch.setattr(main, "_qualification_cache", {})
    monkeypatch.setattr(main, "_local_provider_operation_generation", 0)
    assert main.set_local_provider is config.set_local_provider
    assert main.qualification.qualify is q.qualify

    async def request(method, path, body=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://127.0.0.1:18888") as client:
            return await client.request(method, path, json=body)

    def connect(*, prefix="/v1", slash=False, roles=("agent", "coding"), **knobs):
        engine, ep = engine_factory(api_prefix=prefix, ctx=16384, **knobs)
        body = {"base_url": ep.base_url + ("/" if slash else ""),
                "api_prefix": prefix, "model_id": " fake-model ",
                "context_window": 32768, "roles": list(roles),
                # These client claims must never become the saved evidence.
                "qualification": {"qualified": True, "effective_context": 262144,
                                  "api_prefix": "/client-claim", "model_id": "client-model"},
                "qualified_capabilities": ["tools"]}
        response = run(request("POST", "/inference/local-provider", body))
        return engine, ep, response

    yield main, request, connect
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


@pytest.mark.parametrize("prefix", ["/v1", "/alternate/v2"])
@pytest.mark.parametrize("slash", [False, True])
@pytest.mark.parametrize("roles", [("agent",), ("coding",), ("reasoning", "agent", "coding")])
def test_actual_http_connect_get_and_roles_agree_on_normalized_identity(
        persisted_http_provider, prefix, slash, roles):
    main, request, connect = persisted_http_provider
    engine, ep, response = connect(prefix=prefix, slash=slash, roles=roles)
    assert response.status_code == 200, response.text
    saved = _reload_qualification()
    record = saved["qualification"]
    before = len(engine.wire_requests)
    get = run(request("GET", "/inference/local-provider"))
    assert get.status_code == 200
    settings = get.json()
    assert response.json() == settings
    assert saved["base_url"] == record["base_url"] == settings["base_url"] == ep.base_url
    assert saved["api_prefix"] == record["api_prefix"] == settings["api_prefix"] == prefix
    assert record["schema"] == q.QUALIFICATION_SCHEMA == 2
    assert record["model_id"] == settings["model_id"] == "fake-model"
    assert record["effective_context"] == settings["qualified_context"] == 16384
    assert settings["tools_qualified"] and set(settings["roles"]) == set(roles)
    for role in roles:
        target = role_target(role)
        assert target.endpoint.base_url == ep.base_url and target.endpoint.api_prefix == prefix
        assert target.model == "fake-model" and target.context_window == 16384
        assert ("tools" in target.capabilities) is (role in {"agent", "coding"})
    assert len(engine.wire_requests) == before
    assert ("GET", prefix + "/models") in engine.wire_requests
    assert all(path.startswith(prefix + "/") for _, path in engine.wire_requests)
    assert any(body.get("stream") for body in engine.requests)
    assert any(body.get("tools") for body in engine.requests)
    print(json.dumps({"post": response.json(), "get": settings,
                      "record": record, "wire": engine.wire_requests}))


@pytest.mark.parametrize("prefix,changed", [("/v1", "/alternate/v2"),
                                             ("/alternate/v2", "/v1"),
                                             ("/v1", "/V1")])
def test_actual_http_saved_prefix_mutation_revokes_and_restoration_retains_tools(
        persisted_http_provider, prefix, changed):
    _, request, connect = persisted_http_provider
    engine, _, response = connect(prefix=prefix, slash=True)
    assert response.status_code == 200
    original = config.USER_CONFIG.read_bytes()
    before = len(engine.wire_requests)
    config._save_overlay({"inference": {"endpoints": {"local_provider": {"api_prefix": changed}}}})
    _reload_qualification()
    get = run(request("GET", "/inference/local-provider"))
    assert get.status_code == 200 and not get.json()["tools_qualified"]
    assert get.json()["qualified_context"] == 0 and get.json()["qualified_at"] == ""
    for role in ("agent", "coding"):
        target = role_target(role)
        assert target.endpoint.api_prefix == changed and "tools" not in target.capabilities
    config._save_overlay({"inference": {"endpoints": {"local_provider": {"api_prefix": prefix}}}})
    _reload_qualification()
    assert config.USER_CONFIG.read_bytes() == original
    assert run(request("GET", "/inference/local-provider")).json()["tools_qualified"]
    assert all("tools" in role_target(role).capabilities for role in ("agent", "coding"))
    assert len(engine.wire_requests) == before


@pytest.mark.parametrize("mutation", [
    {"schema": True}, {"schema": "2"}, {"qualified": 1}, {"qualified": None},
    {"model_id": None}, {"base_url": None}, {"api_prefix": False},
])
def test_actual_http_saved_identity_rejects_additional_malformed_types(
        persisted_http_provider, mutation):
    _, request, connect = persisted_http_provider
    engine, _, response = connect(prefix="/alternate/v2", slash=True)
    assert response.status_code == 200
    before = len(engine.wire_requests)
    config._save_overlay({"inference": {"endpoints": {"local_provider": {"qualification": mutation}}}})
    _reload_qualification()
    get = run(request("GET", "/inference/local-provider"))
    assert get.status_code == 200 and not get.json()["tools_qualified"]
    assert get.json()["qualified_context"] == 0 and get.json()["qualified_at"] == ""
    assert all("tools" not in role_target(role).capabilities for role in ("agent", "coding"))
    assert len(engine.wire_requests) == before


def test_actual_http_reasoning_only_normalizes_without_recording_tool_evidence(
        persisted_http_provider):
    _, request, connect = persisted_http_provider
    engine, ep, response = connect(prefix="/alternate/v2", slash=True, roles=("reasoning",),
                                    tools="text", usage=False)
    assert response.status_code == 200, response.text
    saved = _reload_qualification()
    settings = run(request("GET", "/inference/local-provider")).json()
    assert saved["base_url"] == settings["base_url"] == ep.base_url
    assert saved["api_prefix"] == settings["api_prefix"] == "/alternate/v2"
    assert saved["qualification"] is None and not settings["tools_qualified"]
    assert settings["roles"] == ["reasoning"]
    assert "tools" not in role_target("reasoning").capabilities
    assert len(engine.requests) == 1 and not engine.requests[0].get("tools")


def test_actual_http_failed_tool_probe_ignores_client_claim_and_preserves_settings(
        persisted_http_provider):
    _, _, connect = persisted_http_provider
    before = config.USER_CONFIG.read_bytes()
    _, _, response = connect(prefix="/alternate/v2", slash=True, tools="text")
    assert response.status_code == 400 and "can't run Wisp's tools" in response.json()["detail"]
    assert config.USER_CONFIG.read_bytes() == before


# ------------------------------------------------ qualification raw body budget

class QualificationBody(httpx.AsyncByteStream):
    """Lazily yield raw bytes; observe consumption/closure without sockets."""

    def __init__(self, chunks=(), *, endless=False, waiting=False, failure=False):
        self.chunks = chunks
        self.endless, self.waiting, self.failure = endless, waiting, failure
        self.iterations = self.bytes_yielded = self.closes = 0
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        if self.waiting:
            await asyncio.Event().wait()
        if self.failure:
            raise httpx.ReadError("SYNTHETIC_PRIVATE_BODY https://private.invalid/secret")
        if self.endless:
            while True:
                await asyncio.sleep(0)
                self.iterations += 1
                self.bytes_yielded += len(self.chunks[0])
                yield self.chunks[0]
        else:
            for chunk in self.chunks:
                await asyncio.sleep(0)
                self.iterations += 1
                self.bytes_yielded += len(chunk)
                yield chunk

    async def aclose(self):
        self.closes += 1


@pytest.fixture
def qualification_wire(engine_factory):
    """Retain all existing effect refusals, but exercise the real raw response."""
    _, ep = engine_factory()

    async def complete(stream, headers=None, status=200):
        requests = []

        def dispatch(request):
            requests.append(request)
            return httpx.Response(status, headers=headers, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch),
                                     trust_env=False, follow_redirects=False) as client:
            result = await q._completion(client, ep, "fake-model", "Synthetic calibration")
        assert len(requests) == 1
        assert requests[0].url.path == "/v1/chat/completions"
        assert requests[0].headers["accept-encoding"] == "identity"
        return result
    return complete


@pytest.mark.parametrize("headers", [
    {"content-length": "4194305"}, {"content-length": "9" * 5000},
    {"content-length": "-1"}, {"content-length": "1.0"},
    {"content-length": "+2"}, {"content-length": ""},
    {"content-length": "1, 1"}, {"content-length": "1 2"},
    {"content-encoding": "gzip"}, {"content-encoding": "deflate"},
    {"content-encoding": "br"}, {"content-encoding": "identity, gzip"},
])
def test_qualification_headers_reject_before_raw_iteration(qualification_wire, headers):
    # Even a malformed compressed body must never reach a decoder/iterator.
    body = QualificationBody([b"SYNTHETIC_PRIVATE_BODY"])
    assert run(qualification_wire(body, headers)) is None
    assert body.iterations == 0 and body.closes == 1


def test_qualification_compression_bomb_rejects_before_decode(qualification_wire, monkeypatch):
    import gzip
    bomb = gzip.compress(b"x" * (8 * 1024 * 1024))
    assert len(bomb) < 10000
    body = QualificationBody([bomb])

    def forbidden_decode(*args, **kwargs):
        raise AssertionError("compressed qualification body reached a decoder")
    monkeypatch.setattr(httpx._decoders.GZipDecoder, "decode", forbidden_decode)
    assert run(qualification_wire(body, {"content-encoding": "gzip"})) is None
    assert body.iterations == 0 and body.closes == 1


@pytest.mark.parametrize("endless", [False, True])
def test_qualification_undeclared_overflow_stops_before_parse(qualification_wire, monkeypatch, endless):
    chunk = b"x" * (1024 * 1024)
    body = QualificationBody([chunk] * 9, endless=endless)
    parsed = []
    loads = q.json.loads

    def observe(value, *args, **kwargs):
        parsed.append(value)
        return loads(value, *args, **kwargs)
    monkeypatch.setattr(q.json, "loads", observe)
    assert run(qualification_wire(body)) is None
    assert body.iterations == 5 and body.bytes_yielded == 5 * len(chunk)
    assert body.closes == 1 and parsed == []


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b"null", b"42", b'"text"', b"\xff"])
def test_qualification_invalid_json_is_closed_and_unusable(qualification_wire, raw):
    body = QualificationBody([raw])
    assert run(qualification_wire(body)) is None
    assert body.iterations == 1 and body.closes == 1


def test_qualification_large_json_is_not_parsed(qualification_wire, monkeypatch):
    # A well-formed JSON object beyond the ceiling still cannot be accumulated.
    body = QualificationBody([b'{"answer":"', b"x" * (4 * 1024 * 1024), b'"}'])

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("oversized qualification JSON reached parsing")
    monkeypatch.setattr(q.json, "loads", forbidden_parse)
    assert run(qualification_wire(body)) is None
    assert body.iterations == 2 and body.closes == 1


@pytest.mark.parametrize("encoding", [None, "identity", "Identity"])
def test_qualification_fragmented_healthy_response(qualification_wire, encoding):
    raw = b'{"usage":{"prompt_tokens":1234},"choices":[]}'
    body = QualificationBody([raw[:9], raw[9:27], raw[27:]])
    headers = {"content-length": "000" + str(len(raw))}
    if encoding is not None:
        headers["content-encoding"] = encoding
    assert run(qualification_wire(body, headers)) == {"usage": {"prompt_tokens": 1234}, "choices": []}
    assert body.iterations == 3 and body.closes == 1


def test_qualification_exact_byte_ceiling_is_accepted(qualification_wire):
    raw = b"{}" + b" " * (4 * 1024 * 1024 - 2)
    body = QualificationBody([raw[:100], raw[100:]])
    assert run(qualification_wire(body, {"content-length": str(len(raw))})) == {}
    assert body.closes == 1


@pytest.mark.parametrize("status", [302, 400, 500])
def test_qualification_status_rejection_does_not_read_or_follow(qualification_wire, status):
    body = QualificationBody([b"SYNTHETIC_PRIVATE_BODY"])
    assert run(qualification_wire(body, {"location": "https://private.invalid/secret"}, status)) is None
    assert body.iterations == 0 and body.closes == 1


class WaitingQualificationEngine(FakeEngine):
    """Block one actual model/context/tool response; other phases stay healthy."""

    def __init__(self, phase, *, failure=False):
        super().__init__()
        self.phase, self.failure = phase, failure
        self.blocked = None

    def respond(self, request):
        body = json.loads(request.content) if request.method == "POST" else {}
        phase = ("model" if request.method == "GET" else
                 "tool" if body.get("tools") else "context")
        if phase != self.phase:
            return super().respond(request)
        self.wire_requests.append((request.method, request.url.path))
        self.blocked = QualificationBody(waiting=not self.failure, failure=self.failure)
        return httpx.Response(200, stream=self.blocked)


@pytest.mark.parametrize("phase", ["model", "context", "tool"])
@pytest.mark.parametrize("termination", ["cancel", "deadline"])
def test_qualification_response_contexts_close_and_never_save(
        persisted_http_provider, engine_factory, monkeypatch, phase, termination):
    main, request, _ = persisted_http_provider
    engine, _ = engine_factory(engine_type=WaitingQualificationEngine, phase=phase)
    before = config.USER_CONFIG.read_bytes()
    from service.inference.attributed_transport import CredentialTransport
    closures = []
    context_exits = []
    provider_close = CredentialTransport.aclose
    context_exit = httpx.AsyncHTTPTransport.__aexit__

    async def observed_provider_close(transport):
        result = await provider_close(transport)
        closures.append("provider")
        return result

    async def observed_context_exit(transport, exc_type, exc_value, exc_traceback):
        arguments = (exc_type, exc_value, exc_traceback)
        observed = {"arguments": arguments, "completed": False}
        context_exits.append(observed)
        result = await context_exit(transport, *arguments)
        observed["completed"] = True
        observed["result"] = result
        closures.append("context")
        return result

    monkeypatch.setattr(CredentialTransport, "aclose", observed_provider_close)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__aexit__", observed_context_exit)
    # The qualifier's original total deadline covers context and tool checks.
    # Model discovery occurs before that scope; give only that request an outer
    # test deadline, without claiming a new production model-list deadline.
    monkeypatch.setattr(q, "_PROBE_DEADLINE_SECONDS", 0.05)

    async def attempt():
        task = asyncio.create_task(request("POST", "/inference/local-provider", _actual_body(engine, ["agent"])))
        while engine.blocked is None:
            await asyncio.sleep(0)
        await engine.blocked.entered.wait()
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif phase == "model":
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await task
        else:
            response = await task
            assert response.status_code == 400
            assert "too slow" in response.json()["detail"]
            report = next(iter(main._qualification_cache.values()))[1].as_dict()
            assert not report["qualified"] and report["checks"][-1]["id"] == "deadline"
            assert "SYNTHETIC_PRIVATE_BODY" not in json.dumps(report)
    run(attempt())
    assert engine.blocked.closes == 1 and engine.blocked.iterations == 0
    assert closures.count("provider") == (2 if phase == "tool" else 1)
    assert closures.count("context") == (0 if phase == "model" else 1)
    assert len(context_exits) == closures.count("context")
    assert all(observed["completed"] for observed in context_exits)
    for observed in context_exits:
        exc_type, exc_value, exc_traceback = observed["arguments"]
        if phase == "context":
            assert exc_type is asyncio.CancelledError
            assert isinstance(exc_value, asyncio.CancelledError) and exc_traceback is not None
        else:
            # The healthy context measurement finished before the tool phase.
            assert (exc_type, exc_value, exc_traceback) == (None, None, None)
    assert config.USER_CONFIG.read_bytes() == before
    print(json.dumps({"qualification_cleanup": {
        "phase": phase, "termination": termination, "response_closes": engine.blocked.closes,
        "provider_closes_completed": closures.count("provider"),
        "context_exits_completed": closures.count("context"), "persistence_unchanged": True,
        "actual_exit_arguments": [{
            "exception_type": observed["arguments"][0].__name__ if observed["arguments"][0] else None,
            "exception_value": repr(observed["arguments"][1]),
            "traceback_present": observed["arguments"][2] is not None,
            "original_exit_return": repr(observed["result"]),
        } for observed in context_exits],
    }}))


@pytest.mark.parametrize("phase", ["context", "tool"])
def test_qualification_stream_failure_is_sanitized_and_never_saved(
        persisted_http_provider, engine_factory, phase):
    main, request, _ = persisted_http_provider
    engine, _ = engine_factory(engine_type=WaitingQualificationEngine, phase=phase, failure=True)
    before = config.USER_CONFIG.read_bytes()
    response = run(request("POST", "/inference/local-provider", _actual_body(engine, ["agent"])))
    assert response.status_code == 400
    report = next(iter(main._qualification_cache.values()))[1].as_dict()
    assert not report["qualified"] and report["checks"][-1]["id"] == "error"
    for rendered in (json.dumps(report), response.text):
        assert "SYNTHETIC_PRIVATE_BODY" not in rendered and "private.invalid" not in rendered
    assert engine.blocked.closes == 1 and config.USER_CONFIG.read_bytes() == before


def test_context_calibration_ignores_environment_proxy_and_redirect_policy(engine_factory, monkeypatch):
    _, ep = engine_factory()
    monkeypatch.setenv("HTTP_PROXY", "http://private.invalid:9999")
    monkeypatch.setenv("ALL_PROXY", "http://private.invalid:9999")
    monkeypatch.setenv("NO_PROXY", "")
    observed = []
    initialize = httpx.AsyncClient.__init__

    def record(client, *args, **kwargs):
        observed.append(kwargs)
        initialize(client, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", record)
    effective, how = run(q.measure_context(ep, "fake-model", 16384))
    assert effective == 16384 and how == "measured"
    assert len(observed) == 1 and observed[0]["trust_env"] is False
    assert observed[0]["follow_redirects"] is False


# ----------------------------------------------- PR130 backend A1/A2: concurrency
#
# A1  A qualification result must stop being reusable the moment the provider is
#     disconnected, a late result must never repopulate the cache, and a slower,
#     older probe must never overwrite a newer one for the same app.
# A2  Any configuration change that can alter the roles a pending provider
#     connection will bind must supersede that connection.
#
# These run through the real ASGI app (httpx.ASGITransport), the real
# qualification code and the real atomic YAML writer against a temporary file,
# with the in-process fake engine above answering through the patched httpx
# transport: no socket, process, credential, model, native listener or user data.
# Completion order is controlled with asyncio events, not sleeps.

def run_bounded(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


_CONCURRENCY_BASE_URL = "http://127.0.0.1:18888"


def run_bounded(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


class QualificationGate:
    """Wrap qualification so a test decides when each probe COMPLETES.

    The real probe runs against the engine as it is at call time; only its return
    is held. Also records whether the operation lock is ever held while awaiting.
    """

    def __init__(self, main, monkeypatch):
        self.main = main
        self.real = q.qualify
        self.hold: set[int] = set()
        self.calls: list[dict] = []
        self.lock_held_while_awaiting = False
        monkeypatch.setattr(q, "qualify", self._qualify)

    def _note_lock(self):
        if self.main._local_provider_operation_lock.locked():
            self.lock_held_while_awaiting = True

    async def _qualify(self, endpoint, model, context):
        index = len(self.calls)
        entry = {"started": asyncio.Event(), "release": asyncio.Event()}
        self.calls.append(entry)
        self._note_lock()
        report = await self.real(endpoint, model, context)
        self._note_lock()
        entry["started"].set()
        if index in self.hold:
            await entry["release"].wait()
            self._note_lock()
        return report

    async def started(self, index: int):
        while len(self.calls) <= index:
            await asyncio.sleep(0)
        await asyncio.wait_for(self.calls[index]["started"].wait(), 5)

    def release(self, index: int):
        self.calls[index]["release"].set()


class ProviderHarness:
    def __init__(self, main, gate, engine_factory):
        self.main, self.gate, self._factory = main, gate, engine_factory

    def engine(self, **knobs):
        engine, _ = self._factory(**knobs)
        return engine

    @staticmethod
    def body(engine, roles, context=16384):
        return {"base_url": f"http://127.0.0.1:{engine.port}", "api_prefix": "/v1",
                "model_id": "fake-model", "context_window": context, "roles": roles}

    async def call(self, method, path, body=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app),
                                     base_url=_CONCURRENCY_BASE_URL) as client:
            return await client.request(method, path, json=body)

    def connect(self, engine, roles, **kw):
        return self.call("POST", "/inference/local-provider", self.body(engine, roles, **kw))

    def test_probe(self, engine, roles=("agent",), **kw):
        return self.call("POST", "/inference/local-provider/qualify",
                         self.body(engine, list(roles), **kw))

    def settings(self):
        return self.call("GET", "/inference/local-provider")


@pytest.fixture
def concurrency(tmp_path, monkeypatch, engine_factory):
    import service.main as main

    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "OMLX_SETTINGS", tmp_path / "unused-settings.json")
    monkeypatch.setattr(config, "OMLX_MODEL_SETTINGS", tmp_path / "unused-model-settings.json")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    config.set_roles({"general": "managed-general"})
    config.set_roles({"agent": "managed-agent", "coding": "managed-coding",
                      "reasoning": "managed-reasoning"})
    monkeypatch.setattr(main, "_qualification_cache", {})
    monkeypatch.setattr(main, "_local_provider_operation_generation", 0)
    monkeypatch.setattr(main, "_sync_keep_warm", lambda: None)  # unrelated to the fence
    assert main.set_local_provider is config.set_local_provider
    gate = QualificationGate(main, monkeypatch)
    harness = ProviderHarness(main, gate, engine_factory)
    yield harness
    assert not gate.lock_held_while_awaiting, "the operation lock was held across an await"
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


def _saved_yaml():
    return config.USER_CONFIG.read_bytes()


# ============================================================ A1: revocation

def test_disconnect_discards_completed_evidence_so_reconnect_re_probes(concurrency):
    engine = concurrency.engine()

    async def scenario():
        assert (await concurrency.test_probe(engine)).json()["qualified"] is True
        probes_after_test = len(concurrency.gate.calls)
        assert (await concurrency.call("DELETE", "/inference/local-provider")).status_code == 200
        engine.tools = "none"   # same app, same identity, but it can no longer call tools
        before = _saved_yaml()
        reconnect = await concurrency.connect(engine, ["agent"])
        return probes_after_test, before, reconnect, await concurrency.settings()

    probes_after_test, before, reconnect, settings = run_bounded(scenario())
    assert len(concurrency.gate.calls) == probes_after_test + 1, "Disconnect must force a fresh probe"
    assert reconnect.status_code == 400 and "can't run Wisp's tools" in reconnect.json()["detail"]
    assert _saved_yaml() == before, "a failed reconnect must not change the saved configuration"
    assert settings.json()["tools_qualified"] is False and settings.json()["enabled"] is False
    assert role_target("agent").model == "managed-agent"
    assert role_target("agent").endpoint.name == "local"


def test_stale_connect_after_disconnect_is_409_and_leaves_no_reusable_evidence(concurrency):
    engine = concurrency.engine()

    async def scenario():
        concurrency.gate.hold = {0}
        pending = asyncio.create_task(concurrency.connect(engine, ["agent"]))
        await concurrency.gate.started(0)
        assert (await concurrency.call("DELETE", "/inference/local-provider")).status_code == 200
        concurrency.gate.release(0)
        stale = await pending
        fresh = await concurrency.connect(engine, ["agent"])
        return stale, fresh, await concurrency.settings()

    stale, fresh, settings = run_bounded(scenario())
    assert stale.status_code == 409
    assert len(concurrency.gate.calls) == 2, "the next Connect must qualify afresh, not reuse the stale result"
    assert fresh.status_code == 200 and settings.json()["tools_qualified"] is True


def test_stale_dry_run_after_disconnect_cannot_populate_the_cache(concurrency):
    engine = concurrency.engine()

    async def scenario():
        concurrency.gate.hold = {0}
        pending = asyncio.create_task(concurrency.test_probe(engine))
        await concurrency.gate.started(0)
        assert (await concurrency.call("DELETE", "/inference/local-provider")).status_code == 200
        concurrency.gate.release(0)
        stale = await pending
        connect = await concurrency.connect(engine, ["agent"])
        return stale, connect

    stale, connect = run_bounded(scenario())
    assert stale.status_code == 200   # the caller still gets its own result
    assert len(concurrency.gate.calls) == 2, "a dry run finished after Disconnect must not be reusable"
    assert connect.status_code == 200


@pytest.mark.parametrize("older,newer", [("structured", "none"), ("none", "structured")])
def test_reverse_completion_keeps_only_the_newest_evidence(concurrency, older, newer):
    engine = concurrency.engine(tools=older)

    async def scenario():
        concurrency.gate.hold = {0}
        first = asyncio.create_task(concurrency.test_probe(engine))
        await concurrency.gate.started(0)                 # the OLDER probe has measured, completion held
        engine.tools = newer
        second = await concurrency.test_probe(engine)     # the NEWER probe completes first
        concurrency.gate.release(0)
        first_response = await first            # the older one finishes last
        connect = await concurrency.connect(engine, ["agent"])
        return first_response, second, connect

    first, second, connect = run_bounded(scenario())
    assert first.json()["qualified"] is (older == "structured")
    assert second.json()["qualified"] is (newer == "structured")
    assert len(concurrency.gate.calls) == 2, "Connect reuses the newest evidence instead of probing again"
    assert (connect.status_code == 200) is (newer == "structured"), \
        "the older probe finishing last must not overwrite the newer evidence"


# ---------------------------------------------------------------- A1 controls

def test_same_epoch_test_then_connect_still_reuses_the_probe(concurrency):
    engine = concurrency.engine()

    async def scenario():
        await concurrency.test_probe(engine)
        wire = len(engine.requests)
        connect = await concurrency.connect(engine, ["agent"])
        return wire, connect

    wire, connect = run_bounded(scenario())
    assert connect.status_code == 200 and len(concurrency.gate.calls) == 1
    assert len(engine.requests) == wire


def test_ttl_expiry_forces_a_new_probe(concurrency, monkeypatch):
    engine = concurrency.engine()

    async def scenario():
        await concurrency.test_probe(engine)
        monkeypatch.setattr(concurrency.main, "_QUALIFICATION_TTL_SECONDS", 0.0)
        return await concurrency.connect(engine, ["agent"])

    assert run_bounded(scenario()).status_code == 200
    assert len(concurrency.gate.calls) == 2


@pytest.mark.parametrize("change", ["context", "other_app"])
def test_identity_changes_force_a_new_probe(concurrency, change):
    engine, other = concurrency.engine(), concurrency.engine()

    async def scenario():
        await concurrency.test_probe(engine)
        if change == "context":
            return await concurrency.connect(engine, ["agent"], context=8192)
        return await concurrency.connect(other, ["agent"])

    assert run_bounded(scenario()).status_code == 200
    assert len(concurrency.gate.calls) == 2


# ============================================================ A2: role fences

LOCAL_PENDING_CASES = [
    pytest.param(["agent"], "agent", "managed-agent-2", "agent", id="agent-to-managed-agent"),
    pytest.param(["agent"], "general", "managed-general-2", "agent", id="agent-to-managed-general-coupling"),
    pytest.param(["coding"], "coding", "managed-coding-2", "coding", id="coding-to-managed-coding"),
    pytest.param(["reasoning", "agent", "coding"], "coding", "managed-coding-3", "coding",
                 id="multi-role-then-coding"),
    pytest.param(["reasoning", "agent", "coding"], "agent", "managed-agent-3", "agent",
                 id="multi-role-then-agent"),
    pytest.param(["agent"], "reasoning", "managed-reasoning-2", "reasoning",
                 id="existing-reasoning-control"),
]


@pytest.mark.parametrize("pending_roles,role,model,checked", LOCAL_PENDING_CASES)
def test_pending_local_connect_is_superseded_by_a_managed_role_choice(
        concurrency, pending_roles, role, model, checked):
    engine = concurrency.engine()

    async def scenario():
        concurrency.gate.hold = {0}
        pending = asyncio.create_task(concurrency.connect(engine, pending_roles))
        await concurrency.gate.started(0)
        changed = await concurrency.call("POST", "/config", {"role": role, "model": model})
        after_choice = _saved_yaml()
        concurrency.gate.release(0)
        stale = await pending
        return changed, after_choice, stale, await concurrency.settings()

    changed, after_choice, stale, settings = run_bounded(scenario())
    assert changed.status_code == 200
    assert stale.status_code == 409
    assert _saved_yaml() == after_choice, "the stale connect must not rewrite the newer persisted binding"
    assert settings.json()["enabled"] is False and settings.json()["tools_qualified"] is False
    target = role_target(checked)
    assert target.endpoint.name == "local" and target.model == model


@pytest.mark.parametrize("role", ["fast", "router"])
def test_unrelated_role_updates_do_not_supersede_a_pending_connect(concurrency, role):
    engine = concurrency.engine()

    async def scenario():
        concurrency.gate.hold = {0}
        pending = asyncio.create_task(concurrency.connect(engine, ["agent"]))
        await concurrency.gate.started(0)
        changed = await concurrency.call("POST", "/config", {"role": role, "model": "managed-fast-2"})
        concurrency.gate.release(0)
        return changed, await pending, await concurrency.settings()

    changed, connect, settings = run_bounded(scenario())
    assert changed.status_code == 200 and connect.status_code == 200
    assert settings.json()["tools_qualified"] is True and settings.json()["roles"] == ["agent"]


@pytest.mark.parametrize("pending_roles,config_role", [
    pytest.param(["coding"], "coding", id="cloud-coding"),
    pytest.param(["coding", "research"], "research", id="cloud-multi-role-research"),
    pytest.param(["reasoning"], "reasoning", id="existing-cloud-reasoning-control"),
])
def test_pending_cloud_connect_is_superseded_by_a_managed_role_choice(
        monkeypatch, pending_roles, config_role):
    import service.main as main

    probe_started, release_probe = asyncio.Event(), asyncio.Event()
    saved, reassigned = [], []

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
    monkeypatch.setattr(main, "set_cloud_provider", lambda *a, **k: saved.append(a))
    monkeypatch.setattr(main, "set_role", lambda *a: reassigned.append(a))
    monkeypatch.setattr(main, "_sync_keep_warm", lambda: None)
    monkeypatch.setattr(main, "role_to_model", lambda role: "managed")
    monkeypatch.setattr(main, "models_config", lambda: {"roles": {config_role: "managed"}})

    async def exercise():
        pending = asyncio.create_task(main.connect_cloud_inference({
            "provider": "openai-compatible", "base_url": "https://example.com",
            "api_prefix": "/v1", "model_id": "cloud-model", "context_window": 8192,
            "credential_name": "Wisp", "roles": pending_roles}))
        await asyncio.wait_for(probe_started.wait(), 1)
        await main.config({"role": config_role, "model": "managed"})
        assert not main._local_provider_operation_lock.locked()
        release_probe.set()
        with pytest.raises(main.HTTPException) as error:
            await pending
        return error.value

    error = asyncio.run(asyncio.wait_for(exercise(), 30))
    assert error.status_code == 409
    assert saved == [] and reassigned == [(config_role, "managed")]


def test_disconnect_and_cloud_disconnect_still_supersede_a_pending_connect(concurrency, monkeypatch):
    engine = concurrency.engine()
    monkeypatch.setattr(concurrency.main, "disable_cloud_provider", lambda: None)

    async def scenario():
        concurrency.gate.hold = {0}
        pending = asyncio.create_task(concurrency.connect(engine, ["agent"]))
        await concurrency.gate.started(0)
        assert (await concurrency.call("DELETE", "/inference/cloud")).status_code == 200
        concurrency.gate.release(0)
        return await pending

    assert run_bounded(scenario()).status_code == 409


# ------------------------------- PR130 D1/D2: discovery failures and superseded saves
#
# D1  A fresh qualification that fails MODEL DISCOVERY (the model list errors, times
#     out, or lacks the selected model) is newer evidence that the app is not
#     usable. It must retire an older success, and an older in-flight success must
#     not be able to resurrect itself afterwards.
# D2  A Connect that is awaiting its own passing report must not be saved once a
#     newer completed result for the same app has superseded that report.

class DiscoveryEngine(FakeEngine):
    """A fake engine whose model list can fail the ways a real app's can."""

    models_mode = "ok"   # ok | missing | http_error | connect_error | timeout | cancelled

    def respond(self, request):
        if (request.method == "GET" and request.url.path == self.api_prefix + "/models"
                and not self.closed):
            if self.models_mode == "missing":
                return httpx.Response(200, json={"object": "list", "data": [
                    {"id": "some-other-model", "object": "model"}]})
            if self.models_mode == "http_error":
                return httpx.Response(503, json={"error": "unavailable"})
            if self.models_mode == "connect_error":
                raise httpx.ConnectError("connection refused", request=request)
            if self.models_mode == "timeout":
                raise httpx.ReadTimeout("model list timed out", request=request)
            if self.models_mode == "cancelled":
                raise asyncio.CancelledError()
        return super().respond(request)


DISCOVERY_FAILURES = ["missing", "http_error", "connect_error", "timeout"]


@pytest.mark.parametrize("mode", DISCOVERY_FAILURES)
def test_a_failed_fresh_discovery_retires_an_older_success(concurrency, mode):
    engine = concurrency.engine(engine_type=DiscoveryEngine)

    async def scenario():
        assert (await concurrency.test_probe(engine)).json()["qualified"] is True
        engine.models_mode = mode
        failed = await concurrency.test_probe(engine)
        saved_before = _saved_yaml()
        still_failing = await concurrency.connect(engine, ["agent"])
        after_failed_connect = _saved_yaml()
        engine.models_mode = "ok"
        recovered = await concurrency.connect(engine, ["agent"])
        return failed, saved_before, still_failing, after_failed_connect, recovered, \
            await concurrency.settings()

    failed, saved_before, still_failing, after_failed, recovered, settings = run_bounded(scenario())
    assert failed.status_code == 400
    assert still_failing.status_code == 400, "the older success must not authorise a Connect"
    assert after_failed == saved_before, "no tool binding may be saved from retired evidence"
    assert len(concurrency.gate.calls) == 2, \
        "once the app recovers Connect must probe afresh, neither reusing the old success nor " \
        "being blocked for the TTL by a transient discovery failure"
    assert recovered.status_code == 200 and settings.json()["tools_qualified"] is True


@pytest.mark.parametrize("mode", ["missing", "http_error", "timeout"])
def test_an_older_success_cannot_resurrect_after_a_newer_discovery_failure(concurrency, mode):
    engine = concurrency.engine(engine_type=DiscoveryEngine)

    async def scenario():
        concurrency.gate.hold = {0}
        older = asyncio.create_task(concurrency.test_probe(engine))
        await concurrency.gate.started(0)          # the older probe has measured a pass
        engine.models_mode = mode
        newer = await concurrency.test_probe(engine)   # fails in discovery, newer ticket
        concurrency.gate.release(0)
        older_response = await older
        engine.models_mode = "ok"
        return newer, older_response, await concurrency.connect(engine, ["agent"])

    newer, older, connect = run_bounded(scenario())
    assert newer.status_code == 400
    assert older.status_code == 200 and older.json()["qualified"] is True, \
        "the older caller still receives its own historical report"
    assert len(concurrency.gate.calls) == 2, \
        "the older pass must not have become reusable after the newer failure"
    assert connect.status_code == 200


def test_an_older_connect_pass_cannot_be_saved_after_a_newer_failed_dry_run(concurrency):
    engine = concurrency.engine()

    async def scenario():
        before = _saved_yaml()
        concurrency.gate.hold = {0}
        older = asyncio.create_task(concurrency.connect(engine, ["agent"]))
        await concurrency.gate.started(0)
        engine.tools = "none"
        newer = await concurrency.test_probe(engine)
        concurrency.gate.release(0)
        return before, newer, await older, await concurrency.settings()

    before, newer, older, settings = run_bounded(scenario())
    assert newer.json()["qualified"] is False
    assert older.status_code == 409, "a superseded report must not authorise the save"
    assert _saved_yaml() == before
    assert settings.json()["enabled"] is False and settings.json()["tools_qualified"] is False
    assert role_target("agent").model == "managed-agent"
    assert role_target("agent").endpoint.name == "local"


@pytest.mark.parametrize("mode", ["missing", "http_error", "timeout"])
def test_an_older_connect_pass_cannot_be_saved_after_a_newer_discovery_failure(concurrency, mode):
    engine = concurrency.engine(engine_type=DiscoveryEngine)

    async def scenario():
        before = _saved_yaml()
        concurrency.gate.hold = {0}
        older = asyncio.create_task(concurrency.connect(engine, ["agent"]))
        await concurrency.gate.started(0)
        engine.models_mode = mode
        newer = await concurrency.test_probe(engine)
        concurrency.gate.release(0)
        older_response = await older
        engine.models_mode = "ok"
        retry = await concurrency.connect(engine, ["agent"])
        return before, newer, older_response, retry, await concurrency.settings()

    before, newer, older, retry, settings = run_bounded(scenario())
    assert newer.status_code == 400
    assert older.status_code == 409
    assert len(concurrency.gate.calls) == 2, "the retry probes afresh; the cache agrees with the failure"
    assert retry.status_code == 200 and settings.json()["tools_qualified"] is True


def test_a_newer_success_also_supersedes_an_older_connect_and_the_retry_reuses_it(concurrency):
    # Deliberately conservative: only the newest completed evidence may authorise a save.
    engine = concurrency.engine()

    async def scenario():
        before = _saved_yaml()
        concurrency.gate.hold = {0}
        older = asyncio.create_task(concurrency.connect(engine, ["agent"]))
        await concurrency.gate.started(0)
        newer = await concurrency.test_probe(engine)
        concurrency.gate.release(0)
        older_response = await older
        after_stale = _saved_yaml()
        retry = await concurrency.connect(engine, ["agent"])
        return before, newer, older_response, after_stale, retry

    before, newer, older, after_stale, retry = run_bounded(scenario())
    assert newer.json()["qualified"] is True
    assert older.status_code == 409 and after_stale == before
    assert retry.status_code == 200
    assert len(concurrency.gate.calls) == 2, "the retry reuses the newest evidence without another probe"


def test_reasoning_only_connect_is_not_affected_by_evidence_ordering(concurrency):
    engine = concurrency.engine()

    async def scenario():
        engine_response = await concurrency.connect(engine, ["reasoning"])
        return engine_response, await concurrency.settings()

    response, settings = run_bounded(scenario())
    assert response.status_code == 200 and settings.json()["roles"] == ["reasoning"]
    assert len(concurrency.gate.calls) == 0, "a Reasoning-only connect carries no qualification"


# ------------- PR130 E2: a known discovery failure must be retired before any cleanup await
#
# The failure is already known when the discovery probe is closed. If closing it raises
# or is cancelled, the older success must still have been retired and the ticket
# recorded; otherwise a later Connect reuses the older success, or an older in-flight
# pass republishes. A cancellation DURING discovery is a different thing: it is not
# negative evidence about the app and must not be invented as one.

class _CloseSwitch:
    """Make closing the discovery probe fail on demand, after really closing it."""

    def __init__(self, monkeypatch):
        from service.inference.omlx_client import OMLXClient
        self.fail_with = None
        self.calls = 0
        original = OMLXClient.aclose

        async def aclose(client):
            self.calls += 1
            await original(client)
            if self.fail_with is not None:
                raise self.fail_with
        monkeypatch.setattr(OMLXClient, "aclose", aclose)


CLOSE_FAILURES = [
    pytest.param(lambda: RuntimeError("close failed"), id="close-raises-runtime-error"),
    pytest.param(lambda: OSError("connection pool already closed"), id="close-raises-os-error"),
    pytest.param(lambda: asyncio.CancelledError(), id="close-is-cancelled"),
]


async def _awaited(coro):
    """Return a result or the exception (including a cancellation) it ended with."""
    try:
        return await coro
    except BaseException as exc:  # noqa: BLE001
        return exc


@pytest.mark.parametrize("make_failure", CLOSE_FAILURES)
@pytest.mark.parametrize("mode", ["missing", "http_error", "timeout"])
def test_known_discovery_failure_retires_evidence_even_when_closing_the_probe_fails(
        concurrency, monkeypatch, mode, make_failure):
    engine = concurrency.engine(engine_type=DiscoveryEngine)
    close = _CloseSwitch(monkeypatch)
    failure = make_failure()

    async def scenario():
        assert (await concurrency.test_probe(engine)).json()["qualified"] is True
        engine.models_mode = mode
        close.fail_with = failure
        closed_before = close.calls
        failed = await _awaited(concurrency.test_probe(engine))
        cleanup_attempted = close.calls > closed_before
        close.fail_with = None
        saved_before = _saved_yaml()
        still_failing = await concurrency.connect(engine, ["agent"])
        unchanged = _saved_yaml() == saved_before
        engine.models_mode = "ok"
        recovered = await concurrency.connect(engine, ["agent"])
        return failed, cleanup_attempted, still_failing, unchanged, recovered

    failed, cleanup_attempted, still_failing, unchanged, recovered = run_bounded(scenario())
    assert cleanup_attempted, "the probe cleanup must still be attempted"
    if isinstance(failure, asyncio.CancelledError):
        assert isinstance(failed, asyncio.CancelledError), "a cancellation must still propagate"
    else:
        assert getattr(failed, "status_code", None) == 400, \
            "a known discovery failure keeps its sanitized 400 even if cleanup also fails"
    assert still_failing.status_code == 400, "the older success must not authorise a Connect"
    assert unchanged, "no tool binding may be saved from retired evidence"
    assert len(concurrency.gate.calls) == 2, "recovery must qualify afresh, not reuse the retired success"
    assert recovered.status_code == 200


@pytest.mark.parametrize("make_failure", [CLOSE_FAILURES[0], CLOSE_FAILURES[2]])
def test_an_older_success_cannot_republish_after_a_failed_discovery_with_a_failing_close(
        concurrency, monkeypatch, make_failure):
    engine = concurrency.engine(engine_type=DiscoveryEngine)
    close = _CloseSwitch(monkeypatch)

    async def scenario():
        concurrency.gate.hold = {0}
        older = asyncio.create_task(concurrency.test_probe(engine))
        await concurrency.gate.started(0)
        engine.models_mode = "missing"
        close.fail_with = make_failure()
        newer = await _awaited(concurrency.test_probe(engine))
        close.fail_with = None
        concurrency.gate.release(0)
        older_response = await older
        engine.models_mode = "ok"
        return newer, older_response, await concurrency.connect(engine, ["agent"])

    newer, older, connect = run_bounded(scenario())
    assert isinstance(newer, BaseException) or newer.status_code == 400
    assert older.status_code == 200 and older.json()["qualified"] is True
    assert len(concurrency.gate.calls) == 2, "the older pass must not have republished"
    assert connect.status_code == 200


def test_a_cancelled_discovery_does_not_invent_negative_evidence(concurrency):
    engine = concurrency.engine(engine_type=DiscoveryEngine)

    async def scenario():
        assert (await concurrency.test_probe(engine)).json()["qualified"] is True
        engine.models_mode = "cancelled"
        interrupted = await _awaited(concurrency.test_probe(engine))
        engine.models_mode = "ok"
        return interrupted, await concurrency.connect(engine, ["agent"])

    interrupted, connect = run_bounded(scenario())
    assert isinstance(interrupted, asyncio.CancelledError)
    assert connect.status_code == 200
    assert len(concurrency.gate.calls) == 1, \
        "a cancellation says nothing about the app; the earlier success is still the latest evidence"


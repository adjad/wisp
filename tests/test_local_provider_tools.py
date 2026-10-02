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
            return httpx.Response(code, content=content, headers={
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

    async def plain(self, request):
        return await _dispatch(request)

    async def credentialed(self, request):
        return await _dispatch(request)
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
    # Only the existing GET route: held main.py cannot yet run tool-role Connect.
    import service.main as main

    async def get():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://synthetic.invalid") as client:
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

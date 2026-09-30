"""Tool use on a non-oMLX local inference app is earned by a measured probe.

Every engine here is an in-process fake bound to 127.0.0.1; nothing touches the
user's real apps, Keychain, data or network. The fake reproduces the failure
modes seen on real engines: silent context truncation, tool calls written as
text, and a missing ``usage`` block.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
                 call_for_hello=False, long_answer=0):
        self.ctx, self.tools, self.usage = ctx, tools, usage
        self.reject_over_ctx, self.call_for_hello = reject_over_ctx, call_for_hello
        self.long_answer = long_answer
        self.requests = []
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload, stream=False):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
                else:
                    self._send(404, {"error": "nope"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                engine.requests.append(body)
                self._send(*engine.complete(body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

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


@pytest.fixture
def engine_factory():
    made = []

    def make(**knobs):
        engine = FakeEngine(**knobs)
        made.append(engine)
        ep = endpoint_from_config("local_provider", {
            "enabled": True, "provider": "openai-compatible", "base_url": f"http://127.0.0.1:{engine.port}",
            "api_prefix": "/v1", "credential_ref": "none", "readiness_timeout": 10})
        return engine, ep
    yield make
    for engine in made:
        engine.close()


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


def test_missing_usage_falls_back_to_recalling_text_from_the_start(engine_factory):
    _, good = engine_factory(usage=False)
    assert run(q.qualify(good, "fake-model", 16384)).qualified
    _, cut = engine_factory(usage=False, ctx=2048)
    report = run(q.qualify(cut, "fake-model", 16384))
    assert not report.qualified and report.effective_context < q.MIN_TOOL_CONTEXT


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


# ------------------------------------------------------------ server connect

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

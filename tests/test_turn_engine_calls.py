"""Engine HTTP calls per turn: the call sequence is pinned, not just the answers.

Every readiness call to the local oMLX engine pays a full attestation (about
0.9 s in 1.2.0), so a turn must not repeat a check whose answer it already has.
These tests drive the real endpoint / agent loop / TurnInferenceClient /
OMLXClient against an in-process fake engine that logs every request. No socket,
process or real engine is touched, and all state lives under the test's temp
WISP_HOME.

The reductions are only allowed where nothing could have changed. The safety
cases (a different model, an error, foreign engine activity, a stale proof, an
engine that is down) must still run the full check; they are pinned here too.
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess

import httpx
import pytest

from service import main
from service.agent.loop import run_agent
from service.config import role_to_model
from service.inference import engine_epoch, readiness
from service.inference.omlx_client import ModelLoadError, OMLXClient
from service.inference.readiness import TurnInferenceClient
from service.memory import context
from service.memory.store import SessionStore
from service.tools.registry import REGISTRY, Tool

MODEL = role_to_model("agent")
OTHER = "Other-Model-4bit"
EMBEDDER = "Fake-Embedder"
BASE = "http://127.0.0.1:18790"


# ------------------------------------------------------------ fake engine

class FakeOmlx:
    """Answers the oMLX endpoints Wisp uses and logs each one by purpose."""

    def __init__(self, resident=(MODEL,), installed=(MODEL, OTHER, EMBEDDER)):
        self.loaded = set(resident)
        self.installed = list(installed)
        self.script: list = []          # per chat: None (text) or a list of tool names
        self.log: list[tuple[str, str]] = []
        self.chat_bodies: list[dict] = []
        self.failures: dict[str, list] = {}   # purpose -> [status | Exception, ...]
        self.on_chat = None             # callable(body) run before each completion
        self.delays: dict[str, list[float]] = {}   # purpose -> seconds to stall, per call
        self.hooks: dict[str, object] = {}         # purpose -> callable run on arrival
        self.down = False
        self.load_after_polls = 0       # a load only lands after N status polls
        self._pending: dict[str, int] = {}

    @staticmethod
    def purpose(method: str, path: str) -> str:
        if path == "/health":
            return "health"
        if path == "/v1/models/status":
            return "status"
        if path == "/v1/models":
            return "models"
        if path.endswith("/load"):
            return "load"
        if path.endswith("/unload"):
            return "unload"
        if path == "/v1/chat/completions":
            return "chat"
        if path == "/v1/embeddings":
            return "embed"
        if path == "/v1/rerank":
            return "rerank"
        return f"{method} {path}"

    def sequence(self) -> list[str]:
        return [p for p, _ in self.log]

    def count(self, purpose: str) -> int:
        return self.sequence().count(purpose)

    def arrive(self, request: httpx.Request) -> None:
        """Log a request when it reaches the engine, even if it later stalls."""
        path = request.url.path
        purpose = self.purpose(request.method, path)
        self.log.append((purpose, path))
        if purpose in self.hooks:
            self.hooks[purpose]()

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        purpose = self.purpose(request.method, path)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        queued = self.failures.get(purpose)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return httpx.Response(item, json={"error": "injected"})
        if purpose == "health":
            return httpx.Response(200, json={"status": "ok"})
        if purpose == "status":
            for model in list(self._pending):
                if self._pending[model] <= 0:
                    del self._pending[model]
                    self.loaded.add(model)
                else:
                    self._pending[model] -= 1
            return httpx.Response(200, json={"models": [
                {"id": m, "loaded": m in self.loaded} for m in self.installed]})
        if purpose == "models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.installed]})
        if purpose == "load":
            model = path.split("/")[-2]
            if model in self.installed:
                if self.load_after_polls:
                    self._pending[model] = self.load_after_polls
                else:
                    self.loaded.add(model)
            return httpx.Response(200, json={})
        if purpose == "unload":
            self.loaded.discard(path.split("/")[-2])
            return httpx.Response(200, json={})
        if purpose == "embed":
            body = json.loads(request.content)
            self.loaded.add(body["model"])
            return httpx.Response(200, json={"data": [
                {"index": i, "embedding": [0.1, 0.2]} for i in range(len(body["input"]))]})
        if purpose == "rerank":
            self.loaded.add(json.loads(request.content)["model"])
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})
        if purpose == "chat":
            body = json.loads(request.content)
            self.chat_bodies.append(body)
            if self.on_chat:
                self.on_chat(body)
            self.loaded.add(body["model"])
            names = self.script.pop(0) if self.script else None
            if body.get("stream"):
                return httpx.Response(200, content=self._sse(names), headers={
                    "content-type": "text/event-stream"})
            message = self._message(names)
            return httpx.Response(200, json={"choices": [{
                "index": 0, "message": message,
                "finish_reason": "tool_calls" if names else "stop"}]})
        return httpx.Response(404, json={"error": "unexpected"})

    @staticmethod
    def _message(names):
        if names:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{i}", "type": "function",
                "function": {"name": n, "arguments": "{}"}} for i, n in enumerate(names)]}
        return {"role": "assistant", "content": "Done."}

    @classmethod
    def _sse(cls, names) -> bytes:
        def event(delta, reason=None):
            return "data: " + json.dumps({"choices": [
                {"index": 0, "delta": delta, "finish_reason": reason}]}) + "\n\n"
        out = [event({"role": "assistant"})]
        if names:
            for i, name in enumerate(names):
                out.append(event({"tool_calls": [{"index": i, "id": f"call_{i}", "type": "function",
                                                  "function": {"name": name, "arguments": "{}"}}]}))
            out.append(event({}, "tool_calls"))
        else:
            out.append(event({"content": "Done."}))
            out.append(event({}, "stop"))
        out.append("data: [DONE]\n\n")
        return "".join(out).encode()


@pytest.fixture
def engine(monkeypatch, tmp_path):
    """A managed oMLX client wired to the fake engine, plus isolated storage."""
    fake = FakeOmlx()

    def forbidden(*args, **kwargs):
        raise AssertionError("engine-call fixtures must not open sockets or processes")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    from service import config
    monkeypatch.setattr(config, "omlx_api_key", lambda: "test-key")   # never read the real settings
    mock = httpx.MockTransport(lambda request: fake.respond(request))

    async def through_fake(self, request):
        fake.arrive(request)
        stalls = fake.delays.get(FakeOmlx.purpose(request.method, request.url.path))
        if stalls:
            await asyncio.sleep(stalls.pop(0))
        return await mock.handle_async_request(request)

    from service.inference.attributed_transport import CredentialTransport
    monkeypatch.setattr(CredentialTransport, "handle_async_request", through_fake)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", through_fake)

    client = OMLXClient(base_url=BASE, api_key="test-key")
    client.set_keep_warm({MODEL})            # what main.lifespan configures
    store = SessionStore(tmp_path / "sessions.db")
    monkeypatch.setattr(main, "client", client, raising=False)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(context, "store", store)
    monkeypatch.setattr(main, "models_config", lambda: {"tool_retrieval": {"provider": "lexical"}})

    async def cli_unavailable(*args):
        return False
    monkeypatch.setattr(main, "_omlx_cli", cli_unavailable)

    class Env:
        pass
    env = Env()
    env.fake, env.client, env.store = fake, client, store
    return env


@pytest.fixture
def instant_sleep(monkeypatch):
    """Skip the 1 s poll sleeps of a model swap (real time adds nothing here)."""
    real = asyncio.sleep

    async def quick(delay, *args, **kwargs):
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", quick)


def register_tools(monkeypatch, names=("fake_a", "fake_b", "fake_c"), func=None):
    for name in names:
        monkeypatch.setitem(REGISTRY, name, Tool(
            name=name, description=f"fake {name}",
            parameters={"type": "object", "properties": {}}, category="fs_read",
            func=func or (lambda _n=name: f"{_n} result")))
    return list(names)


class Approver:
    async def confirm(self, action):
        return True


def endpoint_turn(prompt, **body):
    """Run the real /agent endpoint to completion and return its events."""
    async def go():
        response = await main.agent({"prompt": prompt, "debug": False, **body})
        events = []
        async for item in response.body_iterator:
            if isinstance(item, bytes):
                item = item.decode()
            events.append(json.loads(item.removeprefix("data: ").strip()))
        return events
    return asyncio.run(go())


def loop_turn(env, tools, *, model=MODEL, retrieval_engine=False, max_steps=6):
    """Run the real agent loop behind a real TurnInferenceClient (as /agent builds it)."""
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    async def go():
        turn = TurnInferenceClient(env.client, main.ensure_omlx, emit=emit)
        if retrieval_engine:
            await turn.ensure_engine()
        return await run_agent(turn, model, [{"role": "user", "content": "do the thing"}],
                               emit, Approver(), tools=tools, max_steps=max_steps)
    final = asyncio.run(go())
    return final, events


def show(label, env):
    print(f"\n[calls] {label}: {' '.join(env.fake.sequence())}")


# ------------------------------------------------------- scenario: hello

def test_hello_is_one_status_read_and_one_chat(engine):
    events = endpoint_turn("hello")
    show("hello", engine)
    assert [e for e in events if e["type"] == "error"] == []
    assert engine.fake.sequence() == ["status", "chat"]
    assert any(e["type"] == "done" for e in events)
    assert engine.fake.chat_bodies[0]["model"] == MODEL


def test_hello_with_early_engine_start_still_needs_one_read(engine, monkeypatch):
    """A semantic-retrieval config starts the engine before routing; that read
    is the only residency read when nothing else touched the engine."""
    monkeypatch.setattr(main, "models_config", lambda: {"tool_retrieval": {"provider": "embedding"}})
    events = endpoint_turn("hello")
    show("hello (embedding retrieval)", engine)
    assert [e for e in events if e["type"] == "error"] == []
    assert engine.fake.sequence() == ["status", "chat"]


# --------------------------------------------------- scenario: tool turns

def test_one_tool_turn_is_one_status_read_and_two_chats(engine, monkeypatch):
    tools = register_tools(monkeypatch)
    engine.fake.script = [["fake_a"], None]
    final, events = loop_turn(engine, tools)
    show("one tool", engine)
    assert final == "Done."
    assert engine.fake.sequence() == ["status", "chat", "chat"]
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["fake_a"]


def test_three_step_tool_turn_reads_status_once(engine, monkeypatch):
    tools = register_tools(monkeypatch)
    engine.fake.script = [["fake_a"], ["fake_b"], ["fake_c"], None]
    final, events = loop_turn(engine, tools)
    show("three tools", engine)
    assert final == "Done."
    assert engine.fake.sequence() == ["status", "chat", "chat", "chat", "chat"]


def test_tool_turn_with_early_engine_start_reads_status_once(engine, monkeypatch):
    tools = register_tools(monkeypatch)
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools, retrieval_engine=True)
    show("one tool, early engine start", engine)
    assert engine.fake.sequence() == ["status", "chat", "chat"]


# ------------------------------------------------ invariants: full check

def test_model_swap_runs_the_full_check_and_keeps_loading_status(engine, instant_sleep):
    engine.fake.loaded = {OTHER}
    engine.fake.load_after_polls = 2
    events = endpoint_turn("hello")
    show("swap", engine)
    seq = engine.fake.sequence()
    assert seq[0] == "status" and "unload" in seq and "load" in seq
    assert seq.index("unload") < seq.index("load") < seq.index("chat")
    assert seq.count("chat") == 1
    texts = [e["text"] for e in events if e["type"] == "status"]
    assert any(t.startswith(f"Loading {MODEL}…") for t in texts), texts
    assert any(t.startswith(f"Loading {MODEL}… (") for t in texts), texts
    assert engine.fake.loaded == {MODEL}


def test_swap_turn_checks_again_before_the_next_step(engine, monkeypatch, instant_sleep):
    """A swap changed residency itself, so no proof is kept for the next step."""
    tools = register_tools(monkeypatch)
    engine.fake.loaded = {OTHER}
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools)
    show("swap + tool", engine)
    seq = engine.fake.sequence()
    first_chat = seq.index("chat")
    assert "status" in seq[first_chat + 1:]
    assert seq == ["status", "status", "unload", "status", "load", "status",
                   "chat", "status", "chat"]
    assert engine.fake.loaded == {MODEL}


def test_foreign_model_loaded_by_a_tool_is_evicted_before_the_next_step(engine, monkeypatch):
    """A tool that generated on another model through any client bumps the
    engine epoch, so the next step re-reads residency and evicts it."""
    foreign = OMLXClient(base_url=BASE, api_key="test-key")

    async def tool_using_other_model():
        await foreign.chat(OTHER, [{"role": "user", "content": "x"}])
        return "used another model"

    tools = register_tools(monkeypatch, names=("fake_a",), func=tool_using_other_model)
    engine.fake.script = [["fake_a"], None, None]
    loop_turn(engine, tools)
    show("foreign chat", engine)
    seq = engine.fake.sequence()
    assert seq[:3] == ["status", "chat", "chat"]        # step 0, then the tool's own chat
    assert "unload" in seq                              # OTHER evicted
    assert seq.index("unload") < len(seq) - 1 and seq[-1] == "chat"
    assert OTHER not in engine.fake.loaded


def test_embedder_load_by_a_tool_forces_a_new_check(engine, monkeypatch):
    from service.search import embedder
    tools_called = []

    async def tool_embedding():
        target = embedder.embedding_target()
        tools_called.append(target.model)
        engine.fake.installed.append(target.model)
        await embedder._embed(["hello"], timeout=5.0)
        return "embedded"

    tools = register_tools(monkeypatch, names=("fake_a",), func=tool_embedding)
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools)
    show("embedder tool", engine)
    seq = engine.fake.sequence()
    assert "embed" in seq
    after_embed = seq[seq.index("embed") + 1:]
    assert after_embed[0] == "status" and "unload" in after_embed
    assert tools_called[0] not in engine.fake.loaded


def test_reranker_use_by_a_tool_forces_a_new_check(engine, monkeypatch):
    from service.router import reranker

    async def tool_reranking():
        async with httpx.AsyncClient(base_url=BASE) as c:
            await reranker._rank_one(c, {}, "query", ["fake_a"], top_n=1, model=EMBEDDER)
        return "reranked"

    tools = register_tools(monkeypatch, names=("fake_a",), func=tool_reranking)
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools)
    show("reranker tool", engine)
    seq = engine.fake.sequence()
    assert seq[seq.index("rerank") + 1] == "status"
    assert EMBEDDER not in engine.fake.loaded


def test_connection_invalidation_forces_a_new_check(engine, monkeypatch):
    """An engine restart path calls invalidate_connections; residency is unknown after it."""
    def tool_restarting():
        engine.client.invalidate_connections()
        return "restarted"

    tools = register_tools(monkeypatch, names=("fake_a",), func=tool_restarting)
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools)
    show("invalidate", engine)
    assert engine.fake.sequence() == ["status", "chat", "status", "chat"]


def test_stale_proof_is_not_trusted(engine, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(readiness, "_now", lambda: clock["t"])

    def slow_tool():
        clock["t"] += readiness.RESIDENCY_LEASE_SECONDS + 1
        return "slow"

    tools = register_tools(monkeypatch, names=("fake_a",), func=slow_tool)
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools)
    show("slow tool", engine)
    assert engine.fake.sequence() == ["status", "chat", "status", "chat"]


def test_a_fresh_proof_is_trusted_up_to_the_lease(engine, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(readiness, "_now", lambda: clock["t"])

    def quick_tool():
        clock["t"] += readiness.RESIDENCY_LEASE_SECONDS - 1
        return "quick"

    tools = register_tools(monkeypatch, names=("fake_a",), func=quick_tool)
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools)
    assert engine.fake.sequence() == ["status", "chat", "chat"]


def test_proof_has_an_absolute_age_limit_even_when_renewed(engine, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(readiness, "_now", lambda: clock["t"])

    def step_tool():
        clock["t"] += readiness.RESIDENCY_LEASE_SECONDS - 1
        return "step"

    tools = register_tools(monkeypatch, names=("fake_a",), func=step_tool)
    # Each step is inside the lease, but together they pass the absolute limit.
    steps = int(readiness.RESIDENCY_MAX_UNVERIFIED_SECONDS // (readiness.RESIDENCY_LEASE_SECONDS - 1)) + 2
    engine.fake.script = [["fake_a"]] * steps + [None]
    loop_turn(engine, tools, max_steps=steps + 2)
    show("long renewed chain", engine)
    seq = engine.fake.sequence()
    assert seq.count("status") >= 2
    assert seq.count("status") <= 1 + steps // 2


def test_a_chat_error_drops_the_proof(engine, monkeypatch):
    tools = register_tools(monkeypatch)
    engine.fake.script = [["fake_a"], None]

    async def go():
        events = []

        async def emit(e):
            events.append(e)
        turn = TurnInferenceClient(engine.client, main.ensure_omlx, emit=emit)
        await turn.ensure_only(MODEL, exclusive=True)
        engine.fake.failures["chat"] = [500]
        with pytest.raises(httpx.HTTPStatusError):
            await turn.chat(MODEL, [{"role": "user", "content": "x"}])
        await turn.ensure_only(MODEL, exclusive=True)
        return turn
    asyncio.run(go())
    show("chat 500", engine)
    assert engine.fake.sequence() == ["status", "chat", "status"]


def test_a_status_error_is_not_cached_and_falls_back_to_the_full_path(engine):
    """A failed read must never be recorded as proof; the old path (health, then
    status) runs and the turn still succeeds once the engine answers."""
    engine.fake.failures["status"] = [503]
    events = endpoint_turn("hello")
    show("status 503 then ok", engine)
    assert [e for e in events if e["type"] == "error"] == []
    assert engine.fake.sequence() == ["status", "health", "status", "chat"]


def test_second_turn_does_not_reuse_the_first_turns_proof(engine):
    endpoint_turn("hello")
    endpoint_turn("thanks")
    show("two turns", engine)
    assert engine.fake.sequence() == ["status", "chat", "status", "chat"]


def test_different_model_in_the_same_turn_runs_the_full_check(engine):
    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL, exclusive=True)
        await turn.ensure_only(OTHER, exclusive=True)
        await turn.ensure_only(OTHER, exclusive=True)
    asyncio.run(go())
    show("model change", engine)
    seq = engine.fake.sequence()
    assert seq[0] == "status"
    assert "unload" in seq and "load" in seq
    assert engine.fake.loaded == {OTHER}


def test_keep_warm_models_are_never_skipped(engine):
    """With a keep-warm model missing, a non-exclusive check must reload it."""
    engine.client.set_keep_warm({MODEL, OTHER})
    engine.fake.loaded = {MODEL}

    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL)
        assert OTHER in engine.fake.loaded
    asyncio.run(go())
    show("keep-warm", engine)
    assert "load" in engine.fake.sequence()


def test_different_exclusivity_does_not_reuse_a_nonmatching_proof(engine):
    engine.fake.loaded = {MODEL, EMBEDDER}

    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL, exclusive=True)
        assert engine.fake.loaded == {MODEL}
    asyncio.run(go())
    assert "unload" in engine.fake.sequence()


# ---------------------------------------------------- engine down / errors

def test_engine_down_gives_the_same_user_visible_failure(engine):
    engine.fake.down = True
    events = endpoint_turn("hello")
    show("engine down", engine)
    assert [(e["type"], e.get("message")) for e in events
            if e["type"] in ("error", "done")] == [
        ("error", "The local oMLX CLI is unavailable"), ("done", None)]
    assert "chat" not in engine.fake.sequence()
    assert "health" in engine.fake.sequence()           # the full start path still ran


def test_missing_model_fails_the_same_way(engine, instant_sleep):
    engine.fake.installed = [OTHER]
    engine.fake.loaded = set()
    real = engine.client.ensure_only

    async def quick(model, **kwargs):          # the real wait is 60 s of polling
        return await real(model, settle_timeout=0.2, **kwargs)
    engine.client.ensure_only = quick
    events = endpoint_turn("hello")
    errors = [e["message"] for e in events if e["type"] == "error"]
    assert errors == ["Inference readiness timed out on local"]
    assert "chat" not in engine.fake.sequence()


def test_engine_up_fast_path_makes_exactly_one_request(engine):
    """With the engine answering, starting it costs one status read, not a health read first."""
    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_engine()
        return turn
    turn = asyncio.run(go())
    assert engine.fake.sequence() == ["status"]
    assert turn._engine_ready


def test_a_hung_status_read_falls_back_to_the_full_path(engine, monkeypatch):
    import time
    monkeypatch.setattr(readiness, "ENGINE_PROBE_SECONDS", 0.2)
    engine.fake.delays["status"] = [3.0]
    started = time.monotonic()
    events = endpoint_turn("hello")
    assert time.monotonic() - started < 1.5          # the probe gave up, it did not wait
    show("hung status", engine)
    assert [e for e in events if e["type"] == "error"] == []
    assert engine.fake.sequence() == ["status", "health", "status", "chat"]


def test_a_failed_residency_check_leaves_no_proof(engine):
    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        engine.fake.loaded = {OTHER}            # not satisfied by the probe's read
        engine.fake.failures["load"] = [500]
        with pytest.raises(httpx.HTTPStatusError):
            await turn.ensure_only(MODEL, exclusive=True)
        assert turn._residency is None
        engine.fake.loaded = {MODEL}
        await turn.ensure_only(MODEL, exclusive=True)
        assert turn._residency is not None
    asyncio.run(go())


def test_an_engine_that_returns_garbage_for_status_uses_the_full_start(engine):
    """A client whose inventory is not a list of names is never trusted."""
    started = []

    class Odd:
        managed = True

        async def loaded_models(self):
            return {"not": "a list"}

        async def ensure_only(self, model, **kwargs):
            started.append(("ensure", model))

        def keep_warm(self):
            return set()

    async def start():
        started.append(("start",))

    async def go():
        turn = TurnInferenceClient(Odd(), start)
        await turn.ensure_only(MODEL, exclusive=True)
        await turn.ensure_only(MODEL, exclusive=True)
    asyncio.run(go())
    # The second step trusts the check that just completed; the inventory was never trusted.
    assert started == [("start",), ("ensure", MODEL)]


def test_a_client_without_a_status_api_uses_the_full_start():
    started = []

    class Bare:
        async def ensure_only(self, model, **kwargs):
            started.append(("ensure", model, kwargs.get("exclusive")))

    async def start():
        started.append(("start",))

    asyncio.run(TurnInferenceClient(Bare(), start).ensure_only(MODEL, exclusive=True))
    assert started == [("start",), ("ensure", MODEL, True)]


def test_cancelling_the_probe_is_not_swallowed(engine, monkeypatch):
    engine.fake.delays["status"] = [30.0]

    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        task = asyncio.create_task(turn.ensure_only(MODEL, exclusive=True))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not turn._engine_ready
    asyncio.run(go())
    assert engine.fake.sequence() == ["status"]


def test_a_generation_that_fails_before_the_engine_drops_the_proof(engine, monkeypatch):
    """No engine contact means no epoch tick, so only clearing the proof forces a re-check."""
    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL, exclusive=True)
        with monkeypatch.context() as local:
            local.setattr(engine.client, "_fit_request",
                          lambda *a, **k: (_ for _ in ()).throw(ValueError("too long")))
            with pytest.raises(ValueError):
                await turn.chat(MODEL, [{"role": "user", "content": "x"}])
        await turn.ensure_only(MODEL, exclusive=True)
    asyncio.run(go())
    assert engine.fake.sequence() == ["status", "status"]


def test_foreign_activity_during_a_generation_is_not_absorbed(engine, monkeypatch):
    tools = register_tools(monkeypatch, names=("fake_a",))
    engine.fake.script = [["fake_a"], None]
    engine.fake.hooks["chat"] = lambda: (engine_epoch.bump(), engine.fake.hooks.pop("chat"))
    loop_turn(engine, tools)
    show("foreign tick during chat", engine)
    assert engine.fake.sequence() == ["status", "chat", "status", "chat"]


def test_an_abandoned_stream_does_not_renew_the_proof(engine):
    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL, exclusive=True)
        engine.fake.script = [["fake_a"]]
        events = turn.stream_events(MODEL, [{"role": "user", "content": "x"}])
        async for _ in events:
            break                                   # the consumer walks away mid-stream
        await events.aclose()
        await turn.ensure_only(MODEL, exclusive=True)
    asyncio.run(go())
    assert engine.fake.sequence() == ["status", "chat", "status"]


def test_activity_during_the_engine_probe_is_not_trusted(engine):
    engine.fake.hooks["status"] = lambda: (engine_epoch.bump(), engine.fake.hooks.pop("status"))

    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL, exclusive=True)
    asyncio.run(go())
    assert engine.fake.sequence() == ["status", "status"]     # the probe's read was not reused


def test_an_unknown_keep_warm_set_is_never_trusted(engine):
    class Opaque(OMLXClient):
        def keep_warm(self):
            raise RuntimeError("unknown")
    opaque = Opaque(base_url=BASE, api_key="test-key")

    async def go():
        turn = TurnInferenceClient(opaque, main.ensure_omlx)
        await turn.ensure_only(MODEL, exclusive=True)
        await turn.ensure_only(MODEL, exclusive=True)
    asyncio.run(go())
    assert engine.fake.sequence() == ["status", "status", "status"]


def test_a_generation_cannot_revive_an_expired_proof(engine, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(readiness, "_now", lambda: clock["t"])

    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL, exclusive=True)
        clock["t"] += readiness.RESIDENCY_LEASE_SECONDS + 1
        await turn.chat(MODEL, [{"role": "user", "content": "x"}])   # already prepared
        await turn.ensure_only(MODEL, exclusive=True)
    asyncio.run(go())
    assert engine.fake.sequence() == ["status", "chat", "status"]


def test_exclusive_check_evicts_a_keep_warm_model_it_previously_tolerated(engine):
    engine.client.set_keep_warm({MODEL, EMBEDDER})
    engine.fake.loaded = {MODEL, EMBEDDER}

    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL)                       # keep-warm both: nothing to do
        assert engine.fake.loaded == {MODEL, EMBEDDER}
        await turn.ensure_only(MODEL, exclusive=True)       # exclusive: only the model
        assert engine.fake.loaded == {MODEL}
    asyncio.run(go())
    assert engine.fake.sequence()[0] == "status" and "unload" in engine.fake.sequence()


def test_embedder_eviction_by_a_tool_forces_a_new_check(engine, monkeypatch):
    from service.router import reranker
    from service.search.embedder import embedding_model
    embedder_model = embedding_model()
    engine.fake.installed.append(embedder_model)

    async def tool_evicting():
        engine.fake.loaded.add(embedder_model)
        async with httpx.AsyncClient(base_url=BASE) as c:
            await reranker._evict_embedder(c, {})
        return "evicted"

    tools = register_tools(monkeypatch, names=("fake_a",), func=tool_evicting)
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools)
    seq = engine.fake.sequence()
    show("embedder eviction tool", engine)
    # step 0, the tool's own status/unload/status, then the step check, then the answer
    assert seq == ["status", "chat", "status", "unload", "status", "status", "chat"]


def test_a_failed_engine_start_is_not_remembered_as_ready(engine):
    engine.fake.down = True

    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        with pytest.raises(ModelLoadError):
            await turn.ensure_only(MODEL, exclusive=True)
        assert not turn._engine_ready and turn._residency is None
        engine.fake.down = False
        await turn.ensure_only(MODEL, exclusive=True)          # the engine came back
        assert turn._engine_ready
    asyncio.run(go())


def test_a_check_with_other_keep_warm_models_never_claims_the_model_is_alone(engine, monkeypatch):
    """After a non-exclusive check the resident set is only known to be a subset of
    the keep set, so a later exclusive check must still evict the other keep-warm model."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(readiness, "_now", lambda: clock["t"])
    engine.client.set_keep_warm({MODEL, EMBEDDER})
    engine.fake.loaded = {MODEL, EMBEDDER}

    async def go():
        turn = TurnInferenceClient(engine.client, main.ensure_omlx)
        await turn.ensure_only(MODEL)                       # probe read covers it
        clock["t"] += readiness.RESIDENCY_LEASE_SECONDS + 1
        await turn.ensure_only(MODEL)                       # real check, nothing to change
        await turn.ensure_only(MODEL, exclusive=True)
        assert engine.fake.loaded == {MODEL}
    asyncio.run(go())
    assert engine.fake.sequence() == ["status", "status", "status", "unload", "status"]


# ------------------------------------------------------- epoch contract

def test_every_generating_or_mutating_client_call_bumps_the_epoch_once(engine):
    async def go():
        marks = {}
        c = engine.client
        e = engine_epoch.current()
        await c.chat(MODEL, [{"role": "user", "content": "x"}])
        marks["chat"] = engine_epoch.current() - e
        e = engine_epoch.current()
        async for _ in c.stream_events(MODEL, [{"role": "user", "content": "x"}]):
            pass
        marks["stream"] = engine_epoch.current() - e
        e = engine_epoch.current()
        await c.load(OTHER)
        marks["load"] = engine_epoch.current() - e
        e = engine_epoch.current()
        await c.unload(OTHER)
        marks["unload"] = engine_epoch.current() - e
        e = engine_epoch.current()
        c.invalidate_connections()
        marks["invalidate"] = engine_epoch.current() - e
        e = engine_epoch.current()
        await c.health()
        await c.status()
        await c.models()
        await c.loaded_models()
        marks["reads"] = engine_epoch.current() - e
        return marks
    marks = asyncio.run(go())
    assert marks == {"chat": 1, "stream": 1, "load": 1, "unload": 1, "invalidate": 1, "reads": 0}


def test_a_read_only_poller_does_not_invalidate_the_proof(engine, monkeypatch):
    """Background status pollers (capture, idle unloader) never change residency."""
    async def tool_polling():
        await engine.client.loaded_models()
        return "polled"

    tools = register_tools(monkeypatch, names=("fake_a",), func=tool_polling)
    engine.fake.script = [["fake_a"], None]
    loop_turn(engine, tools)
    show("poller", engine)
    assert engine.fake.sequence() == ["status", "chat", "status", "chat"]

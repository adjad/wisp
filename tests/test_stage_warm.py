"""Demo mode's load-and-warm (service/stage_warm.py).

Fake engine clients, a synthetic tool registry and an injected sleep only. No
model, engine, network, server, subprocess, real sleep or user data.
"""
import asyncio
import copy
import time

import pytest

from service import idle, memory_saver, stage_mode, stage_warm
from service.agent import loop
from service.memory import prompt_blocks
from tests.test_model_led_integration import Approval, ScriptedClient
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

MODEL = "synthetic-agent"


class FakeEngine:
    """Records every engine call in order. `chat` is the warm request."""
    managed = True
    endpoint_name = "omlx"
    context_window = 32000
    base_url = "http://127.0.0.1:1"

    def __init__(self, loaded=(), rows=None, fail_load=False, fail_chat=False):
        self.loaded, self.rows = set(loaded), rows
        self.fail_load, self.fail_chat = fail_load, fail_chat
        self.calls, self.chats = [], []
        self.target = self

    async def loaded_models(self):
        self.calls.append("loaded_models")
        return sorted(self.loaded)

    async def status(self):
        self.calls.append("status")
        return {"models": self.rows or []}

    async def load(self, model):
        self.calls.append(("load", model))
        if self.fail_load:
            raise RuntimeError("synthetic load failure")
        self.loaded.add(model)

    async def chat(self, model, messages, **kwargs):
        self.calls.append("chat")
        self.chats.append(copy.deepcopy({"model": model, "messages": messages, **kwargs}))
        if self.fail_chat:
            raise RuntimeError("synthetic generation failure")
        return {"choices": [{"message": {"role": "assistant", "content": "H"}}],
                "usage": {"prompt_tokens": 1234, "prompt_tokens_details": {"cached_tokens": 0}}}


@pytest.fixture
def warm(monkeypatch, synthetic):  # noqa: F811
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.setattr(loop, "audit", lambda *a, **k: None)
    monkeypatch.setattr(prompt_blocks, "memory_block", lambda **k: "")
    monkeypatch.setattr(prompt_blocks, "now_line", lambda **k: "Now: synthetic time.")
    monkeypatch.setattr(stage_mode, "_since", None)
    monkeypatch.setattr(stage_mode, "_listeners", [])
    for name, value in (("_client", None), ("_installed", False), ("_task", None), ("_wake", None),
                        ("_warming", False), ("_last_attempt", None)):
        monkeypatch.setattr(stage_warm, name, value)
    monkeypatch.setattr(stage_warm, "_state", {"loaded": None, "warmed_at": None, "skipped": None,
                                              "prefix_tokens": None, "cached_tokens": None})
    monkeypatch.setattr(stage_warm, "_model", lambda: MODEL)
    monkeypatch.setattr(memory_saver, "bundled_engine", lambda client=None: True)
    monkeypatch.setattr(memory_saver, "omlx_app", lambda client=None: False)
    monkeypatch.setattr(memory_saver, "_prewarm_task", None)
    monkeypatch.setattr(idle, "_in_flight", {})
    clock = [1000.0]
    monkeypatch.setattr(stage_warm, "_clock", lambda: clock[0])

    async def no_beat(seconds):
        # Demo mode ends at the first keep-alive nap unless a test says otherwise.
        stage_mode.set_active(False)
    monkeypatch.setattr(stage_warm, "_sleep", no_beat)

    def setup(engine):
        monkeypatch.setattr(stage_warm, "_turn_client", lambda: engine)
        return engine
    setup.clock = clock
    return setup


async def _settle():
    task = stage_warm._task
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            pass


def _turn_on_and_run(engine):
    async def go():
        stage_warm.install(engine)
        stage_mode.set_active(True)
        await _settle()
    asyncio.run(go())


def real_ask_first_request(prompt="hi"):
    """What a real Ask turn sends first: run_agent with the arguments main.py's /agent
    passes on a model-only, model-led turn of a fresh chat (written out here from
    service/main.py's run_agent call, not taken from stage_warm)."""
    from service import main
    from service import model_only as mo
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    forbidden, bindings = routing_guard_contract(prompt, model_led=True)
    preload, stub_rank = main._ask_tool_rank(prompt, "")
    guidance = [mo.ASK_STYLE_HINT, main.MODEL_LED_UNTRUSTED_WEB_HINT]
    client = ScriptedClient(["Synthetic hello."])

    async def emit(event):
        pass
    asyncio.run(loop.run_agent(
        client, MODEL, [{"role": "user", "content": prompt}], emit, Approval(),
        tools=[], active_skill="", allow_skill_context=True, include_identity_context=True,
        force_first_tool=None, expect_tool_first=False, short_circuit_tools=frozenset(),
        style_hint="".join("\n" + g for g in guidance), public_web_synthesis=False,
        model_led_discovery=True, picture_reading=None, picture_label=None,
        model_led_preload=preload, ask_quality=True, ask_direct=False,
        model_led_stub_rank=stub_rank, earlier_effects=False, private_shell_history=False,
        fresh_personal_scope=None,
        claim_effect=lambda tool, args: {"admitted": False},
        finish_effect=lambda claim, ok: True,
        include_memory_context=True, multi_round=True, narration_after=frozenset(),
        direct_calls=[], required_tool_groups=(),
        forbidden_tools=frozenset(forbidden) | mo.ask_forbidden_tools(prompt, REGISTRY),
        forbidden_reason="", conditional_tools=(), tool_argument_bindings=bindings,
        strict_read_limits=None, reminder_action="", test_mode=False, debug=False,
        planner=False, act_first=frozenset()))
    return client.requests[0]


# --- turning it on ---------------------------------------------------------------

def test_on_loads_then_warms_once_in_that_order(warm):
    engine = warm(FakeEngine())
    _turn_on_and_run(engine)
    assert engine.calls == ["loaded_models", ("load", MODEL), "chat"]
    s = stage_warm.state()
    assert s["loaded"] is True and s["warmed_at"] is not None and s["skipped"] is None
    assert s["prefix_tokens"] == 1234 and s["cached_tokens"] == 0


def test_a_resident_model_is_not_loaded_again(warm):
    engine = warm(FakeEngine(loaded={MODEL}))
    _turn_on_and_run(engine)
    assert engine.calls == ["loaded_models", "chat"]


def test_the_warm_request_is_the_real_ask_turns_first_request(warm):
    engine = warm(FakeEngine(loaded={MODEL}))
    _turn_on_and_run(engine)
    sent = engine.chats[0]
    real = real_ask_first_request()
    assert sent["model"] == MODEL
    assert sent["messages"][0] == real["messages"][0]          # system prompt
    assert sent["messages"][0]["role"] == "system" and sent["messages"][0]["content"]
    assert sent["tools"] == real["tools"]                       # tool list, same order
    assert sent["tools"]
    assert sent["messages"] == real["messages"]                 # runtime block and "hi" too
    assert sent.get("chat_template_kwargs") == real.get("chat_template_kwargs")
    assert sent["tool_choice"] == real["tool_choice"]
    assert sent["max_tokens"] == stage_warm.WARM_MAX_TOKENS <= 4


def test_nothing_runs_and_nothing_is_stored_or_counted_as_a_turn(warm, synthetic, monkeypatch):  # noqa: F811
    effects, _ = synthetic
    engine = warm(FakeEngine(loaded={MODEL}))
    noted = []
    monkeypatch.setattr(memory_saver, "note_intent", lambda *a, **k: noted.append(1))
    from service.memory.store import SessionStore
    for name in ("add_turn", "create_session", "claim_model_effect", "set_pinned"):
        if hasattr(SessionStore, name):
            monkeypatch.setattr(SessionStore, name, lambda *a, **k: pytest.fail("store write"))
    _turn_on_and_run(engine)
    assert engine.chats and effects == []
    assert idle.last_turn() is None and not idle.foreground_busy() and noted == []


def test_a_second_on_while_warming_starts_no_second_task(warm):
    engine = warm(FakeEngine())

    async def go():
        stage_warm.install(engine)
        stage_mode.set_active(True)
        first = stage_warm._task
        stage_warm._on_change(True)                 # pushed again
        stage_warm.install(engine)
        assert stage_warm._task is first
        await _settle()
    asyncio.run(go())
    assert engine.calls.count("chat") == 1


def test_off_cancels_a_warm_up_still_running(warm):
    engine = warm(FakeEngine())
    gate = asyncio.Event

    async def go():
        hold = gate()

        async def slow_load(model):
            engine.calls.append(("load", model))
            await hold.wait()
        engine.load = slow_load
        stage_warm.install(engine)
        stage_mode.set_active(True)
        task = stage_warm._task
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        stage_mode.set_active(False)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    asyncio.run(go())
    assert "chat" not in engine.calls


def test_no_running_loop_does_nothing(warm):
    engine = warm(FakeEngine())
    stage_warm.install(engine)
    stage_mode.set_active(True)          # called outside any loop: the switch still works
    assert stage_warm._task is None and stage_mode.active()


# --- failures ---------------------------------------------------------------------

def test_a_load_failure_is_swallowed_and_recorded(warm):
    engine = warm(FakeEngine(fail_load=True))
    _turn_on_and_run(engine)
    s = stage_warm.state()
    assert s["loaded"] is False and s["skipped"] == "load_failed:RuntimeError"
    assert "chat" not in engine.calls


def test_a_generation_failure_is_swallowed_and_recorded(warm):
    engine = warm(FakeEngine(loaded={MODEL}, fail_chat=True))
    _turn_on_and_run(engine)
    s = stage_warm.state()
    assert s["skipped"] == "warm_failed:RuntimeError" and s["warmed_at"] is None


# --- the user's own oMLX app ------------------------------------------------------

def _gemma_row(model, **extra):
    return {"id": model, "loaded": False, "config_model_type": "gemma4", "last_access": 1.0,
            "pinned": False, **extra}


@pytest.mark.parametrize("rows,reason", [
    ([_gemma_row(MODEL.replace("synthetic", "gemma")), ], "omlx:not_reported"),    # ours not listed
    ([_gemma_row("gemma-x"), {"id": "their-model", "loaded": True}], "omlx:other_models"),
])
def test_on_the_users_own_omlx_it_skips_rather_than_evict(warm, monkeypatch, rows, reason):
    monkeypatch.setattr(memory_saver, "bundled_engine", lambda client=None: False)
    monkeypatch.setattr(memory_saver, "omlx_app", lambda client=None: True)
    monkeypatch.setattr(stage_warm, "_model", lambda: "gemma-x")
    engine = warm(FakeEngine(rows=rows))
    _turn_on_and_run(engine)
    assert not any(isinstance(c, tuple) and c[0] == "load" for c in engine.calls)
    assert stage_warm.state()["skipped"] == reason and stage_warm.state()["loaded"] is False


def test_another_local_server_is_left_alone(warm, monkeypatch):
    monkeypatch.setattr(memory_saver, "bundled_engine", lambda client=None: False)
    engine = warm(FakeEngine())
    _turn_on_and_run(engine)
    assert engine.calls == [] and stage_warm.state()["skipped"] == "external"


# --- keep-alive -------------------------------------------------------------------

def test_keep_alive_resends_every_beat_skips_a_busy_one_and_stops_when_off(warm, monkeypatch):
    engine = warm(FakeEngine(loaded={MODEL}))
    naps = []

    async def beat(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            idle.begin_foreground()        # a turn runs through the second beat
        if len(naps) == 3:
            idle.end_foreground()
        if len(naps) == 4:
            stage_mode.set_active(False)
    monkeypatch.setattr(stage_warm, "_sleep", beat)
    _turn_on_and_run(engine)
    assert naps == [stage_warm.KEEP_ALIVE_S] * 4
    # first warm, beat 1, (beat 2 skipped: turn), beat 3; off at beat 4
    assert engine.calls.count("chat") == 3
    assert engine.chats[0]["messages"] == engine.chats[-1]["messages"]


def test_keep_alive_stops_when_demo_mode_expires(warm, monkeypatch):
    engine = warm(FakeEngine(loaded={MODEL}))

    async def beat(seconds):
        monkeypatch.setattr(stage_mode, "_since", -stage_mode.MAX_S * 2.0)   # four hours passed
    monkeypatch.setattr(stage_warm, "_sleep", beat)
    _turn_on_and_run(engine)
    assert engine.calls.count("chat") == 1


def test_keep_alive_skips_while_a_call_of_wisps_own_is_on_the_model(warm, monkeypatch):
    engine = warm(FakeEngine(loaded={MODEL}))
    naps = []

    async def beat(seconds):
        naps.append(seconds)
        if len(naps) == 1:
            idle.begin(MODEL)
        if len(naps) == 2:
            idle.end(MODEL)
        if len(naps) == 3:
            stage_mode.set_active(False)
    monkeypatch.setattr(stage_warm, "_sleep", beat)
    _turn_on_and_run(engine)
    assert engine.calls.count("chat") == 2


# --- rewarm -----------------------------------------------------------------------

def test_rewarm_does_nothing_when_demo_mode_is_off(warm):
    warm(FakeEngine())
    assert asyncio.run(stage_warm.rewarm("test")) == "off"
    assert stage_warm._task is None


def test_rewarm_does_nothing_while_a_turn_runs(warm, monkeypatch):
    warm(FakeEngine())
    monkeypatch.setattr(stage_mode, "_since", 0.0)
    monkeypatch.setattr(stage_mode, "active", lambda now=None: True)
    idle.begin_foreground()
    try:
        assert asyncio.run(stage_warm.rewarm("test")) == "turn"
    finally:
        idle.end_foreground()
    assert stage_warm._task is None


def test_rewarm_is_debounced_and_restarts_a_finished_task(warm):
    engine = warm(FakeEngine(loaded={MODEL}))

    async def go():
        stage_warm.install(engine)
        stage_mode.set_active(True)
        await _settle()                         # warmed, then Demo mode ended at the nap
        assert engine.calls.count("chat") == 1 and stage_warm._task.done()
        stage_mode._since = time.monotonic()  # on again, without the listener restarting it
        warm.clock[0] += 10
        assert await stage_warm.rewarm("test") == "debounced"
        assert stage_warm._task.done()
        warm.clock[0] += stage_warm.REWARM_MIN_S
        assert await stage_warm.rewarm("test") == "started"
        await _settle()
    asyncio.run(go())
    assert engine.calls.count("chat") == 2


def test_rewarm_wakes_the_keep_alive_loop_instead_of_starting_another(warm, monkeypatch):
    engine = warm(FakeEngine(loaded={MODEL}))
    naps = []

    async def beat(seconds):
        naps.append(seconds)
        if len(naps) == 1:
            await asyncio.Event().wait()       # a long nap: only a rewarm ends it
        stage_mode.set_active(False)
    monkeypatch.setattr(stage_warm, "_sleep", beat)

    async def go():
        stage_warm.install(engine)
        stage_mode.set_active(True)
        task = stage_warm._task
        for _ in range(20):
            await asyncio.sleep(0)
        assert engine.calls.count("chat") == 1 and naps == [stage_warm.KEEP_ALIVE_S]
        warm.clock[0] += stage_warm.REWARM_MIN_S
        assert await stage_warm.rewarm("test") == "woken"
        assert stage_warm._task is task
        await _settle()
    asyncio.run(go())
    assert engine.calls.count("chat") == 2


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_stage_warm.py"' in manifest

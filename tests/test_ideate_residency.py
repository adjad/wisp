"""Ideate's residency: only Inkling is resident during a run, nothing loads beside it, and
Inkling is unloaded after the run. A recording stand-in engine client; no model, no network."""
from __future__ import annotations

import asyncio

import pytest

from service import idle
from service.ideate import config, engine
from service.inference import engine_hold, omlx_client
from service.inference.engine_hold import ModelHeldError

INK = config.MODEL_ID
ASK = "Invented-Chat-Model-4bit"
OTHER = "Invented-Summarizer-2bit"


class RecordingEngine:
    """The engine's model table plus the order of every unload/load call."""

    def __init__(self, loaded):
        self.loaded = list(loaded)
        self.calls: list[tuple[str, str]] = []

    async def loaded_models(self):
        return list(self.loaded)

    async def unload(self, model):
        self.calls.append(("unload", model))
        self.loaded = [m for m in self.loaded if m != model]

    async def post_load(self, model):
        self.calls.append(("load", model))
        self.loaded.append(model)


def _client(rec: RecordingEngine, monkeypatch) -> omlx_client.OMLXClient:
    """A real OMLXClient whose engine calls go to the recorder (the swap logic is the real one)."""
    c = omlx_client.OMLXClient.__new__(omlx_client.OMLXClient)
    c._keep_warm = {OTHER}
    monkeypatch.setattr(omlx_client.OMLXClient, "managed", property(lambda self: True), raising=False)
    monkeypatch.setattr(c, "loaded_models", rec.loaded_models, raising=False)
    monkeypatch.setattr(c, "unload", rec.unload, raising=False)

    async def load(model):
        engine_hold.check(model)
        await rec.post_load(model)
    monkeypatch.setattr(c, "load", load, raising=False)
    from service import memory_saver
    monkeypatch.setattr(memory_saver, "omlx_app", lambda client: False)
    monkeypatch.setattr(memory_saver, "prewarm_pending", lambda: False)

    async def settled(timeout=15.0):
        return None
    monkeypatch.setattr(memory_saver, "settle_prewarm", settled)
    return c


@pytest.fixture(autouse=True)
def _clean_hold():
    engine_hold.release(engine_hold.holder() or "")
    yield
    engine_hold.release(engine_hold.holder() or "")


def test_hold_refuses_other_models_only():
    engine_hold.check(ASK)                      # no hold: anything goes
    engine_hold.hold(INK, "Ideate")
    engine_hold.check(INK)
    with pytest.raises(ModelHeldError) as err:
        engine_hold.check(ASK)
    assert err.value.reason == "model_held" and "Ideate" in str(err.value)
    assert "Ask again when the build finishes" in str(err.value)
    engine_hold.release(ASK)                    # someone else's release does nothing
    assert engine_hold.holder() == INK
    engine_hold.release(INK)
    engine_hold.check(ASK)


def test_run_start_unloads_every_other_model_then_loads_inkling(monkeypatch):
    rec = RecordingEngine([ASK, OTHER])
    c = _client(rec, monkeypatch)
    engine_hold.hold(INK, "Ideate")
    asyncio.run(c._ensure_managed(INK, settle_timeout=5, exclusive=True))
    unloads = [m for op, m in rec.calls if op == "unload"]
    assert sorted(unloads) == sorted([ASK, OTHER])        # the keep-warm model goes too
    assert rec.calls[-1] == ("load", INK)                 # loaded only after the others
    assert all(op == "unload" for op, _ in rec.calls[:-1])
    assert rec.loaded == [INK]


def test_inkling_already_loaded_still_clears_the_rest(monkeypatch):
    rec = RecordingEngine([INK, ASK])
    c = _client(rec, monkeypatch)
    engine_hold.hold(INK, "Ideate")
    asyncio.run(c._ensure_managed(INK, settle_timeout=5, exclusive=True))
    assert rec.calls == [("unload", ASK)] and rec.loaded == [INK]


def test_ask_during_a_run_is_refused_before_anything_unloads(monkeypatch):
    rec = RecordingEngine([INK])
    c = _client(rec, monkeypatch)
    engine_hold.hold(INK, "Ideate")
    with pytest.raises(ModelHeldError):
        asyncio.run(c._ensure_managed(ASK, settle_timeout=5))
    with pytest.raises(ModelHeldError):
        asyncio.run(c.load(ASK))
    assert rec.calls == [] and rec.loaded == [INK]        # Inkling untouched, never two resident


def test_after_the_run_ask_swaps_the_normal_way(monkeypatch):
    rec = RecordingEngine([INK])
    c = _client(rec, monkeypatch)
    asyncio.run(c._ensure_managed(ASK, settle_timeout=5))
    # Inkling goes first, then Ask's model; the keep-warm reload is Ask's usual best effort.
    assert rec.calls[:2] == [("unload", INK), ("load", ASK)] and INK not in rec.loaded


class _Residency(engine.Residency):
    def __init__(self, client):
        super().__init__()
        self.fake = client

    def _get_client(self):
        return self.fake


def _release_world(monkeypatch, loaded, *, bundled=True):
    rec = RecordingEngine(loaded)
    c = _client(rec, monkeypatch)
    monkeypatch.setattr(engine, "bundled", lambda: bundled)
    return rec, _Residency(c)


def test_release_unloads_inkling_and_loads_nothing(monkeypatch):
    rec, res = _release_world(monkeypatch, [INK])
    assert asyncio.run(res.release_now()) is True
    assert rec.calls == [("unload", INK)] and rec.loaded == []   # Ask's model waits for Ask


@pytest.mark.parametrize("why", ["held", "in_flight", "not_loaded", "own_omlx"])
def test_release_leaves_it_alone(monkeypatch, why):
    rec, res = _release_world(monkeypatch, [ASK] if why == "not_loaded" else [INK],
                              bundled=why != "own_omlx")
    if why == "held":
        engine_hold.hold(INK, "Ideate")
    if why == "in_flight":
        idle.begin(INK)
    try:
        assert asyncio.run(res.release_now()) is False
    finally:
        if why == "in_flight":
            idle.end(INK)
    assert rec.calls == []


def test_begin_end_order_and_the_grace_timer(monkeypatch):
    """begin holds and registers; end releases, then the unload fires after the grace;
    a run starting inside the grace cancels it."""
    monkeypatch.setattr(config, "RELEASE_AFTER_S", 0.05)
    rec, res = _release_world(monkeypatch, [INK])
    monkeypatch.setattr(engine, "config", config)

    async def scenario():
        res.bind_loop(asyncio.get_running_loop())
        res.begin()
        assert engine_hold.holder() == INK and idle.in_flight(INK) == 1 and idle.foreground_busy()
        res.end()
        assert engine_hold.holder() is None and idle.in_flight(INK) == 0
        await asyncio.sleep(0.01)
        res.begin()                       # a follow-up inside the grace
        await asyncio.sleep(0.15)
        assert rec.calls == []            # cancelled: Inkling stays for the follow-up
        res.end()
        await asyncio.sleep(0.2)
        assert rec.calls == [("unload", INK)]

    asyncio.run(scenario())


def test_wait_for_ask_returns_when_the_turn_ends(monkeypatch):
    busy = iter([True, True, False])
    monkeypatch.setattr(idle, "foreground_busy", lambda: next(busy, False))
    engine.Residency().wait_for_ask(timeout=2, poll=0.01)
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    import time
    t = time.monotonic()
    engine.Residency().wait_for_ask(timeout=0.05, poll=0.01)
    assert time.monotonic() - t < 1


def test_config_rules():
    assert config.RELEASE_AFTER_S > 0 and 0 < config.ASK_WAIT_S <= 60

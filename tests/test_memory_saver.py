"""Save memory (service/memory_saver.py): when the chat model is parked, when a
batch window brings it back, and the switch. A fake client; no engine is contacted."""
import asyncio
import json
import stat

import pytest

from service import idle, idle_unloader
from service import memory_saver as M

MODEL = "chat-model"
MIN = 60.0
REAL_PRESSURE = M.pressure_level      # before the fixture swaps it out


class FakeClient:
    managed = True

    def __init__(self, loaded=True):
        self.loaded = {MODEL} if loaded else set()
        self.calls = []

    def keep_warm(self):
        return {MODEL}

    async def loaded_models(self):
        return sorted(self.loaded)

    async def unload(self, model):
        self.calls.append(("unload", model))
        self.loaded.discard(model)

    async def load(self, model):
        self.calls.append(("load", model))
        self.loaded.add(model)


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "SETTINGS_PATH", tmp_path / "memory_saver" / "settings.json")
    monkeypatch.setattr(M, "_state", M._State(started=0.0))
    monkeypatch.setattr(M, "_prewarm_task", None)
    monkeypatch.setattr(M, "pressure_level", lambda: M.NORMAL)
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_last_turn", None)
    monkeypatch.setattr(idle, "_in_flight", {})
    monkeypatch.setattr(idle, "_last_used", {})
    import service.config
    monkeypatch.setattr(service.config, "role_to_model", lambda role: MODEL)
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)


def tick(client, now, pressure=M.NORMAL):
    return asyncio.run(M.tick(client, now, pressure=pressure))


# --- parking ----------------------------------------------------------------------

def test_the_model_stays_for_30_minutes_after_a_turn_then_parks():
    c = FakeClient()
    idle._last_turn = 0.0
    assert tick(c, 29 * MIN) == M.KEEP
    assert MODEL in c.loaded
    assert tick(c, 30 * MIN) == M.PARK
    assert c.calls == [("unload", MODEL)] and M._state.parked


def test_background_calls_never_extend_the_park_timer():
    c = FakeClient()
    idle._last_turn = 0.0
    idle.touch(MODEL)                      # a gist call, minutes later
    idle._last_used[MODEL] = 29 * MIN
    assert tick(c, 30 * MIN) == M.PARK


def test_never_unloads_during_a_turn_or_a_generation():
    c = FakeClient()
    idle._last_turn = 0.0
    idle._foreground_turns = 1
    assert tick(c, 60 * MIN) == M.KEEP
    idle._foreground_turns = 0
    idle._in_flight[MODEL] = 1
    assert tick(c, 60 * MIN) == M.KEEP
    assert c.calls == []


def test_intent_holds_the_model_five_minutes():
    c = FakeClient()
    idle._last_turn = 0.0
    M.note_intent(28 * MIN)
    assert tick(c, 32 * MIN) == M.KEEP
    assert tick(c, 33 * MIN) == M.PARK


def test_memory_pressure_parks_after_two_quiet_minutes():
    c = FakeClient()
    idle._last_turn = 10 * MIN
    assert tick(c, 11 * MIN, pressure=M.WARN) == M.KEEP      # the user just asked
    assert tick(c, 12 * MIN, pressure=M.CRITICAL) == M.PARK
    assert M._state.last_reason == "pressure"


def test_unknown_pressure_never_parks_early():
    c = FakeClient()
    idle._last_turn = 0.0
    assert tick(c, 10 * MIN, pressure=M.UNKNOWN) == M.KEEP


def test_a_model_wisp_did_not_park_is_left_alone():
    c = FakeClient(loaded=False)
    assert tick(c, 5 * 60 * MIN) == M.LEAVE
    assert c.calls == []


# --- batch windows ----------------------------------------------------------------

def test_an_hour_after_parking_a_window_loads_the_model_and_closes_when_quiet():
    c = FakeClient()
    idle._last_turn = 0.0
    assert tick(c, 30 * MIN) == M.PARK
    assert tick(c, 89 * MIN) == M.LEAVE
    assert tick(c, 90 * MIN) == M.OPEN_WINDOW and MODEL in c.loaded
    # A quiet window still waits BATCH_MIN_S, so a job that stood down just
    # before it opened (the proactive cycle waits 300 s) gets its turn.
    assert tick(c, 95 * MIN) == M.KEEP
    # Background work runs; the window closes 2 quiet minutes after it.
    idle._last_used[MODEL] = 95 * MIN
    assert tick(c, 96 * MIN) == M.KEEP
    assert tick(c, 97 * MIN) == M.CLOSE_WINDOW and MODEL not in c.loaded
    assert M._state.parked and M._state.last_reason == "batch done"
    assert M.BATCH_MIN_S > 300 + 30
    # The next window is an hour after the last one opened.
    assert tick(c, 149 * MIN) == M.LEAVE
    assert tick(c, 150 * MIN) == M.OPEN_WINDOW


def test_a_busy_window_closes_at_its_limit():
    c = FakeClient()
    idle._last_turn = 0.0
    tick(c, 30 * MIN)
    tick(c, 90 * MIN)
    idle._last_used[MODEL] = 104 * MIN
    assert tick(c, 104 * MIN) == M.KEEP
    assert tick(c, 105 * MIN) == M.CLOSE_WINDOW


def test_pressure_closes_a_window_and_keeps_new_ones_shut():
    c = FakeClient()
    idle._last_turn = 0.0
    tick(c, 30 * MIN)
    tick(c, 90 * MIN)
    assert tick(c, 91 * MIN, pressure=M.WARN) == M.CLOSE_WINDOW
    assert tick(c, 200 * MIN, pressure=M.WARN) == M.LEAVE
    assert c.calls[-1] == ("unload", MODEL)


def test_a_user_who_comes_back_during_a_window_keeps_the_model():
    c = FakeClient()
    idle._last_turn = 0.0
    tick(c, 30 * MIN)
    tick(c, 90 * MIN)
    idle._last_turn = 91 * MIN
    assert tick(c, 100 * MIN) == M.KEEP and M._state.window_started is None
    assert not M._state.parked
    assert tick(c, 120 * MIN) == M.KEEP
    assert tick(c, 121 * MIN) == M.PARK


def test_a_turn_that_reloads_a_parked_model_clears_parked():
    c = FakeClient()
    idle._last_turn = 0.0
    tick(c, 30 * MIN)
    c.loaded.add(MODEL)                    # the turn's ensure_only
    idle._last_turn = 40 * MIN
    assert tick(c, 41 * MIN) == M.KEEP and not M._state.parked


def test_the_notch_hiding_counts_as_parked_for_windows():
    c = FakeClient(loaded=False)
    M.note_parked("notch hidden", now=10 * MIN)
    assert tick(c, 69 * MIN) == M.LEAVE
    assert tick(c, 70 * MIN) == M.OPEN_WINDOW


def test_a_failed_window_load_waits_for_the_next_window():
    class Refusing(FakeClient):
        async def load(self, model):
            raise RuntimeError("507")
    c = Refusing(loaded=False)
    M.note_parked("idle", now=0.0)
    assert tick(c, 60 * MIN) == M.LEAVE and M._state.window_started is None
    assert tick(c, 61 * MIN) == M.LEAVE


# --- the switch -------------------------------------------------------------------

def test_off_keeps_the_model_and_brings_back_a_parked_one():
    c = FakeClient()
    idle._last_turn = 0.0
    tick(c, 30 * MIN)
    M.set_save_memory(False)
    assert tick(c, 31 * MIN) == M.LEAVE and MODEL in c.loaded
    assert tick(c, 500 * MIN) == M.LEAVE and MODEL in c.loaded


def test_the_switch_is_on_by_default_persists_and_reads_unreadable_as_the_default():
    assert M.save_memory() is True
    assert M.set_save_memory(False) is False and M.save_memory() is False
    assert json.loads(M.SETTINGS_PATH.read_text()) == {"save_memory": False}
    assert stat.S_IMODE(M.SETTINGS_PATH.stat().st_mode) == 0o600
    M.SETTINGS_PATH.write_text("{not json")
    assert M.save_memory() is True
    with pytest.raises(ValueError):
        M.set_save_memory("false")


def test_the_endpoint_changes_the_switch_from_the_app_only():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.memory_saver_api import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/memory_saver").json()["save_memory"] is True
    assert client.post("/memory_saver", json={"save_memory": False}).json()["save_memory"] is False
    assert M.save_memory() is False
    assert client.post("/memory_saver", json={"save_memory": True},
                       headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/memory_saver", content="save_memory=true",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/memory_saver", json={"save_memory": "true"}).status_code == 422
    assert M.save_memory() is False


# --- prewarm ----------------------------------------------------------------------

def test_prewarm_loads_a_parked_model_and_counts_as_intent():
    async def go():
        c = FakeClient(loaded=False)
        assert await M.prewarm(c) == "loading"
        await M._prewarm_task
        return c
    c = asyncio.run(go())
    assert c.calls == [("load", MODEL)] and M._state.last_intent is not None


def test_prewarm_leaves_a_running_turn_and_critical_pressure_alone(monkeypatch):
    async def go():
        c = FakeClient(loaded=False)
        idle._foreground_turns = 1
        busy = await M.prewarm(c)
        idle._foreground_turns = 0
        monkeypatch.setattr(M, "pressure_level", lambda: M.CRITICAL)
        return busy, await M.prewarm(c), c
    busy, pressure, c = asyncio.run(go())
    assert (busy, pressure) == ("busy", "pressure") and c.calls == []


def test_turns_set_the_last_turn_clock_and_background_calls_do_not():
    idle.begin(MODEL)
    idle.end(MODEL)
    assert idle.last_turn() is None
    idle.begin_foreground()
    assert idle.last_turn() is not None
    idle.end_foreground()


def test_the_unloader_loop_runs_a_saver_tick(monkeypatch):
    seen = []

    async def fake_tick(client):
        seen.append(client)
        raise asyncio.CancelledError

    monkeypatch.setattr(M, "tick", fake_tick)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(idle_unloader.asyncio, "sleep", no_sleep)
    c = FakeClient()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(idle_unloader.run(c))
    assert seen == [c]


def test_pressure_level_reads_a_known_level_and_never_raises():
    # The real sysctl on macOS (normal on an idle CI runner); unknown elsewhere.
    assert REAL_PRESSURE() in (M.NORMAL, M.WARN, M.CRITICAL, M.UNKNOWN)
    assert M._LEVELS == {1: M.NORMAL, 2: M.WARN, 4: M.CRITICAL}

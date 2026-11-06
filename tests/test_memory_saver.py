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


BUNDLED_URL = "http://127.0.0.1:%d"


class FakeClient:
    managed = True

    def __init__(self, loaded=True):
        from service.engine import BUNDLED_PORT
        self.base_url = BUNDLED_URL % BUNDLED_PORT
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
    monkeypatch.setattr(M, "_holds", 0)
    monkeypatch.setattr(M, "pressure_level", lambda: M.NORMAL)
    from service.engine import mode
    monkeypatch.setattr(mode, "is_bundled", lambda: True)
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


def test_critical_pressure_parks_after_two_quiet_minutes():
    c = FakeClient()
    idle._last_turn = 10 * MIN
    assert tick(c, 11 * MIN, pressure=M.CRITICAL) == M.KEEP  # the user just asked
    assert tick(c, 12 * MIN, pressure=M.CRITICAL) == M.PARK
    assert M._state.last_reason == "pressure"


def test_warn_pressure_waits_ten_minutes():
    # Warn is routine on a 16 GB Mac: parking 2 minutes after every question would
    # make most questions pay the reload.
    c = FakeClient()
    idle._last_turn = 10 * MIN
    assert tick(c, 12 * MIN, pressure=M.WARN) == M.KEEP
    assert tick(c, 19 * MIN, pressure=M.WARN) == M.KEEP
    assert tick(c, 20 * MIN, pressure=M.WARN) == M.PARK


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
    # Background work runs; the window closes 4 quiet minutes after it.
    idle._last_used[MODEL] = 95 * MIN
    assert tick(c, 98 * MIN) == M.KEEP
    assert tick(c, 99 * MIN) == M.CLOSE_WINDOW and MODEL not in c.loaded
    assert M._state.parked and M._state.last_reason == "batch done"
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


def _window_then_user_returns(c):
    """Park at 30, a window at 90 that closes quiet at 96, then the user is back
    at 300 and the turn loads the model."""
    idle._last_turn = 0.0
    assert tick(c, 30 * MIN) == M.PARK
    assert tick(c, 90 * MIN) == M.OPEN_WINDOW
    assert tick(c, 96 * MIN) == M.CLOSE_WINDOW
    c.loaded.add(MODEL)
    idle._last_turn = 300 * MIN
    assert tick(c, 301 * MIN) == M.KEEP and not M._state.parked


def test_an_idle_park_counts_its_hour_from_the_park_not_an_old_window():
    c = FakeClient()
    _window_then_user_returns(c)
    assert tick(c, 330 * MIN) == M.PARK
    loads = c.calls.count(("load", MODEL))
    assert tick(c, 330.5 * MIN) == M.LEAVE
    assert tick(c, 389 * MIN) == M.LEAVE
    assert c.calls.count(("load", MODEL)) == loads
    assert tick(c, 390 * MIN) == M.OPEN_WINDOW


def test_the_notch_x_counts_its_hour_from_the_x_not_an_old_window():
    c = FakeClient()
    _window_then_user_returns(c)
    c.loaded.discard(MODEL)                  # /unload_agent
    M.note_notch_hidden(now=310 * MIN)
    assert tick(c, 310.5 * MIN) == M.LEAVE
    assert tick(c, 369 * MIN) == M.LEAVE
    assert tick(c, 370 * MIN) == M.OPEN_WINDOW


def test_a_window_closed_by_pressure_still_counts_from_that_window():
    c = FakeClient()
    idle._last_turn = 0.0
    tick(c, 30 * MIN)
    assert tick(c, 90 * MIN) == M.OPEN_WINDOW
    assert tick(c, 91 * MIN, pressure=M.WARN) == M.CLOSE_WINDOW
    assert M._state.last_reason == "pressure"
    assert tick(c, 149 * MIN) == M.LEAVE
    assert tick(c, 150 * MIN) == M.OPEN_WINDOW


def test_a_turn_waits_for_a_window_load_instead_of_loading_twice():
    class Slow(FakeClient):
        async def load(self, model):
            self.calls.append(("load", model))
            await asyncio.sleep(0.05)
            self.loaded.add(model)

    async def go():
        c = Slow(loaded=False)
        M.note_parked("idle", now=0.0)
        window = asyncio.create_task(M.tick(c, 60 * MIN, pressure=M.NORMAL))
        for _ in range(10):
            if M.prewarm_pending():
                break
            await asyncio.sleep(0)
        assert M.prewarm_pending()
        await M.settle_prewarm(timeout=2)    # what OMLXClient._ensure_managed awaits first
        assert MODEL in c.loaded
        assert await window == M.OPEN_WINDOW
        return c
    c = asyncio.run(go())
    assert c.calls == [("load", MODEL)]


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


def test_window_timings_outlast_every_jobs_retry():
    # Each job stood down while parked; the window must last until its retry.
    from service.assistant import scheduler
    from service.mail_memory import runner
    from service.proactive import cycle
    assert M.BATCH_MIN_S > cycle.IDLE_GAP_S + scheduler.TICK_S
    assert M.BATCH_QUIET_S >= runner.QUIET_AFTER_USE_S + runner.BUSY_GAP_S + scheduler.TICK_S


# --- holds: user-started work that loads the model itself -------------------------

def test_a_hold_keeps_the_model_across_ticks_between_calls():
    c = FakeClient()
    idle._last_turn = 0.0
    with M.hold():
        for minute in range(30, 61, 1):     # a long Research job, idle between fetches
            assert tick(c, minute * MIN) == M.KEEP
        with M.hold():                      # holds overlap
            pass
        assert M.held()
    assert not M.held()
    assert tick(c, 61 * MIN) == M.PARK
    assert c.calls == [("unload", MODEL)]


def test_a_research_job_runs_under_a_hold():
    from service.research.orchestrator import ResearchManager
    seen = []

    async def fake_run(self, job_id, client):
        seen.append(M.held())

    manager = ResearchManager.__new__(ResearchManager)
    import types
    manager._run = types.MethodType(fake_run, manager)
    asyncio.run(manager._run_held("job", None))
    assert seen == [True] and not M.held()


def test_a_search_answer_holds_the_model_and_counts_as_intent(monkeypatch):
    from service.search import synth
    seen = []

    async def fake(client, question, chunks, picks, **kwargs):
        seen.append(M.held())
        return {"answer": "x"}

    monkeypatch.setattr(synth, "_answer_upgraded", fake)
    assert asyncio.run(synth.answer(None, "q", [], [])) == {"answer": "x"}
    assert seen == [True] and not M.held() and M._state.last_intent is not None


# --- the notch's X ------------------------------------------------------------------

def test_the_notch_x_parks_only_with_the_switch_on():
    M.note_notch_hidden(now=0.0)
    assert M._state.parked
    M._state.parked = False
    M.set_save_memory(False)
    M.note_notch_hidden(now=0.0)
    assert not M._state.parked
    c = FakeClient(loaded=False)             # off: no reload 30 s after the X
    assert tick(c, 1 * MIN) == M.LEAVE and c.calls == []


# --- clients the saver leaves alone -------------------------------------------------

def test_an_unmanaged_client_or_a_model_not_kept_warm_is_left_alone():
    remote = FakeClient()
    remote.managed = False
    idle._last_turn = 0.0
    assert tick(remote, 60 * MIN) == M.LEAVE

    class Cold(FakeClient):
        def keep_warm(self):
            return set()
    cold = Cold()
    assert tick(cold, 60 * MIN) == M.LEAVE
    assert remote.calls == [] and cold.calls == []


# --- the user's own oMLX.app --------------------------------------------------------

GEMMA = "gemma-4-E4B-it-r4-step-01840-oQ4e-mtp"
QWEN = "Qwen3.6-27B-oQ3.5e"
BOLT = "NVIDIA-Nemotron-3.5-Bolt-E4B-MLX-oQ4e-mtp"     # a gemma4 config, not a Gemma
KINDS = {GEMMA: "gemma4", QWEN: "qwen3_5", BOLT: "gemma4"}


class FakeOmlx:
    """oMLX.app's HTTP surface as Wisp sees it. Any load or unload is recorded."""
    managed = True
    base_url = "http://127.0.0.1:8000"

    def __init__(self, chat=GEMMA, loaded=(GEMMA,)):
        from service.inference.providers import provider
        self.provider = provider("omlx")
        self.chat = chat
        self.loaded = set(loaded)
        self.loading = set()
        self.pinned = set()
        self.access = {}             # model -> last_access
        self.active = 0
        self.waiting = 0
        self.calls = []
        self.status_fails = False
        self.activity_fails = False
        self.drop_keys = ()

    def keep_warm(self):
        return {self.chat}

    async def status(self):
        if self.status_fails:
            raise OSError("oMLX is away")
        rows = []
        for model, kind in KINDS.items():
            row = {"id": model, "config_model_type": kind, "is_helper": False,
                   "loaded": model in self.loaded, "is_loading": model in self.loading,
                   "pinned": model in self.pinned,
                   "last_access": self.access.get(model, WALL - 3600) if model in self.loaded else None}
            rows.append({k: v for k, v in row.items() if k not in self.drop_keys})
        return {"models": rows}

    async def engine_activity(self):
        if self.activity_fails:
            raise OSError("no /api/status")
        return {"active_requests": self.active, "waiting_requests": self.waiting}

    async def loaded_models(self):
        return sorted(self.loaded)

    async def unload(self, model):
        self.calls.append(("unload", model))
        self.loaded.discard(model)

    async def load(self, model):
        self.calls.append(("load", model))
        self.loaded.add(model)


@pytest.fixture
def on_omlx(monkeypatch):
    """Wisp on the user's oMLX.app, with Wisp's own last call long ago."""
    from types import SimpleNamespace

    import service.config
    import service.config.endpoints
    from service.engine import mode
    monkeypatch.setattr(mode, "is_bundled", lambda: False)
    target = SimpleNamespace(endpoint=SimpleNamespace(managed=True, provider="omlx"))
    monkeypatch.setattr(service.config.endpoints, "role_target", lambda role: target)
    monkeypatch.setattr(idle, "_own_access", {})
    monkeypatch.setattr(idle, "_own_loads", {})
    M._state.started_wall = WALL             # FakeOmlx's untouched stamps are from before this
    idle._last_turn = 0.0

    def chat_role(model):
        monkeypatch.setattr(service.config, "role_to_model", lambda role: model)
    chat_role(GEMMA)
    return chat_role


WALL = 1_000_000.0           # a wall clock for oMLX's last_access; ticks pass their own


def otick(client, now, *, wall=None, pressure=M.NORMAL):
    """A tick `now` seconds in; the wall clock moves with it unless told otherwise."""
    return asyncio.run(M.tick(client, now, pressure=pressure,
                              wall=WALL + now if wall is None else wall))


def test_omlx_parks_a_tuned_gemma_after_30_minutes_and_nothing_else(on_omlx):
    c = FakeOmlx()
    assert otick(c, 29 * MIN) == M.KEEP and c.calls == []
    assert otick(c, 31 * MIN) == M.PARK
    assert c.calls == [("unload", GEMMA)] and M._state.parked
    assert M.state()["applies"] is True and M.state()["engine"] == "omlx"


@pytest.mark.parametrize("chat", [QWEN, BOLT])
def test_omlx_never_touches_a_chat_model_that_isnt_a_gemma(on_omlx, chat):
    on_omlx(chat)
    c = FakeOmlx(chat=chat, loaded=(chat,))
    for now in (31, 200):
        assert otick(c, now * MIN) == M.LEAVE
    assert otick(c, 201 * MIN, pressure=M.CRITICAL) == M.LEAVE
    c.loaded.discard(chat)                   # /unload_agent, the notch's X
    M.note_notch_hidden(now=202 * MIN)
    assert not M._state.parked
    assert otick(c, 400 * MIN) == M.LEAVE    # no batch window
    M.set_save_memory(False)
    M._state.parked = True                   # even a stale parked flag brings no reload
    assert otick(c, 401 * MIN) == M.LEAVE
    assert asyncio.run(M.prewarm(c)) == "external"
    assert c.calls == []
    assert M.state()["applies"] is False and M.state()["why_not"] == M.NOT_GEMMA


def test_omlx_never_names_another_loaded_model_in_a_park(on_omlx):
    # Another app's model stays through the park, the window and the second park.
    c = FakeOmlx(loaded=(GEMMA, QWEN))
    assert otick(c, 31 * MIN) == M.PARK
    assert c.calls == [("unload", GEMMA)] and QWEN in c.loaded


def test_omlx_a_window_and_prewarm_wait_while_another_model_is_loaded(on_omlx):
    # A load can make oMLX evict its least recently used model.
    c = FakeOmlx(loaded=(GEMMA, QWEN))
    otick(c, 31 * MIN)
    c.calls.clear()
    assert otick(c, 31 * MIN + M.BATCH_EVERY_S) == M.LEAVE
    assert M._state.parked and not M.batch_window_open() and c.calls == []

    async def intent():
        status = await M.prewarm(c)
        await M.settle_prewarm()
        return status
    assert asyncio.run(intent()) == "loading"
    assert c.calls == []                     # looked, saw the other model, left it to a turn

    c.loaded.discard(QWEN)                   # once it's gone, the window opens as usual
    assert otick(c, 32 * MIN + M.BATCH_EVERY_S) == M.OPEN_WINDOW
    assert c.calls == [("load", GEMMA)] and M.batch_window_open()


def test_omlx_a_model_still_loading_counts_as_another_model(on_omlx):
    c = FakeOmlx()
    otick(c, 31 * MIN)
    c.calls.clear()
    c.loading.add(QWEN)
    assert otick(c, 31 * MIN + M.BATCH_EVERY_S) == M.LEAVE and c.calls == []


def test_omlx_switching_off_doesnt_reload_over_another_model(on_omlx):
    c = FakeOmlx(loaded=(GEMMA, QWEN))
    otick(c, 31 * MIN)
    c.calls.clear()
    M.set_save_memory(False)
    assert otick(c, 32 * MIN) == M.LEAVE and c.calls == [] and not M._state.parked


def test_omlx_switching_off_alone_brings_the_gemma_back(on_omlx):
    c = FakeOmlx()
    otick(c, 31 * MIN)
    c.calls.clear()
    M.set_save_memory(False)
    assert otick(c, 32 * MIN) == M.LEAVE and c.calls == [("load", GEMMA)]


def test_omlx_another_apps_recent_use_holds_the_park(on_omlx):
    c = FakeOmlx()
    c.access[GEMMA] = WALL + 20 * MIN        # no call of Wisp's explains this stamp
    assert otick(c, 31 * MIN) == M.KEEP and c.calls == []
    assert M._state.other_app_at == WALL + 20 * MIN
    assert otick(c, 49 * MIN) == M.KEEP and c.calls == []
    assert otick(c, 50 * MIN + 1) == M.PARK    # 30 minutes after that app's last use
    assert c.calls == [("unload", GEMMA)]


def test_omlx_another_apps_use_holds_under_memory_pressure_too(on_omlx):
    c = FakeOmlx()
    c.access[GEMMA] = WALL + 30 * MIN
    assert otick(c, 31 * MIN, pressure=M.CRITICAL) == M.KEEP and c.calls == []


def test_omlx_a_use_stays_remembered_after_wisps_own_later_call(on_omlx):
    # Wisp's next request overwrites last_access; the tick already saw the app's use.
    c = FakeOmlx()
    c.access[GEMMA] = WALL + 20 * MIN
    otick(c, 21 * MIN)
    idle._own_access[GEMMA] = WALL + 25 * MIN
    c.access[GEMMA] = WALL + 25 * MIN
    assert otick(c, 31 * MIN) == M.KEEP and c.calls == []


def test_omlx_wisps_own_background_calls_never_hold_the_park(on_omlx):
    # The mail walk touched the model two minutes apart for hours on 2026-11-05 with
    # nobody using Wisp. Those are Wisp's own, and must not keep the model forever.
    c = FakeOmlx()
    for minute in range(2, 30, 2):
        idle.begin(GEMMA)
        idle.end(GEMMA)
        idle._own_access[GEMMA] = WALL + minute * MIN
        c.access[GEMMA] = WALL + minute * MIN + 1.0       # the lease ends just after
        assert otick(c, minute * MIN + 30) == M.KEEP
    assert M._state.other_app_at is None
    assert otick(c, 31 * MIN) == M.PARK


def test_omlx_wisps_own_load_never_looks_like_another_app(on_omlx):
    c = FakeOmlx()
    otick(c, 31 * MIN)
    with idle.own_load(GEMMA):               # mid-load: the stamp lands before it returns
        c.loaded.add(GEMMA)
        c.access[GEMMA] = WALL + 33 * MIN
        otick(c, 33 * MIN)
    assert M._state.other_app_at is None


def test_omlx_a_stamp_from_before_this_backend_started_isnt_another_app(on_omlx):
    M._state.started_wall = WALL + 10 * MIN  # Wisp restarted: its old calls aren't remembered
    c = FakeOmlx()
    c.access[GEMMA] = WALL + 9 * MIN
    assert otick(c, 31 * MIN) == M.PARK


@pytest.mark.parametrize("field", ["active", "waiting"])
def test_omlx_a_request_running_anywhere_holds_the_unload(on_omlx, field):
    # POST /unload aborts running requests, and last_access stops moving during one
    # long generation, so an old stamp doesn't mean nobody is there.
    c = FakeOmlx()
    setattr(c, field, 1)
    assert otick(c, 31 * MIN) == M.KEEP and c.calls == [] and not M._state.parked
    setattr(c, field, 0)
    assert otick(c, 32 * MIN) == M.PARK


def test_omlx_unknown_activity_is_a_no(on_omlx):
    c = FakeOmlx()
    c.activity_fails = True
    assert otick(c, 31 * MIN) == M.KEEP and c.calls == []
    c.activity_fails = False
    c.active = None                          # a reply without the count
    assert otick(c, 32 * MIN) == M.KEEP and c.calls == []


def test_omlx_a_use_landing_between_the_reads_holds_the_unload(on_omlx):
    c = FakeOmlx()
    real = c.engine_activity

    async def touched():
        c.access[GEMMA] = WALL + 31 * MIN
        return await real()
    c.engine_activity = touched
    assert otick(c, 31 * MIN) == M.KEEP and c.calls == []


def test_omlx_a_turn_starting_during_the_reads_holds_the_unload(on_omlx):
    c = FakeOmlx()
    real = c.engine_activity

    async def turn_starts():
        idle.begin_foreground()
        return await real()
    c.engine_activity = turn_starts
    try:
        assert otick(c, 31 * MIN) == M.KEEP and c.calls == []
    finally:
        idle.end_foreground()


def test_omlx_a_pinned_gemma_is_left_alone_and_says_so(on_omlx):
    c = FakeOmlx()
    c.pinned.add(GEMMA)
    assert otick(c, 31 * MIN) == M.LEAVE and c.calls == []
    assert M.state()["applies"] is False and M.state()["why_not"] == M.PINNED


@pytest.mark.parametrize("key", ["last_access", "config_model_type", "pinned"])
def test_omlx_that_cant_say_what_it_is_or_who_used_it_is_never_unloaded(on_omlx, key):
    c = FakeOmlx()
    c.drop_keys = (key,)
    assert otick(c, 31 * MIN) == M.LEAVE and c.calls == []
    assert M.state()["why_not"] == M.NOT_REPORTED


def test_omlx_an_uninstalled_chat_model_is_left_alone(on_omlx):
    on_omlx("gemma-not-installed")
    c = FakeOmlx(chat="gemma-not-installed")
    assert otick(c, 31 * MIN) == M.LEAVE and c.calls == []
    assert M.state()["why_not"] == M.NOT_REPORTED


def test_omlx_unreachable_changes_nothing(on_omlx):
    c = FakeOmlx()
    c.status_fails = True
    with pytest.raises(OSError):              # idle_unloader.run catches it
        otick(c, 31 * MIN)
    assert c.calls == [] and not M._state.parked


def test_omlx_gemma_is_decided_from_config_and_id_together():
    gemma = {"id": GEMMA, "config_model_type": "gemma4"}
    assert M._is_gemma(gemma)
    assert M._is_gemma({"id": "gemma-4-E4B-it-oQ4e-mtp", "config_model_type": "gemma4"})
    assert M._is_gemma({"id": "gemma-4-E4B-it-Wisp-Ask-r3-oQ4e-mtp", "config_model_type": "gemma4"})
    assert not M._is_gemma({"id": BOLT, "config_model_type": "gemma4"})
    assert not M._is_gemma({"id": "gemma-renamed-qwen", "config_model_type": "qwen3"})
    assert not M._is_gemma({"id": "gemma-4-E4B-it-assistant-mtp-donor", "config_model_type": "gemma4_assistant"})
    assert not M._is_gemma({"id": GEMMA, "config_model_type": None})
    assert not M._is_gemma({**gemma, "is_helper": True})


def test_omlx_parked_background_work_catches_up_in_a_window_then_parks_again(on_omlx):
    c = FakeOmlx()
    assert otick(c, 31 * MIN) == M.PARK
    start = 31 * MIN + M.BATCH_EVERY_S
    assert otick(c, start - MIN) == M.LEAVE
    assert otick(c, start) == M.OPEN_WINDOW and M.batch_window_open()
    # The window's own load stamped last_access: Wisp's, so it doesn't hold anything.
    c.access[GEMMA] = WALL + start
    idle._own_access[GEMMA] = WALL + start
    assert otick(c, start + M.BATCH_MIN_S) == M.CLOSE_WINDOW
    assert c.calls == [("unload", GEMMA), ("load", GEMMA), ("unload", GEMMA)]
    assert M._state.parked


def test_omlx_prewarm_brings_back_only_what_save_memory_parked(on_omlx):
    c = FakeOmlx(loaded=())

    async def intent():
        status = await M.prewarm(c)
        await M.settle_prewarm()
        return status
    # Unloaded by the user or by oMLX, not by Wisp: a turn loads it, as ever.
    assert asyncio.run(intent()) == "external" and c.calls == []
    M._state.parked = True
    assert asyncio.run(intent()) == "loading" and c.calls == [("load", GEMMA)]


def test_omlx_a_model_wisp_didnt_park_gets_no_window(on_omlx):
    c = FakeOmlx(loaded=())
    assert otick(c, 500 * MIN) == M.LEAVE and c.calls == []


def test_omlx_notch_x_parks_only_a_gemma_save_memory_acts_on(on_omlx):
    c = FakeOmlx()
    M.note_notch_hidden(now=1 * MIN)
    assert not M._state.parked               # no look at oMLX yet: old behavior
    otick(c, 2 * MIN)
    c.loaded.discard(GEMMA)
    M.note_notch_hidden(now=3 * MIN)
    assert M._state.parked


def test_omlx_another_app_loading_the_gemma_isnt_the_user_coming_back(on_omlx, monkeypatch):
    from service.proactive import return_refresh
    armed = []
    monkeypatch.setattr(return_refresh, "arm", armed.append)
    c = FakeOmlx()
    otick(c, 31 * MIN)
    c.loaded.add(GEMMA)                      # the other app's request loaded it
    c.access[GEMMA] = WALL + 40 * MIN
    assert otick(c, 41 * MIN) == M.KEEP
    assert armed == [] and not M._state.parked
    # And the user coming back after a park is.
    assert otick(c, 71 * MIN) == M.PARK
    idle._last_turn = 72 * MIN
    c.loaded.add(GEMMA)
    otick(c, 73 * MIN)
    assert armed == ["unparked"]


def test_omlx_state_before_the_first_look_goes_by_the_name(on_omlx):
    assert M.state()["applies"] is True
    on_omlx(QWEN)
    assert M.state()["applies"] is False and M.state()["why_not"] == M.NOT_GEMMA


def test_state_says_when_the_model_doesnt_run_in_omlx(on_omlx, monkeypatch):
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: "cloud")
    assert M.state()["applies"] is False and M.state()["why_not"] == M.NOT_OMLX
    assert M.state()["engine"] == "remote"


def test_a_local_provider_beside_omlx_mode_is_left_alone(on_omlx):
    # Another server on loopback: no oMLX residency API, so Save memory stays out.
    from service.inference.providers import provider
    c = FakeOmlx()
    c.provider = provider("openai-compatible")
    c.base_url = "http://127.0.0.1:1234"
    assert otick(c, 31 * MIN) == M.LEAVE and c.calls == []


def test_bundled_engine_never_reads_omlx_status_or_activity():
    # The bundled path is unchanged: it goes by loaded_models alone, never holds for
    # "another app" and never needs the extra reads before an unload.
    c = FakeClient()
    idle._last_turn = 0.0
    assert tick(c, 31 * MIN) == M.PARK
    assert c.calls == [("unload", MODEL)]
    assert not hasattr(c, "status") and not hasattr(c, "engine_activity")
    assert M.state()["applies"] is True and M.state()["engine"] == "bundled"
    assert M.state()["why_not"] is None and M.state()["other_app"] is False


def test_idle_records_own_access_on_calls_and_loads_only(monkeypatch):
    monkeypatch.setattr(idle, "_own_access", {})
    monkeypatch.setattr(idle, "_own_loads", {})
    idle.touch(MODEL)                        # the idle timer starting a clock isn't a request
    assert idle.own_access(MODEL) is None
    idle.begin(MODEL)
    assert idle.own_access_active(MODEL) and idle.own_access(MODEL) is not None
    idle.end(MODEL)
    assert not idle.own_access_active(MODEL)
    with idle.own_load(MODEL):
        assert idle.own_access_active(MODEL)
    assert not idle.own_access_active(MODEL)


def test_a_local_client_beside_the_bundled_engine_is_left_alone():
    # Bundled mode, but this client talks to some other loopback server.
    c = FakeClient()
    c.base_url = "http://127.0.0.1:8000"
    idle._last_turn = 0.0
    assert tick(c, 31 * MIN) == M.LEAVE and c.calls == []
    assert M.bundled_engine() and not M.bundled_engine(c)


def test_prewarm_on_a_remote_profile_does_nothing(monkeypatch):
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: "cloud")
    c = FakeClient(loaded=False)
    assert asyncio.run(M.prewarm(c)) == "remote" and c.calls == []


def test_a_turn_waits_for_a_prewarm_load_in_flight():
    order = []

    class Slow(FakeClient):
        async def load(self, model):
            await asyncio.sleep(0.05)
            order.append("prewarm loaded")
            self.loaded.add(model)

    async def go():
        c = Slow(loaded=False)
        await M.prewarm(c)
        await M.settle_prewarm(timeout=2)   # what OMLXClient._ensure_managed awaits first
        order.append("turn checks residency")
        return c
    c = asyncio.run(go())
    assert order == ["prewarm loaded", "turn checks residency"] and MODEL in c.loaded


def test_ensure_managed_settles_a_prewarm_before_it_reads_residency():
    import inspect
    from service.inference.omlx_client import OMLXClient
    src = inspect.getsource(OMLXClient._ensure_managed)
    assert src.index("settle_prewarm()") < src.index("await self.loaded_models()")


def test_a_paused_research_job_lets_go_and_takes_the_hold_back_on_resume():
    from service.research.orchestrator import ResearchManager
    jobs = {"j": {"pause_requested": True}}
    seen = []

    class Store:
        def get_job(self, job_id):
            return jobs[job_id]

    manager = ResearchManager.__new__(ResearchManager)
    manager.store = Store()

    async def go():
        with M.hold():
            waiting = asyncio.create_task(manager._checkpoint("j"))
            await asyncio.sleep(0.1)
            seen.append(M.held())            # paused: not held
            jobs["j"]["pause_requested"] = False
            assert await asyncio.wait_for(waiting, 2) is True
            seen.append(M.held())            # resumed: held again
        seen.append(M.held())
    asyncio.run(go())
    assert seen == [False, True, False]


def test_a_cancelled_research_job_releases_its_hold():
    from service.research.orchestrator import ResearchManager
    import types

    async def forever(self, job_id, client):
        await asyncio.sleep(60)

    manager = ResearchManager.__new__(ResearchManager)
    manager._run = types.MethodType(forever, manager)

    async def go():
        task = asyncio.create_task(manager._run_held("j", None))
        await asyncio.sleep(0.05)
        assert M.held()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())
    assert not M.held()


def test_a_finished_research_job_counts_as_intent():
    from service.research.orchestrator import ResearchManager
    import types

    async def done(self, job_id, client):
        return None

    manager = ResearchManager.__new__(ResearchManager)
    manager._run = types.MethodType(done, manager)
    asyncio.run(manager._run_held("j", None))
    assert M._state.last_intent is not None


def test_a_failed_or_finished_prewarm_never_holds_a_turn():
    class Failing(FakeClient):
        async def load(self, model):
            raise RuntimeError("507")

    async def go():
        await M.prewarm(Failing(loaded=False))
        await asyncio.sleep(0)
        await asyncio.wait_for(M.settle_prewarm(timeout=5), 1)
        return M.prewarm_pending()
    assert asyncio.run(go()) is False


def test_a_turn_waiting_on_a_prewarm_shows_the_loading_status(monkeypatch):
    from service.inference.omlx_client import OMLXClient
    events = []

    class Probe(OMLXClient):
        def __init__(self):
            self._keep_warm = set()

        async def loaded_models(self):
            raise StopAsyncIteration          # stop right after the status and the wait

    monkeypatch.setattr(M, "prewarm_pending", lambda: True)
    async def settle():
        events.append("waited")
    monkeypatch.setattr(M, "settle_prewarm", settle)

    async def emit(event):
        events.append(event["text"].split("…")[0])

    with pytest.raises(StopAsyncIteration):
        asyncio.run(Probe()._ensure_managed(MODEL, emit=emit))
    assert events[0].startswith("Loading") and events[1] == "waited"


def test_unheld_outside_a_hold_never_leaks_a_count():
    with M.unheld():
        assert not M.held()
    assert M._holds == 0


def test_two_jobs_one_paused_then_cancelled_while_paused():
    from service.research.orchestrator import ResearchManager
    jobs = {"paused": {"pause_requested": True}}

    class Store:
        def get_job(self, job_id):
            return jobs[job_id]

        def update_job(self, *a, **k):
            pass

        def event(self, *a, **k):
            pass

    manager = ResearchManager.__new__(ResearchManager)
    manager.store = Store()

    async def paused_run(self, job_id, client):
        await self._checkpoint(job_id)

    import types
    manager._run = types.MethodType(paused_run, manager)

    async def go():
        running = M.hold()
        running.__enter__()                      # another job, still working
        task = asyncio.create_task(manager._run_held("paused", None))
        await asyncio.sleep(0.1)
        assert M._holds == 1 and M.held()        # the paused job let go; the running one holds
        running.__exit__(None, None, None)
        assert M._holds == 0                     # only the paused job left: the model may park
        task.cancel()                            # cancelled during the paused sleep
        await asyncio.gather(task, return_exceptions=True)
        return M._holds
    assert asyncio.run(go()) == 0


# --- the real client, on a mock transport -----------------------------------------

def _real_client(handler):
    import httpx

    from service.inference.omlx_client import OMLXClient
    c = OMLXClient(base_url="http://127.0.0.1:8000", api_key="test")
    c._client = httpx.AsyncClient(base_url=c.base_url, transport=httpx.MockTransport(handler))
    return c


def test_the_real_client_counts_as_omlx_and_reads_activity_read_only(on_omlx):
    import httpx
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"active_requests": 2, "waiting_requests": 1})
    c = _real_client(handler)
    assert M.omlx_app(c) is True
    counts = asyncio.run(c.engine_activity())
    assert counts["active_requests"] == 2 and counts["waiting_requests"] == 1
    assert seen == [("GET", "/api/status")]


def test_the_real_client_stamps_its_own_load(on_omlx):
    import httpx
    during = []

    def handler(request):
        during.append(idle.own_access_active(GEMMA))
        return httpx.Response(200, json={"status": "ok"})
    c = _real_client(handler)
    asyncio.run(c.load(GEMMA))
    assert during == [True]
    assert idle.own_access(GEMMA) is not None and not idle.own_access_active(GEMMA)


def test_a_real_tick_against_omlxs_http_shape_unloads_only_the_gemma(on_omlx):
    # The real client and tick over oMLX's actual routes and row fields.
    import httpx
    rows = {GEMMA: dict(loaded=True, config_model_type="gemma4", last_access=WALL - 3600),
            QWEN: dict(loaded=True, config_model_type="qwen3_5", last_access=WALL + 30 * MIN)}
    posts = []

    def handler(request):
        path = request.url.path
        if path == "/v1/models/status":
            return httpx.Response(200, json={"models": [
                dict(id=model, is_loading=False, pinned=False, is_helper=False, **row)
                for model, row in rows.items()]})
        if path == "/api/status":
            return httpx.Response(200, json={"status": "ok", "active_requests": 0, "waiting_requests": 0})
        posts.append(path)
        return httpx.Response(200, json={"status": "ok"})

    c = _real_client(handler)
    c.set_keep_warm({GEMMA})
    assert asyncio.run(M.tick(c, 31 * MIN, pressure=M.NORMAL, wall=WALL + 31 * MIN)) == M.PARK
    assert posts == [f"/v1/models/{GEMMA}/unload"]       # QWEN, loaded and in use, is untouched


def test_a_turn_reloads_a_model_parked_on_omlx_cleanly(on_omlx):
    # The turn's own path over HTTP: one load, no eviction when Gemma is alone, and
    # the next tick sees the model back and clears the parked state.
    import httpx
    loaded = set()
    posts = []

    def handler(request):
        path = request.url.path
        if path == "/v1/models/status":
            return httpx.Response(200, json={"models": [dict(
                id=GEMMA, config_model_type="gemma4", is_helper=False, is_loading=False, pinned=False,
                loaded=GEMMA in loaded, last_access=WALL + 40 * MIN if GEMMA in loaded else None)]})
        posts.append(path)
        loaded.add(GEMMA)
        return httpx.Response(200, json={"status": "ok"})
    c = _real_client(handler)
    c.set_keep_warm({GEMMA})
    M.note_parked("idle", 31 * MIN)

    async def run():
        await c._ensure_managed(GEMMA)
        idle._last_turn = 40 * MIN
        idle._own_access[GEMMA] = WALL + 40 * MIN
        return await M.tick(c, 41 * MIN, pressure=M.NORMAL, wall=WALL + 41 * MIN)
    assert asyncio.run(run()) == M.KEEP
    assert posts == [f"/v1/models/{GEMMA}/load"] and not M._state.parked
    assert M._state.other_app_at is None

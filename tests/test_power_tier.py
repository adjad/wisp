"""GPU power tiers and the "Save power when away" switch; no engine is contacted."""
import asyncio
import json
import stat
from types import SimpleNamespace

import httpx
import pytest

from service.inference import power_tier as P
from service.inference.omlx_client import OMLXClient, _power_fields


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in P._ENV.values():
        monkeypatch.delenv(name, raising=False)
    # Every test starts with the switch at its default, in a private settings
    # file, the user present and on AC (no readings yet), no turn and no call
    # anyone is waiting on.
    monkeypatch.setattr(P, "SETTINGS_PATH", tmp_path / "power" / "settings.json")
    monkeypatch.setattr(P, "_away", None)
    monkeypatch.setattr(P, "_battery", None)
    monkeypatch.setattr(P, "_watched_calls", 0)
    monkeypatch.setattr(P, "_on_battery_probe", lambda: False)
    from service import idle
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    # No other app on the GPU, unless a test says so (the real check is tested
    # on its own below, against a fake ioreg).
    monkeypatch.setattr(P, "_gpu_quiet", lambda sample: True)
    monkeypatch.setattr(P, "_gpu_sample", None)


_REAL_GPU_QUIET = P._gpu_quiet
_REAL_BATTERY_PROBE = P._on_battery_probe


@pytest.fixture
def away():
    """Nobody has touched the keyboard or mouse for longer than AWAY_AFTER_S."""
    P.refresh_away(P.AWAY_AFTER_S + 1)


@pytest.fixture
def battery():
    """The user is at the Mac, and it is on battery."""
    P.refresh_away(5, on_battery=True)


# --- other apps on the GPU --------------------------------------------------------

_IOREG = '''+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x1>
    {
      "AppUsage" = ({"API"="Metal","accumulatedGPUTime"=%d},{"API"="Metal","accumulatedGPUTime"=5})
      "IOUserClientCreator" = "pid 200, Game"
    }
+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x2>
    {
      "AppUsage" = ()
      "IOUserClientCreator" = "pid 300, Idle"
    }
'''


def test_gpu_times_are_summed_per_process():
    assert P._parse_gpu_times(_IOREG % 100) == {200: 105, 300: 0}
    assert P._parse_gpu_times("") == {}


def test_another_app_on_the_gpu_keeps_background_uncapped(monkeypatch):
    clock = [1000.0]
    gpu = {"ns": 0}
    monkeypatch.setattr(P, "_gpu_quiet", _REAL_GPU_QUIET)
    monkeypatch.setattr(P.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(P, "_other_gpu_times", lambda: {200: gpu["ns"]})

    assert P.refresh_away(P.AWAY_AFTER_S + 1) is False       # first look: no baseline yet
    clock[0] += 30
    gpu["ns"] += int(30e9 * 0.5)                                # a game held half the GPU
    assert P.refresh_away(P.AWAY_AFTER_S + 1) is False
    clock[0] += 30
    gpu["ns"] += int(30e9 * 0.01)                               # the game was closed
    assert P.refresh_away(P.AWAY_AFTER_S + 1) is True
    assert P.refresh_away(30) is False                          # the user came back: baseline reset
    assert P._gpu_sample is None
    monkeypatch.setattr(P, "_other_gpu_times", lambda: None)
    clock[0] += 30
    assert P.refresh_away(P.AWAY_AFTER_S + 1) is False          # an unreadable ioreg: uncapped
    clock[0] += 30
    assert P.refresh_away(P.AWAY_AFTER_S + 1) is False


def test_a_refresh_under_five_seconds_after_the_last_counts_as_busy(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(P, "_gpu_quiet", _REAL_GPU_QUIET)
    monkeypatch.setattr(P.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(P, "_other_gpu_times", lambda: {200: 0})
    assert P.refresh_away(P.AWAY_AFTER_S + 1) is False          # baseline
    clock[0] += 4
    assert P.refresh_away(P.AWAY_AFTER_S + 1) is False          # too soon to tell
    clock[0] += 30
    assert P.refresh_away(P.AWAY_AFTER_S + 1) is True


@pytest.mark.parametrize("failure", [OSError("no ioreg"),
                                     __import__("subprocess").TimeoutExpired("ioreg", 5),
                                     UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")])
def test_a_failed_ioreg_read_is_none(monkeypatch, failure):
    import subprocess

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", run)
    assert P._other_gpu_times() is None


def test_ioreg_output_that_isnt_utf8_is_still_read(monkeypatch):
    import subprocess
    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=_IOREG % 7, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(P, "_own", lambda pid: False)
    assert P._other_gpu_times() == {200: 12, 300: 0}
    assert seen.get("errors") == "replace"


def test_wisp_its_engine_and_windowserver_dont_count(monkeypatch):
    from service.inference import local_peer
    paths = {1: "/Applications/oMLX.app/Contents/Resources/Python/cpython-3.11/bin/python3.11",
             2: "/Applications/Wisp.app/Contents/MacOS/Wisp",
             3: "/System/Library/PrivateFrameworks/SkyLight.framework/Versions/A/Resources/WindowServer",
             4: "/Applications/Blender.app/Contents/MacOS/Blender"}

    def path(pid):
        if pid not in paths:
            raise ProcessLookupError(pid)
        return paths[pid]

    monkeypatch.setattr(local_peer, "process_path", path)
    assert [pid for pid in (1, 2, 3, 4, 5) if not P._own(pid)] == [4, 5]


# --- the switch -------------------------------------------------------------------

def test_the_switch_is_on_by_default_persists_and_reads_unreadable_as_the_default():
    assert P.save_when_away() is True
    assert P.set_save_when_away(False) is False and P.save_when_away() is False
    assert json.loads(P.SETTINGS_PATH.read_text()) == {"save_when_away": False}
    assert stat.S_IMODE(P.SETTINGS_PATH.stat().st_mode) == 0o600
    assert P.set_save_when_away(True) is True and P.save_when_away() is True
    P.SETTINGS_PATH.write_text("{not json")
    assert P.save_when_away() is True
    P.SETTINGS_PATH.write_text('{"save_when_away": "no"}')
    assert P.save_when_away() is True
    with pytest.raises(ValueError):
        P.set_save_when_away("false")


def test_the_endpoint_reads_and_writes_the_switch_from_the_app_only():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from service.power_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/power").json() == {"save_when_away": True}
    assert client.post("/power", json={"save_when_away": False}).json() == {"save_when_away": False}
    assert P.save_when_away() is False
    # A web page (Origin header), a non-JSON body or a non-bool value can't change it.
    assert client.post("/power", json={"save_when_away": True},
                       headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/power", content="save_when_away=true",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/power", json={"save_when_away": "true"}).status_code == 422
    assert client.post("/power", json={"save_when_away": True, "watts": 5}).status_code == 422
    assert P.save_when_away() is False
    # The old Low Power Mode endpoints are gone.
    assert client.post("/power/other_app", json={"event": "launched"}).status_code in (404, 405)
    assert client.post("/power/activity", json={}).status_code in (404, 405)


# --- away -------------------------------------------------------------------------

def test_away_needs_ten_idle_input_minutes_a_fresh_reading_and_no_turn(monkeypatch):
    from service import idle
    assert P.away() is False                                   # never read: present
    assert P.refresh_away(P.AWAY_AFTER_S - 5) is False and P.away() is False
    assert P.refresh_away(P.AWAY_AFTER_S) is True and P.away() is True
    assert P.refresh_away(None, probe=False) is False and P.away() is False  # unreadable: present
    P.refresh_away(P.AWAY_AFTER_S + 1)
    monkeypatch.setattr(idle, "_foreground_turns", 1)
    assert P.away() is False                                   # a turn is running
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    mono, wall, value = P._away
    P._away = (mono - P._AWAY_STALE_S - 1, wall, value)
    assert P.away() is False                                   # a stale reading


def test_a_reading_from_before_sleep_is_stale_at_wake(away):
    # time.monotonic() doesn't advance while the Mac sleeps, so only the wall
    # clock shows the night: the wake backfill must not run capped on it.
    assert P.away() is True
    mono, wall, value = P._away
    P._away = (mono, wall - 8 * 3600, value)
    assert P.away() is False
    P._away = (mono, wall + 3600, value)                       # wall clock set back
    assert P.away() is False


def test_the_scheduler_refreshes_away_off_the_event_loop():
    import inspect

    from service.assistant import scheduler
    source = inspect.getsource(scheduler)
    assert "await asyncio.to_thread(refresh_away)" in source


# --- tiers ------------------------------------------------------------------------

def test_the_caps_are_10_w_away_and_15_w_on_battery():
    assert P.watts(P.AWAY) == 10
    assert P.watts(P.BATTERY) == 15
    assert P.watts(None) is None and P.watts(P.CHAT) is None
    with pytest.raises(ValueError):
        with P.power_tier("intensive"):
            pass


def test_env_overrides_zero_turns_off_and_out_of_range_is_ignored(monkeypatch):
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "7")
    assert P.watts(P.AWAY) == 7
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "0")
    assert P.watts(P.AWAY) is None
    for bad in ("1", "5000", "ten"):
        monkeypatch.setenv("WISP_BACKGROUND_GPU_W", bad)
        assert P.watts(P.AWAY) == 10
    monkeypatch.setenv("WISP_BATTERY_GPU_W", "12")
    assert P.watts(P.BATTERY) == 12


def _caps():
    out = {}
    for tier in (None, P.CHAT, P.BRIEF, P.BACKGROUND):
        if tier is None:
            out[tier] = P.payload_fields().get("gpu_power_limit_w")
        else:
            with P.power_tier(tier):
                out[tier] = P.payload_fields().get("gpu_power_limit_w")
    return out


def test_on_ac_with_the_user_present_nothing_is_capped():
    P.refresh_away(5, on_battery=False)
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: None, P.BACKGROUND: None}


def test_on_battery_chat_and_brief_run_at_15_w_and_the_rest_uncapped(battery):
    assert _caps() == {None: None, P.CHAT: 15, P.BRIEF: 15, P.BACKGROUND: None}


def test_away_brief_and_background_run_at_10_w_and_chat_uncapped(away):
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: 10, P.BACKGROUND: 10}


def test_away_on_battery_takes_the_lower_cap():
    P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=True)
    assert _caps() == {None: None, P.CHAT: 15, P.BRIEF: 10, P.BACKGROUND: 10}


def test_the_switch_off_caps_nothing(battery):
    P.set_save_when_away(False)
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: None, P.BACKGROUND: None}
    P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=True)
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: None, P.BACKGROUND: None}


def test_another_app_on_the_gpu_lifts_the_battery_cap_too(monkeypatch):
    monkeypatch.setattr(P, "_gpu_quiet", lambda sample: False)
    P.refresh_away(5, on_battery=True)
    assert _caps()[P.CHAT] is None


def test_a_stale_battery_reading_counts_as_ac(battery):
    mono, wall, value = P._battery
    P._battery = (mono, wall - 8 * 3600, value)                # read before a night asleep
    assert P.on_battery() is False and _caps()[P.CHAT] is None


def test_the_battery_probe_reads_the_cached_power_source(monkeypatch):
    from service.mail_memory import runner
    monkeypatch.setattr(P, "_on_battery_probe", _REAL_BATTERY_PROBE)
    monkeypatch.setattr(runner, "_probe", {})
    monkeypatch.setattr(runner, "_power", lambda: (False, 80, False))
    assert P._on_battery_probe() is True
    monkeypatch.setattr(runner, "_probe", {})
    monkeypatch.setattr(runner, "_power", lambda: (True, None, False))   # AC, or a failed read
    assert P._on_battery_probe() is False


def test_brief_and_background_go_uncapped_while_a_watched_call_runs(away):
    started = P.call_started()                                 # a reply, research, a phone turn
    assert started is True and P.watched_running()
    with P.power_tier(P.BRIEF):
        assert P.payload_fields() == {}
        assert P.call_started() is False                       # Brief calls aren't watched
    with P.power_tier(P.BACKGROUND):
        assert P.payload_fields() == {}
    P.call_ended(started)
    assert not P.watched_running()
    with P.power_tier(P.BACKGROUND):
        assert P.payload_fields() == {"gpu_power_limit_w": 10}


def test_a_chat_on_battery_keeps_its_cap_beside_another_watched_call(battery):
    started = P.call_started()
    with P.power_tier(P.CHAT):
        assert P.payload_fields() == {"gpu_power_limit_w": 15}
    P.call_ended(started)


def test_tier_is_scoped_to_the_block_and_inherited_by_tasks(away):
    async def seen():
        return P.payload_fields()

    async def run():
        with P.power_tier(P.BACKGROUND):
            task = asyncio.ensure_future(seen())
        return await task

    assert asyncio.run(run()) == {"gpu_power_limit_w": 10}
    assert P.current_tier() is None
    with pytest.raises(ValueError):
        with P.power_tier("turbo"):
            pass


# --- the client -------------------------------------------------------------------

def _client(managed, reply_hook=None):
    bodies = []

    def reply(request):
        bodies.append(json.loads(request.content))
        if reply_hook:
            reply_hook()
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                      "message": {"content": "ok"}}]})

    client = OMLXClient.__new__(OMLXClient)
    client.managed = managed
    client.api_prefix = "/v1"
    client.provider = SimpleNamespace(name="omlx")
    client._fit_request = lambda m, messages, tools, tokens, **kw: (m, messages, tools, tokens)
    client._client = httpx.AsyncClient(base_url="http://test.invalid",
                                       transport=httpx.MockTransport(reply))
    return client, bodies


def test_the_client_sends_each_call_its_per_request_cap():
    client, bodies = _client(managed=True)
    hi = [{"role": "user", "content": "hi"}]

    async def run():
        try:
            await client.chat("m", hi)                        # 0: untiered (research...), AC
            with P.power_tier(P.CHAT):
                await client.chat("m", hi)                    # 1: chat, AC
                P.refresh_away(5, on_battery=True)
                await client.chat("m", hi)                    # 2: chat, battery
            with P.power_tier(P.BRIEF):
                await client.chat("m", hi)                    # 3: Brief, battery
            with P.power_tier(P.BACKGROUND):
                await client.chat("m", hi)                    # 4: background, battery
                P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=False)
                await client.chat("m", hi)                    # 5: background, away
                await client.chat("m", hi, gpu_power_limit_w=12)  # 6: explicit wins
            await client.chat("m", hi)                        # 7: untiered, away
        finally:
            await client._client.aclose()

    asyncio.run(run())
    caps = [body.get("gpu_power_limit_w") for body in bodies]
    assert caps == [None, None, 15, 15, None, 10, 12, None]


def test_the_client_counts_a_watched_call_only_while_it_runs(away):
    during = []
    client, _ = _client(managed=True, reply_hook=lambda: during.append(P.watched_running()))

    async def run():
        try:
            with P.power_tier(P.CHAT):
                await client.chat("m", [{"role": "user", "content": "hi"}])
            with P.power_tier(P.BRIEF):
                await client.chat("m", [{"role": "user", "content": "brief"}])
            with P.power_tier(P.BACKGROUND):
                await client.chat("m", [{"role": "user", "content": "bg"}])
        finally:
            await client._client.aclose()

    asyncio.run(run())
    assert during == [True, False, False] and not P.watched_running()


def test_a_remote_provider_never_gets_the_field(away):
    client = OMLXClient.__new__(OMLXClient)
    client.managed = False
    with P.power_tier(P.BACKGROUND):
        assert _power_fields(client) == {}
    assert _power_fields(client) == {}


def test_no_model_level_cap_is_ever_seeded():
    from service.setup import catalog

    for model in catalog.PRIMARY_MODELS:
        assert "gpu_power_limit_w" not in model.engine_profile, model.id


# --- each path carries its tier ---------------------------------------------------

def _seen():
    return (P.current_tier(), P.payload_fields())


@pytest.mark.parametrize("where, cap", [("away", 10), ("battery", 15)])
def test_the_gist_call_runs_at_the_brief_tier(monkeypatch, request, where, cap):
    request.getfixturevalue(where)
    import service.config as config
    from service.assistant import brief, mail_gists

    seen = []

    class Client:
        async def loaded_models(self):
            return ["fast"]

        def keep_warm(self):
            return set()

        async def chat(self, model, messages, **kwargs):
            seen.append(_seen())
            return {"choices": [{"finish_reason": "stop", "message": {"content": "gist"}}]}

    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(config, "no_thinking_capable", lambda: ["fast"])
    monkeypatch.setattr(config, "thinks_explicitly", lambda name: False)
    monkeypatch.setattr(brief, "_c", lambda: Client())
    assert asyncio.run(mail_gists._resident_reply("s", "m", lambda: False)) == "gist"
    assert seen == [(P.BRIEF, {"gpu_power_limit_w": cap})]


def test_the_scheduled_daily_summary_has_no_tier_because_it_calls_no_model(monkeypatch, away):
    import inspect
    from datetime import date

    from service.assistant import brief, scheduler

    seen = []

    async def fake_brief(part, **kwargs):
        seen.append(_seen())
        return None

    monkeypatch.setattr(brief, "run_scheduled_brief", fake_brief)
    monkeypatch.setattr(scheduler.idle, "foreground_busy", lambda: False)
    asyncio.run(scheduler._run_daily_brief(date(2026, 10, 28), "morning"))
    assert seen == [(None, {})]
    # The brief renders from caches; its model-written parts are the gists.
    assert "NO MODEL" in inspect.getsource(brief).split('"""')[1]


def test_the_behavior_tick_runs_at_the_background_tier(monkeypatch, away):
    import service.behavior_switch as switch
    from service.assistant import behavior_runtime, scheduler

    seen = []

    async def fake_tick(store, hub, **kwargs):
        seen.append(_seen())
        return []

    monkeypatch.setattr(switch, "enabled", lambda: True)
    monkeypatch.setattr(behavior_runtime, "tick", fake_tick)
    monkeypatch.setattr(scheduler, "_behavior_task", None)

    async def run():
        await scheduler._start_behavior_tick()

    asyncio.run(run())
    assert seen == [(P.BACKGROUND, {"gpu_power_limit_w": 10})]


class _Recorder:
    """A fake client that records the tier and cap active when chat() runs."""

    managed = True
    base_url = "local"

    def __init__(self):
        self.seen = []

    async def loaded_models(self):
        return ["fast"]

    async def models(self):
        return ["fast"]

    async def ensure_only(self, model, **kwargs):
        return None

    async def chat(self, model, messages, **kwargs):
        self.seen.append(_seen())
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}


def test_the_mail_memory_walk_runs_at_the_background_tier(monkeypatch, away):
    import service.config as config
    from service import idle
    from service.assistant import brief
    from service.mail_memory import runner

    client = _Recorder()

    async def ready():
        return None

    monkeypatch.setattr(runner, "_stand_down", ready)
    monkeypatch.setattr(brief, "_c", lambda: client)
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(idle, "in_flight", lambda key: 1)
    assert asyncio.run(runner._reply([{"role": "user", "content": "mail"}])) == "ok"
    assert client.seen == [(P.BACKGROUND, {"gpu_power_limit_w": 10})]


def test_the_behavior_phraser_runs_at_the_background_tier(monkeypatch, away):
    from service import idle
    from service.assistant.behavior_phraser import WispPhraser
    from service.config.endpoints import role_target

    client = _Recorder()
    resident = role_target("fast").model

    async def loaded():
        return [resident]

    client.loaded_models = loaded
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    phraser = WispPhraser(client_factory=lambda target: client)
    asyncio.run(phraser._complete_async("s", "u", max_tokens=20, temperature=0))
    assert client.seen == [(P.BACKGROUND, {"gpu_power_limit_w": 10})]


def test_research_the_user_started_is_never_capped(monkeypatch, away):
    from service import idle
    from service.research import orchestrator as O

    client = _Recorder()
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(O, "_research_model", lambda: "fast")
    monkeypatch.setattr(O, "_select_research_model", lambda configured, available, explicit=False: "fast")
    monkeypatch.setattr(O, "models_config", lambda: {})

    async def run():
        manager = O.ResearchManager.__new__(O.ResearchManager)
        manager._model_lock = asyncio.Lock()
        manager._resolved_model = None
        return await manager._call_on_endpoint(client, "s", "u")

    assert asyncio.run(run()) == "ok"
    assert client.seen == [(None, {})]


def test_memory_capture_wraps_its_extraction_call_in_the_background_tier():
    import inspect

    from service.memory.capture import MemoryWorker

    source = inspect.getsource(MemoryWorker.process)
    tier = source.index("with power_tier(BACKGROUND):")
    assert tier < source.index("await client.chat(") < tier + 200


def test_a_chat_turn_runs_at_the_chat_tier():
    import inspect
    import service.main as main
    source = inspect.getsource(main)
    runner = source[source.index("    async def runner():"):]
    head = runner[:runner.index("await runner_body()")]
    assert "power_tier(CHAT)" in head


def test_image_and_document_questions_run_at_the_chat_tier():
    import inspect
    from service import documents, vision
    answer = inspect.getsource(vision.answer)
    assert answer.index("with power_tier(CHAT):") < answer.index("await _answer(")
    text = inspect.getsource(documents.answer_text)
    assert text.index("with power_tier(CHAT):") < text.index("client.chat(")


def test_the_mail_memory_walk_on_battery_with_the_user_present_is_uncapped(monkeypatch, battery):
    import service.config as config
    from service import idle
    from service.assistant import brief
    from service.mail_memory import runner

    client = _Recorder()

    async def ready():
        return None

    monkeypatch.setattr(runner, "_stand_down", ready)
    monkeypatch.setattr(brief, "_c", lambda: client)
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(idle, "in_flight", lambda key: 1)
    assert asyncio.run(runner._reply([{"role": "user", "content": "mail"}])) == "ok"
    assert client.seen == [(P.BACKGROUND, {})]

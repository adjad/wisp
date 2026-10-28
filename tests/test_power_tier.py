"""Low Power Mode and GPU power tiers; no engine is contacted."""
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
    # Every test starts with Low Power Mode off, in a private settings file, and
    # with the user's idle clock fresh (a long suite would otherwise age it
    # past the 10-minute auto-off).
    monkeypatch.setattr(P, "SETTINGS_PATH", tmp_path / "power" / "settings.json")
    import time

    from service import idle
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_last_user_activity", idle._awake_or_asleep_now())


@pytest.fixture
def on():
    P.set_low_power(True)


# --- the setting ------------------------------------------------------------------

def test_low_power_is_off_by_default_persists_and_reads_unreadable_as_off():
    assert P.low_power() is False
    assert P.set_low_power(True) is True and P.low_power() is True
    assert json.loads(P.SETTINGS_PATH.read_text()) == {"low_power": True}
    assert stat.S_IMODE(P.SETTINGS_PATH.stat().st_mode) == 0o600
    assert P.set_low_power(False) is False and P.low_power() is False
    P.SETTINGS_PATH.write_text("{not json")
    assert P.low_power() is False
    P.SETTINGS_PATH.write_text('{"low_power": "yes"}')
    assert P.low_power() is False
    with pytest.raises(ValueError):
        P.set_low_power("true")


def test_the_endpoint_reads_and_writes_the_setting_from_the_app_only():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from service.power_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/power").json() == {"low_power": False}
    assert client.post("/power", json={"low_power": True}).json() == {"low_power": True}
    assert P.low_power() is True
    # A web page (Origin header), a non-JSON body or a non-bool value can't change it.
    assert client.post("/power", json={"low_power": False},
                       headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/power", content="low_power=false",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/power", json={"low_power": "false"}).status_code == 422
    assert client.post("/power", json={"low_power": False, "watts": 5}).status_code == 422
    assert P.low_power() is True


# --- tiers ------------------------------------------------------------------------

def test_defaults_are_10_w_background_15_w_intensive_20_w_chat():
    assert P.watts(P.BACKGROUND) == 10
    assert P.watts(P.INTENSIVE) == 15
    assert P.watts(P.CHAT) == 20
    assert P.watts(None) is None


def test_env_overrides_zero_turns_off_and_out_of_range_is_ignored(monkeypatch):
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "7")
    assert P.watts(P.BACKGROUND) == 7
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "0")
    assert P.watts(P.BACKGROUND) is None
    for bad in ("1", "5000", "ten"):
        monkeypatch.setenv("WISP_BACKGROUND_GPU_W", bad)
        assert P.watts(P.BACKGROUND) == 10


def test_off_sends_nothing_from_any_tier():
    assert P.payload_fields() == {}
    for tier in (P.BACKGROUND, P.INTENSIVE, P.CHAT):
        with P.power_tier(tier):
            assert P.payload_fields() == {}


def test_on_caps_each_tier_and_untiered_calls_get_the_chat_cap(on):
    assert P.payload_fields() == {"gpu_power_limit_w": 20}
    with P.power_tier(P.BACKGROUND):
        assert P.payload_fields() == {"gpu_power_limit_w": 10}
    with P.power_tier(P.INTENSIVE):
        assert P.payload_fields() == {"gpu_power_limit_w": 15}
    assert P.payload_fields() == {"gpu_power_limit_w": 20}


def test_tier_is_scoped_to_the_block_and_inherited_by_tasks(on):
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

def _client(managed):
    bodies = []

    def reply(request):
        bodies.append(json.loads(request.content))
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


def test_off_sends_no_field_and_on_sends_a_per_request_cap_that_ends_with_the_call():
    client, bodies = _client(managed=True)
    hi = [{"role": "user", "content": "hi"}]

    async def run():
        try:
            await client.chat("m", hi)                        # 0: off
            with P.power_tier(P.BACKGROUND):
                await client.chat("m", hi)                    # 1: off, tiered
            P.set_low_power(True)
            await client.chat("m", hi)                        # 2: on, chat
            with P.power_tier(P.INTENSIVE):
                await client.chat("m", hi)                    # 3: on, intensive
                await client.chat("m", hi, gpu_power_limit_w=12)  # 4: explicit wins
            await client.chat("m", hi)                        # 5: tier ended -> chat cap
            P.set_low_power(False)
            await client.chat("m", hi)                        # 6: switched off -> nothing
        finally:
            await client._client.aclose()

    asyncio.run(run())
    caps = [body.get("gpu_power_limit_w") for body in bodies]
    assert caps == [None, None, 20, 15, 12, 20, None]


def test_a_remote_provider_never_gets_the_field(on):
    client = OMLXClient.__new__(OMLXClient)
    client.managed = False
    with P.power_tier(P.BACKGROUND):
        assert _power_fields(client) == {}
    assert _power_fields(client) == {}


def test_a_chat_beside_a_running_background_tier_requests_the_chat_cap(on):
    # Request bodies only: the cap actually in force is the engine's
    # min(active requests), so the chat runs at 10 W until the background call ends.
    client, bodies = _client(managed=True)
    background_started, foreground_done = asyncio.Event(), asyncio.Event()

    async def background():
        with P.power_tier(P.BACKGROUND):
            background_started.set()
            await foreground_done.wait()       # still inside the tier while the turn runs
            await client.chat("m", [{"role": "user", "content": "bg"}])

    async def foreground():
        await background_started.wait()
        await client.chat("m", [{"role": "user", "content": "fg"}])
        foreground_done.set()

    async def run():
        try:
            await asyncio.gather(background(), foreground())
        finally:
            await client._client.aclose()

    asyncio.run(run())
    by_text = {body["messages"][0]["content"]: body for body in bodies}
    assert by_text["fg"]["gpu_power_limit_w"] == 20
    assert by_text["bg"]["gpu_power_limit_w"] == 10


def test_no_model_level_cap_is_ever_seeded():
    from service.setup import catalog

    for model in catalog.PRIMARY_MODELS:
        assert "gpu_power_limit_w" not in model.engine_profile, model.id


# --- each background path carries its tier (Low Power Mode on) --------------------

def test_the_gist_call_runs_at_the_background_tier(monkeypatch, on):
    import service.config as config
    from service.assistant import brief, mail_gists

    seen = []

    class Client:
        async def loaded_models(self):
            return ["fast"]

        def keep_warm(self):
            return set()

        async def chat(self, model, messages, **kwargs):
            seen.append(P.payload_fields())
            return {"choices": [{"finish_reason": "stop", "message": {"content": "gist"}}]}

    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(config, "no_thinking_capable", lambda: ["fast"])
    monkeypatch.setattr(config, "thinks_explicitly", lambda name: False)
    monkeypatch.setattr(brief, "_c", lambda: Client())
    assert asyncio.run(mail_gists._resident_reply("s", "m", lambda: False)) == "gist"
    assert seen == [{"gpu_power_limit_w": 10}]


def test_the_scheduled_daily_summary_runs_at_the_intensive_tier(monkeypatch, on):
    from datetime import date

    from service.assistant import brief, scheduler

    seen = []

    async def fake_brief(part, **kwargs):
        seen.append(P.payload_fields())
        return None

    monkeypatch.setattr(brief, "run_scheduled_brief", fake_brief)
    monkeypatch.setattr(scheduler.idle, "foreground_busy", lambda: False)
    asyncio.run(scheduler._run_daily_brief(date(2026, 10, 28), "morning"))
    assert seen == [{"gpu_power_limit_w": 15}]


class _Recorder:
    """A fake client that records the cap active when chat() runs."""

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
        self.seen.append(P.payload_fields())
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}


def test_the_mail_memory_walk_runs_at_the_background_tier(monkeypatch, on):
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
    assert client.seen == [{"gpu_power_limit_w": 10}]


def test_the_behavior_phraser_runs_at_the_background_tier(monkeypatch, on):
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
    assert client.seen == [{"gpu_power_limit_w": 10}]


def test_research_calls_run_at_the_intensive_tier(monkeypatch, on):
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
    assert client.seen == [{"gpu_power_limit_w": 15}]


def test_memory_capture_wraps_its_extraction_call_in_the_background_tier():
    import inspect

    from service.memory.capture import MemoryWorker

    source = inspect.getsource(MemoryWorker.process)
    tier = source.index("with power_tier(BACKGROUND):")
    assert tier < source.index("await client.chat(") < tier + 200


# --- auto-off -----------------------------------------------------------------

@pytest.fixture
def fresh_user(monkeypatch):
    """The user just used Wisp; no turn is running."""
    import time

    from service import idle
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_last_user_activity", idle._awake_or_asleep_now())
    return idle


def _age(idle, seconds):
    idle._last_user_activity -= seconds


def test_ten_idle_minutes_turn_it_off_and_store_off(fresh_user, caplog):
    P.set_low_power(True)
    _age(fresh_user, P.IDLE_OFF_S - 5)
    assert P.low_power() is True
    _age(fresh_user, 10)
    with caplog.at_level("INFO", logger="wisp.power"):
        assert P.low_power() is False
    assert json.loads(P.SETTINGS_PATH.read_text()) == {"low_power": False}
    assert [r.getMessage() for r in caplog.records] == ["Low Power Mode turned off: Wisp unused for 10 minutes"]
    assert P.payload_fields() == {}


def test_a_night_asleep_counts_as_idle(fresh_user, monkeypatch):
    # The clock keeps counting through sleep, so waking after a night with the
    # lid closed finds Low Power Mode off before the backfill runs.
    P.set_low_power(True)
    asleep_at = fresh_user._awake_or_asleep_now()
    monkeypatch.setattr(fresh_user, "_awake_or_asleep_now", lambda: asleep_at + 8 * 3600)
    assert P.low_power() is False
    import time
    assert fresh_user._awake_or_asleep_now is not time.monotonic


def test_the_idle_clock_is_one_that_counts_sleep():
    import inspect

    from service import idle
    assert "CLOCK_MONOTONIC)" in inspect.getsource(idle._awake_or_asleep_now)


def test_a_turn_resets_the_idle_clock_and_holds_it_while_running(fresh_user):
    P.set_low_power(True)
    _age(fresh_user, P.IDLE_OFF_S - 5)
    fresh_user.begin_foreground()
    try:
        _age(fresh_user, 3600)                 # a long turn is never idle
        assert P.low_power() is True
    finally:
        fresh_user.end_foreground()            # ending the turn is use, too
    assert fresh_user.user_idle_seconds() < 1
    assert P.low_power() is True


def test_opening_the_notch_or_a_window_counts_as_use(fresh_user):
    P.set_low_power(True)
    _age(fresh_user, P.IDLE_OFF_S - 5)
    fresh_user.note_user_activity()
    _age(fresh_user, 10)
    assert P.low_power() is True


def test_background_model_work_does_not_count_as_use(fresh_user):
    P.set_low_power(True)
    _age(fresh_user, P.IDLE_OFF_S + 1)
    fresh_user.begin("gemma")                  # a background generation
    fresh_user.end("gemma")
    fresh_user.touch("gemma")
    assert P.low_power() is False


@pytest.mark.parametrize("event,why", [("launched", "another app was opened"),
                                       ("activated", "you switched to another app")])
def test_another_app_launch_or_switch_turns_it_off_unless_a_turn_is_running(fresh_user, caplog, event, why):
    P.set_low_power(True)
    fresh_user.begin_foreground()
    try:
        assert P.other_app(event) is False and P.low_power() is True
    finally:
        fresh_user.end_foreground()
    with caplog.at_level("INFO", logger="wisp.power"):
        assert P.other_app(event) is True
    assert P.low_power() is False
    assert why in caplog.records[-1].getMessage()
    assert P.other_app(event) is False         # already off: no second log line
    with pytest.raises(ValueError):
        P.other_app("focused")


def test_off_at_launch_even_when_the_file_said_on(caplog):
    P.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    P.SETTINGS_PATH.write_text('{"low_power": true}')
    with caplog.at_level("INFO", logger="wisp.power"):
        assert P.reset_at_launch() is True
    assert json.loads(P.SETTINGS_PATH.read_text()) == {"low_power": False}
    assert caplog.records[-1].getMessage() == "Low Power Mode turned off: Wisp started"
    assert P.reset_at_launch() is False


def test_the_app_endpoints_report_activity_and_launches(fresh_user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from service.power_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post("/power", json={"low_power": True}).json() == {"low_power": True}
    # Coming back after 10 idle minutes: the switch is already off.
    _age(fresh_user, P.IDLE_OFF_S + 1)
    assert client.post("/power/activity", json={}).json() == {"low_power": False}
    assert fresh_user.user_idle_seconds() < 1
    for event in ("launched", "activated"):
        client.post("/power", json={"low_power": True})
        assert client.post("/power/other_app", json={"event": event}).json() == {"low_power": False}
    assert client.post("/power/other_app", json={"event": "focused"}).status_code == 422
    assert client.post("/power/other_app", json={}).status_code == 422
    for path in ("/power/activity", "/power/other_app"):
        assert client.post(path, json={"event": "launched"},
                           headers={"Origin": "https://example.com"}).status_code == 403

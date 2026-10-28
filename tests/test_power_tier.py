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
    # file, the user present (no away reading yet), no turn and no intensive call.
    monkeypatch.setattr(P, "SETTINGS_PATH", tmp_path / "power" / "settings.json")
    monkeypatch.setattr(P, "_away", None)
    monkeypatch.setattr(P, "_intensive_calls", 0)
    from service import idle
    monkeypatch.setattr(idle, "_foreground_turns", 0)


@pytest.fixture
def away():
    """Nobody has touched the keyboard or mouse for longer than AWAY_AFTER_S."""
    P.refresh_away(P.AWAY_AFTER_S + 1)


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
    read_at, value = P._away
    P._away = (read_at - P._AWAY_STALE_S - 1, value)
    assert P.away() is False                                   # a stale reading


def test_the_scheduler_refreshes_away_off_the_event_loop():
    import inspect

    from service.assistant import scheduler
    source = inspect.getsource(scheduler)
    assert "await asyncio.to_thread(refresh_away)" in source


# --- tiers ------------------------------------------------------------------------

def test_defaults_are_20_w_chat_10_w_background_and_intensive_uncapped():
    assert P.watts(P.CHAT) == 20
    assert P.watts(P.BACKGROUND) == 10
    assert P.watts(P.INTENSIVE) is None
    assert P.watts(None) is None


def test_env_overrides_zero_turns_off_and_out_of_range_is_ignored(monkeypatch):
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "7")
    assert P.watts(P.BACKGROUND) == 7
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "0")
    assert P.watts(P.BACKGROUND) is None
    for bad in ("1", "5000", "ten"):
        monkeypatch.setenv("WISP_BACKGROUND_GPU_W", bad)
        assert P.watts(P.BACKGROUND) == 10
    monkeypatch.setenv("WISP_CHAT_GPU_W", "0")
    assert P.payload_fields() == {}


def test_chat_is_always_20_w_whatever_the_switch_or_away():
    for on in (True, False):
        P.set_save_when_away(on)
        for hid in (0, P.AWAY_AFTER_S + 1):
            P.refresh_away(hid)
            assert P.payload_fields() == {"gpu_power_limit_w": 20}


def test_background_is_capped_only_when_the_switch_is_on_and_the_user_away(away):
    with P.power_tier(P.BACKGROUND):
        assert P.payload_fields() == {"gpu_power_limit_w": 10}
        P.set_save_when_away(False)
        assert P.payload_fields() == {}
        P.set_save_when_away(True)
        P.refresh_away(30)                                     # someone is at the Mac
        assert P.payload_fields() == {}


def test_intensive_work_is_never_capped(away):
    with P.power_tier(P.INTENSIVE):
        assert P.payload_fields() == {}


def test_background_goes_uncapped_while_intensive_work_runs(away):
    with P.power_tier(P.INTENSIVE):
        started = P.call_started()
    assert started is True and P.intensive_running()
    with P.power_tier(P.BACKGROUND):
        assert P.payload_fields() == {}
    P.call_ended(started)
    assert not P.intensive_running()
    with P.power_tier(P.BACKGROUND):
        assert P.payload_fields() == {"gpu_power_limit_w": 10}
    assert P.call_started() is False                           # a chat call isn't counted


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


def test_the_client_sends_each_tier_its_per_request_cap():
    client, bodies = _client(managed=True)
    hi = [{"role": "user", "content": "hi"}]

    async def run():
        try:
            await client.chat("m", hi)                        # 0: chat
            with P.power_tier(P.BACKGROUND):
                await client.chat("m", hi)                    # 1: background, present
                P.refresh_away(P.AWAY_AFTER_S + 1)
                await client.chat("m", hi)                    # 2: background, away
                await client.chat("m", hi, gpu_power_limit_w=12)  # 3: explicit wins
            with P.power_tier(P.INTENSIVE):
                await client.chat("m", hi)                    # 4: intensive
            await client.chat("m", hi)                        # 5: chat again
        finally:
            await client._client.aclose()

    asyncio.run(run())
    caps = [body.get("gpu_power_limit_w") for body in bodies]
    assert caps == [20, None, 10, 12, None, 20]


def test_the_client_counts_an_intensive_call_only_while_it_runs(away):
    during = []
    client, _ = _client(managed=True, reply_hook=lambda: during.append(P.intensive_running()))

    async def run():
        try:
            with P.power_tier(P.INTENSIVE):
                await client.chat("m", [{"role": "user", "content": "hi"}])
        finally:
            await client._client.aclose()

    asyncio.run(run())
    assert during == [True] and not P.intensive_running()


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


def test_the_gist_call_runs_at_the_background_tier(monkeypatch, away):
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
    assert seen == [(P.BACKGROUND, {"gpu_power_limit_w": 10})]


def test_the_scheduled_daily_summary_runs_at_the_intensive_tier(monkeypatch, away):
    from datetime import date

    from service.assistant import brief, scheduler

    seen = []

    async def fake_brief(part, **kwargs):
        seen.append(_seen())
        return None

    monkeypatch.setattr(brief, "run_scheduled_brief", fake_brief)
    monkeypatch.setattr(scheduler.idle, "foreground_busy", lambda: False)
    asyncio.run(scheduler._run_daily_brief(date(2026, 10, 28), "morning"))
    assert seen == [(P.INTENSIVE, {})]


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


def test_research_calls_run_at_the_intensive_tier(monkeypatch, away):
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
    assert client.seen == [(P.INTENSIVE, {})]


def test_memory_capture_wraps_its_extraction_call_in_the_background_tier():
    import inspect

    from service.memory.capture import MemoryWorker

    source = inspect.getsource(MemoryWorker.process)
    tier = source.index("with power_tier(BACKGROUND):")
    assert tier < source.index("await client.chat(") < tier + 200

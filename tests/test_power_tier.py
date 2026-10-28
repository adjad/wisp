"""GPU power tiers for background model calls; no engine is contacted."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from service.inference import power_tier as P
from service.inference.omlx_client import OMLXClient


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in P._ENV.values():
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_10_w_background_and_15_w_intensive():
    assert P.watts(P.BACKGROUND) == 10
    assert P.watts(P.INTENSIVE) == 15
    assert P.watts(None) is None


def test_env_overrides_zero_turns_off_and_out_of_range_is_ignored(monkeypatch):
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "7")
    assert P.watts(P.BACKGROUND) == 7
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "0")
    assert P.watts(P.BACKGROUND) is None
    for bad in ("1", "5000", "ten"):
        monkeypatch.setenv("WISP_BACKGROUND_GPU_W", bad)
        assert P.watts(P.BACKGROUND) == 10


def test_tier_is_scoped_to_the_block_and_inherited_by_tasks():
    assert P.payload_fields() == {}

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


def test_managed_engine_gets_the_cap_only_inside_a_tier():
    client, bodies = _client(managed=True)

    async def run():
        try:
            await client.chat("m", [{"role": "user", "content": "hi"}])
            with P.power_tier(P.INTENSIVE):
                await client.chat("m", [{"role": "user", "content": "hi"}])
                # An explicit field from the caller wins over the tier.
                await client.chat("m", [{"role": "user", "content": "hi"}], gpu_power_limit_w=20)
        finally:
            await client._client.aclose()

    asyncio.run(run())
    assert "gpu_power_limit_w" not in bodies[0]
    assert bodies[1]["gpu_power_limit_w"] == 15
    assert bodies[2]["gpu_power_limit_w"] == 20


def test_a_remote_provider_never_gets_the_field():
    client = OMLXClient.__new__(OMLXClient)
    client.managed = False
    with P.power_tier(P.BACKGROUND):
        assert client._power_fields() == {}


def test_the_gist_call_runs_at_the_background_tier(monkeypatch):
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


def test_the_scheduled_daily_summary_runs_at_the_intensive_tier(monkeypatch):
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
    """A fake client that records the tier active when chat() runs."""

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


def test_the_mail_memory_walk_runs_at_the_background_tier(monkeypatch):
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


def test_the_behavior_phraser_runs_at_the_background_tier(monkeypatch):
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


def test_research_calls_run_at_the_intensive_tier(monkeypatch):
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


def test_a_live_turn_carries_no_tier():
    # Ask/chat turns never enter power_tier, so they fall back to the
    # engine's model-level cap (20 W for Gemma on the user's Mac).
    assert P.current_tier() is None and P.payload_fields() == {}

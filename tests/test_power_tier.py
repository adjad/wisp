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

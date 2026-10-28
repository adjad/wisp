"""The model-only switch: on in the packaged config (0.5.0-rc3), env wins,
a user overlay wins over the package, bad values and a missing key stay off."""
from __future__ import annotations

import pytest

from service import model_only as model_led


def test_missing_key_falls_back_off(monkeypatch):
    # Code fallback only: the packaged models.yaml sets the key (see below).
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    monkeypatch.setattr("service.config.models_config", lambda: {})
    assert model_led.model_only_enabled() is False


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("on", True),
                                            ("0", False), ("false", False), ("nope", False)])
def test_environment_wins(monkeypatch, value, expected):
    monkeypatch.setenv("WISP_MODEL_ONLY", value)
    monkeypatch.setattr("service.config.models_config", lambda: {"model_only": not expected})
    assert model_led.model_only_enabled() is expected


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("yes", True), (None, False)])
def test_config_key(monkeypatch, value, expected):
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    monkeypatch.setattr("service.config.models_config", lambda: {"model_only": value})
    assert model_led.model_only_enabled() is expected


def test_unreadable_config_stays_off(monkeypatch):
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)

    def boom():
        raise OSError("unreadable")
    monkeypatch.setattr("service.config.models_config", boom)
    assert model_led.model_only_enabled() is False


def _packaged():
    import yaml
    from pathlib import Path
    return yaml.safe_load((Path(__file__).resolve().parents[1] / "service/config/models.yaml").read_text())


def test_packaged_default_is_on():
    # 0.5.0-rc3: model-led routing is the default (was False through rc2).
    # Narration thinking stays on with it: Gemma 4 E4B writes nothing after a
    # tool result with thinking off.
    packaged = _packaged()
    assert packaged.get("model_only") is True
    assert packaged.get("model_only_narration_thinking") is True


@pytest.mark.parametrize("value", ["false", "0", "off", "no"])
def test_env_false_turns_the_packaged_default_off(monkeypatch, value):
    monkeypatch.setenv("WISP_MODEL_ONLY", value)
    monkeypatch.setattr("service.config.models_config", _packaged)
    assert model_led.model_only_enabled() is False
    monkeypatch.delenv("WISP_MODEL_ONLY")
    assert model_led.model_only_enabled() is True


def test_user_overlay_false_wins_over_the_package(monkeypatch):
    from service import config
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    merged = config._deep_merge(_packaged(), {"model_only": False})
    monkeypatch.setattr("service.config.models_config", lambda: merged)
    assert model_led.model_only_enabled() is False


def test_reset_drops_a_saved_model_only_override():
    # /resetwisp keeps only the engine and model choice, so a saved
    # `model_only: false` (or narration override) does not survive a reset and
    # the packaged default applies again.
    from service import wisp_reset
    kept = wisp_reset.trimmed_config({"model_only": False, "model_only_narration_thinking": False,
                                      "engine": {"mode": "bundled"}, "roles": {"agent": "m"}})
    assert kept == {"engine": {"mode": "bundled"}, "roles": {"agent": "m"}}


def test_turn_override_is_scoped_to_its_own_context(monkeypatch):
    import contextvars
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")

    def turn():
        model_led.disable_for_turn()
        return model_led.model_only_enabled()
    assert contextvars.copy_context().run(turn) is False
    assert model_led.model_only_enabled() is True


class _Endpoint:
    def __init__(self, name, managed, base_url="http://127.0.0.1:8000"):
        self.name, self.managed, self.base_url = name, managed, base_url


class _Target:
    def __init__(self, endpoint):
        self.endpoint = endpoint


class _Client:
    managed = True
    base_url = "http://127.0.0.1:8000/"


@pytest.mark.parametrize("endpoint,eligible", [
    (_Endpoint("local", True), True),
    (_Endpoint("local_provider", False, "http://127.0.0.1:1234"), False),
    (_Endpoint("cloud", False, "https://example.com"), False),
])
def test_model_only_applies_only_where_a_turn_can_be_model_led(monkeypatch, endpoint, eligible):
    # A cloud or local-provider agent binding used to raise "Model-led routing
    # requires the configured managed local agent connection" on every turn
    # once model_only was on. It now keeps the rules path for that turn, and
    # the agent loop follows the turn (thinking/sampling rules), not the global flag.
    import contextvars
    from service import main
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.setattr(main, "remote_only_profile", lambda: None)
    monkeypatch.setattr(main, "role_target", lambda role: _Target(endpoint))

    def turn():
        return main._model_only_turn(_Client), model_led.model_only_enabled()
    assert contextvars.copy_context().run(turn) == (eligible, eligible)


def test_unresolvable_agent_binding_keeps_the_rules_path(monkeypatch):
    import contextvars
    from service import main
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.setattr(main, "remote_only_profile", lambda: None)

    def broken(role):
        raise ValueError("bad binding")
    monkeypatch.setattr(main, "role_target", broken)
    assert contextvars.copy_context().run(main._model_only_turn, _Client) is False


def test_env_off_never_consults_the_agent_binding(monkeypatch):
    from service import main
    monkeypatch.setenv("WISP_MODEL_ONLY", "false")

    def unexpected(role):
        raise AssertionError("model_only off needs no binding")
    monkeypatch.setattr(main, "role_target", unexpected)
    assert main._model_only_turn(_Client) is False


def test_agent_steps_think_on_and_keep_profile_sampling(monkeypatch):
    from service.agent import loop
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    for model in ("Ling-3.0-tiny-Wisp-V3-merged-oQ4e", "Mellum2.1-12B-A2.5B-Thinking-oQ5e", "LFM2.5-2.6B-oQ6e"):
        for no_thinking in (False, True):
            assert loop._template_kwargs(model, no_thinking=no_thinking, think_on=False) == {
                "chat_template_kwargs": {"enable_thinking": True}}


def test_switch_off_keeps_the_normal_thinking_rules(monkeypatch):
    from service.agent import loop
    monkeypatch.setenv("WISP_MODEL_ONLY", "0")
    assert loop._template_kwargs("Ling-3.0-tiny-Wisp-V3-merged-oQ4e", no_thinking=False, think_on=False) == {
        "chat_template_kwargs": {"enable_thinking": False}}


def _reasoning_turn(monkeypatch, tmp_path, script, model_only="1"):
    import asyncio
    import json
    from service import diagnostics
    from service.agent import loop
    monkeypatch.setenv("WISP_MODEL_ONLY", model_only)
    monkeypatch.setattr(loop, "audit", lambda *a, **k: None)

    class Client:
        async def ensure_only(self, *a, **k):
            return None

        async def stream_events(self, model, messages, **kw):
            think, call, text = script.pop(0) if script else ("", None, "ok")
            if think:
                yield {"kind": "reasoning", "text": think}
            if call:
                yield {"kind": "final", "message": {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": call, "arguments": json.dumps({"period": "tomorrow"})}}]}}
            else:
                yield {"kind": "content", "text": text}
                yield {"kind": "final", "message": {"role": "assistant", "content": text, "tool_calls": None}}

    class Approver:
        async def confirm(self, action):
            return False

    events = []

    async def emit(ev):
        events.append(ev)

    async def go():
        with diagnostics.use(diagnostics.Trace("agent", root=tmp_path)):
            await loop.run_agent(Client(), "fixture-model", [{"role": "user", "content": "plan for tomorrow"}],
                                 emit, Approver(), tools=["get_upcoming"], test_mode=True, debug=False,
                                 include_memory_context=False, include_identity_context=False)
    asyncio.run(go())
    return [e for e in events if e["type"] in ("reasoning", "text", "done")]


def test_model_only_shows_reasoning_from_every_step(monkeypatch, tmp_path):
    out = _reasoning_turn(monkeypatch, tmp_path, [("Need the calendar.", "get_upcoming", ""),
                                                  ("Two events.", None, "You have two events.")])
    reasoning = [e["text"] for e in out if e["type"] == "reasoning"]
    assert reasoning == ["Step 1:\nNeed the calendar.\n\nStep 2:\nTwo events."]
    assert [e["type"] for e in out][-2:] == ["reasoning", "done"]


def test_model_only_shows_tool_step_reasoning_when_the_answer_step_has_none(monkeypatch, tmp_path):
    out = _reasoning_turn(monkeypatch, tmp_path, [("Need the calendar.", "get_upcoming", ""),
                                                  ("", None, "You have two events.")])
    assert [e["text"] for e in out if e["type"] == "reasoning"] == ["Need the calendar."]


def test_normal_path_still_shows_only_the_final_step(monkeypatch, tmp_path):
    out = _reasoning_turn(monkeypatch, tmp_path, [("Need the calendar.", "get_upcoming", ""),
                                                  ("", None, "You have two events.")], model_only="0")
    assert [e for e in out if e["type"] == "reasoning"] == []


def test_no_engine_client_keeps_the_rules_path(monkeypatch):
    # An in-process caller that never started the engine has no client to lead.
    import contextvars
    from service import main
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.setattr(main, "remote_only_profile", lambda: None)
    monkeypatch.setattr(main, "role_target", lambda role: _Target(_Endpoint("local", True)))
    assert contextvars.copy_context().run(main._model_only_turn, lambda: None) is False


@pytest.mark.parametrize("super_model,eligible", [(False, True), (True, False)])
def test_super_model_on_keeps_the_rules_path(monkeypatch, super_model, eligible):
    # 0.5.0-rc3 audit M1: Super Model only routes on the rules path, and a
    # model-led turn always runs the local agent model. With the toggle on the
    # turn keeps the rules path, so the setting the user sees is honoured.
    import contextvars
    from service import main
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.setattr(main, "remote_only_profile", lambda: None)
    monkeypatch.setattr(main, "role_target", lambda role: _Target(_Endpoint("local", True)))
    monkeypatch.setattr(main, "cloud_super_model_enabled", lambda: super_model)

    def turn():
        return main._model_only_turn(_Client), model_led.model_only_enabled()
    assert contextvars.copy_context().run(turn) == (eligible, eligible)

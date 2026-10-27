"""Model-led routing keeps the 1.2.5 deterministic claims and remote builds
while model_only is off; under the model_only default (0.5.0-rc3) the model
answers those turns too, with every effect still owned and confirmed.

Uses the synthetic /agent entrypoint fixture from test_model_led_entrypoint:
no native tool, provider, credential or model runs.
"""
import pytest

from service import main
from service.router import router
from tests.test_model_led_entrypoint import endpoint  # noqa: F401  (fixture)


@pytest.fixture
def behavior_on(monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "1")


@pytest.mark.parametrize("prompt", [
    "what have you learned about me", "describe me", "what are my habits"])
def test_behavior_claim_is_recognized(behavior_on, prompt):
    assert router.behavior_route_claim(prompt)


@pytest.mark.parametrize("prompt", ["what is up this week", "what did Mom say", "hello"])
def test_ordinary_prompts_are_not_behavior_claims(behavior_on, prompt):
    assert not router.behavior_route_claim(prompt)


def test_behavior_claim_follows_the_behavior_kill_switch(monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    assert not router.behavior_route_claim("what have you learned about me")


def _describe_me():
    from service.tools import registry
    async def describe(subject=None):
        return "Synthetic patterns."
    describe.__module__ = "service.tools.behavior_tools"
    registry.REGISTRY["describe_me"] = registry.Tool(
        "describe_me", "Synthetic learned routines", {"type": "object", "properties": {
            "subject": {"type": "string"}}}, "assistant_read", describe)


def test_behavior_question_uses_the_legacy_route_under_model_led(endpoint, behavior_on, monkeypatch):
    # WISP_MODEL_LED_ROUTING alone (model_only off) keeps the 1.2.5 claim.
    # Since 0.5.0-rc3 model_only is on by default; see the test below.
    monkeypatch.setenv("WISP_MODEL_ONLY", "0")
    from service.tools import registry
    async def describe(subject=None):
        return "Synthetic patterns."
    describe.__module__ = "service.tools.behavior_tools"
    registry.REGISTRY["describe_me"] = registry.Tool(
        "describe_me", "Synthetic learned routines", {"type": "object", "properties": {
            "subject": {"type": "string"}}}, "assistant_read", describe)
    monkeypatch.setattr(main, "route", router.route)   # the real 1.2.5 router
    endpoint.request("what have you learned about me")
    call = endpoint.agent_calls[0]
    assert call["model_led_discovery"] is False and call["claim_effect"] is None
    assert [name for name, _ in call["direct_calls"]] == ["describe_me"]
    assert "describe_me" not in call["forbidden_tools"]
    assert "get_upcoming" in call["forbidden_tools"]


def test_behavior_question_goes_to_the_model_under_the_model_only_default(endpoint, behavior_on, monkeypatch):
    # 0.5.0-rc3 default: no deterministic claim answers first. The model gets
    # the question with describe_me reachable, and every effect it picks is
    # owned (claim_effect) and goes through the loop's policy/confirmation.
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    _describe_me()
    monkeypatch.setattr(main, "route", router.route)
    endpoint.request("what have you learned about me")
    call = endpoint.agent_calls[0]
    assert call["model_led_discovery"] is True and call["claim_effect"] is not None
    assert call["ask_quality"] is True
    assert not call["direct_calls"] and call["force_first_tool"] is None
    assert "describe_me" not in call["forbidden_tools"]
    assert not endpoint.baseline_calls


def test_ordinary_prompt_still_uses_model_led_discovery(endpoint, behavior_on):
    endpoint.request("what is up this week")
    assert not endpoint.baseline_calls
    assert endpoint.agent_calls[0]["model_led_discovery"] is True


def test_literal_envelope_uses_the_legacy_route_under_model_led(endpoint, monkeypatch):
    # WISP_MODEL_LED_ROUTING alone (model_only off) keeps the literal claim.
    monkeypatch.setenv("WISP_MODEL_ONLY", "0")
    endpoint.request("What's queued to send?")
    assert len(endpoint.baseline_calls) == 1
    assert not any(call["model_led_discovery"] for call in endpoint.agent_calls)


def test_literal_envelope_goes_to_the_model_under_the_model_only_default(endpoint, monkeypatch):
    # 0.5.0-rc3 default: the queued-send review is the model's to answer. Any
    # send it attempts is still an owned, confirmed effect (claim_effect here;
    # the always-confirm floor in test_model_led_send_still_requires_confirmation).
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    endpoint.request("What's queued to send?")
    assert not endpoint.baseline_calls
    call = endpoint.agent_calls[0]
    assert call["model_led_discovery"] is True and call["claim_effect"] is not None
    assert call["messages"][-1] == {"role": "user", "content": "What's queued to send?"}


def test_remote_only_build_never_enters_model_led_routing(monkeypatch):
    """A remote-only build has no managed local agent connection. Model-led
    routing would refuse every turn there; the gate keeps it on the legacy
    router (whose remote tool contract run_tool/decide still enforce)."""
    import inspect
    source = inspect.getsource(main.agent)
    gate = source[source.index("model_led_turn = False"):source.index("model_led_forbidden, model_led_bindings")]
    assert "remote_only_profile() is None" in gate
    assert "not deterministic_claim" in gate


@pytest.mark.parametrize("value, enabled", [(None, False), ("0", False), ("off", False),
                                            ("1", True), ("true", True)])
def test_model_led_routing_is_opt_in(value, enabled):
    from service.router.model_led import model_led_enabled
    assert model_led_enabled(value) is enabled


def test_unset_flags_use_model_led_discovery_by_default(endpoint, monkeypatch):
    # 0.5.0-rc3: the packaged model_only is on, so with neither variable set
    # every eligible turn is model-led (it was the legacy router through rc2).
    monkeypatch.delenv("WISP_MODEL_LED_ROUTING", raising=False)
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    endpoint.request("what is up this week")
    assert not endpoint.baseline_calls
    assert endpoint.agent_calls[0]["model_led_discovery"] is True


def test_model_only_off_and_unset_flag_use_the_legacy_router(endpoint, monkeypatch):
    # The documented rollback: WISP_MODEL_ONLY=false (or model_only: false).
    monkeypatch.delenv("WISP_MODEL_LED_ROUTING", raising=False)
    monkeypatch.setenv("WISP_MODEL_ONLY", "false")
    endpoint.request("what is up this week")
    assert len(endpoint.baseline_calls) == 1
    assert not any(call["model_led_discovery"] for call in endpoint.agent_calls)
    assert not endpoint.owned


@pytest.mark.parametrize("full_access", [False, True])
def test_model_led_send_still_requires_confirmation(monkeypatch, full_access):
    """Sends keep the always-confirm floor under model-led discovery, even with
    full access; a declined confirmation never reaches the effect."""
    from tests.test_model_led_integration import Approval, call, discover, run
    from tests.test_model_led_integration import synthetic as _synthetic
    effects, register = _synthetic.__wrapped__(monkeypatch)
    monkeypatch.setattr("service.safety.policy._FULL_ACCESS", full_access)
    register("send_email", "action_tools", "email_send", parameters={"type": "object", "properties": {
        "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
        "required": ["to", "body"]})
    approval = Approval(allowed=False)
    run([[discover("mail", "send_email")],
         [call("send_email", to="maya@synthetic.invalid", subject="Hi", body="Hello")],
         "Not sent."], prompt="email maya saying hello", approval=approval,
        claim_effect=lambda tool, args: {"admitted": True}, finish_effect=lambda claim, ok: True)
    assert approval.calls, "send_email ran without asking for confirmation"
    assert not any(name == "send_email" for name, _ in effects)

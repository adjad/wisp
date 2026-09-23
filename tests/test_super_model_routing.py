import asyncio
import sys
from types import SimpleNamespace

import service.inference.super_model as super_model


def decision(**overrides):
    values = {
        "needs_tools": False,
        "light_read": False,
        "tool_subset": [],
        "direct_calls": [],
        "required_tool_groups": (),
        "tool_argument_bindings": {},
        "strict_read_limits": {},
        "force_first_tool": None,
        "route_source": "test",
        "expect_tool_first": False,
        "reminder_action": "",
        "conditional_tools": (),
        "multi_round": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def classify(prompt, route=None):
    return asyncio.run(super_model.cloud_super_model_eligible(
        prompt, route or decision()))


def test_high_confidence_standalone_generation_uses_cloud(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.12, 0.08, 0.10))
    assert classify("How does a rocket work?") == (
        True, "Laya classified this as standalone non-sensitive generation")


def test_laya_privacy_or_context_risk_stays_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.02, 0.01, 0.71))
    allowed, reason = classify("What about the second option?")
    assert allowed is False
    assert "conversation context" in reason


def test_uncertain_private_score_stays_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.49, 0.01, 0.01))
    assert classify("Can you help me interpret this personal situation?")[0] is False


def test_computer_question_excludes_public_web_tools():
    question = super_model._QUESTIONS["computer"]["instructions"]
    assert "public web search" in question
    assert "external tool" not in question


def test_private_or_unscoped_tools_stay_local_without_calling_laya(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (_ for _ in ()).throw(AssertionError("called")))
    assert classify("What is on my calendar?", decision(needs_tools=True))[0] is False
    assert classify("Summarize the source", decision(light_read=True))[0] is False


def test_public_read_only_tools_can_use_cloud_synthesis(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.08, 0.04, 0.03))
    news = decision(
        needs_tools=True,
        tool_subset=["web_search"],
        direct_calls=[("web_search", {"query": "stock market news today"})],
        required_tool_groups=(frozenset({"web_search"}),),
        tool_argument_bindings={"web_search": {"query": "stock market news today"}},
        force_first_tool="web_search",
    )
    assert classify("What is happening in the stock market today?", news)[0] is True


def test_arbitrary_page_fetch_stays_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (_ for _ in ()).throw(AssertionError("called")))
    page = decision(needs_tools=True, tool_subset=["web_fetch"],
                    direct_calls=[("web_fetch", {"url": "https://example.com/story"})])
    assert classify("Summarize https://example.com/story", page)[0] is False
    ambiguous = decision(route_source="default", needs_tools=True,
                         tool_subset=["run_shell", "web_fetch"])
    assert classify("Summarize https://example.com/story", ambiguous)[0] is False


def test_actual_rocket_route_becomes_tool_free_cloud_generation(monkeypatch):
    from service.router.router import route

    prompt = "How does a rocket work?"
    routed = asyncio.run(route(prompt))
    assert routed.route_source == "default"
    assert routed.needs_tools
    assert "run_shell" in (routed.tool_subset or ())
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (0.0009, 0.0327, 0.0061))

    assert classify(prompt, routed)[0] is True
    super_model.prepare_cloud_standalone(routed)
    assert routed.needs_tools is False
    assert routed.tool_subset == []
    assert routed.expect_tool_first is False
    assert routed.multi_round is False


def test_default_tool_menu_stays_local_when_laya_finds_private_risk(monkeypatch):
    from service.router.router import route

    routed = asyncio.run(route("How does this personal situation work?"))
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (0.49, 0.01, 0.01))
    assert classify("How does this personal situation work?", routed)[0] is False
    assert routed.needs_tools is True


def test_default_route_preserves_installed_skill_tools(monkeypatch):
    from service.tools.registry import REGISTRY

    monkeypatch.setitem(REGISTRY, "count_vowels", SimpleNamespace(category="skill_tool"))
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (_ for _ in ()).throw(AssertionError("called")))
    for selected in (["run_shell", "count_vowels"], ["run_shell", "use_skill"]):
        routed = decision(route_source="default", needs_tools=True,
                          tool_subset=selected[:])
        assert classify("Count the vowels in banana", routed)[0] is False
        super_model.prepare_cloud_standalone(routed)
        assert routed.needs_tools is True
        assert routed.tool_subset == selected


def test_mixed_public_and_private_or_effect_tools_stay_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (_ for _ in ()).throw(AssertionError("called")))
    mixed = decision(
        needs_tools=True,
        tool_subset=["web_search", "send_message"],
        required_tool_groups=(frozenset({"web_search"}), frozenset({"send_message"})),
    )
    assert classify("Find the news and text it to Mom", mixed)[0] is False


def test_cloud_public_web_prompt_excludes_identity_memory_and_skills(monkeypatch):
    from service.agent import loop
    from service.memory import identity, prompt_blocks
    from service import skills

    monkeypatch.setattr(identity, "identity_prompt_block",
                        lambda **_kw: "PRIVATE_IDENTITY")
    monkeypatch.setattr(prompt_blocks, "memory_block",
                        lambda **_kw: "PRIVATE_MEMORY")
    monkeypatch.setattr(skills, "skills_context_block",
                        lambda *_args: "PRIVATE_SKILL")

    class Client:
        def __init__(self):
            self.requests = []

        async def ensure_only(self, *_args, **_kwargs):
            return None

        async def stream_events(self, _model, messages, **_kwargs):
            self.requests.append([dict(message) for message in messages])
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "A public answer.", "tool_calls": None}}

    class Approver:
        async def confirm(self, _action):
            raise AssertionError("unexpected approval")

    async def emit(_event):
        return None

    client = Client()
    asyncio.run(loop.run_agent(
        client, "Agents-A1-4B-oQe6",
        [{"role": "user", "content": "What is on the news today?"}],
        emit, Approver(), tools=["web_search"], max_steps=1,
        public_web_synthesis=True, include_memory_context=False))

    assert client.requests
    sent = str(client.requests[0])
    for private_marker in ("PRIVATE_IDENTITY", "PRIVATE_MEMORY", "PRIVATE_SKILL"):
        assert private_marker not in sent


def test_explicit_secrets_and_local_override_stay_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya", lambda prompt: (0.0, 0.0, 0.0))
    for prompt in (
        "My API key is sk-example and I need help.",
        "Read /Users/person/private.txt and explain it.",
        "Use only the local model for this request.",
        "Keep this on my machine.",
        "Stay local.",
        "Keep local.",
        "Local only, please.",
        "Review ghp_abcdefghijklmnopqrstuvwxyz123456.",
        "Explain /tmp/private-notes.txt.",
        "Open ./private-notes.txt.",
        "Read \"/Users/person/private.txt\" and explain it.",
        "Open `./private-notes.txt`.",
        "Never send this to the cloud.",
    ):
        assert classify(prompt)[0] is False


def test_laya_loader_pins_revision_and_uses_cpu_gpu(monkeypatch):
    calls = []
    fake_agent = object()
    fake_laya = SimpleNamespace(load=lambda model_id, **kwargs: (
        calls.append((model_id, kwargs)) or fake_agent))
    monkeypatch.setitem(sys.modules, "laya_coreml", fake_laya)
    monkeypatch.setattr(super_model, "_agent", None)
    monkeypatch.setattr(super_model, "_load_failed", False)

    assert super_model._load_agent() is fake_agent
    assert calls == [(super_model.LAYA_MODEL_ID, {
        "revision": super_model.LAYA_MODEL_REVISION,
        "compute_units": "cpu_gpu",
    })]


def test_missing_or_malformed_laya_fails_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(super_model, "start_laya_warmup", lambda: None)
    allowed, reason = classify("Explain a public scientific concept.")
    assert allowed is False
    assert "unavailable or uncertain" in reason
